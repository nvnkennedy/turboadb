"""
On-demand downloader for the external tools TurboADB drives, into a per-user
cache (``~/.turboadb/tools/``) — so a plain ``pip install turboadb`` stays small
and license-clean (you fetch Google's adb yourself; we never redistribute it),
yet the tools are one command (or one click) away.

  * ``adb``    — Google's official platform-tools zip (Apache-2.0 source; the
                 prebuilt is under the Android SDK Terms, which you accept by
                 downloading it here).
  * ``scrcpy`` — Genymobile's official GitHub release (Apache-2.0). Prebuilt
                 archives exist for Windows; on macOS/Linux use brew/apt.

Nothing here runs at install time (wheels don't run code on install); it runs
when you ask: ``turboadb fetch-tools``, the GUI's download prompt, or
``turboadb.fetch_tools()``.
"""

from __future__ import annotations

import os
import re
import sys
import json
import shutil
import zipfile
import tempfile
import platform
import subprocess
import urllib.request

from .exceptions import ADBNotFoundError
from .tools import find_adb, find_scrcpy, NO_WINDOW

PLATFORM_TOOLS_REPO_XML = "https://dl.google.com/android/repository/repository2-3.xml"

PLATFORM_TOOLS_URLS = {
    "windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
    "linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
}
SCRCPY_RELEASES_API = "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"
_UA = {"User-Agent": "turboadb"}


def _os_key() -> str:
    s = sys.platform
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "darwin"
    return "linux"


def tools_dir() -> str:
    """The managed cache directory for downloaded tools."""
    d = os.path.join(os.path.expanduser("~"), ".turboadb", "tools")
    os.makedirs(d, exist_ok=True)
    return d


def adb_dir() -> str:
    return os.path.join(tools_dir(), "platform-tools")


def scrcpy_dir() -> str:
    return os.path.join(tools_dir(), "scrcpy")


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def managed_adb() -> str | None:
    p = os.path.join(adb_dir(), _exe("adb"))
    return p if os.path.isfile(p) else None


def managed_scrcpy() -> str | None:
    p = os.path.join(scrcpy_dir(), _exe("scrcpy"))
    return p if os.path.isfile(p) else None


# --------------------------------------------------------------------------- #
# download + extract helpers
# --------------------------------------------------------------------------- #
def _download(url: str, dest: str, on_progress=None) -> None:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if on_progress and total:
                    try:
                        on_progress(int(done * 100 / total))
                    except Exception:
                        pass
    if on_progress:
        try:
            on_progress(100)
        except Exception:
            pass


def _extract_zip(zip_path: str, dest_parent: str, *, strip_top_to: str | None = None):
    """Extract *zip_path*. If *strip_top_to* is given, the archive's single
    top-level folder is flattened into that exact directory."""
    with zipfile.ZipFile(zip_path) as z:
        if strip_top_to:
            tops = {n.split("/")[0] for n in z.namelist() if n.strip("/")}
            top = next(iter(tops)) if len(tops) == 1 else None
            if os.path.isdir(strip_top_to):
                shutil.rmtree(strip_top_to, ignore_errors=True)
            os.makedirs(strip_top_to, exist_ok=True)
            for member in z.namelist():
                if member.endswith("/"):
                    continue
                rel = member[len(top) + 1:] if top and member.startswith(top + "/") \
                    else member
                if not rel:
                    continue
                target = os.path.join(strip_top_to, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        else:
            z.extractall(dest_parent)


def _chmod_x(path: str) -> None:
    if os.name != "nt" and os.path.isfile(path):
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# public download functions
# --------------------------------------------------------------------------- #
def download_platform_tools(*, force: bool = False, on_progress=None) -> str:
    """Download + extract Google's platform-tools into the cache. Returns the
    path to the cached ``adb`` executable."""
    existing = managed_adb()
    if existing and not force:
        return existing
    key = _os_key()
    url = PLATFORM_TOOLS_URLS[key]
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "platform-tools.zip")
        _download(url, zip_path, on_progress)
        # the zip already contains a top-level "platform-tools/" folder
        if os.path.isdir(adb_dir()):
            shutil.rmtree(adb_dir(), ignore_errors=True)
        _extract_zip(zip_path, tools_dir())
    adb = managed_adb()
    if not adb:
        raise ADBNotFoundError("platform-tools downloaded but adb was not found "
                               "in the archive (unexpected layout).")
    _chmod_x(adb)
    return adb


