"""
Locate the ``adb`` and ``scrcpy`` executables robustly, with clear, actionable
errors when they are missing.

Search order (first hit wins):
  1. an explicit path argument
  2. the ``TURBOADB_ADB`` / ``TURBOADB_SCRCPY`` environment variables
  3. the GUI's ``adb_path`` / ``scrcpy_path`` setting (``~/.turboadb/settings.json``)
  4. TurboADB's managed download cache (``~/.turboadb/tools``)
  5. binaries bundled inside this package (``turboadb/bin/...``), if present
  6. the system ``PATH``
  7. common Android SDK / install locations

An explicit path or an environment variable that names no executable raises
:class:`ADBNotFoundError` instead of falling through to the others: a different
adb would restart the server the chosen one started.  The Settings path is a
preference, so a stale one is skipped with a warning.

Resolution never modifies ``os.environ``; code that launches a child process
which must use the same adb (scrcpy, a local terminal) passes it explicitly.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager

from .config import user_path
from .exceptions import ADBNotFoundError

ADB_DOWNLOAD = "https://developer.android.com/tools/releases/platform-tools"
SCRCPY_DOWNLOAD = "https://github.com/Genymobile/scrcpy"
DEFAULT_ADB_SERVER_PORT = 5037

_log = logging.getLogger(__name__)

# Hide the console window when spawning adb from a windowed (GUI/frozen) app so
# the user never sees a black box flash. No-op on non-Windows.
if os.name == "nt":
    NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW
    # A process that outlives its starter and its console: an adb server.
    DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
else:
    NO_WINDOW = 0
    DETACHED = 0


def parse_version(v: str) -> tuple[int, ...]:
    """A lenient numeric version tuple ('35.0.2-12147458' -> (35, 0, 2))."""
    out = []
    for part in str(v).split(".")[:4]:
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def managed_tools_dir() -> str:
    """Path of the managed download cache (``~/.turboadb/tools``). Never creates it.

    Resolved through :func:`turboadb.config.user_dir`, the single per-user state
    directory resolver, so tools and settings can never come from two different
    home directories in one process."""
    return user_path("tools")


def windowless_python() -> str:
    """The interpreter to launch background/GUI Python processes with:
    ``pythonw.exe`` beside the running interpreter when it exists (no console
    window on Windows), otherwise :data:`sys.executable`."""
    import sys

    exe = sys.executable or "python"
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.name == "nt" and os.path.exists(cand) else exe


def _recv_exact(sock, size: int, *, retry_timeouts: bool = False) -> bytes | None:
    """Read exactly *size* bytes from an ADB protocol socket, or return None.

    TCP may split the four-byte ADB status and length fields across ``recv``
    calls.  Treating a partial field as a failed request causes unnecessary
    slow CLI fallbacks and can look like a transient device disconnect.
    """
    data = bytearray()
    while len(data) < size:
        try:
            chunk = sock.recv(size - len(data))
        except (socket.timeout, TimeoutError):
            # socket.timeout only became an alias of TimeoutError in 3.10.
            if retry_timeouts:
                continue
            raise
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


def _bundled_candidates(name: str) -> list:
    """Paths where an offline-bundled binary might live inside the package."""
    here = os.path.dirname(os.path.abspath(__file__))
    binroot = os.path.join(here, "bin")
    exe = _exe(name)
    return [
        os.path.join(binroot, exe),
        os.path.join(binroot, "platform-tools", exe),
        os.path.join(binroot, "scrcpy", exe),
        os.path.join(binroot, name, exe),
    ]


def _managed_candidates(name: str) -> list:
    """Paths in the on-demand download cache (~/.turboadb/tools)."""
    cache = managed_tools_dir()
    exe = _exe(name)
    if name == "adb":
        return [
            os.path.join(cache, "platform-tools", exe),
            os.path.join(cache, "scrcpy", exe),
        ]  # scrcpy bundles adb on Win
    return [os.path.join(cache, "scrcpy", exe)]


def _sdk_candidates(name: str) -> list:
    """Common Android SDK / package-manager install locations."""
    exe = _exe(name)
    home = os.path.expanduser("~")
    paths = []
    if name == "adb":
        roots = [
            os.environ.get("ANDROID_HOME", ""),
            os.environ.get("ANDROID_SDK_ROOT", ""),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"),
            os.path.join(home, "Android", "Sdk"),
            os.path.join(home, "Library", "Android", "sdk"),
            "/usr/lib/android-sdk",
        ]
        for r in roots:
            if r:
                paths.append(os.path.join(r, "platform-tools", exe))
        paths += [
            os.path.join(home, "platform-tools", exe),
            "/usr/local/bin/" + exe,
            "/usr/bin/" + exe,
            "/opt/homebrew/bin/" + exe,
        ]
    else:  # scrcpy
        paths += [
            os.path.join(home, "scrcpy", exe),
            "/usr/local/bin/" + exe,
            "/usr/bin/" + exe,
            "/opt/homebrew/bin/" + exe,
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "scrcpy", exe),
        ]
    return paths


_CACHE: dict = {}
# Keyed on (name, explicit, env, setting): a process that builds many
# handlers with different adb paths would otherwise grow it forever.
_CACHE_MAX = 64


def clear_tools_cache() -> None:
    """Clear cached resolution paths for adb and scrcpy."""
    _CACHE.clear()


def _gui_setting(name: str) -> str | None:
    """The GUI's configured tool path, if the settings module is importable."""
    try:
        from .gui import settings as settings_mod  # stdlib-only module

        key = "adb_path" if name == "adb" else "scrcpy_path"
        return (settings_mod.get(key) or "").strip() or None
    except Exception:
        return None


