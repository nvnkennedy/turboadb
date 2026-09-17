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
``turboadb.fetch_tools()``. The automatic :func:`ensure_tools` only downloads a
tool that is MISSING; upgrades happen only through :func:`upgrade_tools`
(``turboadb upgrade-tools`` / self-update / the GUI Upgrade button).
"""

from __future__ import annotations

import os
import re
import sys
import json
import shutil
import logging
import zipfile
import tempfile
import platform
import threading
import subprocess
import urllib.error
import urllib.request

from .exceptions import ADBError, ADBNotFoundError
from .tools import (  # noqa: F401  (_exe re-exported for existing importers)
    NO_WINDOW,
    _adb_version_output,
    _exe,
    find_adb,
    find_scrcpy,
    managed_tools_dir,
    parse_version,
)

PLATFORM_TOOLS_REPO_XML = "https://dl.google.com/android/repository/repository2-3.xml"

PLATFORM_TOOLS_URLS = {
    "windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
    "linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
}
PLATFORM_TOOLS_BASE = "https://dl.google.com/android/repository/"
SCRCPY_RELEASES_API = "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"
_UA = {"User-Agent": "turboadb"}

_log = logging.getLogger(__name__)

# The <host-os> value Google's manifest uses for each of our platforms.
_MANIFEST_HOST_OS = {"windows": "windows", "darwin": "macosx", "linux": "linux"}


def _os_key() -> str:
    s = sys.platform
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "darwin"
    return "linux"


def scrcpy_download_supported() -> bool:
    """True where a prebuilt scrcpy can be downloaded (Windows only)."""
    return _os_key() == "windows"


def tools_dir() -> str:
    """The managed cache directory for downloaded tools (not created on read)."""
    return managed_tools_dir()


def _ensure_tools_dir() -> str:
    d = tools_dir()
    os.makedirs(d, exist_ok=True)
    return d


def adb_dir() -> str:
    return os.path.join(tools_dir(), "platform-tools")


def scrcpy_dir() -> str:
    return os.path.join(tools_dir(), "scrcpy")


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
    top-level folder is flattened into that exact directory. Member paths are
    sanitized ('..' / absolute / backslash tricks dropped) so a crafted archive
    can never write outside the target directory (zip-slip)."""
    with zipfile.ZipFile(zip_path) as z:
        members = z.namelist()
        names = [n.replace("\\", "/") for n in members]
        top = None
        if strip_top_to:
            tops = {n.split("/")[0] for n in names if n.strip("/")}
            top = next(iter(tops)) if len(tops) == 1 else None
        target_dir = strip_top_to or dest_parent
        os.makedirs(target_dir, exist_ok=True)
        root = os.path.realpath(target_dir)
        for member, name in zip(members, names):
            if name.endswith("/"):
                continue
            rel = name[len(top) + 1 :] if top and name.startswith(top + "/") else name
            parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
            if not parts:
                continue
            target = os.path.join(root, *parts)
            if not os.path.realpath(target).startswith(os.path.join(root, "")):
                continue  # zip-slip attempt — skip it
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _kill_adb_server(adb_path: str | None = None) -> None:
    """Best-effort stop of the adb server. On Windows a running server LOCKS its
    own adb.exe, so replacing the managed platform-tools in place silently fails
    (rmtree with ignore_errors dropped the error) — which is exactly why
    'upgrade adb' sometimes did nothing. Always stop the server first."""
    try:
        exe = adb_path or managed_adb() or find_adb()
    except Exception:
        return
    try:
        subprocess.run(
            [exe, "kill-server"], capture_output=True, timeout=15, creationflags=NO_WINDOW
        )
    except Exception:
        pass