def _scrcpy_asset_url() -> str:
    key = _os_key()
    if key != "windows":
        raise ADBNotFoundError(
            "Prebuilt scrcpy is downloaded only on Windows. On macOS use "
            "'brew install scrcpy'; on Linux use 'apt install scrcpy' (or your "
            "distro's package).")
    is64 = platform.machine().endswith("64") or sys.maxsize > 2**32
    want = "win64" if is64 else "win32"
    req = urllib.request.Request(SCRCPY_RELEASES_API, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    assets = data.get("assets", [])
    for a in assets:
        if a["name"].endswith(".zip") and want in a["name"]:
            return a["browser_download_url"]
    # fall back to any windows zip
    for a in assets:
        if a["name"].endswith(".zip") and "win" in a["name"]:
            return a["browser_download_url"]
    raise ADBNotFoundError("No suitable scrcpy Windows release asset was found.")


def download_scrcpy(*, force: bool = False, on_progress=None) -> str:
    """Download + extract the latest scrcpy (Windows prebuilt) into the cache.
    Returns the path to the cached ``scrcpy`` executable."""
    existing = managed_scrcpy()
    if existing and not force:
        return existing
    url = _scrcpy_asset_url()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "scrcpy.zip")
        _download(url, zip_path, on_progress)
        _extract_zip(zip_path, tools_dir(), strip_top_to=scrcpy_dir())
    scr = managed_scrcpy()
    if not scr:
        raise ADBNotFoundError("scrcpy downloaded but scrcpy.exe was not found "
                               "in the archive (unexpected layout).")
    return scr


def _stamp_path() -> str:
    return os.path.join(tools_dir(), ".stamp")


