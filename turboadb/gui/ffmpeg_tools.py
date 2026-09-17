"""Get hold of ffmpeg for the host-webcam feature, and enumerate / capture local
DirectShow cameras on Windows.

ffmpeg is too big to bundle in the PyPI wheel (it would push us over the 100 MB
limit), so we fetch a Windows build once from a public GitHub release and cache it
under ``~/.turboadb/ffmpeg/``. A user-supplied ffmpeg (Settings → ffmpeg path, or
one already on PATH) overrides the download for fully offline setups.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from typing import Optional

from ..config import user_path
from ..tools import NO_WINDOW as _NO_WINDOW

_log = logging.getLogger(__name__)

_CACHE = user_path("ffmpeg")
# BtbN's FFmpeg-Builds "latest" release — a stable, public Windows build URL.
# It is a rolling asset, so TURBOADB_FFMPEG_URL can pin a dated build
# (…/releases/download/autobuild-YYYY-MM-DD-hh-mm/…) and TURBOADB_FFMPEG_SHA256
# the exact binary that build must extract to.
_FFMPEG_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)
_URL_ENV = "TURBOADB_FFMPEG_URL"
_SHA_ENV = "TURBOADB_FFMPEG_SHA256"
# The recorded checksum lives beside the binary, so a cached ffmpeg that changed
# on disk is noticed instead of being executed forever on trust.
_SHA_SUFFIX = ".sha256"


def ffmpeg_url() -> str:
    """The build to download; ``TURBOADB_FFMPEG_URL`` pins a specific release."""
    return (os.environ.get(_URL_ENV) or "").strip() or _FFMPEG_URL


def expected_sha256() -> str:
    """A pinned binary hash from ``TURBOADB_FFMPEG_SHA256`` (lowercase hex), or ""."""
    return (os.environ.get(_SHA_ENV) or "").strip().lower()


class DownloadCancelled(RuntimeError):
    """The caller's ``should_cancel()`` asked to abandon the one-time download."""


def parse_dshow_devices(text: str) -> list:
    """Parse camera names out of ffmpeg's ``-list_devices`` output.

    DirectShow's output changed in current FFmpeg builds: a real camera may be
    reported as ``"Camera" (none)`` with no ``DirectShow video devices`` header.
    Treat that form as video while still excluding explicit audio devices and
    ``Alternative name`` entries.
    """
    cams, in_video = [], False
    for line in (text or "").splitlines():
        low = line.lower()
        if "directshow video devices" in low:
            in_video = True
            continue
        if "directshow audio devices" in low:
            in_video = False
            continue
        if "alternative name" in low:
            continue
        m = re.search(r'"([^"]+)"', line)
        if not m:
            continue
        name = m.group(1)
        if name.startswith("@device"):  # alt-name device path, skip
            continue
        if "(audio)" in low:
            continue
        if "(video)" in low or "(none)" in low or in_video:
            cams.append(name)
    seen, out = set(), []
    for c in cams:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def cached_ffmpeg() -> str | None:
    """Return an ffmpeg we can use without downloading: an explicit Settings
    path, our cache, or an ffmpeg already on PATH."""
    try:
        from . import settings as _s

        manual = (_s.get("ffmpeg_path") or "").strip()
        if manual and os.path.exists(manual):
            return manual
    except Exception:
        pass
    p = os.path.join(_CACHE, "ffmpeg.exe")
    if os.path.exists(p):
        return p
    return shutil.which("ffmpeg")  # already installed system-wide?


def _auto_download_supported() -> bool:
    """The BtbN build we fetch is a Windows binary."""
    return os.name == "nt"


# One download at a time inside this process; a second process is tolerated by
# unique temp names + an atomic os.replace (see ensure_local_ffmpeg).
_DOWNLOAD_LOCK = threading.Lock()
_PART_SUFFIX = ".part"
_STALE_PART_S = 24 * 3600
# Ceilings for the one-time fetch. urlopen's timeout is per read, so a throttled
# server could hold the camera page for hours without either of these.
_DOWNLOAD_DEADLINE_S = 30 * 60
# Hashing 160 MB takes a moment; remember the copies already checked in this
# process, keyed by what would change if the file were replaced.
_VERIFIED = {}


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log.debug("couldn't remove %s: %s", path, exc)


