"""Enumerate attached devices (USB + network) via ``adb devices -l``."""

from __future__ import annotations

import os
import time
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .config import parse_host_port, validate_port
from .scrcpy import TUNNEL_PORT_FIREWALL_RANGE
from .tools import (
    DEFAULT_ADB_SERVER_PORT,
    NO_WINDOW,
    _recv_exact,
    find_adb,
    windowless_python,
)

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclass
class Device:
    """One entry from ``adb devices -l``."""

    serial: str
    state: str  # device | offline | unauthorized | no permissions
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_online(self) -> bool:
        return self.state == "device"

    @property
    def is_network(self) -> bool:
        """A TCP/IP target looks like ``host:port`` (``192.168.0.5:5555``)."""
        host, port = parse_host_port(self.serial)
        return bool(host) and port is not None

    @property
    def label(self) -> str:
        name = self.model or self.product or self.device or "device"
        kind = "net" if self.is_network else "usb"
        return f"{name} [{kind}]"

    def __str__(self) -> str:
        bits = [self.serial, self.state]
        if self.model:
            bits.append(f"model={self.model}")
        return "  ".join(bits)


def _parse_line(line: str) -> Optional[Device]:
    line = line.strip()
    if not line or line.startswith("List of devices"):
        return None
    if line.startswith("*") or line.startswith("adb "):
        return None  # daemon chatter ("* daemon started successfully")
    parts = line.split()
    if len(parts) < 2:
        return None
    serial, state = parts[0], parts[1]
    # adb emits e.g. "SERIAL  no permissions; see ...".  Treating the state
    # as just "no" loses the actionable reason and disagrees with Device.state.
    extra_start = 2
    if state == "no" and len(parts) >= 3 and parts[2].startswith("permissions"):
        state = "no permissions"
        extra_start = 3
    extra = {}
    for tok in parts[extra_start:]:
        if ":" in tok:
            k, _, v = tok.partition(":")
            extra[k] = v
    return Device(
        serial=serial,
        state=state,
        model=extra.get("model", ""),
        product=extra.get("product", ""),
        device=extra.get("device", ""),
        transport_id=extra.get("transport_id", ""),
        extra=extra,
    )


