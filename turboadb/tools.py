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
import sys
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
        return [os.path.join(cache, "platform-tools", exe),
                os.path.join(cache, "scrcpy", exe)]   # scrcpy bundles adb on Win
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
        paths += [os.path.join(home, "platform-tools", exe),
                  "/usr/local/bin/" + exe, "/usr/bin/" + exe,
                  "/opt/homebrew/bin/" + exe]
    else:  # scrcpy
        paths += [os.path.join(home, "scrcpy", exe),
                  "/usr/local/bin/" + exe, "/usr/bin/" + exe,
                  "/opt/homebrew/bin/" + exe,
                  os.path.join(os.environ.get("LOCALAPPDATA", ""),
                               "scrcpy", exe)]
    return paths


def _resolve(name: str, explicit, env_var: str) -> str | None:
    if explicit:
        p = os.path.expanduser(explicit)
        if os.path.isfile(p):
            return p
        # allow passing a directory that contains the exe, or a bare name on PATH
        cand = os.path.join(p, _exe(name))
        if os.path.isfile(cand):
            return cand
        which = shutil.which(explicit)
        if which:
            return which
    env = os.environ.get(env_var)
    if env and os.path.isfile(env):
        return env
    for cand in _managed_candidates(name):     # on-demand download cache
        if os.path.isfile(cand):
            return cand
    for cand in _bundled_candidates(name):
        if os.path.isfile(cand):
            return cand
    which = shutil.which(name)
    if which:
        return which
    for cand in _sdk_candidates(name):
        if cand and os.path.isfile(cand):
            return cand
    return None


def find_adb(explicit: str | None = None) -> str:
    """Return an absolute path to ``adb`` or raise a guided ADBNotFoundError."""
    path = _resolve("adb", explicit, "TURBOADB_ADB")
    if path:
        return path
    raise ADBNotFoundError(
        "Could not find 'adb' (Android Platform-Tools).\n"
        "  • Let TurboADB fetch it for you:  turboadb fetch-tools\n"
        "    (or in Python:  from turboadb import fetch_tools; fetch_tools())\n"
        f"  • Or install it yourself: {ADB_DOWNLOAD}\n"
        "    then add platform-tools to your PATH, or pass adb_path=... in\n"
        "    ADBConfig (or set the TURBOADB_ADB env var).")


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
        "  • Or pass scrcpy_path=... (or set the TURBOADB_SCRCPY env var).")


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
        out = subprocess.run([adb, "version"], capture_output=True, text=True,
                             timeout=15, creationflags=NO_WINDOW)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as exc:  # pragma: no cover
        return f"adb at {adb} (version query failed: {exc})"


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