def _path_candidate(name: str, value: str) -> str | None:
    """A file path, a directory containing the exe, or a bare name on PATH."""
    p = os.path.expanduser(value)
    if os.path.isfile(p):
        return p
    cand = os.path.join(p, _exe(name))
    if os.path.isfile(cand):
        return cand
    return shutil.which(value)


def _same_path(a: str, b: str) -> bool:
    def norm(value):
        return os.path.normcase(os.path.abspath(os.path.expanduser(value.strip())))

    try:
        return norm(a) == norm(b)
    except (TypeError, ValueError):
        return False


def _is_setting(name: str, value: str) -> bool:
    """True when *value* is the GUI's Settings path for *name*.  The GUI hands
    that path on as an explicit one, yet it stays a preference."""
    setting = _gui_setting(name)
    return bool(setting) and _same_path(setting, value)


def _not_found_at(name: str, value: str, env_var: str | None) -> ADBNotFoundError:
    """The error for a path that was set on purpose but names no executable."""
    where = f"{env_var}={value}" if env_var else f"The {name} path given ({value})"
    why = (
        "A different adb would restart the adb server the chosen one started, "
        "so TurboADB does not fall back to another one."
        if name == "adb" else
        f"TurboADB does not fall back to another {name} for a path set on purpose."
    )
    return ADBNotFoundError(
        f"{where} is not {name} (an executable, or a folder with one in it).\n"
        f"  {why}\n"
        f"  Correct it, or leave it out to let TurboADB find {name}.",
        configured=value,
    )


def _discover(name: str) -> str | None:
    """The managed, bundled, PATH and SDK copies of *name*, in that order."""
    for cand in _managed_candidates(name) + _bundled_candidates(name):
        if os.path.isfile(cand):
            return cand
    which = shutil.which(name)
    if which:
        return which
    for cand in _sdk_candidates(name):
        if cand and os.path.isfile(cand):
            return cand
    return None


def _stale_setting_text(name: str, value: str, found: str | None) -> str:
    return (
        f"The {name} path in Settings ({value}) does not exist; "
        + (f"using {found} instead" if found else f"no other {name} was found either")
        + ". Correct or clear it in Settings → Tools."
    )


def stale_setting_note(name: str = "adb") -> str | None:
    """The warning :func:`find_adb` / :func:`find_scrcpy` log when the Settings
    path of *name* names no executable and they use another copy, or None when
    that path is fine, empty or not consulted (``TURBOADB_ADB`` /
    ``TURBOADB_SCRCPY`` wins).  The GUI shows it in its log panel: the log
    file alone was easy to miss."""
    env_var = "TURBOADB_ADB" if name == "adb" else "TURBOADB_SCRCPY"
    if (os.environ.get(env_var) or "").strip():
        return None
    setting = _gui_setting(name)
    if not setting or _path_candidate(name, setting):
        return None
    return _stale_setting_text(name, setting, _discover(name))


def _search(name: str, explicit, env, setting, env_var: str | None = None) -> str | None:
    """The first configured value decides: *explicit*, else *env*, else the
    Settings path.  An explicit or environment path that names no executable
    raises; a stale Settings path is logged and the other copies are searched."""
    if explicit:
        value, source = explicit, "explicit"
    elif env:
        value, source = env, "env"
    else:
        value, source = setting, "setting"
    if value:
        hit = _path_candidate(name, value)
        if hit:
            return hit
        if source == "explicit" and _is_setting(name, value):
            source = "setting"
        if source != "setting":
            raise _not_found_at(name, value, env_var if source == "env" else None)
    found = _discover(name)
    if value:
        _log.warning("%s", _stale_setting_text(name, value, found))
    return found


def _resolve(name: str, explicit, env_var: str) -> str | None:
    env = (os.environ.get(env_var) or "").strip() or None
    # The GUI setting only matters when nothing more specific was given, so it
    # is read (from the settings module's in-memory cache) only in that case.
    setting = None if (explicit or env) else _gui_setting(name)
    # Every input that influences the answer is part of the key: changing the
    # env var or the GUI setting must not return a stale cached path.
    cache_key = (name, explicit, env, setting)
    cached = _CACHE.get(cache_key)
    if cached and os.path.isfile(cached):
        return cached
    found = _search(name, explicit, env, setting, env_var)
    if found:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[cache_key] = found
    return found