def _clean_stale_parts(folder: str) -> None:
    """Drop temp files left behind by a crashed/killed earlier download.  Only
    old ones: a young ``.part`` may belong to another process downloading now."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    now = time.time()
    for name in names:
        if not (name.startswith("ffmpeg-") and name.endswith(_PART_SUFFIX)):
            continue
        path = os.path.join(folder, name)
        try:
            if now - os.path.getmtime(path) > _STALE_PART_S:
                _remove_quietly(path)
        except OSError:
            pass


def _temp_path(folder: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix="ffmpeg-", suffix=suffix + _PART_SUFFIX, dir=folder)
    os.close(fd)
    return path


def _file_key(path: str):
    """What changes when a file is replaced: (size, mtime)."""
    st = os.stat(path)
    return (st.st_size, st.st_mtime)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _recorded_hash(exe: str) -> str:
    """The SHA-256 written beside *exe* by a previous download, or ""."""
    try:
        with open(exe + _SHA_SUFFIX, "r", encoding="ascii") as fh:
            return fh.read().split()[0].strip().lower()
    except (OSError, IndexError, UnicodeDecodeError):
        return ""


def _record_hash(exe: str, digest: str | None = None) -> str:
    """Write *exe*'s SHA-256 next to it and remember it as verified."""
    digest = (digest or _sha256_file(exe)).lower()
    try:
        with open(exe + _SHA_SUFFIX, "w", encoding="ascii") as fh:
            fh.write(digest + "\n")
    except OSError as exc:  # a read-only cache still runs, just unverifiable
        _log.debug("couldn't record the ffmpeg checksum: %s", exc)
    try:
        _VERIFIED[exe] = _file_key(exe)
    except OSError:
        pass
    return digest


def verify_cached(exe: str, log=lambda m: None) -> bool:
    """True when *exe* still matches the checksum recorded beside it, and the
    pinned ``TURBOADB_FFMPEG_SHA256`` when one is set.

    A copy left by an older TurboADB has no record yet; it is adopted (its hash
    is written) rather than re-downloaded, unless a pin says otherwise.
    """
    pinned = expected_sha256()
    recorded = _recorded_hash(exe)
    try:
        if _VERIFIED.get(exe) == _file_key(exe):  # unchanged since the last check
            return True
        if not pinned and not recorded:
            log(f"Recorded the cached ffmpeg checksum (SHA-256 {_record_hash(exe)[:16]}…)")
            return True
        digest = _sha256_file(exe)
    except OSError as exc:
        _log.warning("couldn't check the cached ffmpeg (%s): %s", exe, exc)
        return False
    if pinned and digest != pinned:
        log("The cached ffmpeg does not match TURBOADB_FFMPEG_SHA256 — fetching it again.")
        _log.warning("cached ffmpeg %s is %s, pinned %s", exe, digest, pinned)
        return False
    if recorded and digest != recorded:
        log("The cached ffmpeg changed since it was downloaded — fetching it again.")
        _log.warning("cached ffmpeg %s is %s, recorded %s", exe, digest, recorded)
        return False
    _record_hash(exe, digest)
    return True


def _usable_cached(log) -> Optional[str]:
    """:func:`cached_ffmpeg` with an integrity check on OUR downloaded copy.

    A user-supplied path or an ffmpeg on PATH is theirs to manage and is used as
    is; the managed copy is dropped when it no longer matches its checksum, so
    the next call replaces it.
    """
    path = cached_ffmpeg()
    managed = os.path.join(_CACHE, "ffmpeg.exe")
    if not path or os.path.normcase(path) != os.path.normcase(managed):
        if not os.path.exists(managed):
            _remove_quietly(managed + _SHA_SUFFIX)  # a record without its binary
        return path
    if verify_cached(path, log):
        return path
    _remove_quietly(path)
    _remove_quietly(path + _SHA_SUFFIX)
    _VERIFIED.pop(path, None)
    return None


def _download_zip(url: str, dest: str, log, should_cancel=None) -> None:
    """Stream *url* into *dest* (never held in memory).

    ``urlopen``'s timeout only bounds a single read — a dead connection fails
    there, but a trickling one never does — so the loop enforces an overall
    elapsed ceiling as well, and honours *should_cancel()* between chunks. A
    throttled server must not hold the main window indefinitely.
    """
    def cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    if cancelled():
        raise DownloadCancelled("cancelled before the download started")
    req = urllib.request.Request(url, headers={"User-Agent": "turboadb"})
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as fh:
        total = int(r.headers.get("Content-Length") or 0)
        got = last = 0
        while True:
            chunk = r.read(1024 * 256)
            if cancelled():
                raise DownloadCancelled("cancelled")
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if time.monotonic() - start > _DOWNLOAD_DEADLINE_S:
                raise OSError(
                    f"still downloading after {_DOWNLOAD_DEADLINE_S // 60} minutes "
                    f"({got // (1024 * 1024)} MB so far)"
                )
            mb = got // (1024 * 1024)
            if mb >= last + 5:  # report every ~5 MB
                last = mb
                pct = f" ({got * 100 // total}%)" if total else ""
                log(f"Downloading ffmpeg… {mb} MB{pct}")
    if total and got != total:
        raise OSError(f"download truncated ({got} of {total} bytes)")


def _install_from_zip(zip_path: str, out: str) -> None:
    """Stream ``*/bin/ffmpeg.exe`` out of *zip_path* into a temp file next to
    *out*, then atomically move it into place.  A failure part-way (disk full,
    corrupt zip, killed process) never leaves a truncated ffmpeg.exe that would
    be "found" and cached forever.  Reading the member to EOF makes zipfile
    verify its CRC-32."""
    folder = os.path.dirname(out) or "."
    tmp = _temp_path(folder, ".exe")
    try:
        with zipfile.ZipFile(zip_path) as z:
            member = next(
                (i for i in z.infolist() if i.filename.endswith("/bin/ffmpeg.exe")), None
            )
            if member is None:
                raise RuntimeError("the download doesn't contain bin/ffmpeg.exe")
            with z.open(member) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        if os.path.getsize(tmp) != member.file_size:
            raise RuntimeError("extracted ffmpeg.exe has the wrong size")
        try:
            os.replace(tmp, out)
        except PermissionError:
            # Windows: another TurboADB process installed (and is running) the
            # same exe a moment ago — theirs is complete, so use it.
            if not os.path.exists(out):
                raise
    finally:
        _remove_quietly(tmp)


