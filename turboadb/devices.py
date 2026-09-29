"""Enumerate attached devices (USB + network) via ``adb devices -l``."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .config import adb_server_args, parse_host_port, user_path, validate_port
from .scrcpy import TUNNEL_PORT_FIREWALL_RANGE
from .tools import (
    DEFAULT_ADB_SERVER_PORT,
    DETACHED,
    NO_WINDOW,
    find_adb,
    windowless_python,
)

_log = logging.getLogger(__name__)

# The serial adb gives a Wireless-debugging device it connected by itself
# through mDNS: the service instance, "adb-SERIAL-xxxxxx._adb-tls-connect._tcp"
# (some adb versions end it with a dot; older ones use the "_adb._tcp" service).
_MDNS_SERIAL_RE = re.compile(r"\._adb(?:-tls-connect)?\._tcp\.?$", re.IGNORECASE)


def is_mdns_serial(serial) -> bool:
    """True for a device adb found and connected through mDNS (its serial is
    the service name, which has no ``:port``)."""
    return bool(_MDNS_SERIAL_RE.search(str(serial or "").strip()))


def is_network_serial(serial) -> bool:
    """True when adb reaches the device *serial* over the network: a TCP/IP
    target (``192.168.0.5:5555``, ``[fe80::1]:5555``) or a device adb connected
    through mDNS (``adb-XXXX-yyyyyy._adb-tls-connect._tcp``)."""
    host, port = parse_host_port(serial)
    return (bool(host) and port is not None) or is_mdns_serial(serial)


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
        """Reached over the network (see :func:`is_network_serial`)."""
        return is_network_serial(self.serial)

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


def _list_devices_socket(host: str = "127.0.0.1", port: int = DEFAULT_ADB_SERVER_PORT, timeout: float = 1.0) -> list[Device] | None:
    """Query connected devices directly via ADB server socket protocol.

    Fast path (~10-15ms) that avoids spawning adb.exe subprocesses repeatedly on Windows.
    Returns None if the server is not reachable so caller can fall back to CLI.
    On a loopback *host* the default port follows ``ANDROID_ADB_SERVER_PORT``.
    """
    from .tools import adb_query

    data = adb_query("host:devices-l", host, port, timeout)
    if data is None:
        return None
    devices = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        dev = _parse_line(line)
        if dev is not None:
            devices.append(dev)
    return devices


def list_devices(
    adb_path: str | None = None,
    timeout: float = 5.0,
    server_host: str | None = None,
    server_port: int = DEFAULT_ADB_SERVER_PORT,
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
    from .scrcpy import is_local_host

    target_host = server_host or "127.0.0.1"
    # The one rule for "this PC", which the mirror follows too: a LAN address
    # or the name of this machine is its own adb server, not a remote one.
    is_local = is_local_host(target_host)
    if not server_host:
        from .tools import local_adb_port

        # adb's default port, as ANDROID_ADB_SERVER_PORT may have moved it
        server_port = local_adb_port(server_port)
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
        cmd += adb_server_args(server_host, server_port)
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


def remote_devices(server_host: str, server_port: int = DEFAULT_ADB_SERVER_PORT, adb_path: str | None = None) -> list:
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


def mdns_devices(
    adb_path: str | None = None,
    timeout: float = 10.0,
    server_host: str | None = None,
    server_port: int = DEFAULT_ADB_SERVER_PORT,
) -> list:
    """Discover Android 11+ *Wireless debugging* devices on the LAN via
    ``adb mdns services``. Returns a list of dicts
    ``{"name","service","host","port","address"}`` where ``service`` is
    ``connect`` (ready for ``adb connect``) or ``pairing`` (shows a pair code).

    The adb server does the discovery, so with *server_host* the devices are
    the ones on **that** machine's network (``adb -H host -P port``), the
    same server an ``adb connect`` through it would use; a non-default
    *server_port* alone picks a local server on that port.

    Returns ``[]`` when nothing is found or this adb has no mdns support —
    never raises for those cases (only for adb itself being missing)."""
    adb = find_adb(adb_path)
    cmd = [adb]
    if server_host:
        cmd += adb_server_args(server_host, server_port)
    elif server_port != DEFAULT_ADB_SERVER_PORT:
        cmd += ["-P", str(server_port)]
    try:
        out = subprocess.run(
            cmd + ["mdns", "services"],
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


def _listens_on_all_interfaces(port: int, timeout: float = 0.5) -> Optional[bool]:
    """Whether the server on *port* is bound to every interface, asked on a
    second loopback address: a localhost-only adb server is bound to 127.0.0.1
    and refuses 127.0.0.2, while a shared one (``adb -a``) accepts it at once.
    Needs no LAN address.  None when this system cannot tell (no 127.0.0.2)."""
    import socket

    try:
        with socket.create_connection(("127.0.0.2", port), timeout=timeout):
            return True
    except ConnectionRefusedError:
        return False
    except socket.timeout:
        # Windows retries a refused loopback connect for about 2 s, where an
        # accept takes a millisecond.
        return False if os.name == "nt" else None
    except OSError:
        return None


def server_is_shared(port: int = DEFAULT_ADB_SERVER_PORT, adb_path: str | None = None) -> bool:
    """True if an adb server is up AND listening beyond loopback — i.e. another
    machine could drive this PC's devices (``turboadb serve``, ``adb -a``).

    Uses socket probes, never ``adb devices`` (which silently STARTS a
    localhost-only daemon when none is running). A PC without a LAN address
    no longer counts every server as shared: a localhost-only one is not.
    *adb_path* is accepted for backward compatibility and is no longer needed."""
    from .tools import is_adb_server_alive, local_adb_port

    port = local_adb_port(validate_port(port))
    if not is_adb_server_alive(port=port):
        return False
    everywhere = _listens_on_all_interfaces(port)
    if everywhere is not None:
        return everywhere
    # cannot tell from here: judge by this PC's LAN address, if it has one
    return bool(_lan_reachable(port))


def _beyond_loopback(port: int) -> Optional[bool]:
    """Whether the server on *port* listens beyond loopback (see
    :func:`_listens_on_all_interfaces`, then this PC's LAN address); None
    when neither can tell."""
    everywhere = _listens_on_all_interfaces(port)
    if everywhere is not None:
        return everywhere
    return _lan_reachable(port)


def _shared_now(port: int) -> bool:
    """A server answers on *port* and other machines can reach it.  Unlike
    :func:`server_is_shared`, a PC that cannot tell counts it as shared, as
    :func:`start_shared_server` does."""
    from .tools import is_adb_server_alive

    return is_adb_server_alive(port=port, timeout=0.25) and _beyond_loopback(port) is not False


def _wait_shared(port: int, timeout: float) -> bool:
    """Wait up to *timeout* s for a shared server on *port* (:func:`_shared_now`)."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if _shared_now(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


# --------------------------------------------------------------------------- #
# What this account's device sharing remembers (~/.turboadb/sharing.json)
# --------------------------------------------------------------------------- #
def _sharing_path(home: str | None = None) -> str:
    """This account's sharing record, or the one in the profile folder *home*."""
    if home:
        from .config import USER_DIR_NAME

        return os.path.join(home, USER_DIR_NAME, "sharing.json")
    return user_path("sharing.json")


def _read_sharing(path: str | None = None) -> dict:
    try:
        with open(path or _sharing_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _change_sharing(change, path: str | None = None) -> bool:
    """Apply *change* (a function of the record) and save the record; False
    when it could not be saved.  The record only saves work later, so a
    failure is never a reason to fail the sharing itself."""
    path = path or _sharing_path()
    data = _read_sharing(path)
    change(data)
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(temporary, path)
        return True
    except OSError as exc:
        _log.debug("could not save %s: %s", path, exc)
        try:
            os.remove(temporary)
        except OSError:
            pass
        return False


def _remember_shared_port(port: int, shared: bool = True) -> None:
    def change(data):
        ports = [p for p in (data.get("ports") or []) if isinstance(p, int) and p != port]
        data["ports"] = sorted(ports + [port]) if shared else ports

    _change_sharing(change)


def recorded_shared_ports() -> list:
    """The ports this account started a shared adb server on (``turboadb
    serve --port N``, Share THIS PC's devices), and the port of the SYSTEM
    startup task it installed.  A port on the list may no longer be shared:
    ask :func:`server_is_shared`.  A tools update uses it to stop, and then
    bring back, every server that runs the adb it replaces."""
    data = _read_sharing()
    ports = []
    for value in list(data.get("ports") or []) + [data.get("task_port")]:
        try:
            port = validate_port(value)
        except ValueError:
            continue
        if port not in ports:
            ports.append(port)
    return ports


# --------------------------------------------------------------------------- #
# Firewall rules for a shared server
# --------------------------------------------------------------------------- #
# The adb server has no password: anyone who reaches its port can drive every
# device plugged in here.  The rules therefore apply on Domain and Private
# networks only, never on the Public networks of cafés, hotels and airports.
FIREWALL_PROFILES = "domain,private"
_PROFILE_NAMES = ("domain", "private", "public")
# What netsh accepts after remoteip=: addresses, ranges, subnets and keywords
# such as localsubnet, comma-separated.
_REMOTE_IP = re.compile(r"[A-Za-z0-9.:/,\-]+")


def _rule_name(spec: str) -> str:
    return f"TurboADB TCP {spec}"


def _port_specs(ports) -> tuple:
    """``(valid, invalid)``: each entry of *ports* as netsh spells it (a port,
    or an inclusive ``start-end`` range), or in *invalid* when it is neither."""
    valid, invalid = [], []
    for p in ports:
        try:
            raw = str(p).strip()
            if "-" in raw:
                first, last = raw.split("-", 1)
                first, last = validate_port(first), validate_port(last)
                if first > last:
                    raise ValueError("invalid descending port range")
                valid.append(f"{first}-{last}")
            else:
                valid.append(str(validate_port(raw)))
        except ValueError:
            invalid.append(str(p))
    return valid, invalid


def _firewall_profiles(value) -> Optional[str]:
    """*value* as netsh's ``profile=`` takes it, or None when it is not one."""
    names = [n.strip().lower() for n in str(value or "").split(",") if n.strip()]
    if names == ["any"]:
        return "any"
    if not names or any(n not in _PROFILE_NAMES for n in names):
        return None
    return ",".join(n for n in _PROFILE_NAMES if n in names)


def _on_networks(profiles: str) -> str:
    if profiles == "any":
        return "on every network"
    names = [n.capitalize() for n in profiles.split(",")]
    if len(names) > 1:
        names = [", ".join(names[:-1]), names[-1]]
    return f"on {' and '.join(names)} networks"


def _public_networks() -> list:
    """The names of this PC's connected networks Windows treats as Public, where
    rules for Domain and Private networks do not apply.  Best-effort: [] when
    it cannot tell (no PowerShell, Windows 7)."""
    script = (
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); "
        "Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq 'Public' } "
        "| ForEach-Object { $_.Name }"
    )
    try:
        found = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )
    except Exception:
        return []
    if found.returncode != 0:
        return []
    text = (found.stdout or b"").decode("utf-8", "replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def open_firewall(
    ports=(5037, TUNNEL_PORT_FIREWALL_RANGE), *, profiles: str = FIREWALL_PROFILES,
    remote_ip: str | None = None,
) -> str:
    """Best-effort: open the given TCP ports in the Windows firewall so a remote
    machine can reach this PC's adb server (5037) AND scrcpy's video tunnel
    range. Entries may be a port or an inclusive ``start-end`` range. Needs
    admin rights; returns a status string (never raises).

    The rules apply on Domain and Private networks only (*profiles*, a
    comma-separated list of ``domain``, ``private``, ``public``, or ``any``):
    the shared server has no password, and a laptop taken to a café or hotel
    network (Public) used to let anyone there drive its devices.  On a
    network Windows calls Public nobody reaches it until that network is made
    Private, which the status says.  *remote_ip* narrows the rules to what
    netsh accepts after ``remoteip=`` (``localsubnet``, ``10.1.0.0/16``, …);
    by default any address on those networks may connect, as VPN and RDP labs
    span subnets."""
    if os.name != "nt":
        return "firewall: not Windows, skipped"
    scope = _firewall_profiles(profiles)
    if scope is None:
        return (f"firewall: not changed — {profiles!r} is not a list of firewall profiles "
                "(domain, private, public, or any)")
    remote = str(remote_ip or "").strip()
    if remote and not _REMOTE_IP.fullmatch(remote):
        return f"firewall: not changed — {remote_ip!r} is not an address list netsh accepts"
    specs, failed = _port_specs(ports)
    opened = []
    for p in specs:
        rule = _rule_name(p)
        try:
            # remove any old rule (an older one allowed every network), then add
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule}"],
                capture_output=True,
                timeout=15,
                creationflags=NO_WINDOW,
            )
            cmd = [
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
                f"profile={scope}",
            ]
            if remote:
                cmd.append(f"remoteip={remote}")
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=NO_WINDOW,
            )
            (opened if r.returncode == 0 else failed).append(p)
        except Exception:
            failed.append(p)
    if not opened:
        return (
            "firewall: could not open ports (run TurboADB/`turboadb serve` as "
            f"Administrator, or open TCP 5037 + {TUNNEL_PORT_FIREWALL_RANGE} manually)"
        )
    where = _on_networks(scope) + (f" for {remote}" if remote else "")
    status = f"firewall: opened TCP {', '.join(opened)} {where}"
    if failed:
        status += f"; could NOT open {', '.join(failed)} (run as Administrator to allow those)"
    if scope != "any" and "public" not in scope:
        public = _public_networks()
        if public:
            names = ", ".join(repr(name) for name in public)
            status += (f"; this PC is on {names}, a Public network: make it Private in "
                       "Windows Settings → Network & internet for other PCs there to connect")
    return status + ". The adb server has no password: share it on trusted networks only."