def find_adb(explicit: str | None = None) -> str:
    """Return an absolute path to ``adb`` or raise a guided ADBNotFoundError.

    Precedence: *explicit* > ``TURBOADB_ADB`` > GUI setting > managed/bundled
    tools > ``PATH`` > SDK locations. Does not modify the process environment.
    """
    path = _resolve("adb", explicit, "TURBOADB_ADB")
    if path:
        return path
    raise ADBNotFoundError(
        "Could not find 'adb' (Android Platform-Tools).\n"
        "  • Let TurboADB fetch it for you:  turboadb fetch-tools\n"
        "    (or in Python:  from turboadb import fetch_tools; fetch_tools())\n"
        f"  • Or install it yourself: {ADB_DOWNLOAD}\n"
        "    then add platform-tools to your PATH, or pass adb_path=... in\n"
        "    ADBConfig (or set the TURBOADB_ADB env var)."
    )


def find_scrcpy(explicit: str | None = None) -> str:
    """Return an absolute path to ``scrcpy`` or raise a guided ADBNotFoundError."""
    path = _resolve("scrcpy", explicit, "TURBOADB_SCRCPY")
    if path:
        return path
    raise ADBNotFoundError(
        "Could not find 'scrcpy' (screen mirroring).\n"
        "  • Let TurboADB fetch it (Windows):  turboadb fetch-tools\n"
        f"  • Or install it: {SCRCPY_DOWNLOAD}\n"
        "    (Windows: winget install scrcpy  •  macOS: brew install scrcpy\n"
        "     Linux: apt install scrcpy)\n"
        "  • Or pass scrcpy_path=... (or set the TURBOADB_SCRCPY env var)."
    )


def adb_available(explicit: str | None = None) -> bool:
    try:
        find_adb(explicit)
        return True
    except ADBNotFoundError:
        return False


def scrcpy_available(explicit: str | None = None) -> bool:
    try:
        find_scrcpy(explicit)
        return True
    except ADBNotFoundError:
        return False


def _adb_version_output(exe: str, timeout: float = 15.0) -> str:
    """Raw ``adb version`` output (stdout, else stderr). May raise OSError/TimeoutExpired."""
    out = subprocess.run(
        [exe, "version"], capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW
    )
    return out.stdout or out.stderr or ""


def adb_version(explicit: str | None = None) -> str:
    """Return the `adb version` banner (first line), or raise ADBNotFoundError."""
    adb = find_adb(explicit)
    try:
        return _adb_version_output(adb).strip().splitlines()[0]
    except Exception as exc:  # pragma: no cover
        return f"adb at {adb} (version query failed: {exc})"


# --------------------------------------------------------------------------- #
# The local adb server: one starter, one stopper
# --------------------------------------------------------------------------- #
# Held while a launcher is chosen or the server is stopped.  Re-entrant, so a
# caller can hold it across a stop and a start of its own (a shared server).
_SERVER_LOCK = threading.RLock()
_LAST_ADB_SERVER_ERROR = ""
# The `adb start-server` launcher still waiting for its daemon, by port.  Every
# caller waits on that one: at a cold start the GUI pre-warm, the main window's
# startup check and the first device tab each used to launch their own.
_INFLIGHT: dict = {}
# Ports whose running server a launcher of THIS process started, and the adb it
# ran.  Only such a server is stopped when the GUI exits; stopping forgets it.
_STARTED_HERE: dict = {}
# A launcher whose server answered, by port, until it says whether that server
# is the one it started.  Two programs that both find no server start one each,
# and the loser's launcher then sees the winner's server answer: it counted as
# TurboADB's and was stopped on exit, although another program had started it.
_UNCONFIRMED: dict = {}
# What adb's launcher prints once the daemon it started is up (the loser of a
# start race prints "failed to start daemon" instead).
_DAEMON_STARTED = "daemon started successfully"
# A launcher still running after this long is stuck: the next caller ends it.
_LAUNCH_MAX_AGE = 30.0
# `adb start-server` can exit before its daemon has bound the port.
_LAUNCH_EXIT_GRACE = 0.75
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
# The deadline (monotonic) this thread gives the server lock, if any: see
# server_lock_patience.
_PATIENCE = threading.local()


class ServerLockBusy(TimeoutError):
    """Another thread kept the local adb server busy (a restart, a share, a
    tab's disconnect) past the patience of this one (see
    :func:`server_lock_patience`)."""