def _swap_dir(new_dir: str, dest: str) -> None:
    """Replace *dest* with *new_dir* atomically-ish: move the old directory
    aside (kept as ``dest.old`` for a manual rollback), then move the new one
    in. If a file in the old dir is still locked (a running adb server / scrcpy
    session), the rename fails LOUDLY instead of silently leaving a
    half-updated mix of old and new files."""
    old = None
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.isdir(dest):
        old = dest + ".old"
        shutil.rmtree(old, ignore_errors=True)
        try:
            os.rename(dest, old)
        except OSError as exc:
            raise ADBError(
                f"cannot replace {dest} — a file in it is still in use "
                f"(a running adb server or scrcpy/mirror session locks its exe; "
                f"close mirrors/recordings and retry): {exc}"
            ) from exc
    try:
        shutil.move(new_dir, dest)
    except Exception:
        if old and not os.path.isdir(dest):
            try:
                os.rename(old, dest)  # roll the old version back
            except OSError:
                pass
        raise
    # keep the previous version as *.old — a one-step offline rollback if a new
    # platform-tools/scrcpy release turns out to be broken (it's replaced on the
    # next swap, so at most one extra copy sits on disk)


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
    path to the cached ``adb`` executable.

    The archive is extracted to a STAGING directory and swapped in atomically,
    with the adb server stopped first — extracting straight over a live adb.exe
    fails on Windows (the running server locks its own file), and with the old
    ``ignore_errors`` delete that failure was silent.

    The versioned archive named in Google's ``repository2-3.xml`` is preferred
    over the ``-latest-`` alias, because only that one comes with a published
    size and checksum to verify the download against before the swap."""
    existing = managed_adb()
    if existing and not force:
        return existing
    archive = latest_adb_archive()
    url = archive.get("url") or PLATFORM_TOOLS_URLS[_os_key()]
    if not archive.get("url"):
        archive = {}  # the alias isn't the artifact the manifest describes
        _log.warning(
            "Could not read Google's platform-tools manifest; downloading the "
            "'latest' alias with no published checksum to verify it against."
        )
    _ensure_tools_dir()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "platform-tools.zip")
        _download(url, zip_path, on_progress)
        _check_zip(zip_path)  # a truncated download must fail BEFORE the swap
        _verify_platform_tools(zip_path, archive)
        # the zip contains a top-level "platform-tools/" folder
        staging = os.path.join(tmp, "new")
        _extract_zip(zip_path, staging)
        new_dir = os.path.join(staging, "platform-tools")
        if not os.path.isfile(os.path.join(new_dir, _exe("adb"))):
            raise ADBNotFoundError(
                "platform-tools downloaded but adb was not "
                "found in the archive (unexpected layout)."
            )
        if existing:
            _kill_adb_server(existing)  # unlock adb.exe before the swap
        _swap_dir(new_dir, adb_dir())
    adb = managed_adb()
    if not adb:
        raise ADBNotFoundError(
            "platform-tools downloaded but adb was not found after install (unexpected layout)."
        )
    _chmod_x(adb)
    _sync_scrcpy_adb()
    return adb


def _release_cache_path() -> str:
    return os.path.join(tools_dir(), ".scrcpy-release.json")


def _github_release(timeout: float = 30) -> tuple:
    """``(release_json, authoritative)`` for scrcpy's latest release.

    **ETag caching**: repeated checks send If-None-Match and reuse the cached
    body on 304, so the unauthenticated GitHub rate limit (60 req/h/IP) stops
    making update checks randomly fail.

    *authoritative* is True for a fresh 200 **and** for a 304 (GitHub confirmed
    the cached body is still current). It is False when the body is a STALE
    fallback used because the request failed — offline, rate-limited, 5xx.
    Conflating the two reported "up to date" for a check that never happened,
    and meant the `unknown` branch could never fire for scrcpy."""
    cache_path = _release_cache_path()
    cached = None
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cached = json.load(fh)
    except Exception:
        pass
    headers = dict(_UA)
    if cached and cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    req = urllib.request.Request(SCRCPY_RELEASES_API, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
            etag = resp.headers.get("ETag") or ""
        try:
            _ensure_tools_dir()
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump({"etag": etag, "data": data}, fh)
        except Exception:
            pass
        return data, True
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and cached and cached.get("data"):
            return cached["data"], True  # unchanged since last time
        if cached and cached.get("data"):
            return cached["data"], False  # rate-limited etc. — stale cache
        raise
    except Exception:
        if cached and cached.get("data"):
            return cached["data"], False
        raise


def _github_release_json(timeout: float = 30) -> dict:
    """The scrcpy latest-release JSON, cached copy included (see
    :func:`_github_release`). Use it where a stale body is still useful — for
    picking a download asset — never to answer "is this up to date?"."""
    return _github_release(timeout)[0]


def _scrcpy_assets() -> tuple:
    """(zip_url, zip_name, sums_url|None) for the right Windows scrcpy asset.
    *sums_url* points at the release's SHA256SUMS.txt when it publishes one."""
    if not scrcpy_download_supported():
        raise ADBNotFoundError(
            "Prebuilt scrcpy is downloaded only on Windows. On macOS use "
            "'brew install scrcpy'; on Linux use 'apt install scrcpy' (or your "
            "distro's package)."
        )
    is64 = platform.machine().endswith("64") or sys.maxsize > 2**32
    want = "win64" if is64 else "win32"
    data = _github_release_json()
    assets = data.get("assets", [])
    sums = next(
        (a["browser_download_url"] for a in assets if a["name"].upper().startswith("SHA256SUMS")),
        None,
    )
    for a in assets:
        if a["name"].endswith(".zip") and want in a["name"]:
            return a["browser_download_url"], a["name"], sums
    for a in assets:  # fall back to any windows zip
        if a["name"].endswith(".zip") and "win" in a["name"]:
            return a["browser_download_url"], a["name"], sums
    raise ADBNotFoundError("No suitable scrcpy Windows release asset was found.")