def _rule_exists(rule: str) -> bool:
    try:
        shown = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"],
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )
    except Exception:
        return True  # cannot tell: try to delete it anyway
    return shown.returncode == 0


def _close_firewall(ports) -> tuple:
    """``(closed, failed)``: the port specs whose TurboADB rule was deleted,
    and those whose rule is there but could not be (no admin rights)."""
    closed, failed = [], []
    if os.name != "nt":
        return closed, failed
    for p in _port_specs(ports)[0]:
        rule = _rule_name(p)
        if not _rule_exists(rule):
            continue
        try:
            ok = subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule}"],
                capture_output=True,
                timeout=15,
                creationflags=NO_WINDOW,
            ).returncode == 0
        except Exception:
            ok = False
        (closed if ok else failed).append(p)
    return closed, failed


def _closed_note(closed, failed) -> str:
    bits = []
    if closed:
        bits.append(f"firewall: closed TCP {', '.join(closed)}")
    if failed:
        bits.append(f"firewall: could not close TCP {', '.join(failed)} (run as Administrator, "
                    "or delete the 'TurboADB TCP' rules in Windows Defender Firewall)")
    return "; ".join(bits)


def close_firewall(ports=(DEFAULT_ADB_SERVER_PORT, TUNNEL_PORT_FIREWALL_RANGE)) -> str:
    """Best-effort: delete the rules :func:`open_firewall` added for *ports*,
    once nothing here is shared any more.  Needs admin rights, like adding
    them; returns a status string (never raises)."""
    if os.name != "nt":
        return "firewall: not Windows, skipped"
    return _closed_note(*_close_firewall(ports)) or "firewall: no TurboADB rule was open"