@contextmanager
def server_lock_patience(seconds: float):
    """Within the block, this thread waits at most *seconds* in all for the
    local adb server: for the lock another thread holds while it starts or
    stops the server, and for a ``kill-server`` this thread runs itself.  A
    call that would wait longer raises :class:`ServerLockBusy`.

    For the way out of the GUI: the lock is held across blocking adb calls (a
    kill waits up to 20 s, a shared-server start 10 s more, a tab's disconnect
    about 10 s), and the exit used to sit behind them with the app already
    closed.  Other threads keep waiting as long as it takes: skipping the lock
    would let a start and a stop overlap again."""
    outer = getattr(_PATIENCE, "deadline", None)
    deadline = time.monotonic() + max(0.0, float(seconds))
    if outer is not None:
        deadline = min(deadline, outer)
    _PATIENCE.deadline = deadline
    try:
        yield
    finally:
        _PATIENCE.deadline = outer


def _patience_left() -> float | None:
    """Seconds this thread still waits for the adb server, or None (no limit)."""
    deadline = getattr(_PATIENCE, "deadline", None)
    return None if deadline is None else max(0.0, deadline - time.monotonic())


@contextmanager
def _server_locked():
    """Hold the server lock; within :func:`server_lock_patience` wait for it
    no longer than the patience lasts."""
    left = _patience_left()
    if left is None:
        _SERVER_LOCK.acquire()
    elif not (_SERVER_LOCK.acquire(timeout=left) if left > 0
              else _SERVER_LOCK.acquire(blocking=False)):
        raise ServerLockBusy(
            "another task is still starting or stopping the adb server "
            "(a restart, a share or a tab's disconnect)")
    try:
        yield
    finally:
        _SERVER_LOCK.release()


def local_adb_port(port: int | None = None) -> int:
    """The port of the local adb server a plain ``adb`` command talks to.

    ``None`` and 5037 mean adb's default port, which ``ANDROID_ADB_SERVER_PORT``
    moves for every adb command run without ``-P``.  TurboADB's commands omit
    ``-P`` for the default port, so its socket probes, the device tracker and
    the server start follow the variable too; they used to watch 5037 while
    every command went to the other server.  Any other port is returned as is.
    """
    if port is not None and int(port) != DEFAULT_ADB_SERVER_PORT:
        return int(port)
    return _env_adb_port() or DEFAULT_ADB_SERVER_PORT


def _env_adb_port() -> int | None:
    raw = (os.environ.get("ANDROID_ADB_SERVER_PORT") or "").strip()
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


def adb_server_env_note() -> tuple | None:
    """``(level, text)`` about environment variables that move the local adb
    server away from where TurboADB looks for it, or None."""
    raw = (os.environ.get("ANDROID_ADB_SERVER_PORT") or "").strip()
    if raw and _env_adb_port() is None:
        return ("WARNING", f"ANDROID_ADB_SERVER_PORT={raw} is not a port number, so adb "
                           "commands fail until it is corrected or removed.")
    port = local_adb_port()
    for var in ("ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return ("WARNING", f"{var}={value} is set: adb commands follow it, but TurboADB "
                               f"watches the local server on 127.0.0.1:{port}, so devices may "
                               "not appear. Remove it, or use Connect → Remote for an adb "
                               "server on another machine.")
    if port != DEFAULT_ADB_SERVER_PORT:
        return ("INFO", f"Using the local adb server on port {port} (ANDROID_ADB_SERVER_PORT).")
    return None


def last_adb_server_error() -> str:
    """Return the concise failure text from the most recent local start attempt."""
    return _LAST_ADB_SERVER_ERROR


def _launcher_text(fh) -> str:
    """What the ``adb start-server`` launcher printed so far (captured to a
    temp file), up to 4 KB.

    Reading the launcher's own output replaces the old approach of running a
    SECOND ``adb start-server`` just to see an error, which could itself start
    or race the daemon."""
    if fh is None:
        return ""
    try:
        fh.flush()
        fh.seek(0)
        data = fh.read(4096)
        # The launcher writes through the same file position: leave it at the
        # end, so what it prints next is added rather than written over.
        fh.seek(0, 2)
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        return data
    except Exception:
        return ""


def adb_request(sock, service: str) -> bool:
    """Send one request of the adb server's socket protocol on *sock* (the
    length of *service* in four hex digits, then *service*) and read the
    server's four-byte status: True for ``OKAY``.

    The one place a request is framed.  The length used to be written out by
    hand beside each service string (``b"000chost:version"``), where a changed
    string kept its old length and the server answered ``FAIL``.  A request
    that streams (``host:track-devices-l``) reads its replies with
    :func:`adb_reply` afterwards."""
    data = service.encode("ascii")
    sock.sendall(b"%04x" % len(data) + data)
    return _recv_exact(sock, 4) == b"OKAY"