def _list_devices_socket(host: str = "127.0.0.1", port: int = 5037, timeout: float = 1.0) -> list[Device] | None:
    """Query connected devices directly via ADB server socket protocol.

    Fast path (~10-15ms) that avoids spawning adb.exe subprocesses repeatedly on Windows.
    Returns None if the server is not reachable so caller can fall back to CLI.
    """
    import socket

    s = None
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Command is "000ehost:devices-l" (14 chars = 0x000e)
        s.sendall(b"000ehost:devices-l")
        status = _recv_exact(s, 4)
        if status != b"OKAY":
            return None
        hex_len = _recv_exact(s, 4)
        if hex_len is None:
            return None
        length = int(hex_len, 16)
        data = _recv_exact(s, length)
        if data is None:
            return None
        text = data.decode("utf-8", errors="replace")
        devices = []
        for line in text.splitlines():
            dev = _parse_line(line)
            if dev is not None:
                devices.append(dev)
        return devices
    except Exception:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def list_devices(
    adb_path: str | None = None,
    timeout: float = 5.0,
    server_host: str | None = None,
    server_port: int = 5037,
    strict: bool = False,
) -> list:
    """Return a list of :class:`Device` for every attached/known target.

    With *server_host* set, the query is sent to a **remote** machine's adb
    server (``adb -H host -P port devices``) — i.e. the devices physically
    attached to *that* machine. Without it, a non-default *server_port* selects
    a local server on that port.

    Raises :class:`ADBNotFoundError` if adb is missing; returns ``[]`` if adb
    runs but nothing is connected. Raises ConnectionError if a remote server is
    requested but unreachable.  With *strict=True*, local server/startup failures
    (including a hung ``adb devices``) are raised too; the GUI uses that mode to
    show an actionable status instead of mislabelling a broken daemon as an
    empty device list.
    """
    target_host = server_host or "127.0.0.1"
    is_local = target_host in _LOCAL_HOSTS
    # Keep the fast local path genuinely fast.
    sock_timeout = min(0.25, timeout) if is_local else min(2.0, timeout)
    sock_devs = _list_devices_socket(target_host, server_port, timeout=sock_timeout)
    if sock_devs is not None:
        return sock_devs

    if is_local:
        try:
            from .tools import ensure_adb_server

            # ensure_adb_server probes the socket itself before launching.
            if not ensure_adb_server(adb_path, timeout=min(4.0, timeout), port=server_port):
                message = (
                    f"ADB server could not start on port {server_port}. Check that the "
                    "port is not held by another ADB version, then use Device → "
                    "Restart ADB server."
                )
                if strict:
                    raise ConnectionError(message)
                return []
            # One retry is enough once we have explicitly started the daemon.
            sock_devs = _list_devices_socket(target_host, server_port, timeout=min(0.5, timeout))
            if sock_devs is not None:
                return sock_devs
        except ConnectionError:
            raise
        except Exception:
            # The CLI fallback below is useful for unusual ADB socket protocol
            # variants, but it must remain bounded and surface its own failure.
            pass

    adb = find_adb(adb_path)
    cmd = [adb]
    if server_host:
        cmd += ["-H", server_host, "-P", str(server_port)]
    elif server_port != DEFAULT_ADB_SERVER_PORT:
        cmd += ["-P", str(server_port)]
    cmd += ["devices", "-l"]
    _unreachable = (
        f"Could not reach the adb server at {server_host}:{server_port}. On that "
        f"machine run:  adb -a nodaemon server start  (and allow TCP {server_port} "
        f"through its firewall)."
    )
    run_timeout = min(timeout, 3.0) if is_local else timeout
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=run_timeout,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        if server_host and not is_local:  # a hung connection = unreachable server
            raise ConnectionError(_unreachable) from exc
        if strict:
            raise ConnectionError(f"adb devices timed out after {run_timeout:g}s") from exc
        return []
    text = (out.stdout or "") + (out.stderr or "")
    if out.returncode != 0:
        detail = text.strip().replace("\n", " ")[:400] or f"exit code {out.returncode}"
        if server_host:
            raise ConnectionError(_unreachable + f" ({detail})")
        if strict:
            raise ConnectionError(f"adb devices failed: {detail}")
        return []
    if server_host and ("cannot connect" in text.lower() or "failed to connect" in text.lower()):
        raise ConnectionError(_unreachable)
    devices = []
    for line in (out.stdout or "").splitlines():
        dev = _parse_line(line)
        if dev is not None:
            devices.append(dev)
    return devices


def remote_devices(server_host: str, server_port: int = 5037, adb_path: str | None = None) -> list:
    """Convenience: list devices attached to a remote machine's adb server."""
    return list_devices(adb_path, server_host=server_host, server_port=server_port)


def first_online(adb_path: str | None = None) -> Optional[Device]:
    """Return the first online device, or None."""
    for d in list_devices(adb_path):
        if d.is_online:
            return d
    return None


def _parse_mdns_line(line: str) -> Optional[dict]:
    """One line of ``adb mdns services``:
    ``adb-XXXX-YYYY	_adb-tls-connect._tcp	192.168.1.5:5555``.
    Whitespace-separated on some adb builds; returns None for non-service lines."""
    line = line.strip()
    if not line or line.lower().startswith("list of discovered"):
        return None
    parts = line.split()
    if len(parts) < 3 or "._tcp" not in parts[1]:
        return None
    name, service, addr = parts[0], parts[1].rstrip("."), parts[2]
    host, port = parse_host_port(addr)
    if not host or port is None:
        return None
    kind = "connect" if "connect" in service else "pairing" if "pairing" in service else service
    return {"name": name, "service": kind, "host": host, "port": port, "address": addr}


