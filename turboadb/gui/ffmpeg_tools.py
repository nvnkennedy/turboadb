"""Get hold of ffmpeg for the host-webcam feature, and enumerate / capture local
DirectShow cameras on Windows.

ffmpeg is too big to bundle in the PyPI wheel (it would push us over the 100 MB
limit), so we fetch a Windows build once from a public GitHub release and cache it
under ``~/.turboadb/ffmpeg/``. A user-supplied ffmpeg (Settings → ffmpeg path, or
one already on PATH) overrides the download for fully offline setups.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
import subprocess
import urllib.request

_CACHE = os.path.join(os.path.expanduser("~"), ".turboadb", "ffmpeg")
# BtbN's FFmpeg-Builds "latest" release — a stable, public Windows build URL.
_FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
               "ffmpeg-master-latest-win64-gpl.zip")

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def parse_dshow_devices(text: str) -> list:
    """Parse camera names out of ffmpeg's ``-list_devices`` output. Handles both
    the newer ``"Cam" (video)`` lines and the older section-header style
    (``DirectShow video devices`` … ``DirectShow audio devices``)."""
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
        if name.startswith("@device"):       # alt-name device path, skip
            continue
        if "(video)" in low or in_video:
            cams.append(name)
    seen, out = set(), []
    for c in cams:
        if c not in seen:
            seen.add(c); out.append(c)
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
    return shutil.which("ffmpeg")            # already installed system-wide?


def ensure_local_ffmpeg(log=lambda m: None) -> str:
    """Return a usable ffmpeg(.exe), downloading + caching it on first use.
    Raises RuntimeError if it can't be obtained."""
    have = cached_ffmpeg()
    if have:
        return have
    if os.name != "nt":
        raise RuntimeError(
            "ffmpeg wasn't found. Install it (so it's on PATH) or set its path in "
            "Settings → ffmpeg path. (Auto-download is Windows-only.)")
    os.makedirs(_CACHE, exist_ok=True)
    zip_path = os.path.join(_CACHE, "ffmpeg.zip")
    log("Downloading ffmpeg (one-time, ~160 MB — please wait)…")
    try:
        req = urllib.request.Request(_FFMPEG_URL, headers={"User-Agent": "turboadb"})
        with urllib.request.urlopen(req, timeout=60) as r, open(zip_path, "wb") as fh:
            total = int(r.headers.get("Content-Length") or 0)
            got = last = 0
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                mb = got // (1024 * 1024)
                if mb >= last + 5:           # report every ~5 MB
                    last = mb
                    pct = f" ({got * 100 // total}%)" if total else ""
                    log(f"Downloading ffmpeg… {mb} MB{pct}")
    except Exception as exc:
        raise RuntimeError(
            f"Couldn't download ffmpeg ({exc}). Install ffmpeg (so it's on PATH) "
            f"or set its path in Settings → ffmpeg path.")
    log("Extracting ffmpeg…")
    try:
        with zipfile.ZipFile(zip_path) as z:
            member = next(n for n in z.namelist() if n.endswith("/bin/ffmpeg.exe"))
            with z.open(member) as src, \
                    open(os.path.join(_CACHE, "ffmpeg.exe"), "wb") as dst:
                dst.write(src.read())
    except Exception as exc:
        raise RuntimeError(f"Couldn't extract ffmpeg from the download: {exc}")
    finally:
        try:
            os.remove(zip_path)
        except Exception:
            pass
    out = os.path.join(_CACHE, "ffmpeg.exe")
    if not os.path.exists(out):
        raise RuntimeError("ffmpeg.exe not found after extraction.")
    return out


def list_local_cameras(ffmpeg: str) -> list:
    """List DirectShow cameras on THIS machine (Windows)."""
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=_NO_WINDOW)
        return parse_dshow_devices((p.stdout or "") + "\n" + (p.stderr or ""))
    except Exception:
        return []


def local_capture_args(ffmpeg: str, camera: str, *, width: int = 1280,
                       height: int = 720, fps: int = 25, quality: int = 6) -> list:
    """ffmpeg argv to capture a LOCAL camera and emit MJPEG on stdout, low-delay."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-f", "dshow", "-rtbufsize", "16M", "-i", f"video={camera}",
            "-an", "-vf", f"scale={int(width)}:{int(height)}", "-r", str(int(fps)),
            "-f", "mjpeg", "-q:v", str(int(quality)), "-"]
