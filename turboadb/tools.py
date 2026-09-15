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

Resolution never modifies ``os.environ``; code that launches a child process
which must use the same adb (scrcpy, a local terminal) passes it explicitly.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time

from .exceptions import ADBNotFoundError

ADB_DOWNLOAD = "https://developer.android.com/tools/releases/platform-tools"
SCRCPY_DOWNLOAD = "https://github.com/Genymobile/scrcpy"
DEFAULT_ADB_SERVER_PORT = 5037

# Hide the console window when spawning adb from a windowed (GUI/frozen) app so
# the user never sees a black box flash. No-op on non-Windows.
if os.name == "nt":
    NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW
else:
    NO_WINDOW = 0


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
    """Path of the managed download cache (``~/.turboadb/tools``). Never creates it."""
    return os.path.join(os.path.expanduser("~"), ".turboadb", "tools")


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


def _search(name: str, explicit, env, setting) -> str | None:
    for configured in (explicit, env, setting):
        if configured:
            hit = _path_candidate(name, configured)
            if hit:
                return hit
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
    found = _search(name, explicit, env, setting)
    if found:
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


_SERVER_LOCK = threading.Lock()
_LAST_ADB_SERVER_ERROR = ""


def last_adb_server_error() -> str:
    """Return the concise failure text from the most recent local start attempt."""
    return _LAST_ADB_SERVER_ERROR


def _launcher_output(fh) -> str:
    """What the ``adb start-server`` launcher printed (captured to a temp file).

    Reading the launcher's own output replaces the old approach of running a
    SECOND ``adb start-server`` just to see an error, which could itself start
    or race the daemon."""
    if fh is None:
        return ""
    try:
        fh.flush()
        fh.seek(0)
        data = fh.read(4096)
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        return " ".join(data.split())[:360]
    except Exception:
        return ""


def is_adb_server_alive(
    host: str = "127.0.0.1", port: int = DEFAULT_ADB_SERVER_PORT, timeout: float = 0.25
) -> bool:
    """Check if an adb server daemon is currently listening and responsive on host:port.

    This is a pure socket probe: unlike ``adb devices`` it never starts a daemon."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            # ADB protocol requires 4 hex length + payload: "000chost:version"
            s.sendall(b"000chost:version")
            return _recv_exact(s, 4) == b"OKAY"
    except Exception:
        return False


def ensure_adb_server(
    adb_path: str | None = None, timeout: float = 15.0, port: int = DEFAULT_ADB_SERVER_PORT
) -> bool:
    """Start the local ADB server (on *port*) if needed and confirm its socket becomes ready.

    The `adb start-server` *client* can linger while the daemon finalises USB
    discovery.  It is launched with :class:`subprocess.Popen`, so callers such
    as the GUI's socket tracker can observe the daemon as soon as it listens
    instead of waiting for that client process to exit.
    """
    # This function is called during launch as well as on demand.  A local
    # loopback probe only needs a short bound; the actual daemon launch still
    # gets the caller's timeout.
    global _LAST_ADB_SERVER_ERROR
    probe_timeout = min(0.10, max(0.02, timeout))
    if is_adb_server_alive(port=port, timeout=probe_timeout):
        _LAST_ADB_SERVER_ERROR = ""
        return True
    with _SERVER_LOCK:
        if is_adb_server_alive(port=port, timeout=probe_timeout):
            _LAST_ADB_SERVER_ERROR = ""
            return True
        try:
            adb = find_adb(adb_path)
        except Exception as exc:
            _LAST_ADB_SERVER_ERROR = str(exc)[:360]
            return False
        if not adb or not os.path.exists(adb):
            _LAST_ADB_SERVER_ERROR = "Configured adb executable was not found"
            return False
        out = None
        try:
            import tempfile

            out = tempfile.TemporaryFile()
        except Exception:
            out = None
        try:
            flags = NO_WINDOW
            if os.name == "nt":
                flags |= 0x00000008 | 0x00000200
            cmd = [adb]
            if port != DEFAULT_ADB_SERVER_PORT:
                cmd += ["-P", str(port)]
            started = subprocess.Popen(
                cmd + ["start-server"],
                stdin=subprocess.DEVNULL,
                stdout=out if out is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if out is not None else subprocess.DEVNULL,
                creationflags=flags,
            )
            deadline = time.monotonic() + timeout
            launcher_exited_at = None
            while time.monotonic() < deadline:
                # The socket is the source of truth.  Return as soon as the
                # daemon is usable, even if adb.exe is still printing its
                # startup banner or doing follow-up USB work.
                if is_adb_server_alive(port=port, timeout=0.075):
                    _LAST_ADB_SERVER_ERROR = ""
                    return True
                # `adb start-server` is only the launcher.  On Windows it can
                # return before adbd has bound the port, so a client exit is
                # not an immediate readiness failure. Give the daemon a short
                # grace period to bind; after that, fail fast for a real error
                # (for example a bad user profile or port conflict).
                code = started.poll()
                if code is not None:
                    if launcher_exited_at is None:
                        launcher_exited_at = time.monotonic()
                    elif time.monotonic() - launcher_exited_at >= 0.75:
                        _LAST_ADB_SERVER_ERROR = _launcher_output(out) or (
                            f"adb start-server exited with code {code}"
                        )
                        return False
                time.sleep(0.025)
            ready = is_adb_server_alive(port=port, timeout=0.075)
            _LAST_ADB_SERVER_ERROR = (
                ""
                if ready
                else _launcher_output(out)
                or f"ADB server did not become ready on port {port} within {timeout:g}s"
            )
            return ready
        except Exception as exc:
            _LAST_ADB_SERVER_ERROR = str(exc)[:360]
            return False
        finally:
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass


def diagnose() -> dict:
    """Report what tooling is available, for a pre-flight / `turboadb doctor`."""
    info = {"adb": None, "adb_path": None, "scrcpy": None, "scrcpy_path": None}
    try:
        info["adb_path"] = find_adb()
        info["adb"] = adb_version()
    except ADBNotFoundError:
        pass
    try:
        info["scrcpy_path"] = find_scrcpy()
        info["scrcpy"] = "found"
    except ADBNotFoundError:
        pass
    return info