def mdns_devices(adb_path: str | None = None, timeout: float = 10.0) -> list:
    """Discover Android 11+ *Wireless debugging* devices on the LAN via
    ``adb mdns services``. Returns a list of dicts
    ``{"name","service","host","port","address"}`` where ``service`` is
    ``connect`` (ready for ``adb connect``) or ``pairing`` (shows a pair code).

    Returns ``[]`` when nothing is found or this adb has no mdns support —
    never raises for those cases (only for adb itself being missing)."""
    adb = find_adb(adb_path)
    try:
        out = subprocess.run(
            [adb, "mdns", "services"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except Exception:
        return []
    found = []
    for line in (out.stdout or "").splitlines():
        d = _parse_mdns_line(line)
        if d is not None:
            found.append(d)
    return found


# --------------------------------------------------------------------------- #
# Shared adb server (expose THIS PC's devices to the network)
# --------------------------------------------------------------------------- #
def _lan_addresses() -> set:
    """This machine's non-loopback IPv4 addresses (best-effort)."""
    import socket

    try:
        ips = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except Exception:
        return set()
    return {ip for ip in ips if not ip.startswith("127.")}


def _lan_reachable(port: int, timeout: float = 2.0) -> Optional[bool]:
    """True if *port* accepts a TCP connection on one of this PC's LAN
    addresses, False if none does, None when the PC has no LAN address."""
    import socket

    ips = _lan_addresses()
    if not ips:
        return None
    for ip in sorted(ips):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def server_is_shared(port: int = 5037, adb_path: str | None = None) -> bool:
    """True if an adb server is up AND reachable on a non-loopback interface —
    i.e. another machine could actually drive this PC's devices.

    Uses a socket probe, never ``adb devices`` (which silently STARTS a
    localhost-only daemon when none is running). *adb_path* is accepted for
    backward compatibility and is no longer needed."""
    from .tools import is_adb_server_alive

    port = validate_port(port)
    if not is_adb_server_alive(port=port):
        return False
    lan = _lan_reachable(port)
    # no LAN address at all (offline machine): fall back to "server is up"
    return True if lan is None else lan


def open_firewall(ports=(5037, TUNNEL_PORT_FIREWALL_RANGE)) -> str:
    """Best-effort: open the given TCP ports in the Windows firewall so a remote
    machine can reach this PC's adb server (5037) AND scrcpy's video tunnel
    range. Entries may be a port or an inclusive ``start-end`` range. Needs
    admin rights; returns a status string (never raises)."""
    if os.name != "nt":
        return "firewall: not Windows, skipped"
    opened, failed = [], []
    for p in ports:
        try:
            raw = str(p).strip()
            if "-" in raw:
                first, last = raw.split("-", 1)
                first, last = validate_port(first), validate_port(last)
                if first > last:
                    raise ValueError("invalid descending port range")
                p = f"{first}-{last}"
            else:
                p = str(validate_port(raw))
        except ValueError:
            failed.append(p)
            continue
        rule = f"TurboADB TCP {p}"
        try:
            # remove any old rule, then add (idempotent)
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule}"],
                capture_output=True,
                timeout=15,
                creationflags=NO_WINDOW,
            )
            r = subprocess.run(
                [
                    "netsh",
                    "advfirewall",
                    "firewall",
                    "add",
                    "rule",
                    f"name={rule}",
                    "dir=in",
                    "action=allow",
                    "protocol=TCP",
                    f"localport={p}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=NO_WINDOW,
            )
            (opened if r.returncode == 0 else failed).append(p)
        except Exception:
            failed.append(p)
    if opened and not failed:
        return f"firewall: opened TCP {', '.join(map(str, opened))}"
    if opened:
        return (
            f"firewall: opened {opened}; could NOT open {failed} "
            "(run as Administrator to allow those)"
        )
    return (
        "firewall: could not open ports (run TurboADB/`turboadb serve` as "
        f"Administrator, or open TCP 5037 + {TUNNEL_PORT_FIREWALL_RANGE} manually)"
    )


def start_shared_server(
    port: int = 5037, adb_path: str | None = None, *, restart: bool = True
) -> str:
    """Start an adb server that listens on **all** network interfaces so other
    machines can drive this PC's devices via ``adb -H thispc -P {port}``.

    This is the automated equivalent of ``adb -a nodaemon server start`` — but it
    is launched **detached in the background**, so it keeps listening without
    blocking. A localhost-only server already running would stop ``-a`` from
    binding to ``0.0.0.0``, so by default we replace it first.

    Readiness is confirmed with socket probes only (``adb devices`` would start a
    plain localhost daemon of its own and report a false success), and the port
    must also answer on this PC's LAN address.

    Returns a short status string; raises RuntimeError on failure.
    """
    from .tools import is_adb_server_alive

    port = validate_port(port)
    adb = find_adb(adb_path)
    if restart:
        # drop any localhost-only server so the new one can bind all interfaces
        subprocess.run(
            [adb, "-P", str(port), "kill-server"],
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )
        deadline = time.monotonic() + 5.0
        while is_adb_server_alive(port=port, timeout=0.2) and time.monotonic() < deadline:
            time.sleep(0.1)  # wait for the old daemon to release the port
    elif is_adb_server_alive(port=port, timeout=0.2):
        if _lan_reachable(port) is not False:
            return f"shared adb server is already listening on 0.0.0.0:{port}"
        raise RuntimeError(
            f"a localhost-only adb server is already running on port {port}; "
            "restart it (restart=True) so the shared server can bind all interfaces"
        )
    flags = NO_WINDOW
    extra = {}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survives, no console
        flags |= 0x00000008 | 0x00000200
    else:
        extra["start_new_session"] = True
    proc = subprocess.Popen(
        [adb, "-a", "-P", str(port), "nodaemon", "server", "start"],
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **extra,
    )
    deadline = time.monotonic() + 10.0
    while True:
        if is_adb_server_alive(port=port, timeout=0.2):
            break
        code = proc.poll()
        if code is not None:
            raise RuntimeError(
                f"adb server exited with code {code} before listening on port {port} "
                "(port in use by another adb, or an incompatible adb binary)"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(f"adb server did not come up on port {port}: no response")
        time.sleep(0.1)
    if proc.poll() is not None and _lan_reachable(port) is False:
        # our process died but SOMETHING answers on loopback: a localhost-only
        # server grabbed the port first — exactly the old false success
        raise RuntimeError(
            f"adb server exited with code {proc.poll()}; another localhost-only adb "
            f"server holds port {port}, so this PC is NOT shared"
        )
    if _lan_reachable(port) is False:
        raise RuntimeError(
            f"an adb server answers on 127.0.0.1:{port} but not on this PC's LAN "
            "address, so other machines cannot reach it"
        )
    devs = _list_devices_socket("127.0.0.1", port, timeout=2.0)
    count = f"{len(devs)}" if devs is not None else "?"
    return f"shared adb server is listening on 0.0.0.0:{port} ({count} device(s) attached here)"


def stop_shared_server(port: int = 5037, adb_path: str | None = None) -> str:
    """Stop the network-shared adb server and return to a normal local-only one:
    kill the ``-a`` (all-interfaces) server, then start a plain server that binds
    to localhost again, so this PC keeps working but no longer shares its devices.
    Best-effort; returns a short status string."""
    port = validate_port(port)
    adb = find_adb(adb_path)
    stopped = subprocess.run(
        [adb, "-P", str(port), "kill-server"],
        capture_output=True,
        timeout=15,
        creationflags=NO_WINDOW,
    )
    if stopped.returncode != 0:
        detail = (stopped.stderr or stopped.stdout or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"could not stop shared adb server: {detail or 'unknown error'}")
    started = subprocess.run(
        [adb, "-P", str(port), "start-server"],
        capture_output=True,
        timeout=15,
        creationflags=NO_WINDOW,
    )
    if started.returncode != 0:
        detail = (started.stderr or started.stdout or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"could not start local adb server: {detail or 'unknown error'}")
    return "shared adb server stopped — back to local-only (localhost)"


def _startup_dir() -> str:
    """The current user's Windows Startup folder (programs run at login)."""
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _serve_launcher(port: int) -> str:
    """The quoted command that runs ``turboadb serve`` headlessly.

    The standalone (PyInstaller) exe cannot do this: ``TurboADB.exe -m turboadb
    serve`` just opens the GUI and never starts a server, so a login launcher or
    SYSTEM task built from it would silently share nothing."""
    import sys

    if getattr(sys, "frozen", False):
        raise RuntimeError(
            "The standalone TurboADB executable cannot run the headless 'serve' "
            "command at startup. Install the Python package on this PC "
            "(pip install turboadb) and run:  turboadb serve --startup-task"
        )
    return f'"{windowless_python()}" -m turboadb serve --port {port}'


def install_startup(port: int = 5037) -> str:
    """Make the shared adb server start automatically at every Windows login by
    dropping a tiny launcher in the Startup folder. Returns the file path.
    So it really never has to be done by hand again."""
    if os.name != "nt":
        raise RuntimeError("Startup install is only supported on Windows.")
    port = validate_port(port)
    launcher = _serve_launcher(port)
    d = _startup_dir()
    os.makedirs(d, exist_ok=True)
    bat = os.path.join(d, "turboadb-shared-adb.bat")
    # pythonw -m turboadb serve, detached, no window
    line = f'@echo off\r\nstart "" /b {launcher}\r\n'
    with open(bat, "w", encoding="utf-8") as fh:
        fh.write(line)
    return bat


def uninstall_startup() -> bool:
    """Remove the login auto-start launcher if present."""
    if os.name != "nt":
        return False
    bat = os.path.join(_startup_dir(), "turboadb-shared-adb.bat")
    if os.path.exists(bat):
        os.remove(bat)
        return True
    return False


def _pythonw() -> str:
    """Best windowless Python to run the background server with."""
    return windowless_python()


_SERVE_TASK = "TurboADBSharedADB"


def install_serve_task(port: int = 5037, *, run_now: bool = True) -> str:
    """Register a Scheduled Task that runs the shared adb server at SYSTEM
    **startup** — headless and persistent (survives logoff and needs no login,
    unlike the Startup-folder launcher). Optionally start it immediately via the
    scheduler, which detaches it from whatever session created it (e.g. a WinRM
    remote-deploy session). Returns the task name. Needs admin rights."""
    if os.name != "nt":
        raise RuntimeError("Scheduled-task install is Windows-only.")
    port = validate_port(port)
    tr = _serve_launcher(port)
    created = subprocess.run(
        [
            "schtasks",
            "/create",
            "/tn",
            _SERVE_TASK,
            "/tr",
            tr,
            "/sc",
            "onstart",
            "/ru",
            "SYSTEM",
            "/rl",
            "highest",
            "/f",
        ],
        capture_output=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    if created.returncode != 0:
        detail = (created.stderr or created.stdout or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"could not create Scheduled Task {_SERVE_TASK}: {detail or 'unknown error'}")
    if run_now:
        started = subprocess.run(
            ["schtasks", "/run", "/tn", _SERVE_TASK],
            capture_output=True,
            timeout=30,
            creationflags=NO_WINDOW,
        )
        if started.returncode != 0:
            detail = (started.stderr or started.stdout or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(f"Scheduled Task {_SERVE_TASK} was created but could not start: {detail or 'unknown error'}")
    return _SERVE_TASK


def uninstall_serve_task() -> bool:
    """Remove the startup Scheduled Task if present."""
    if os.name != "nt":
        return False
    r = subprocess.run(
        ["schtasks", "/delete", "/tn", _SERVE_TASK, "/f"],
        capture_output=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    return r.returncode == 0