def adb_reply(sock, *, retry_timeouts: bool = False) -> bytes | None:
    """One reply payload (its length in four hex digits, then the bytes), or
    None when the connection ends or the length is not a number.  A stream
    that stays quiet between replies (``host:track-devices-l``) passes
    *retry_timeouts* (see :func:`_recv_exact`)."""
    size = _recv_exact(sock, 4, retry_timeouts=retry_timeouts)
    if size is None:
        return None
    try:
        length = int(size, 16)
    except ValueError:
        return None
    return _recv_exact(sock, length, retry_timeouts=retry_timeouts)


def adb_query(
    service: str, host: str = "127.0.0.1", port: int = DEFAULT_ADB_SERVER_PORT,
    timeout: float = 0.25,
) -> bytes | None:
    """The reply payload of one host service (``host:version``,
    ``host:devices-l`` …) on the adb server at *host*:*port*, or None when
    no server answers or it refuses.  Never starts a server.  On a loopback
    *host* the default port follows ``ANDROID_ADB_SERVER_PORT``."""
    if host in _LOOPBACK:
        port = local_adb_port(port)
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if not adb_request(s, service):
                return None
            return adb_reply(s)
    except Exception:
        return None


def is_adb_server_alive(
    host: str = "127.0.0.1", port: int = DEFAULT_ADB_SERVER_PORT, timeout: float = 0.25
) -> bool:
    """Check if an adb server daemon is currently listening and responsive on host:port.

    This is a pure socket probe: unlike ``adb devices`` it never starts a daemon.
    On a loopback *host* the default port follows ``ANDROID_ADB_SERVER_PORT``
    (see :func:`local_adb_port`)."""
    if host in _LOOPBACK:
        port = local_adb_port(port)
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            return adb_request(s, "host:version")
    except Exception:
        return False


class _Launch:
    """One ``adb start-server`` launcher, and the temp file its output goes to
    (see :func:`_launcher_text`)."""

    def __init__(self, adb: str, port: int):
        self.adb = adb
        self.started = time.monotonic()
        self.exited_at = None  # first time a waiter saw the launcher gone
        self.text = None  # what it printed, kept once the file is closed
        try:
            import tempfile

            self.out = tempfile.TemporaryFile()
        except Exception:
            self.out = None
        self.captured = self.out is not None
        cmd = [adb]
        if port != DEFAULT_ADB_SERVER_PORT:
            cmd += ["-P", str(port)]
        try:
            self.proc = subprocess.Popen(
                cmd + ["start-server"],
                stdin=subprocess.DEVNULL,
                stdout=self.out if self.out is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if self.out is not None else subprocess.DEVNULL,
                creationflags=NO_WINDOW | DETACHED,
            )
        except Exception:
            self.close()
            raise

    def joinable(self) -> bool:
        """Still worth waiting for: running and not stuck, or only just exited
        (its daemon may still be binding the port)."""
        now = time.monotonic()
        if self.proc.poll() is None:
            return now - self.started < _LAUNCH_MAX_AGE
        if self.exited_at is None:
            self.exited_at = now
        return now - self.exited_at < _LAUNCH_EXIT_GRACE

    def _printed(self) -> str:
        return _launcher_text(self.out) if self.text is None else self.text

    def output(self) -> str:
        """What it printed, in one short line (for an error message)."""
        return " ".join(self._printed().split())[:360]

    def started_daemon(self) -> bool | None:
        """Whether this launcher started the server that answers: True once
        adb said "daemon started successfully", False once it ended without
        saying so (the server was up before it, or another program's took the
        port while both were starting one), None while it still runs.  Without
        its output to read, the server answering is all there is: True."""
        if not self.captured or _DAEMON_STARTED in self._printed():
            return True
        if self.proc.poll() is None:
            return None
        return _DAEMON_STARTED in self._printed()  # it may have said so as it ended

    def close(self) -> None:
        if self.text is None:
            self.text = _launcher_text(self.out)
        out, self.out = self.out, None
        if out is not None:
            try:
                out.close()
            except Exception:
                pass


def _retire(port: int, launch: _Launch, *, kill: bool = False) -> None:
    """Stop waiting for *launch* (ending the process first with *kill*).  Call
    with the server lock held."""
    if _INFLIGHT.get(port) is launch:
        del _INFLIGHT[port]
    if _UNCONFIRMED.get(port) is launch:
        del _UNCONFIRMED[port]
    if kill:
        try:
            if launch.proc.poll() is None:
                launch.proc.kill()
        except Exception:
            pass
    launch.close()


def _settle(port: int, *, alive: bool | None = None) -> None:
    """Credit the server on *port* to the launcher in flight there, once it
    answers (the launcher may have finished after every caller gave up), and
    take the credit back if that launcher turns out not to have started it.
    Call with the server lock held."""
    launch = _INFLIGHT.get(port)
    if launch is not None:
        if alive is None:
            alive = is_adb_server_alive(port=port, timeout=0.075)
        if alive:
            # A launcher lingers (for its daemon's first USB scan) after that
            # daemon answers: nothing to wait for any more, but it has yet to
            # say whose server this is.
            del _INFLIGHT[port]
            earlier = _UNCONFIRMED.pop(port, None)
            if earlier is not None:
                _retire(port, earlier)
            _STARTED_HERE[port] = launch.adb
            _UNCONFIRMED[port] = launch
    _confirm(port)