# --------------------------------------------------------------------------- #
# Starting and stopping the shared server
# --------------------------------------------------------------------------- #
class _PortTaken(RuntimeError):
    """A localhost-only server took the port while the shared one was starting."""


def start_shared_server(
    port: int = DEFAULT_ADB_SERVER_PORT, adb_path: str | None = None, *, restart: bool = True
) -> str:
    """Start an adb server that listens on **all** network interfaces so other
    machines can drive this PC's devices via ``adb -H thispc -P {port}``.

    This is the automated equivalent of ``adb -a nodaemon server start`` — but it
    is launched **detached in the background**, so it keeps listening without
    blocking. A localhost-only server already running would stop ``-a`` from
    binding to ``0.0.0.0``, so by default we replace it first.

    Readiness is confirmed with socket probes only (``adb devices`` would start a
    plain localhost daemon of its own and report a false success), and the port
    must also answer on this PC's LAN address.  The port is recorded, so a
    tools update can bring this server back (see :func:`recorded_shared_ports`).

    Returns a short status string; raises RuntimeError on failure.
    """
    from . import tools

    port = tools.local_adb_port(validate_port(port))
    adb = find_adb(adb_path)
    # Held from the stop to the new server's first answer: a device tab that
    # reconnects meanwhile would otherwise start a localhost-only server that
    # takes the port first.
    with tools.adb_server_lock():
        status = _start_shared_server(port, adb, restart)
    _remember_shared_port(port)
    return status


