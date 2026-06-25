"""Enumerate attached devices (USB + network) via ``adb devices -l``."""

from __future__ import annotations

import os
import time
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .tools import find_adb, NO_WINDOW


@dataclass
class Device:
    """One entry from ``adb devices -l``."""

    serial: str
    state: str                       # device | offline | unauthorized | no permissions
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
        host, _, port = self.serial.rpartition(":")
        return bool(host) and port.isdigit()

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
    extra = {}
    for tok in parts[2:]:
        if ":" in tok:
            k, _, v = tok.partition(":")
            extra[k] = v
    return Device(
        serial=serial, state=state,
        model=extra.get("model", ""), product=extra.get("product", ""),
        device=extra.get("device", ""), transport_id=extra.get("transport_id", ""),
        extra=extra,
    )


def list_devices(adb_path: str | None = None, timeout: float = 15.0,
                 server_host: str | None = None, server_port: int = 5037) -> list:
    """Return a list of :class:`Device` for every attached/known target.

    With *server_host* set, the query is sent to a **remote** machine's adb
    server (``adb -H host -P port devices``) — i.e. the devices physically
    attached to *that* machine.

    Raises :class:`ADBNotFoundError` if adb is missing; returns ``[]`` if adb
    runs but nothing is connected. Raises ConnectionError if a remote server is
    requested but unreachable.
    """
    adb = find_adb(adb_path)
    cmd = [adb]
    if server_host:
        cmd += ["-H", server_host, "-P", str(server_port)]
    cmd += ["devices", "-l"]
    _unreachable = (
        f"Could not reach the adb server at {server_host}:{server_port}. On that "
        f"machine run:  adb -a nodaemon server start  (and allow TCP {server_port} "
        f"through its firewall).")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        if server_host:                 # a hung connection = unreachable server
            raise ConnectionError(_unreachable) from exc
        raise
    text = (out.stdout or "") + (out.stderr or "")
    if server_host and ("cannot connect" in text.lower()
                        or "failed to connect" in text.lower()):
        raise ConnectionError(_unreachable)
    devices = []
    for line in (out.stdout or "").splitlines():
        dev = _parse_line(line)
        if dev is not None:
            devices.append(dev)
    return devices


def remote_devices(server_host: str, server_port: int = 5037,
                   adb_path: str | None = None) -> list:
    """Convenience: list devices attached to a remote machine's adb server."""
    return list_devices(adb_path, server_host=server_host, server_port=server_port)


def first_online(adb_path: str | None = None) -> Optional[Device]:
    """Return the first online device, or None."""
    for d in list_devices(adb_path):
        if d.is_online:
            return d
    return None