def _read_stamp() -> str:
    try:
        with open(_stamp_path(), encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def _write_stamp(version: str) -> None:
    try:
        with open(_stamp_path(), "w", encoding="utf-8") as fh:
            fh.write(version)
    except Exception:
        pass


def _pkg_version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "?"


def auto_fetch_enabled() -> bool:
    """Auto-download is ON by default; disable with TURBOADB_AUTO_FETCH=0
    (handy for offline / CI / locked-down environments)."""
    v = os.environ.get("TURBOADB_AUTO_FETCH")
    if v is None:
        return True
    return v.strip().lower() not in ("0", "false", "no", "off", "")


_ensured = False


def ensure_tools(*, on_progress=None, notify=None, scrcpy: bool = True) -> dict:
    """Make sure the managed cache has adb (and scrcpy) for the **current**
    TurboADB version. Runs at most once per process. On a fresh install it
    downloads; after an upgrade it re-downloads the LATEST platform-tools +
    scrcpy so you're always current. Best-effort and never raises — if the
    network is down, detection simply falls back to whatever's already on PATH.

    Disable entirely with the ``TURBOADB_AUTO_FETCH=0`` environment variable.
    """
    global _ensured
    norm = lambda note, errors=None: {"note": note, "adb": managed_adb(),
                                      "scrcpy": managed_scrcpy(),
                                      "errors": errors or {}}
    if _ensured:
        return norm("already-ensured")
    _ensured = True
    if not auto_fetch_enabled():
        return norm("disabled")
    version = _pkg_version()
    upgraded = _read_stamp() != version
    have_adb = managed_adb() is not None
    if have_adb and not upgraded:
        return norm("up-to-date")
    errors = {}
    note = ""
    try:
        if not have_adb:
            # fresh install: download the latest tools
            note = "installed"
            if notify:
                notify("Downloading latest platform-tools + scrcpy "
                       "(one-time; set TURBOADB_AUTO_FETCH=0 to skip)…")
            res = fetch_tools(adb=True, scrcpy=scrcpy, force=False,
                              on_progress=on_progress)
            errors = res.get("errors", {})
        elif upgraded:
            # TurboADB itself was upgraded: check for newer adb/scrcpy and
            # download ONLY what's outdated (not a blind re-download)
            up = upgrade_tools(on_progress=on_progress, notify=notify)
            errors = up.get("errors", {})
            note = "up-to-date" if up.get("up_to_date") else "updated"
        if managed_adb():
            _write_stamp(version)
    except Exception as exc:           # never let auto-fetch break a real command
        errors["ensure"] = str(exc)
    return norm(note, errors)


# --------------------------------------------------------------------------- #
# version checks ("only download if there's a newer version")
# --------------------------------------------------------------------------- #
def installed_adb_version(adb_path: str | None = None) -> str | None:
    try:
        exe = adb_path or find_adb()
        out = subprocess.run([exe, "version"], capture_output=True, text=True,
                             timeout=15, creationflags=NO_WINDOW)
        m = re.search(r"Version\s+(\d+\.\d+\.\d+)", out.stdout or out.stderr or "")
        return m.group(1) if m else None
    except Exception:
        return None


def latest_adb_version() -> str | None:
    try:
        req = urllib.request.Request(PLATFORM_TOOLS_REPO_XML, headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml = resp.read().decode("utf-8", "replace")
        block = re.search(r'path="platform-tools".*?</revision>', xml, re.S)
        if not block:
            return None
        rev = re.search(r"<major>(\d+)</major>\s*<minor>(\d+)</minor>"
                        r"\s*<micro>(\d+)</micro>", block.group(0))
        return ".".join(rev.groups()) if rev else None
    except Exception:
        return None


def installed_scrcpy_version(scrcpy_path: str | None = None) -> str | None:
    try:
        exe = scrcpy_path or find_scrcpy()
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=15, creationflags=NO_WINDOW)
        m = re.search(r"scrcpy\s+(\d+\.\d+(?:\.\d+)?)", out.stdout or out.stderr or "")
        return m.group(1) if m else None
    except Exception:
        return None


def latest_scrcpy_version() -> str | None:
    try:
        req = urllib.request.Request(SCRCPY_RELEASES_API, headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        tag = (data.get("tag_name") or "").lstrip("vV")
        return tag or None
    except Exception:
        return None


def _decide(installed: str | None, latest: str | None) -> bool | None:
    """True = upgrade available; False = up to date; None = couldn't determine."""
    if installed is None:
        return True                 # not installed -> "upgrade" means install
    if latest is None:
        return None                 # can't reach the version source
    return latest != installed


def check_updates() -> dict:
    """Compare installed adb/scrcpy against the latest available. Returns
    ``{"adb": {"installed","latest","upgrade"}, "scrcpy": {...}}`` where
    ``upgrade`` is True / False / None (unknown). Makes no changes."""
    ai, al = installed_adb_version(), latest_adb_version()
    si, sl = installed_scrcpy_version(), latest_scrcpy_version()
    return {
        "adb": {"installed": ai, "latest": al, "upgrade": _decide(ai, al)},
        "scrcpy": {"installed": si, "latest": sl, "upgrade": _decide(si, sl)},
    }


def upgrade_tools(*, on_progress=None, notify=None) -> dict:
    """Check for newer adb/scrcpy and download **only** the ones that are
    outdated (or missing). If everything's current, downloads nothing. Returns a
    summary dict including the version check and what was updated."""
    checks = check_updates()
    updated = {}
    errors = {}
    want_adb = checks["adb"]["upgrade"] is True
    want_scrcpy = checks["scrcpy"]["upgrade"] is True
    if not want_adb and not want_scrcpy:
        return {"checks": checks, "updated": {}, "errors": {},
                "up_to_date": True}
    if notify:
        bits = []
        if want_adb:
            bits.append(f"adb {checks['adb']['installed']}→{checks['adb']['latest']}")
        if want_scrcpy:
            bits.append(f"scrcpy {checks['scrcpy']['installed']}→"
                        f"{checks['scrcpy']['latest']}")
        notify("Updating " + ", ".join(bits) + " …")
    if want_adb:
        try:
            updated["adb"] = download_platform_tools(force=True,
                                                     on_progress=on_progress)
        except Exception as exc:
            errors["adb"] = str(exc)
    if want_scrcpy:
        try:
            updated["scrcpy"] = download_scrcpy(force=True, on_progress=on_progress)
        except Exception as exc:
            errors["scrcpy"] = str(exc)
    return {"checks": checks, "updated": updated, "errors": errors,
            "up_to_date": False}


def fetch_tools(*, adb: bool = True, scrcpy: bool = True, force: bool = False,
                on_progress=None) -> dict:
    """Download whatever's requested into the cache. Returns
    ``{"adb": path|None, "scrcpy": path|None, "errors": {...}}``.

    scrcpy bundles its own adb on Windows; downloading scrcpy alone is enough to
    get both, but fetching platform-tools gives you the latest standalone adb.
    """
    result = {"adb": None, "scrcpy": None, "errors": {}}
    if adb:
        try:
            result["adb"] = download_platform_tools(force=force,
                                                    on_progress=on_progress)
        except Exception as exc:
            result["errors"]["adb"] = str(exc)
    if scrcpy:
        try:
            result["scrcpy"] = download_scrcpy(force=force, on_progress=on_progress)
        except Exception as exc:
            result["errors"]["scrcpy"] = str(exc)
    return result