def _server_env() -> Optional[dict]:
    """The environment for a shared server: this account's own plus the adb
    keys recorded for it (see :func:`_share_keys_with_system`), or None to
    inherit it unchanged."""
    keys = [k for k in (_read_sharing().get("adb_vendor_keys") or [])
            if isinstance(k, str) and k and os.path.exists(k)]
    if not keys:
        return None
    env = dict(os.environ)
    current = [p for p in (env.get("ADB_VENDOR_KEYS") or "").split(os.pathsep) if p]
    env["ADB_VENDOR_KEYS"] = os.pathsep.join(current + [k for k in keys if k not in current])
    return env


def _start_shared_server(port: int, adb: str, restart: bool) -> str:
    from .tools import is_adb_server_alive, kill_adb_server

    if not restart and is_adb_server_alive(port=port, timeout=0.2):
        if _lan_reachable(port) is not False:
            return f"shared adb server is already listening on 0.0.0.0:{port}"
        raise RuntimeError(
            f"a localhost-only adb server is already running on port {port}; "
            "restart it (restart=True) so the shared server can bind all interfaces"
        )
    if restart:
        # drop any localhost-only server so the new one can bind all interfaces
        # (kill_adb_server waits for the old daemon to release the port)
        kill_adb_server(adb, port)
    try:
        return _launch_shared_server(port, adb)
    except _PortTaken:
        if not restart:
            raise
    # An adb client outside the server lock (a terminal's command, another
    # tool) started a localhost-only server in the gap: stop it, try once
    # more.  A plain server lets go of the port at once; one that does not
    # within a second is not going away, and the second attempt says so.
    kill_adb_server(adb, port, wait=1.0)
    return _launch_shared_server(port, adb)