# --------------------------------------------------------------------------- #
# Shared adb server (expose THIS PC's devices to the network)
# --------------------------------------------------------------------------- #
def server_is_shared(port: int = 5037, adb_path: str | None = None) -> bool:
    """True if an adb server is already up AND reachable on all interfaces — i.e.
    another machine could drive this PC's devices. We probe the loopback first
    (cheap) and treat a running server on *port* as good enough to skip a restart
    when it was started with -a."""
    adb = find_adb(adb_path)
    try:
        out = subprocess.run([adb, "-P", str(port), "devices"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW)
        return out.returncode == 0 and "daemon not running" not in (out.stderr or "")
    except Exception:
        return False


def open_firewall(ports=(5037, 27184)) -> str:
    """Best-effort: open the given TCP ports in the Windows firewall so a remote
    machine can reach this PC's adb server (5037) AND scrcpy's video tunnel
    (27184). Needs admin rights; returns a status string (never raises)."""
    if os.name != "nt":
        return "firewall: not Windows, skipped"
    opened, failed = [], []
    for p in ports:
        rule = f"TurboADB TCP {p}"
        try:
            # remove any old rule, then add (idempotent)
            subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule",
                            f"name={rule}"], capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
            r = subprocess.run(
                ["netsh", "advfirewall", "firewall", "add", "rule", f"name={rule}",
                 "dir=in", "action=allow", "protocol=TCP", f"localport={p}"],
                capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
            (opened if r.returncode == 0 else failed).append(p)
        except Exception:
            failed.append(p)
    if opened and not failed:
        return f"firewall: opened TCP {', '.join(map(str, opened))}"
    if opened:
        return (f"firewall: opened {opened}; could NOT open {failed} "
                "(run as Administrator to allow those)")
    return ("firewall: could not open ports (run TurboADB/`turboadb serve` as "
            "Administrator, or open TCP 5037 + 27184 manually)")


def start_shared_server(port: int = 5037, adb_path: str | None = None,
                        *, restart: bool = True) -> str:
    """Start an adb server that listens on **all** network interfaces so other
    machines can drive this PC's devices via ``adb -H thispc -P {port}``.

    This is the automated equivalent of ``adb -a nodaemon server start`` — but it
    is launched **detached in the background**, so it keeps listening without
    blocking. A localhost-only server already running would stop ``-a`` from
    binding to ``0.0.0.0``, so by default we replace it first.

    Returns a short status string; raises on failure.
    """
    adb = find_adb(adb_path)
    if restart:
        # drop any localhost-only server so the new one can bind all interfaces
        subprocess.run([adb, "-P", str(port), "kill-server"],
                       capture_output=True, timeout=15, creationflags=NO_WINDOW)
    flags = NO_WINDOW
    extra = {}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survives, no console
        flags |= 0x00000008 | 0x00000200
    else:
        extra["start_new_session"] = True
    subprocess.Popen([adb, "-a", "-P", str(port), "nodaemon", "server", "start"],
                     creationflags=flags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **extra)
    # give it a moment, then confirm it answers
    last = ""
    for _ in range(10):
        time.sleep(0.4)
        try:
            out = subprocess.run([adb, "-P", str(port), "devices"],
                                 capture_output=True, text=True, timeout=10,
                                 creationflags=NO_WINDOW)
            if out.returncode == 0:
                n = len([d for d in (out.stdout or "").splitlines()
                         if _parse_line(d)])
                return (f"shared adb server is listening on 0.0.0.0:{port} "
                        f"({n} device(s) attached here)")
            last = (out.stderr or out.stdout or "").strip()
        except Exception as exc:
            last = str(exc)
    raise RuntimeError(f"adb server did not come up on port {port}: "
                       f"{last or 'no response'}")


def stop_shared_server(port: int = 5037, adb_path: str | None = None) -> str:
    """Stop the network-shared adb server and return to a normal local-only one:
    kill the ``-a`` (all-interfaces) server, then start a plain server that binds
    to localhost again, so this PC keeps working but no longer shares its devices.
    Best-effort; returns a short status string."""
    adb = find_adb(adb_path)
    subprocess.run([adb, "-P", str(port), "kill-server"],
                   capture_output=True, timeout=15, creationflags=NO_WINDOW)
    subprocess.run([adb, "start-server"],
                   capture_output=True, timeout=15, creationflags=NO_WINDOW)
    return "shared adb server stopped — back to local-only (localhost)"


def _startup_dir() -> str:
    """The current user's Windows Startup folder (programs run at login)."""
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def install_startup(port: int = 5037) -> str:
    """Make the shared adb server start automatically at every Windows login by
    dropping a tiny launcher in the Startup folder. Returns the file path.
    So it really never has to be done by hand again."""
    if os.name != "nt":
        raise RuntimeError("Startup install is only supported on Windows.")
    d = _startup_dir()
    os.makedirs(d, exist_ok=True)
    bat = os.path.join(d, "turboadb-shared-adb.bat")
    # pythonw -m turboadb serve, detached, no window
    line = (f'@echo off\r\n'
            f'start "" /b "{_pythonw()}" -m turboadb serve --port {port}\r\n')
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
    import sys
    exe = sys.executable or "python"
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


_SERVE_TASK = "TurboADBSharedADB"


def install_serve_task(port: int = 5037, *, run_now: bool = True) -> str:
    """Register a Scheduled Task that runs the shared adb server at SYSTEM
    **startup** — headless and persistent (survives logoff and needs no login,
    unlike the Startup-folder launcher). Optionally start it immediately via the
    scheduler, which detaches it from whatever session created it (e.g. a WinRM
    remote-deploy session). Returns the task name. Needs admin rights."""
    if os.name != "nt":
        raise RuntimeError("Scheduled-task install is Windows-only.")
    tr = f'"{_pythonw()}" -m turboadb serve --port {port}'
    subprocess.run(["schtasks", "/create", "/tn", _SERVE_TASK, "/tr", tr,
                    "/sc", "onstart", "/ru", "SYSTEM", "/rl", "highest", "/f"],
                   capture_output=True, timeout=30, creationflags=NO_WINDOW)
    if run_now:
        subprocess.run(["schtasks", "/run", "/tn", _SERVE_TASK],
                       capture_output=True, timeout=30, creationflags=NO_WINDOW)
    return _SERVE_TASK


def uninstall_serve_task() -> bool:
    """Remove the startup Scheduled Task if present."""
    if os.name != "nt":
        return False
    r = subprocess.run(["schtasks", "/delete", "/tn", _SERVE_TASK, "/f"],
                       capture_output=True, timeout=30, creationflags=NO_WINDOW)
    return r.returncode == 0