def _confirm(port: int) -> None:
    """Keep or take back the credit for the server on *port* once its launcher
    has said whether it started it.  Call with the server lock held."""
    launch = _UNCONFIRMED.get(port)
    if launch is None:
        return
    started = launch.started_daemon()
    if started is None:
        return
    if not started:
        # it found a server there after all: another program's, not ours to stop
        _STARTED_HERE.pop(port, None)
    _retire(port, launch)


def _await_launch(launch: _Launch, port: int, timeout: float) -> bool:
    """Wait up to *timeout* s for the server *launch* is starting (less within
    :func:`server_lock_patience`)."""
    global _LAST_ADB_SERVER_ERROR
    left = _patience_left()
    if left is not None:
        timeout = min(timeout, left)
    deadline = time.monotonic() + timeout
    while True:
        # The socket is the source of truth.  Return as soon as the daemon is
        # usable, even if adb.exe is still printing its startup banner or doing
        # follow-up USB work.
        if is_adb_server_alive(port=port, timeout=0.075):
            _LAST_ADB_SERVER_ERROR = ""
            with _server_locked():
                _settle(port, alive=True)
            return True
        # `adb start-server` is only the launcher.  On Windows it can return
        # before adbd has bound the port, so a client exit is not an immediate
        # readiness failure. Give the daemon a short grace period to bind;
        # after that, fail fast for a real error (for example a bad user
        # profile or port conflict).
        code = launch.proc.poll()
        if code is not None:
            now = time.monotonic()
            if launch.exited_at is None:
                launch.exited_at = now
            elif now - launch.exited_at >= _LAUNCH_EXIT_GRACE:
                with _server_locked():
                    _retire(port, launch)
                _LAST_ADB_SERVER_ERROR = launch.output() or (
                    f"adb start-server exited with code {code}"
                )
                return False
        if time.monotonic() >= deadline:
            break
        time.sleep(0.025)
    if is_adb_server_alive(port=port, timeout=0.075):
        _LAST_ADB_SERVER_ERROR = ""
        with _server_locked():
            _settle(port, alive=True)
        return True
    # Give up, but leave the launcher in flight: the next caller waits for it
    # instead of starting another.
    with _server_locked():
        text = launch.output()
    _LAST_ADB_SERVER_ERROR = text or (
        f"ADB server did not become ready on port {port} within {timeout:g}s"
    )
    return False


def ensure_adb_server(
    adb_path: str | None = None, timeout: float = 15.0, port: int = DEFAULT_ADB_SERVER_PORT
) -> bool:
    """Start the local ADB server (on *port*) if needed and confirm its socket becomes ready.

    One launcher per port: while an ``adb start-server`` this process started
    is still waiting for its daemon, every other caller (the GUI pre-warm, the
    main window's startup check, device tabs, the device list) waits on that
    one instead of starting another, each for its own *timeout*.  A caller that
    gives up leaves the launcher to the next one; a launcher that has run for
    30 s is ended and replaced.

    The `adb start-server` *client* can linger while the daemon finalises USB
    discovery.  It is launched with :class:`subprocess.Popen`, so callers such
    as the GUI's socket tracker can observe the daemon as soon as it listens
    instead of waiting for that client process to exit.
    """
    # This function is called during launch as well as on demand.  A local
    # loopback probe only needs a short bound; the actual daemon launch still
    # gets the caller's timeout.
    global _LAST_ADB_SERVER_ERROR
    port = local_adb_port(port)
    probe_timeout = min(0.10, max(0.02, timeout))
    if is_adb_server_alive(port=port, timeout=probe_timeout):
        _LAST_ADB_SERVER_ERROR = ""
        # Credit a launcher whose server is up, but never wait for the lock on
        # this fast path (owned_adb_server settles it later otherwise).
        if _INFLIGHT and _SERVER_LOCK.acquire(blocking=False):
            try:
                _settle(port, alive=True)
            finally:
                _SERVER_LOCK.release()
        return True
    with _server_locked():
        if is_adb_server_alive(port=port, timeout=probe_timeout):
            _LAST_ADB_SERVER_ERROR = ""
            _settle(port, alive=True)
            return True
        launch = _INFLIGHT.get(port)
        if launch is not None and not launch.joinable():
            _retire(port, launch, kill=True)
            launch = None
        if launch is None:
            try:
                adb = find_adb(adb_path)
            except Exception as exc:
                _LAST_ADB_SERVER_ERROR = str(exc)[:360]
                return False
            if not adb or not os.path.exists(adb):
                _LAST_ADB_SERVER_ERROR = "Configured adb executable was not found"
                return False
            try:
                launch = _Launch(adb, port)
            except Exception as exc:
                _LAST_ADB_SERVER_ERROR = str(exc)[:360]
                return False
            _INFLIGHT[port] = launch
    return _await_launch(launch, port, timeout)