def _verify_sha256(path: str, name: str, sums_url: str) -> None:
    """Check *path* against the published SHA256SUMS.txt entry for *name*.
    Best-effort: if the sums file can't be fetched or has no entry, skip; a
    MISMATCH always raises (corrupt or tampered download)."""
    import hashlib

    try:
        req = urllib.request.Request(sums_url, headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            sums = resp.read().decode("utf-8", "replace")
    except Exception:
        return  # can't fetch the sums — skip check
    expected = None
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            expected = parts[0].lower()
            break
    if not expected:
        return
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest().lower() != expected:
        raise ADBError(
            f"SHA-256 mismatch for {name} — the download is corrupt "
            f"or tampered with; nothing was installed. Try again."
        )


def _file_digest(path: str, algorithm: str):
    import hashlib

    h = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _verify_platform_tools(path: str, archive: dict) -> None:
    """Check a downloaded platform-tools zip against Google's own manifest.

    adb used to be installed on a CRC check alone while scrcpy was verified
    against the release's SHA256SUMS, so a mirror or proxy serving different
    bytes was accepted. A MISMATCH always raises; when the manifest published no
    checksum the size is still checked and the gap is LOGGED rather than
    silently skipped."""
    size = archive.get("size")
    actual = os.path.getsize(path)
    if size and actual != size:
        raise ADBError(
            f"platform-tools download is {actual} bytes, but Google's manifest "
            f"publishes {size}; nothing was installed. Try again."
        )
    digest, kind = archive.get("checksum"), archive.get("checksum_type") or "sha1"
    if not digest:
        _log.warning(
            "Google published no checksum for platform-tools %s; verified the "
            "archive's size (%s bytes) and CRC only.",
            archive.get("version") or "?",
            actual,
        )
        return
    try:
        got = _file_digest(path, kind)
    except (ValueError, TypeError):
        _log.warning("Google published an unsupported %s checksum; size/CRC only.", kind)
        return
    if got != digest:
        raise ADBError(
            f"{kind.upper()} mismatch for the platform-tools download (expected "
            f"{digest}, got {got}) — it is corrupt or tampered with; nothing was "
            "installed. Try again."
        )


def _check_zip(path: str) -> None:
    """CRC-check the whole archive so a truncated download fails BEFORE any
    swap, not halfway through extraction."""
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
    except zipfile.BadZipFile as exc:
        raise ADBError(f"downloaded archive is not a valid zip: {exc}") from exc
    if bad:
        raise ADBError(
            f"downloaded archive is corrupt (bad CRC: {bad}); nothing was installed. Try again."
        )


def download_scrcpy(*, force: bool = False, on_progress=None) -> str:
    """Download + extract the latest scrcpy (Windows prebuilt) into the cache.
    Returns the path to the cached ``scrcpy`` executable."""
    existing = managed_scrcpy()
    if existing and not force:
        return existing
    url, asset_name, sums_url = _scrcpy_assets()
    _ensure_tools_dir()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "scrcpy.zip")
        _download(url, zip_path, on_progress)
        _check_zip(zip_path)
        if sums_url:
            _verify_sha256(zip_path, asset_name, sums_url)
        staging = os.path.join(tmp, "scrcpy-new")
        _extract_zip(zip_path, tmp, strip_top_to=staging)
        if not os.path.isfile(os.path.join(staging, _exe("scrcpy"))):
            raise ADBNotFoundError(
                "scrcpy downloaded but scrcpy.exe was not found in the archive (unexpected layout)."
            )
        if existing:
            # the scrcpy folder bundles its own adb.exe, which may be running
            # as the server — stop it so the swap isn't blocked by a file lock
            _kill_adb_server()
        _swap_dir(staging, scrcpy_dir())
    scr = managed_scrcpy()
    if not scr:
        raise ADBNotFoundError(
            "scrcpy downloaded but scrcpy.exe was not found after install (unexpected layout)."
        )
    _sync_scrcpy_adb()
    return scr