def _launch_shared_server(port: int, adb: str) -> str:
    from .tools import is_adb_server_alive

    extra = {}
    if os.name != "nt":
        extra["start_new_session"] = True
    proc = subprocess.Popen(
        [adb, "-a", "-P", str(port), "nodaemon", "server", "start"],
        creationflags=NO_WINDOW | DETACHED,  # survives, no console
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_server_env(),
        **extra,
    )
    deadline = time.monotonic() + 10.0
    while True:
        if is_adb_server_alive(port=port, timeout=0.2):
            break
        code = proc.poll()
        if code is not None:
            if is_adb_server_alive(port=port, timeout=0.2) and _lan_reachable(port) is False:
                raise _PortTaken(
                    f"adb server exited with code {code}; another localhost-only adb "
                    f"server holds port {port}, so this PC is NOT shared"
                )
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
        raise _PortTaken(
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


def stop_shared_server(port: int = DEFAULT_ADB_SERVER_PORT, adb_path: str | None = None) -> str:
    """Stop the network-shared adb server and return to a normal local-only one:
    kill the ``-a`` (all-interfaces) server, then start a plain server that binds
    to localhost again, so this PC keeps working but no longer shares its devices.
    Best-effort; returns a short status string.

    The stop waits for the shared daemon to release the port (a start in that
    gap reached the dying server, reported success and left no server), and
    the start goes through the shared launcher like every other start.  The
    firewall rules :func:`open_firewall` added are deleted too (the video
    tunnel's only when no other shared server needs them)."""
    from . import tools

    port = tools.local_adb_port(validate_port(port))
    adb = find_adb(adb_path)
    stopped = tools.kill_adb_server(adb, port)
    # Several adb builds exit non-zero from kill-server when no daemon is
    # running, so "Stop sharing" used to fail loudly when it was ALREADY
    # stopped. Like core.restart_server, a failed kill is only a note and
    # success is judged by the local server coming back.
    if not tools.ensure_adb_server(adb, timeout=15.0, port=port):
        detail = tools.last_adb_server_error()
        raise RuntimeError(f"could not start local adb server: {detail or 'unknown error'}")
    _remember_shared_port(port, shared=False)
    rules = [port]
    if not any(p != port and server_is_shared(p) for p in recorded_shared_ports()):
        rules.append(TUNNEL_PORT_FIREWALL_RANGE)
    closed = _closed_note(*_close_firewall(rules))
    note = " (it was not running)" if stopped is not None and stopped.returncode != 0 else ""
    return (f"shared adb server stopped — back to local-only (localhost){note}"
            + (f"  ·  {closed}" if closed else ""))


def restart_shared_server(port: int = DEFAULT_ADB_SERVER_PORT, adb_path: str | None = None) -> str:
    """Share this PC's devices again after the shared server had to stop (a
    tools update replaces the adb it runs).  The SYSTEM startup task starts it
    when it is installed for this port, so the server stays one that survives
    logoff; otherwise, or when this user may not run that task, it starts as
    this user.  The task's server counts once other machines can reach it: a
    localhost-only server something else started meanwhile answers at once,
    and that is not the task's.  Returns a short status string; raises
    RuntimeError on failure."""
    from .tools import local_adb_port

    port = local_adb_port(validate_port(port))
    if os.name == "nt" and _task_port() == port and _serve_task_installed():
        try:
            ran = subprocess.run(
                ["schtasks", "/run", "/tn", _SERVE_TASK],
                capture_output=True,
                timeout=30,
                creationflags=NO_WINDOW,
            )
        except Exception:
            ran = None
        if ran is not None and ran.returncode == 0 and _wait_shared(port, 20.0):
            return f"shared adb server restarted by the {_SERVE_TASK} startup task"
    return start_shared_server(port, adb_path, restart=True)


def _startup_dir() -> str:
    """The current user's Windows Startup folder (programs run at login).

    Without APPDATA there is no Startup folder to write to; the old fallback to
    the home directory happily reported success for a launcher that Windows
    would never run."""
    appdata = (os.environ.get("APPDATA") or "").strip()
    if not appdata:
        raise RuntimeError(
            "APPDATA is not set, so this account's Windows Startup folder cannot "
            "be located. Use the SYSTEM startup task instead:  "
            "turboadb serve --startup-task"
        )
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _short_path(path: str) -> str:
    """The DOS 8.3 form of *path* (always plain ASCII), or *path* unchanged.

    The login ``.bat`` is read by cmd.exe in the console codepage, which may have
    no room for a profile directory with an accent; the short path always has."""
    if os.name != "nt" or not path:
        return path
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(4096)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
        if 0 < n < len(buf) and buf.value:
            return buf.value
    except Exception:
        pass
    return path


def _batch_encoding() -> str:
    """The codepage cmd.exe decodes a ``.bat`` file with (the OEM codepage).

    Writing the launcher as UTF-8 silently produced a broken launcher for any
    non-ASCII interpreter path — cmd.exe never reads batch files as UTF-8."""
    if os.name != "nt":
        return "utf-8"
    try:
        import ctypes

        cp = int(ctypes.windll.kernel32.GetOEMCP())
        if cp:
            return f"cp{cp}"
    except Exception:
        pass
    return "mbcs"


def _pinned_adb(adb_path: str | None = None) -> Optional[str]:
    """The adb THIS user resolves, to be pinned into a launcher/task.

    A SYSTEM scheduled task runs under a profile that has no
    ``~/.turboadb/tools`` and no GUI settings, so it resolved a *different* adb
    and bound port 5037 with it — two adb binaries taking turns on 5037 is a
    known cause of Windows device disconnects. Returns None when adb cannot be
    resolved here either (the launcher then falls back to its own search)."""
    try:
        return find_adb(adb_path)
    except Exception:
        return None


def _quotable(path: str, what: str) -> str:
    """*path*, or a clear error when it cannot be safely double-quoted.

    The result goes into a cmd.exe ``.bat`` and into ``schtasks /tr``, which
    runs as SYSTEM. A quote would end the quoted argument early and let the
    rest be read as further arguments, and a line break would start a new
    command; neither can appear in a Windows path. ``%`` can (it is legal in
    a folder name), but cmd.exe expands it even inside quotes, so the
    launcher could not run that path as written. ``&``, ``^`` and the like
    are literal inside the quotes and are allowed."""
    bad = [ch for ch in (chr(34), chr(10), chr(13), "%") if ch in path]
    if bad:
        raise RuntimeError(
            f"refusing to build a startup command: the {what} path contains "
            f"{' '.join(repr(c) for c in bad)}, which cmd.exe and the task "
            f"scheduler would read as syntax -- {path!r}"
        )
    return path


def _serve_launcher(port: int, adb_path: str | None = None, *, short_paths: bool = False) -> str:
    """The quoted command that runs ``turboadb serve`` headlessly.

    *adb_path* pins the adb executable into the command so the background
    launcher can never fork a second toolchain (see :func:`_pinned_adb`).

    The standalone (PyInstaller) exe cannot do this: ``TurboADB.exe -m turboadb
    serve`` just opens the GUI and never starts a server, so a login launcher or
    SYSTEM task built from it would silently share nothing."""
    if getattr(sys, "frozen", False):
        raise RuntimeError(
            "The standalone TurboADB executable cannot run the headless 'serve' "
            "command at startup. Install the Python package on this PC "
            "(pip install turboadb) and run:  turboadb serve --startup-task"
        )
    python = windowless_python()
    if short_paths:
        python = _short_path(python)
        adb_path = _short_path(adb_path) if adb_path else adb_path
    cmd = f'"{_quotable(python, "interpreter")}" -m turboadb serve --port {validate_port(port)}'
    if adb_path:
        cmd += f' --adb-path "{_quotable(adb_path, "adb")}"'
    return cmd


def install_startup(port: int = DEFAULT_ADB_SERVER_PORT, adb_path: str | None = None) -> str:
    """Make the shared adb server start automatically at every Windows login by
    dropping a tiny launcher in the Startup folder. Returns the file path.
    So it really never has to be done by hand again."""
    from .tools import local_adb_port

    if os.name != "nt":
        raise RuntimeError("Startup install is only supported on Windows.")
    port = local_adb_port(validate_port(port))
    adb = _pinned_adb(adb_path)
    d = _startup_dir()
    os.makedirs(d, exist_ok=True)
    bat = os.path.join(d, "turboadb-shared-adb.bat")
    # pythonw -m turboadb serve, detached, no window. The bytes must be written
    # in the codepage cmd.exe reads, not UTF-8.
    encoding = _batch_encoding()
    text = f'@echo off\r\nstart "" /b {_serve_launcher(port, adb)}\r\n'
    try:
        data = text.encode(encoding)
    except UnicodeEncodeError:
        text = f'@echo off\r\nstart "" /b {_serve_launcher(port, adb, short_paths=True)}\r\n'
        try:
            data = text.encode(encoding)
        except UnicodeEncodeError as exc:
            raise RuntimeError(
                "the interpreter path cannot be written into a batch file cmd.exe "
                f"can read ({encoding}). Use the SYSTEM startup task instead:  "
                "turboadb serve --startup-task"
            ) from exc
    with open(bat, "wb") as fh:
        fh.write(data)
    return bat


def uninstall_startup() -> bool:
    """Remove the login auto-start launcher if present."""
    if os.name != "nt":
        return False
    try:
        bat = os.path.join(_startup_dir(), "turboadb-shared-adb.bat")
    except RuntimeError:
        return False  # no Startup folder -> nothing was ever installed
    if os.path.exists(bat):
        os.remove(bat)
        return True
    return False


_SERVE_TASK = "TurboADBSharedADB"


def _serve_task_installed() -> bool:
    """True when the SYSTEM startup task of ``turboadb serve`` exists."""
    try:
        found = subprocess.run(
            ["schtasks", "/query", "/tn", _SERVE_TASK],
            capture_output=True,
            timeout=30,
            creationflags=NO_WINDOW,
        )
    except Exception:
        return False
    return found.returncode == 0


def _task_last_result(task: str) -> str:
    """The scheduler's own verdict on the last run, for an actionable error.

    Best-effort: schtasks localises its labels, so the 'Last Result' line is
    matched loosely and an empty string simply means 'not available'."""
    try:
        q = subprocess.run(
            ["schtasks", "/query", "/tn", task, "/fo", "list", "/v"],
            capture_output=True,
            timeout=30,
            creationflags=NO_WINDOW,
        )
    except Exception:
        return ""
    text = (q.stdout or b"").decode("utf-8", "replace")
    for line in text.splitlines():
        head, _, tail = line.partition(":")
        if "result" in head.strip().lower() and tail.strip():
            return f"{head.strip()}: {tail.strip()}"
    return ""


def _task_port() -> int:
    """The port the SYSTEM startup task shares, as recorded when this account
    installed it; a task installed before that was recorded serves the
    default one."""
    from .tools import local_adb_port

    try:
        return validate_port(_read_sharing().get("task_port"))
    except ValueError:
        return local_adb_port()


def _system_home() -> Optional[str]:
    """The SYSTEM account's profile folder, the startup task's ``~`` (None
    anywhere but Windows)."""
    if sys.platform != "win32":
        return None
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(root, "System32", "config", "systemprofile")


def _user_adb_keys() -> list:
    """This user's adb key files, the ones this user's devices already trust:
    ``adbkey`` in adb's user folder (``ANDROID_USER_HOME``, else
    ``~/.android``) and whatever ``ADB_VENDOR_KEYS`` names."""
    folder = (os.environ.get("ANDROID_USER_HOME") or "").strip()
    if not folder:
        sdk_home = (os.environ.get("ANDROID_SDK_HOME") or "").strip()
        folder = os.path.join(sdk_home or os.path.expanduser("~"), ".android")
    keys = []
    candidates = [os.path.join(folder, "adbkey")]
    candidates += (os.environ.get("ADB_VENDOR_KEYS") or "").split(os.pathsep)
    for path in candidates:
        path = path.strip()
        if path and os.path.exists(path):
            path = os.path.abspath(path)
            if path not in keys:
                keys.append(path)
    return keys


def _share_keys_with_system() -> None:
    """Let the SYSTEM task's adb server sign in to devices with this user's
    adb keys as well as its own.

    adb signs with the key of the account it runs as, and devices had only
    ever been told to trust this user's: every one of them showed as
    "unauthorized" to the task's server until someone accepted SYSTEM's key
    at its screen, which a headless rig cannot do.  adb also tries the keys
    named in ``ADB_VENDOR_KEYS``; a scheduled task cannot set variables, so
    they are recorded in SYSTEM's own sharing record, which the task's
    :func:`start_shared_server` passes on (SYSTEM can read this profile)."""
    system_home = _system_home()
    if not system_home:
        return
    keys = _user_adb_keys()
    path = _sharing_path(system_home)
    if not keys:
        _log.warning("this user has no adb key yet (~/.android/adbkey): each device must "
                     "accept the startup task's own key once, at its screen")
        if "adb_vendor_keys" in _read_sharing(path):  # another user's, from before
            _change_sharing(lambda data: data.pop("adb_vendor_keys", None), path)
        return
    if _read_sharing(path).get("adb_vendor_keys") == keys:
        return
    if _change_sharing(lambda data: data.__setitem__("adb_vendor_keys", keys), path):
        _log.info("the startup task's adb server also signs in with this user's adb key "
                  "(%s)", ", ".join(keys))
    else:
        _log.warning("could not give the startup task this user's adb key (%s): each device "
                     "must accept the task's own key once, at its screen", path)


def _serve_on(port: int) -> Optional[str]:
    """What answers on *port* now: "shared", "local" or None (nothing)."""
    from .tools import is_adb_server_alive

    if not is_adb_server_alive(port=port, timeout=0.25):
        return None
    return "shared" if _beyond_loopback(port) is not False else "local"


def _serve_again(kind: Optional[str], port: int, adb: Optional[str]) -> str:
    """Start the server that answered on *port* before (*kind*, see
    :func:`_serve_on`) again after the startup task failed; a sentence for
    the error, or ""."""
    from . import tools

    try:
        if kind == "shared":
            start_shared_server(port, adb, restart=True)
            return " This PC shares its devices as this user meanwhile, until logoff."
        if kind == "local" and not tools.ensure_adb_server(adb, port=port):
            return (" The local adb server did not start again: "
                    f"{tools.last_adb_server_error() or 'unknown error'}.")
    except Exception as exc:
        return f" The adb server that was running did not start again: {exc}."
    return ""


def install_serve_task(
    port: int = DEFAULT_ADB_SERVER_PORT, *, run_now: bool = True, adb_path: str | None = None,
    ready_timeout: float = 20.0,
) -> str:
    """Register a Scheduled Task that runs the shared adb server at SYSTEM
    **startup** — headless and persistent (survives logoff and needs no login,
    unlike the Startup-folder launcher). Optionally start it immediately via the
    scheduler, which detaches it from whatever session created it (e.g. a WinRM
    remote-deploy session). Returns the task name. Needs admin rights.

    The adb THIS user resolves is pinned into the task's command line, because
    SYSTEM's profile has neither ``~/.turboadb/tools`` nor the GUI settings and
    would otherwise bind port 5037 with a different adb binary.  This user's
    adb keys go with it (:func:`_share_keys_with_system`), so the devices it
    already uses need no new authorization.

    With *run_now*, success means the TASK's server is listening for other
    machines.  ``schtasks /run`` only reports that the task was launched, and
    a server already on the port (the one ``turboadb serve`` itself started a
    moment earlier) answered the check before the task had done anything, so
    a task that could not even start Python passed.  That server is stopped
    first, the port is polled for *ready_timeout* seconds, and when the task
    does not bring its own up, its last result is reported and the server
    that was there is started again."""
    from . import tools

    if os.name != "nt":
        raise RuntimeError("Scheduled-task install is Windows-only.")
    # SYSTEM does not have this user's ANDROID_ADB_SERVER_PORT: the task gets
    # the port it moves the server to, or its server is never found.
    port = tools.local_adb_port(validate_port(port))
    adb = _pinned_adb(adb_path)
    tr = _serve_launcher(port, adb)
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
    _change_sharing(lambda data: data.__setitem__("task_port", port))
    _share_keys_with_system()
    if run_now:
        before = _serve_on(port)
        if before:
            tools.kill_adb_server(adb, port)
        started = subprocess.run(
            ["schtasks", "/run", "/tn", _SERVE_TASK],
            capture_output=True,
            timeout=30,
            creationflags=NO_WINDOW,
        )
        if started.returncode != 0:
            detail = (started.stderr or started.stdout or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(
                f"Scheduled Task {_SERVE_TASK} was created but could not start: "
                f"{detail or 'unknown error'}." + _serve_again(before, port, adb))
        if not _wait_shared(port, ready_timeout):
            last = _task_last_result(_SERVE_TASK)
            raise RuntimeError(
                f"Scheduled Task {_SERVE_TASK} was started but no adb server is "
                f"listening on port {port} for other machines after {ready_timeout:g}s"
                + (f" — {last}" if last else "")
                + ". Check that Python and turboadb are installed machine-wide "
                "(SYSTEM cannot see a per-user install)."
                + _serve_again(before, port, adb)
            )
    return _SERVE_TASK


def uninstall_serve_task() -> bool:
    """Remove the startup Scheduled Task if present (and what was recorded for
    it: its port, and the adb keys it was given)."""
    if os.name != "nt":
        return False
    r = subprocess.run(
        ["schtasks", "/delete", "/tn", _SERVE_TASK, "/f"],
        capture_output=True,
        timeout=30,
        creationflags=NO_WINDOW,
    )
    if r.returncode != 0:
        return False
    _change_sharing(lambda data: data.pop("task_port", None))
    system_home = _system_home()
    if system_home and "adb_vendor_keys" in _read_sharing(_sharing_path(system_home)):
        _change_sharing(lambda data: data.pop("adb_vendor_keys", None), _sharing_path(system_home))
    return True
