"""Launch scrcpy for live screen mirroring + control — the "visual session"
(analogous to launching RDP for an SSH host). Non-blocking: returns a handle
wrapping the scrcpy subprocess."""

from __future__ import annotations

import os
import re
import socket
import subprocess
from typing import Optional

from .config import ScrcpyOptions
from .tools import find_scrcpy, NO_WINDOW
from .exceptions import ScrcpyError

# Fixed port for the scrcpy video tunnel to a REMOTE adb server, so it can be
# opened in that machine's firewall (otherwise scrcpy picks a random high port).
TUNNEL_PORT = 27184


def resolve_host(host: Optional[str]) -> Optional[str]:
    """Resolve a hostname to an IPv4 address. scrcpy's tunnel needs an actual IP
    (it won't resolve a name), so a remote adb server given by hostname must be
    resolved first — otherwise the mirror silently never connects. IP strings and
    failures pass through unchanged so plain IPs always work.

    Any accidental ``:port`` suffix is stripped first (the port is supplied
    separately) so we never produce a doubled ``host:port:port``."""
    if not host:
        return host
    host = host.strip()
    # drop ANY number of accidental ":port" suffixes ("ip:5037:5037" -> "ip")
    while ":" in host:
        h, _, p = host.rpartition(":")
        if h and p.isdigit():
            host = h
        else:
            break
    try:
        socket.inet_aton(host)               # already a dotted IPv4 — keep as-is
        return host
    except OSError:
        pass
    try:
        return socket.gethostbyname(host)    # DNS / hosts-file lookup
    except OSError:
        return host                          # let adb try; we did our best


def is_local_host(host: Optional[str]) -> bool:
    """True if *host* refers to THIS machine (localhost, 127.0.0.1, or one of this
    machine's own IPs/hostname). When the "remote" adb server is actually local —
    e.g. TurboADB is running on the same PC the device is plugged into, just
    addressed by its LAN IP — scrcpy must run LOCALLY (no network video tunnel),
    which is exactly how running scrcpy directly there works."""
    if not host:
        return True
    h = resolve_host(host)                       # strip :port, resolve name → IP
    if h in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    try:
        local = set()
        name = socket.gethostname()
        for n in (name, socket.getfqdn()):
            try:
                for info in socket.getaddrinfo(n, None):
                    local.add(info[4][0])
            except OSError:
                pass
        return h in local
    except Exception:
        return False


def is_remote_session() -> bool:
    """True when running inside a Windows Remote Desktop session, where scrcpy's
    default GPU renderer (Direct3D/OpenGL) usually fails — software rendering is
    needed instead."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))  # SM_REMOTESESSION
    except Exception:
        return False


def _server_env(host: Optional[str], port: int, adb_path: Optional[str] = None):
    """Environment for the scrcpy subprocess:

    * ``ADB`` — pin scrcpy to the SAME adb binary TurboADB uses. Otherwise scrcpy
      falls back to its own bundled adb (or PATH adb) of a different version; the
      two servers then fight ("version doesn't match, killing…") and scrcpy dies
      with *"adb start-server exited unexpectedly / server connection failed"* —
      the exact error seen over Remote Desktop.
    * ``ANDROID_ADB_SERVER_HOST/PORT`` — point scrcpy at a remote adb server.
    """
    env = None
    if adb_path:
        env = os.environ.copy()
        env["ADB"] = adb_path
    if host:
        ip = resolve_host(host)                      # clean IP, no :port suffix
        env = env or os.environ.copy()
        # Point scrcpy's adb at the remote server. VERIFIED against adb v37:
        # ANDROID_ADB_SERVER_ADDRESS must be the **bare host** (just the IP) —
        # adb wraps it ITSELF into "tcp:<addr>:<port>". Passing "tcp:ip:port"
        # gets double-wrapped into the infamous "tcp:tcp:ip:port:port" / "no
        # host" error. HOST+PORT alone are ignored (adb falls back to the LOCAL
        # server → "no adb device found"). So: bare IP in ADDRESS + the port.
        env["ANDROID_ADB_SERVER_ADDRESS"] = ip
        env["ANDROID_ADB_SERVER_HOST"] = ip
        env["ANDROID_ADB_SERVER_PORT"] = str(port)
    return env


def list_displays(serial: Optional[str] = None, *,
                  scrcpy_path: Optional[str] = None, timeout: float = 25.0,
                  adb_server_host: Optional[str] = None,
                  adb_server_port: int = 5037,
                  adb_path: Optional[str] = None) -> list:
    """Enumerate the device's displays via ``scrcpy --list-displays`` — essential
    on Android Automotive / IVI head units, which expose several displays
    (center stack, cluster, passenger). Returns ``[{"id": int, "size": str}, …]``.

    Raises :class:`ScrcpyError` if scrcpy can't reach the device.
    """
    exe = find_scrcpy(scrcpy_path)
    cmd = [exe]
    if serial:
        cmd += ["--serial", serial]
    if adb_server_host:
        cmd += ["--tunnel-host", resolve_host(adb_server_host)]
    cmd += ["--list-displays"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             creationflags=NO_WINDOW,
                             env=_server_env(adb_server_host, adb_server_port,
                                             adb_path))
    except subprocess.TimeoutExpired as exc:
        raise ScrcpyError("scrcpy --list-displays timed out") from exc
    text = (out.stdout or "") + "\n" + (out.stderr or "")
    displays = []
    seen = set()
    for m in re.finditer(r"--display(?:-id)?[= ](\d+)\s*(?:\(([^)]*)\))?", text):
        did = int(m.group(1))
        if did not in seen:
            seen.add(did)
            displays.append({"id": did, "size": (m.group(2) or "").strip()})
    if not displays and out.returncode != 0:
        raise ScrcpyError("scrcpy could not list displays: "
                          f"{text.strip()[:400] or 'no device reachable'}")
    return displays


class ScrcpySession:
    """A running scrcpy process. Call :meth:`stop` to close the mirror window."""

    def __init__(self, proc: subprocess.Popen, serial: Optional[str],
                 log_path: Optional[str] = None, logfh=None):
        self._proc = proc
        self.serial = serial
        self.log_path = log_path
        self._logfh = logfh

    def read_log(self) -> str:
        """Return scrcpy's captured stdout/stderr (for diagnosing a failed start)."""
        if not self.log_path:
            return ""
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except Exception:
            return ""

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def running(self) -> bool:
        return self._proc.poll() is None

    def wait(self, timeout: Optional[float] = None) -> int:
        return self._proc.wait(timeout=timeout)

    def stop(self) -> None:
        try:
            if self.running:
                self._proc.terminate()
        except Exception:
            pass
        try:
            if self._logfh:
                self._logfh.close()
        except Exception:
            pass

    def __enter__(self) -> "ScrcpySession":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def __repr__(self) -> str:
        state = "running" if self.running else "stopped"
        return f"<ScrcpySession serial={self.serial!r} pid={self.pid} {state}>"


