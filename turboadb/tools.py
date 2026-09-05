"""
Locate the ``adb`` and ``scrcpy`` executables robustly, with clear, actionable
errors when they are missing.

Search order (first hit wins):
  1. an explicit path argument
  2. the ``TURBOADB_ADB`` / ``TURBOADB_SCRCPY`` environment variables
  3. binaries bundled inside this package (``turboadb/bin/...``), if present
  4. the system ``PATH``
  5. common Android SDK / install locations
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .exceptions import ADBNotFoundError

ADB_DOWNLOAD = "https://developer.android.com/tools/releases/platform-tools"
SCRCPY_DOWNLOAD = "https://github.com/Genymobile/scrcpy"

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
    cache = os.path.join(os.path.expanduser("~"), ".turboadb", "tools")
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


def _resolve(name: str, explicit, env_var: str) -> str | None:
    cache_key = (name, explicit)
    if cache_key in _CACHE and os.path.isfile(_CACHE[cache_key]):
        return _CACHE[cache_key]

    if not explicit:
        try:
            from .gui import settings as settings_mod
            setting_key = "adb_path" if name == "adb" else "scrcpy_path"
            val = (settings_mod.get(setting_key) or "").strip()
            if val:
                explicit = val
        except Exception:
            pass

    if explicit:
        p = os.path.expanduser(explicit)
        if os.path.isfile(p):
            _CACHE[cache_key] = p
            return p
        # allow passing a directory that contains the exe, or a bare name on PATH
        cand = os.path.join(p, _exe(name))
        if os.path.isfile(cand):
            _CACHE[cache_key] = cand
            return cand
        which = shutil.which(explicit)
        if which:
            _CACHE[cache_key] = which
            return which
    env = os.environ.get(env_var)
    if env and os.path.isfile(env):
        _CACHE[cache_key] = env
        return env
    for cand in _managed_candidates(name):  # on-demand download cache
        if os.path.isfile(cand):
            _CACHE[cache_key] = cand
            return cand
    for cand in _bundled_candidates(name):
        if os.path.isfile(cand):
            _CACHE[cache_key] = cand
            return cand
    which = shutil.which(name)
    if which:
        _CACHE[cache_key] = which
        return which
    for cand in _sdk_candidates(name):
        if cand and os.path.isfile(cand):
            _CACHE[cache_key] = cand
            return cand
    return None


def find_adb(explicit: str | None = None) -> str:
    """Return an absolute path to ``adb`` or raise a guided ADBNotFoundError."""
    path = _resolve("adb", explicit, "TURBOADB_ADB")
    if path:
        try:
            adb_dir = os.path.dirname(os.path.abspath(path))
            current_path = os.environ.get("PATH", "")
            if adb_dir.lower() not in current_path.lower():
                os.environ["PATH"] = adb_dir + os.pathsep + current_path
            os.environ["ADB"] = path
        except Exception:
            pass
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


def adb_version(explicit: str | None = None) -> str:
    """Return the `adb version` banner (first line), or raise ADBNotFoundError."""
    adb = find_adb(explicit)
    try:
        out = subprocess.run(
            [adb, "version"], capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW
        )
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as exc:  # pragma: no cover
        return f"adb at {adb} (version query failed: {exc})"


import threading

_SERVER_LOCK = threading.Lock()


def is_adb_server_alive(host: str = "127.0.0.1", port: int = 5037, timeout: float = 0.25) -> bool:
    """Check if an adb server daemon is currently listening and responsive on host:port."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            # ADB protocol requires 4 hex length + payload: "000chost:version"
            s.sendall(b"000chost:version")
            resp = s.recv(4)
            if resp == b"OKAY":
                try:
                    s.settimeout(0.2)
                    s.recv(16)
                except Exception:
                    pass
                return True
            return False
    except Exception:
        return False


def ensure_adb_server(adb_path: str | None = None, timeout: float = 15.0) -> bool:
    """Start the adb server in the background if it is not already running."""
    if is_adb_server_alive():
        return True
    with _SERVER_LOCK:
        if is_adb_server_alive():
            return True
        try:
            adb = find_adb(adb_path)
        except Exception:
            return False
        if not adb or not os.path.exists(adb):
            return False
        try:
            flags = NO_WINDOW
            if os.name == "nt":
                flags |= 0x00000008 | 0x00000200
            subprocess.run(
                [adb, "start-server"],
                capture_output=True,
                timeout=timeout,
                creationflags=flags,
            )
            import time
            for _ in range(25):
                if is_adb_server_alive():
                    return True
                time.sleep(0.05)
            return is_adb_server_alive()
        except Exception:
            return False


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

