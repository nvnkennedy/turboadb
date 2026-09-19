"""Launch scrcpy for live screen mirroring + control — the "visual session"
(analogous to launching RDP for an SSH host). Non-blocking: returns a handle
wrapping the scrcpy subprocess."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
from typing import Optional

from .config import ScrcpyOptions, parse_host_port
from .tools import find_scrcpy, NO_WINDOW, DEFAULT_ADB_SERVER_PORT
from .exceptions import ScrcpyError

# A bounded port range for video tunnels to a REMOTE adb server. A single forced
# port made concurrent device tabs race for 27184; scrcpy can safely choose the
# first available port from this firewall-friendly range.
TUNNEL_PORT = 27184
TUNNEL_PORT_LAST = 27199
# scrcpy's own ``--port=FIRST:LAST`` syntax
TUNNEL_PORT_RANGE = f"{TUNNEL_PORT}:{TUNNEL_PORT_LAST}"
# the same range as written for firewall rules / user messages ("FIRST-LAST")
TUNNEL_PORT_FIREWALL_RANGE = f"{TUNNEL_PORT}-{TUNNEL_PORT_LAST}"


def resolve_host(host: Optional[str]) -> Optional[str]:
    """Resolve a hostname to an IPv4 address. scrcpy's tunnel needs an actual IP
    (it won't resolve a name), so a remote adb server given by hostname must be
    resolved first — otherwise the mirror silently never connects. IP strings and
    failures pass through unchanged so plain IPs always work.

    Any accidental ``:port`` suffix is stripped first (the port is supplied
    separately) so we never produce a doubled ``host:port:port``."""
    if not host:
        return host
    host = parse_host_port(host)[0]
    if not host:
        return host
    try:
        socket.inet_aton(host)  # already a dotted IPv4 — keep as-is
        return host
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)  # already an IPv6 — keep as-is
        return host
    except (OSError, AttributeError):
        pass
    try:
        return socket.gethostbyname(host)  # DNS / hosts-file lookup
    except OSError:
        return host  # let adb try; we did our best


def is_local_host(host: Optional[str]) -> bool:
    """True if *host* refers to THIS machine (localhost, 127.0.0.1, or one of this
    machine's own IPs/hostname). When the "remote" adb server is actually local —
    e.g. TurboADB is running on the same PC the device is plugged into, just
    addressed by its LAN IP — scrcpy must run LOCALLY (no network video tunnel),
    which is exactly how running scrcpy directly there works."""
    if not host:
        return True
    h = resolve_host(host)  # strip :port, resolve name → IP
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


def _remote_server_host(host: Optional[str]) -> Optional[str]:
    """*host* when it names ANOTHER machine's adb server, else None — resolved
    once per call so the tunnel flag and the ``ANDROID_ADB_SERVER_*`` environment
    always agree (a "remote" host that is really this PC must use the local
    server, not its LAN address)."""
    if host and not is_local_host(host):
        return host
    return None


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
    * ``ANDROID_ADB_SERVER_HOST/PORT`` — point scrcpy at a remote adb server
      (or, without a host, at a local server on a non-default port).
    """
    env = None
    if adb_path:
        env = os.environ.copy()
        env["ADB"] = adb_path
    if not host and port and int(port) != DEFAULT_ADB_SERVER_PORT:
        env = env or os.environ.copy()
        env["ANDROID_ADB_SERVER_PORT"] = str(port)
    if host:
        ip = resolve_host(host)  # clean IP, no :port suffix
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


def list_displays(
    serial: Optional[str] = None,
    *,
    scrcpy_path: Optional[str] = None,
    timeout: float = 25.0,
    adb_server_host: Optional[str] = None,
    adb_server_port: int = 5037,
    adb_path: Optional[str] = None,
) -> list:
    """Enumerate the device's displays via ``scrcpy --list-displays`` — essential
    on Android Automotive / IVI head units, which expose several displays
    (center stack, cluster, passenger). Returns ``[{"id": int, "size": str}, …]``.

    Raises :class:`ScrcpyError` if scrcpy can't reach the device.
    """
    exe = find_scrcpy(scrcpy_path)
    remote = _remote_server_host(adb_server_host)
    cmd = [exe]
    if serial:
        cmd += ["--serial", serial]
    if remote:
        cmd += ["--tunnel-host", resolve_host(remote)]
    cmd += ["--list-displays"]
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=NO_WINDOW,
            env=_server_env(remote, adb_server_port, adb_path),
        )
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
        raise ScrcpyError(
            f"scrcpy could not list displays: {text.strip()[:400] or 'no device reachable'}"
        )
    return displays


def _post_close_to_pid(pid: int) -> bool:
    """Find windows owned by *pid* and post WM_CLOSE so SDL/scrcpy exits cleanly."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        found = []

        def visit(hwnd, _lparam):
            p = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid:
                found.append(hwnd)
                u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return True

        u.EnumWindows(WNDENUMPROC(visit), 0)
        return bool(found)
    except Exception:
        return False


# Raises Ctrl+Break on another process's console from a short-lived helper, for
# a caller that already owns a console (AttachConsole refuses a second one).
_CTRL_BREAK_HELPER = (
    "import ctypes,sys\n"
    "k=ctypes.windll.kernel32\n"
    "p=int(sys.argv[1])\n"
    "k.FreeConsole()\n"
    "sys.exit(0 if k.AttachConsole(p) and k.GenerateConsoleCtrlEvent(1,p) else 1)\n"
)
_ERROR_ACCESS_DENIED = 5


def _send_ctrl_break(pid: int) -> bool:
    """Deliver CTRL_BREAK_EVENT to *pid*'s process group on its own console.

    scrcpy handles Ctrl+Break like a closed window: it quits cleanly and writes
    a recording's MP4 index.  ``Popen.send_signal`` cannot deliver it, because
    GenerateConsoleCtrlEvent only reaches processes on the caller's console and
    scrcpy runs on its own hidden console (CREATE_NO_WINDOW).  A windowless
    (``--no-window``) recorder was therefore always terminated, leaving an MP4
    without its index.  Attach to scrcpy's console just long enough to raise the
    event; the caller is not in scrcpy's process group, so it never receives it.
    """
    if os.name != "nt" or not pid:
        return False
    try:
        import ctypes
        import sys
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.AttachConsole.restype = wintypes.BOOL
        k.AttachConsole.argtypes = [wintypes.DWORD]
        k.FreeConsole.restype = wintypes.BOOL
        k.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
        k.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.GetStdHandle.restype = wintypes.HANDLE
        k.GetStdHandle.argtypes = [wintypes.DWORD]
        k.SetStdHandle.restype = wintypes.BOOL
        k.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
        std_ids = (0xFFFFFFF6, 0xFFFFFFF5, 0xFFFFFFF4)  # STD_INPUT/OUTPUT/ERROR_HANDLE
        saved = [k.GetStdHandle(n) for n in std_ids]
        if k.AttachConsole(pid):
            try:
                return bool(k.GenerateConsoleCtrlEvent(1, pid))  # CTRL_BREAK_EVENT
            finally:
                k.FreeConsole()
                # AttachConsole may have replaced empty standard handles with
                # console handles that FreeConsole just invalidated.
                for n, handle in zip(std_ids, saved):
                    k.SetStdHandle(n, handle)
        if ctypes.get_last_error() != _ERROR_ACCESS_DENIED:
            return False  # the process is gone, or has no console
        if getattr(sys, "frozen", False) or not sys.executable:
            return False
        done = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _CTRL_BREAK_HELPER, str(int(pid))],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
            timeout=10,
        )
        return done.returncode == 0
    except Exception:
        return False


# Every scrcpy this process started and has not stopped yet. A mirror window
# outliving TurboADB keeps a device busy (and its adb connection open), so the
# app stops the survivors on the way out — see live_sessions()/stop_all().
_LIVE_SESSIONS: "list" = []
_LIVE_LOCK = threading.Lock()


def live_sessions() -> list:
    """The scrcpy sessions this process started that are still running."""
    with _LIVE_LOCK:
        sessions = list(_LIVE_SESSIONS)
    alive = [s for s in sessions if s.running]
    if len(alive) != len(sessions):
        with _LIVE_LOCK:
            _LIVE_SESSIONS[:] = [s for s in _LIVE_SESSIONS if s.running]
    return alive


def stop_all(timeout: float = 5.0) -> int:
    """Stop every scrcpy still running from this process; returns how many.

    Called when TurboADB closes, so no mirror window (and no adb connection
    behind it) is left behind — a scrcpy that was never stopped keeps running
    after the app exits."""
    stopped = 0
    for session in live_sessions():
        try:
            session.stop(timeout=timeout)
            stopped += 1
        except Exception:
            pass
    return stopped


class ScrcpySession:
    """A running scrcpy process. Call :meth:`stop` to close the mirror window."""

    def __init__(
        self,
        proc: subprocess.Popen,
        serial: Optional[str],
        log_path: Optional[str] = None,
        logfh=None,
    ):
        self._proc = proc
        self.serial = serial
        self.log_path = log_path
        self._logfh = logfh
        with _LIVE_LOCK:
            # drop the ones that have since exited, so a long session that
            # starts many screens does not hold on to every finished process
            _LIVE_SESSIONS[:] = [s for s in _LIVE_SESSIONS if s.running]
            _LIVE_SESSIONS.append(self)

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

    def stop(self, timeout: float = 5.0) -> None:
        """Ask scrcpy to quit cleanly, as closing its window does, and wait up to
        *timeout* seconds (a recording's MP4 index is written then) before
        ending the process."""
        try:
            if self.running:
                if os.name == "nt":
                    _post_close_to_pid(self._proc.pid)
                    # a windowless session (--no-window) has nothing to close
                    _send_ctrl_break(self._proc.pid)
                    try:
                        self._proc.wait(timeout=timeout)
                    except Exception:
                        pass
                else:
                    try:
                        self._proc.terminate()
                        self._proc.wait(timeout=timeout)
                    except Exception:
                        pass
                if self.running:
                    self._proc.terminate()
            try:
                self._proc.wait(timeout=3.0)
            except Exception:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=1.0)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            if self._logfh:
                self._logfh.close()
        except Exception:
            pass
        with _LIVE_LOCK:
            if self in _LIVE_SESSIONS:
                _LIVE_SESSIONS.remove(self)

    def __enter__(self) -> "ScrcpySession":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def __repr__(self) -> str:
        state = "running" if self.running else "stopped"
        return f"<ScrcpySession serial={self.serial!r} pid={self.pid} {state}>"


def launch_scrcpy(
    serial: Optional[str] = None,
    options: Optional[ScrcpyOptions] = None,
    *,
    scrcpy_path: Optional[str] = None,
    adb_server_host: Optional[str] = None,
    adb_server_port: int = 5037,
    log_path: Optional[str] = None,
    adb_path: Optional[str] = None,
) -> ScrcpySession:
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
    remote = _remote_server_host(adb_server_host)
    cmd = [exe]
    if serial:
        cmd += ["--serial", serial]
    if remote:
        # Remote adb server: scrcpy must tunnel the VIDEO socket across the
        # network. Keep selection inside a known firewall range, while allowing
        # simultaneous tabs to choose different ports instead of colliding.
        cmd += [
            f"--tunnel-host={resolve_host(remote)}",
            f"--port={TUNNEL_PORT_RANGE}",
        ]
    cmd += opts.to_args()

    env = _server_env(remote, adb_server_port, adb_path)
    if (opts.render_driver or "").lower() == "software":
        # belt-and-braces: also force SDL software rendering via env, so it holds
        # even if this scrcpy build ignores --render-driver
        env = env or os.environ.copy()
        env["SDL_RENDER_DRIVER"] = "software"
        env["SDL_FRAMEBUFFER_ACCELERATION"] = "0"
        # linear filtering when SDL scales the frame to the window — the default
        # nearest-neighbour is what made the software-rendered (RDP) mirror look
        # jagged/blocky whenever the window size didn't match the video size
        env["SDL_RENDER_SCALE_QUALITY"] = "1"

    logfh = None
    if log_path:
        try:
            logfh = open(log_path, "w", encoding="utf-8", errors="replace")
            # record EXACTLY what we run, so a failure is fully diagnosable
            logfh.write("TurboADB launched scrcpy as:\n  " + " ".join(cmd) + "\n")
            if env and env.get("ANDROID_ADB_SERVER_ADDRESS"):
                logfh.write(
                    "  adb server = tcp:{}:{}\n".format(
                        env["ANDROID_ADB_SERVER_ADDRESS"],
                        env.get("ANDROID_ADB_SERVER_PORT", "5037"),
                    )
                )
            logfh.write("-" * 60 + "\n")
            logfh.flush()
        except Exception:
            logfh = None
    flags = NO_WINDOW
    if os.name == "nt":
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            cmd,
            creationflags=flags,
            env=env,
            stdout=(logfh or None),
            stderr=(subprocess.STDOUT if logfh else None),
        )

    except Exception as exc:  # pragma: no cover
        raise ScrcpyError(f"Failed to launch scrcpy: {exc}") from exc
    return ScrcpySession(proc, serial, log_path=log_path, logfh=logfh)