def launch_scrcpy(serial: Optional[str] = None,
                  options: Optional[ScrcpyOptions] = None, *,
                  scrcpy_path: Optional[str] = None,
                  adb_server_host: Optional[str] = None,
                  adb_server_port: int = 5037,
                  log_path: Optional[str] = None,
                  adb_path: Optional[str] = None) -> ScrcpySession:
    """
    Start scrcpy for *serial* (or the only device) with *options*. Returns a
    :class:`ScrcpySession` immediately; the mirror runs in its own window.

    With *adb_server_host* set, scrcpy is pointed at that remote adb server (so
    you can mirror a device plugged into another machine).

    Raises :class:`ADBNotFoundError` if scrcpy is missing, or
    :class:`ScrcpyError` if it cannot be launched.
    """
    exe = find_scrcpy(scrcpy_path)
    opts = options or ScrcpyOptions()
    cmd = [exe]
    if serial:
        cmd += ["--serial", serial]
    if adb_server_host:
        # Remote adb server: scrcpy must tunnel the VIDEO socket across the
        # network. We force a FORWARD tunnel to a FIXED, known port on the remote
        # machine (--tunnel-host + --tunnel-port) instead of a random one, so it
        # can be opened in that machine's firewall. (Resolve a hostname → IP;
        # scrcpy's tunnel won't resolve names itself.)
        cmd += [f"--tunnel-host={resolve_host(adb_server_host)}",
                f"--tunnel-port={TUNNEL_PORT}"]
    cmd += opts.to_args()

    env = _server_env(adb_server_host, adb_server_port, adb_path)
    if (opts.render_driver or "").lower() == "software":
        # belt-and-braces: also force SDL software rendering via env, so it holds
        # even if this scrcpy build ignores --render-driver
        env = env or os.environ.copy()
        env["SDL_RENDER_DRIVER"] = "software"
        env["SDL_FRAMEBUFFER_ACCELERATION"] = "0"

    logfh = None
    if log_path:
        try:
            logfh = open(log_path, "w", encoding="utf-8", errors="replace")
            # record EXACTLY what we run, so a failure is fully diagnosable
            logfh.write("TurboADB launched scrcpy as:\n  " + " ".join(cmd) + "\n")
            if env and env.get("ANDROID_ADB_SERVER_ADDRESS"):
                logfh.write("  adb server = tcp:{}:{}\n".format(
                    env["ANDROID_ADB_SERVER_ADDRESS"],
                    env.get("ANDROID_ADB_SERVER_PORT", "5037")))
            logfh.write("-" * 60 + "\n")
            logfh.flush()
        except Exception:
            logfh = None
    try:
        proc = subprocess.Popen(
            cmd, creationflags=NO_WINDOW, env=env,
            stdout=(logfh or None),
            stderr=(subprocess.STDOUT if logfh else None))
    except Exception as exc:  # pragma: no cover
        raise ScrcpyError(f"Failed to launch scrcpy: {exc}") from exc
    return ScrcpySession(proc, serial, log_path=log_path, logfh=logfh)