def _stamp_path() -> str:
    return os.path.join(tools_dir(), ".stamp")


def _read_stamp() -> str:
    """The TurboADB version whose tools were last ensured (see
    :func:`ensure_tools`, which skips its work when this matches)."""
    try:
        with open(_stamp_path(), encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def _write_stamp(version: str) -> None:
    try:
        _ensure_tools_dir()
        with open(_stamp_path(), "w", encoding="utf-8") as fh:
            fh.write(version)
    except Exception:
        pass


def _pkg_version() -> str:
    from .update import current_version

    return current_version()


def auto_fetch_enabled() -> bool:
    """Auto-download is ON by default; disable with TURBOADB_AUTO_FETCH=0
    (handy for offline / CI / locked-down environments)."""
    v = os.environ.get("TURBOADB_AUTO_FETCH")
    if v is None:
        return True
    return v.strip().lower() not in ("0", "false", "no", "off", "")


_ensured = False
_ENSURE_LOCK = threading.Lock()


def ensure_tools(*, on_progress=None, notify=None, scrcpy: bool = True) -> dict:
    """Make sure the managed cache has adb (and, where a prebuilt exists,
    scrcpy). Runs at most once per process and downloads ONLY tools that are
    missing from the cache — it never upgrades an existing copy, because
    replacing platform-tools means stopping the running adb server (which drops
    every device session). Upgrades go through :func:`upgrade_tools`.

    Best-effort and never raises — if the network is down, detection simply
    falls back to whatever's already on PATH. Thread-safe: concurrent callers
    wait for the first one instead of racing the same download.

    A ``.stamp`` naming the TurboADB version whose tools were last ensured short-
    circuits the whole function when everything it would check is already in the
    cache — that is what :func:`_write_stamp` (also written by the GUI's startup
    tool check) exists for.

    Disable entirely with the ``TURBOADB_AUTO_FETCH=0`` environment variable.
    """
    global _ensured

    def norm(note, errors=None):
        return {
            "note": note,
            "adb": managed_adb(),
            "scrcpy": managed_scrcpy(),
            "errors": errors or {},
        }

    with _ENSURE_LOCK:
        if _ensured:
            return norm("already-ensured")
        _ensured = True
        if not auto_fetch_enabled():
            return norm("disabled")
        # The stamp only short-circuits when the tools it covers are still
        # present, so deleting one from the cache still re-fetches it.
        stamp_covers_scrcpy = bool(scrcpy) and scrcpy_download_supported()
        if (
            _read_stamp() == _pkg_version()
            and managed_adb()
            and (managed_scrcpy() if stamp_covers_scrcpy else True)
        ):
            return norm("stamped")
        errors = {}
        note = "up-to-date"
        try:
            want_adb = managed_adb() is None
            # Requesting scrcpy where no prebuilt exists always "failed", so the
            # stamp was never written and every command retried the network.
            want_scrcpy = bool(scrcpy) and scrcpy_download_supported() and managed_scrcpy() is None
            if want_adb or want_scrcpy:
                note = "installed"
                if notify:
                    what = " + ".join(
                        n for n, w in (("platform-tools", want_adb), ("scrcpy", want_scrcpy)) if w
                    )
                    notify(
                        f"Downloading latest {what} (one-time; set TURBOADB_AUTO_FETCH=0 to skip)…"
                    )
                res = fetch_tools(
                    adb=want_adb, scrcpy=want_scrcpy, force=False, on_progress=on_progress
                )
                errors = dict(res.get("errors", {}))
            # only mark this TurboADB version when nothing FAILED
            if managed_adb() and not errors:
                _write_stamp(_pkg_version())
            _sync_scrcpy_adb()
        except Exception as exc:  # never let auto-fetch break a real command
            errors["ensure"] = str(exc)
        return norm(note, errors)


def _sync_scrcpy_adb() -> None:
    """Ensure scrcpy's bundled adb on Windows matches platform-tools adb to eliminate
    daemon killing conflicts. Called after every managed download, not only from
    :func:`ensure_tools`."""
    if os.name != "nt":
        return
    m_adb = managed_adb()
    s_dir = scrcpy_dir()
    if not m_adb or not os.path.isdir(s_dir):
        return
    pt_dir = os.path.dirname(m_adb)
    for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
        src = os.path.join(pt_dir, name)
        dst = os.path.join(s_dir, name)
        if os.path.exists(src):
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# version checks ("only download if there's a newer version")
# --------------------------------------------------------------------------- #
def installed_adb_version(adb_path: str | None = None) -> str | None:
    try:
        exe = adb_path or find_adb()
        m = re.search(r"Version\s+(\d+\.\d+\.\d+)", _adb_version_output(exe))
        return m.group(1) if m else None
    except Exception:
        return None


def _parse_platform_tools_manifest(xml: str, host_os: str) -> dict:
    """Pull everything we need out of Google's ``repository2-3.xml`` in ONE pass:
    ``{"version", "url", "size", "checksum", "checksum_type"}`` (keys absent when
    the manifest doesn't publish them) for the platform-tools archive matching
    *host_os*. Returns ``{}`` when the entry can't be found at all."""
    out: dict = {}
    pkg = re.search(
        r"<[\w:]*remotePackage[^>]*path=\"platform-tools\".*?</[\w:]*remotePackage>", xml, re.S
    )
    # Fall back to the old, looser revision-only match if the schema moves on:
    # losing the checksum is bad, losing the version check entirely is worse.
    block = pkg.group(0) if pkg else ""
    if not block:
        loose = re.search(r'path="platform-tools".*?</revision>', xml, re.S)
        block_for_rev = loose.group(0) if loose else ""
    else:
        block_for_rev = block
    rev = re.search(
        r"<major>(\d+)</major>\s*<minor>(\d+)</minor>\s*<micro>(\d+)</micro>", block_for_rev
    )
    if rev:
        out["version"] = ".".join(rev.groups())
    if not block:
        return out
    archives = re.findall(r"<[\w:]*archive>.*?</[\w:]*archive>", block, re.S)
    chosen = None
    for archive in archives:
        m = re.search(r"<[\w:]*host-os>([\w-]+)</[\w:]*host-os>", archive)
        if m and m.group(1).strip().lower() == host_os:
            chosen = archive
            break
    if chosen is None and len(archives) == 1:
        chosen = archives[0]  # a single, OS-independent archive
    if chosen is None:
        return out
    url = re.search(r"<[\w:]*url>([^<]+)</[\w:]*url>", chosen)
    size = re.search(r"<[\w:]*size>(\d+)</[\w:]*size>", chosen)
    checksum = re.search(r"<[\w:]*checksum(?:\s+type=\"([\w-]+)\")?\s*>([0-9a-fA-F]+)<", chosen)
    if url:
        href = url.group(1).strip()
        out["url"] = href if "://" in href else PLATFORM_TOOLS_BASE + href.lstrip("/")
    if size:
        out["size"] = int(size.group(1))
    if checksum:
        out["checksum_type"] = (checksum.group(1) or "sha1").lower()
        out["checksum"] = checksum.group(2).lower()
    return out


def latest_adb_archive() -> dict:
    """Google's published platform-tools archive for THIS OS: ``{"version",
    "url", "size", "checksum", "checksum_type"}``, or ``{}`` when the manifest
    is unreachable. The same fetch answers both "is there a newer adb?" and
    "what should the downloaded zip hash to?"."""
    try:
        req = urllib.request.Request(PLATFORM_TOOLS_REPO_XML, headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml = resp.read().decode("utf-8", "replace")
    except Exception:
        return {}
    try:
        return _parse_platform_tools_manifest(xml, _MANIFEST_HOST_OS[_os_key()])
    except Exception:
        return {}


def latest_adb_version() -> str | None:
    return latest_adb_archive().get("version")


def installed_scrcpy_version(scrcpy_path: str | None = None) -> str | None:
    try:
        exe = scrcpy_path or find_scrcpy()
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW
        )
        m = re.search(r"scrcpy\s+(\d+\.\d+(?:\.\d+)?)", out.stdout or out.stderr or "")
        return m.group(1) if m else None
    except Exception:
        return None


def latest_scrcpy_version() -> str | None:
    """The newest published scrcpy version, or None when it could NOT be
    determined (offline, GitHub rate limit). A stale cached release body is not
    an answer — reporting it as one turned a failed check into "up to date"."""
    try:
        data, authoritative = _github_release()  # ETag-cached, rate-limit friendly
    except Exception:
        return None
    if not authoritative:
        return None
    tag = (data.get("tag_name") or "").lstrip("vV")
    return tag or None


_vtuple = parse_version


def _decide(installed: str | None, latest: str | None) -> bool | None:
    """True = upgrade available; False = up to date; None = couldn't determine.
    Compares numerically so a formatting difference (or an installed version
    NEWER than the published one) never triggers a pointless re-download."""
    if installed is None:
        return True  # not installed -> "upgrade" means install
    if latest is None:
        return None  # can't reach the version source
    return _vtuple(latest) > _vtuple(installed)


def check_updates() -> dict:
    """Compare installed adb/scrcpy against the latest available. Returns
    ``{"adb": {"installed","latest","upgrade","path","system","supported"},
    "scrcpy": {...}}`` where ``upgrade`` is True / False / None (unknown) and
    ``supported`` says whether TurboADB can download that tool on this OS.
    Makes no changes.

    Only the MANAGED copy (the one downloads replace, and the one TurboADB
    prefers) is version-checked. ``installed`` is None when there is no managed
    copy — so the decision is "install", not "up to date". Passing the missing
    path straight to ``installed_*_version`` made it fall back to the PATH tool,
    which reported everything current while the managed cache stayed EMPTY and
    ``gui_adb_path()`` kept returning None forever. The PATH/system version is
    reported separately as ``system``."""
    a_path, s_path = managed_adb(), managed_scrcpy()
    ai = installed_adb_version(a_path) if a_path else None
    si = installed_scrcpy_version(s_path) if s_path else None
    al, sl = latest_adb_version(), latest_scrcpy_version()
    return {
        "adb": {
            "installed": ai,
            "latest": al,
            "upgrade": _decide(ai, al),
            "path": a_path,
            # what a plain `adb` on this machine resolves to, for context only
            "system": None if a_path else installed_adb_version(),
            "supported": True,
        },
        "scrcpy": {
            "installed": si,
            "latest": sl,
            "upgrade": _decide(si, sl),
            "path": s_path,
            "system": None if s_path else installed_scrcpy_version(),
            "supported": scrcpy_download_supported(),
        },
    }


def upgrade_tools(*, on_progress=None, notify=None, checks: dict | None = None) -> dict:
    """Check for newer adb/scrcpy and download **only** the ones that are
    outdated (or missing). If everything's current, downloads nothing. Returns a
    summary dict including the version check and what was updated.

    Pass *checks* (a :func:`check_updates` result) to reuse a check that was
    already made instead of querying the version sources twice. scrcpy is only
    considered where a prebuilt download exists (Windows)."""
    if checks is None:
        checks = check_updates()
    updated = {}
    errors = {}
    tools = ["adb", "scrcpy"] if scrcpy_download_supported() else ["adb"]
    want_adb = "adb" in tools and checks["adb"]["upgrade"] is True
    want_scrcpy = "scrcpy" in tools and checks["scrcpy"]["upgrade"] is True
    unknown = [t for t in tools if checks[t]["upgrade"] is None]
    if not want_adb and not want_scrcpy:
        # 'unknown' (couldn't reach the version source / GitHub rate limit) is
        # NOT the same as up to date — reporting it as such hid failed checks
        if unknown and notify:
            notify(
                "couldn't determine the latest "
                + "/".join(unknown)
                + " version (network / rate limit) — nothing was changed; "
                "try again in a while"
            )
        return {
            "checks": checks,
            "updated": {},
            "errors": {},
            "up_to_date": not unknown,
            "unknown": unknown,
        }
    if notify:
        bits = []
        if want_adb:
            bits.append(f"adb {checks['adb']['installed']}→{checks['adb']['latest']}")
        if want_scrcpy:
            bits.append(f"scrcpy {checks['scrcpy']['installed']}→{checks['scrcpy']['latest']}")
        notify("Updating " + ", ".join(bits) + " …")
    if want_adb:
        try:
            updated["adb"] = download_platform_tools(force=True, on_progress=on_progress)
        except Exception as exc:
            errors["adb"] = str(exc)
    if want_scrcpy:
        try:
            updated["scrcpy"] = download_scrcpy(force=True, on_progress=on_progress)
        except Exception as exc:
            errors["scrcpy"] = str(exc)
    return {
        "checks": checks,
        "updated": updated,
        "errors": errors,
        "up_to_date": False,
        "unknown": unknown,
    }


def fetch_tools(
    *, adb: bool = True, scrcpy: bool = True, force: bool = False, on_progress=None, on_stage=None
) -> dict:
    """Download whatever's requested into the cache. Returns
    ``{"adb": path|None, "scrcpy": path|None, "errors": {...}}``.

    *on_stage* (optional) is called with a short label ("adb (1/2)", …) as each
    tool starts, so a progress UI can say WHAT is downloading instead of its
    bar appearing to jump backwards between the two 0-100 runs.

    scrcpy bundles its own adb on Windows; downloading scrcpy alone is enough to
    get both, but fetching platform-tools gives you the latest standalone adb.
    """

    def stage(label):
        if on_stage:
            try:
                on_stage(label)
            except Exception:
                pass

    total = int(adb) + int(scrcpy)
    result = {"adb": None, "scrcpy": None, "errors": {}}
    if adb:
        stage(f"platform-tools / adb (1/{total})")
        try:
            result["adb"] = download_platform_tools(force=force, on_progress=on_progress)
        except Exception as exc:
            result["errors"]["adb"] = str(exc)
    if scrcpy:
        stage(f"scrcpy ({total}/{total})")
        try:
            result["scrcpy"] = download_scrcpy(force=force, on_progress=on_progress)
        except Exception as exc:
            result["errors"]["scrcpy"] = str(exc)
    return result