def ensure_local_ffmpeg(log=lambda m: None, should_cancel=None) -> str:
    """Return a usable ffmpeg(.exe), downloading + caching it on first use.
    Raises RuntimeError if it can't be obtained, or :class:`DownloadCancelled`
    when *should_cancel()* asked to stop.

    Integrity: BtbN's "latest" asset is rebuilt continuously and has no stable
    published checksum, so the download relies on HTTPS plus the zip's CRC-32,
    and the SHA-256 of the binary we install is logged and recorded beside it.
    Every later reuse re-checks that record, and ``TURBOADB_FFMPEG_URL`` /
    ``TURBOADB_FFMPEG_SHA256`` pin an exact build for locked-down setups.
    """
    have = _usable_cached(log)
    if have:
        return have
    if not _auto_download_supported():
        raise RuntimeError(
            "ffmpeg wasn't found. Install it (so it's on PATH) or set its path in "
            "Settings → ffmpeg path. (Auto-download is Windows-only.)"
        )
    with _DOWNLOAD_LOCK:
        have = _usable_cached(log)  # another thread finished it while we waited
        if have:
            return have
        os.makedirs(_CACHE, exist_ok=True)
        _clean_stale_parts(_CACHE)
        out = os.path.join(_CACHE, "ffmpeg.exe")
        zip_path = _temp_path(_CACHE, ".zip")
        try:
            log("Downloading ffmpeg (one-time, ~160 MB — please wait)…")
            try:
                _download_zip(ffmpeg_url(), zip_path, log, should_cancel)
            except DownloadCancelled:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"Couldn't download ffmpeg ({exc}). Install ffmpeg (so it's on PATH) "
                    f"or set its path in Settings → ffmpeg path."
                )
            log("Extracting ffmpeg…")
            try:
                _install_from_zip(zip_path, out)
            except Exception as exc:
                raise RuntimeError(f"Couldn't extract ffmpeg from the download: {exc}")
        finally:
            _remove_quietly(zip_path)
    if not os.path.exists(out):
        raise RuntimeError("ffmpeg.exe not found after extraction.")
    try:
        digest = _sha256_file(out)
    except OSError as exc:
        raise RuntimeError(f"Couldn't read the installed ffmpeg to check it: {exc}")
    pinned = expected_sha256()
    if pinned and digest != pinned:
        _remove_quietly(out)
        raise RuntimeError(
            f"the downloaded ffmpeg is SHA-256 {digest}, not the pinned {pinned}. "
            f"Check {_URL_ENV} / {_SHA_ENV}."
        )
    _record_hash(out, digest)
    _log.info("installed ffmpeg %s (SHA-256 %s)", out, digest)
    log(f"ffmpeg ready (SHA-256 {digest[:16]}…)")
    return out


def list_local_cameras(ffmpeg: str) -> list:
    """List DirectShow cameras on THIS machine (Windows)."""
    try:
        p = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_NO_WINDOW,
        )
        return parse_dshow_devices((p.stdout or "") + "\n" + (p.stderr or ""))
    except (OSError, subprocess.SubprocessError) as exc:
        # surfaces as "No cameras found"; keep the real reason in the log
        _log.warning("listing local cameras with %s failed: %s", ffmpeg, exc)
        return []


def local_capture_args(
    ffmpeg: str,
    camera: str,
    *,
    width: int = 1280,
    height: int = 720,
    fps: Optional[int] = 25,
    quality: int = 6,
) -> list:
    """ffmpeg argv to capture a LOCAL camera and emit MJPEG on stdout, low-delay.

    With ``fps=None`` the DirectShow camera chooses its native frame rate.  The
    UI uses that one-shot fallback when a camera rejects a requested 30 fps.
    """
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-thread_queue_size",
        "512",
        "-f",
        "dshow",
        "-rtbufsize",
        "64M",
    ]
    # DirectShow chooses its default frame rate unless it is asked before
    # opening the input. Output-side ``-r`` alone only duplicates/drops frames
    # and cannot turn a 10-fps capture into a 30-fps one.
    if fps is not None:
        args += ["-framerate", str(int(fps))]
    args += [
        "-i",
        f"video={camera}",
        "-an",
        "-vf",
        f"scale={int(width)}:{int(height)}",
    ]
    if fps is not None:
        args += ["-r", str(int(fps))]
    return args + [
        "-flush_packets",
        "1",
        "-f",
        "mjpeg",
        "-q:v",
        str(int(quality)),
        "-",
    ]