def settle_adb_server_start(timeout: float = 3.0, port: int = DEFAULT_ADB_SERVER_PORT) -> None:
    """Wait up to *timeout* s for a server this process is still starting on
    *port*, so it can be told apart from one someone else started.  Never
    starts one (the GUI calls it on the way out)."""
    port = local_adb_port(port)
    with _server_locked():
        launch = _INFLIGHT.get(port)
    if launch is not None:
        _await_launch(launch, port, timeout)


def end_server_launchers() -> None:
    """End every ``adb start-server`` launcher still waiting (on the way out).
    A daemon that has not answered yet does not survive its launcher.  One
    that answers is left alone, and so is its launcher, which ends by itself
    once that daemon has reported to it."""
    with _server_locked():
        for port, launch in list(_INFLIGHT.items()):
            _retire(port, launch, kill=True)
        for port, launch in list(_UNCONFIRMED.items()):
            _retire(port, launch)


def _reset_server_state() -> None:
    """Forget every launcher and started server (between tests)."""
    global _LAST_ADB_SERVER_ERROR
    with _SERVER_LOCK:
        for waiting in (_INFLIGHT, _UNCONFIRMED):
            for port, launch in list(waiting.items()):
                _retire(port, launch)
        _STARTED_HERE.clear()
        _LAST_ADB_SERVER_ERROR = ""


def adb_server_lock():
    """The re-entrant lock held while the local server is started or stopped.
    Hold it across a stop and a start of your own (a shared server) so that
    :func:`ensure_adb_server` cannot start a plain server in between."""
    return _SERVER_LOCK


def kill_adb_server(
    adb_path: str | None = None,
    port: int = DEFAULT_ADB_SERVER_PORT,
    *,
    wait: float = 5.0,
    timeout: float = 15.0,
):
    """Stop the local adb server on *port* and wait for it to let go of the port.

    The one stopper: a restart, "Stop sharing", a tools upgrade and the exit
    all come through here.  ``adb kill-server`` can return while the daemon
    still answers, and a start in that gap reaches the dying server and
    reports a success that is gone a moment later, so the port is polled for
    up to *wait* seconds.  A launcher still starting a server on the port is
    ended, and the server no longer counts as started by this process.

    Returns the finished ``kill-server`` process (``returncode``, ``stdout``,
    ``stderr``), or None when adb could not be run.  Within
    :func:`server_lock_patience` both waits are cut to the patience left
    (``kill-server`` still gets a second)."""
    port = local_adb_port(port)
    with _server_locked():
        launch = _INFLIGHT.get(port)
        if launch is not None:
            _retire(port, launch, kill=True)
        launch = _UNCONFIRMED.get(port)
        if launch is not None:
            _retire(port, launch)
        _STARTED_HERE.pop(port, None)
        try:
            # An explicit path is used as given: resolving it again could fall
            # back to (and stop the server of) a different adb.
            adb = adb_path or find_adb()
        except Exception:
            return None
        cmd = [adb]
        if port != DEFAULT_ADB_SERVER_PORT:
            cmd += ["-P", str(port)]
        left = _patience_left()
        if left is not None:
            timeout = min(timeout, max(1.0, left))
        try:
            done = subprocess.run(
                cmd + ["kill-server"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            done = None
        left = _patience_left()
        if left is not None:
            wait = min(wait, left)
        deadline = time.monotonic() + max(0.0, wait)
        while is_adb_server_alive(port=port, timeout=0.1) and time.monotonic() < deadline:
            time.sleep(0.1)
        return done


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def owned_adb_server(port: int = DEFAULT_ADB_SERVER_PORT) -> str | None:
    """The adb this process started the local server on *port* with, while that
    server is the one answering; None for a server it did not start (another
    tool, a terminal, ``turboadb serve``, one already running at launch, one
    another program started while TurboADB was starting its own) or one
    another adb has replaced since.  The GUI stops only this server on exit."""
    port = local_adb_port(port)
    with _server_locked():
        _settle(port)
        adb = _STARTED_HERE.get(port)
    if not adb:
        return None
    running = (adb_server_status(port) or {}).get("executable")
    if running and not _same_file(running, adb):
        return None
    return adb


# --------------------------------------------------------------------------- #
# Which adb server is it?
# --------------------------------------------------------------------------- #
def adb_server_version(port: int = DEFAULT_ADB_SERVER_PORT, timeout: float = 0.25) -> int | None:
    """The protocol version the local adb server reports (``host:version``: 41
    for every current adb), or None when no server answers.

    adb clients compare this number, not the release: a client whose number
    differs kills the server and starts its own, so two tools whose adbs speak
    different protocols keep restarting each other's server."""
    payload = adb_query("host:version", port=port, timeout=timeout)
    try:
        return int(payload, 16) if payload else None
    except ValueError:
        return None


def _protobuf_strings(data: bytes) -> dict:
    """The length-delimited fields of a protobuf message, by field number."""
    fields = {}
    i = 0

    def varint():
        nonlocal i
        value = shift = 0
        while True:
            byte = data[i]
            i += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    try:
        while i < len(data):
            key = varint()
            wire = key & 7
            if wire == 0:
                varint()
            elif wire == 1:
                i += 8
            elif wire == 5:
                i += 4
            elif wire == 2:
                size = varint()
                fields[key >> 3] = data[i:i + size]
                i += size
            else:
                break
    except IndexError:
        pass
    return fields


def adb_server_status(port: int = DEFAULT_ADB_SERVER_PORT, timeout: float = 0.5) -> dict | None:
    """What the local adb server says about itself (``host:server-status``):
    ``{"version": "37.0.1", "build": "15733141", "executable": ".../adb.exe"}``.
    None when no server answers, or it is too old to be asked."""
    payload = adb_query("host:server-status", port=port, timeout=timeout)
    if not payload:
        return None
    fields = _protobuf_strings(payload)
    status = {
        name: fields[number].decode("utf-8", "replace")
        for number, name in ((5, "version"), (6, "build"), (7, "executable"))
        if number in fields
    }
    return status or None


_PROTOCOLS: dict = {}


def adb_protocol_version(adb_path: str) -> int | None:
    """The server protocol an adb executable speaks, from ``adb version``
    ("Android Debug Bridge version 1.0.41" is 41); cached per file."""
    try:
        st = os.stat(adb_path)
    except OSError:
        return None
    key = (os.path.normcase(os.path.abspath(adb_path)), st.st_mtime, st.st_size)
    if key not in _PROTOCOLS:
        try:
            text = _adb_version_output(adb_path, timeout=10.0)
        except Exception:
            return None
        match = re.search(r"Android Debug Bridge version \d+\.\d+\.(\d+)", text)
        _PROTOCOLS[key] = int(match.group(1)) if match else None
    return _PROTOCOLS[key]


def describe_adb_server(adb_path: str | None, port: int = DEFAULT_ADB_SERVER_PORT) -> tuple | None:
    """``(level, text)`` when the local adb server is not one *adb_path* would
    have started, else None.  Nothing is ever stopped.

    A server of another protocol is a WARNING: the first command from
    *adb_path* kills and restarts it, and the tool that started it restarts it
    back, so every shell, logcat and screen drops each time.  The same protocol
    from another executable is only an INFO: such builds share one server."""
    port = local_adb_port(port)
    server = adb_server_version(port)
    if server is None or not adb_path:
        return None
    status = adb_server_status(port) or {}
    running = status.get("executable") or ""
    if running and _same_file(running, adb_path):
        return None
    ours = adb_protocol_version(adb_path)
    release = f" {status['version']}" if status.get("version") else ""
    other = f"{running}{release}" if running else "another adb"
    if ours is not None and ours != server:
        return ("WARNING", f"The adb server on port {port} runs {other} (protocol {server}), "
                           f"but TurboADB's adb {adb_path} speaks protocol {ours}. Its first "
                           "command restarts that server and the tool that started it restarts "
                           "it back, so every shell, logcat and screen drops each time. Use "
                           "one adb for both (Settings → Tools → adb path), or close the "
                           "other tool.")
    if running:
        return ("INFO", f"The adb server on port {port} was started by {other}; it speaks "
                        f"the same protocol ({server}) as TurboADB's adb, so both use it. "
                        "TurboADB leaves it running on exit.")
    return None


def diagnose() -> dict:
    """Report what tooling is available, for a pre-flight / `turboadb doctor`.

    ``adb_error`` / ``scrcpy_error`` say why a path set on purpose
    (``TURBOADB_ADB`` / ``TURBOADB_SCRCPY``) is not used: that path is to be
    corrected, and a download would not change it."""
    info = {"adb": None, "adb_path": None, "adb_error": None,
            "scrcpy": None, "scrcpy_path": None, "scrcpy_error": None}
    try:
        info["adb_path"] = find_adb()
        info["adb"] = adb_version(info["adb_path"])
    except ADBNotFoundError as exc:
        if getattr(exc, "configured", None):
            info["adb_error"] = str(exc).splitlines()[0]
    try:
        info["scrcpy_path"] = find_scrcpy()
        info["scrcpy"] = "found"
    except ADBNotFoundError as exc:
        if getattr(exc, "configured", None):
            info["scrcpy_error"] = str(exc).splitlines()[0]
    return info
