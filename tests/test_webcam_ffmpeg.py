"""The webcam's ffmpeg: the local camera list and the cached copy's check.

- ffmpeg writes DirectShow camera names in UTF-8, and a name goes back to it
  as ``video=<name>``. Read with the Windows code page, "Caméra intégrée"
  came back garbled and could not be opened, and a byte that code page lacks
  lost the whole list.
- A cached ffmpeg that failed its checksum but could not be deleted (another
  camera stream runs it) was adopted as an old copy with no record, its hash
  then recorded as trusted.

No ffmpeg, camera or network is used.
"""

import os
import subprocess
import sys

import pytest


def test_a_camera_is_listed_under_its_own_name(monkeypatch):
    from turboadb.gui import ffmpeg_tools

    listing = (
        '[dshow @ 000001] "Caméra intégrée" (video)\n'
        '[dshow @ 000001]   Alternative name "@device_pnp_\\\\?\\usb#vid_0bda"\n'
        '[dshow @ 000001] "Álvaro\'s USB cam" (video)\n'
        '[dshow @ 000001] "Microphone (Réaltek)" (audio)\n'
    )
    script = "import sys; sys.stderr.buffer.write(sys.argv[1].encode('utf-8'))"
    real_run = subprocess.run

    def ffmpeg(argv, **kwargs):
        return real_run([sys.executable, "-c", script, listing], **kwargs)

    monkeypatch.setattr(ffmpeg_tools.subprocess, "run", ffmpeg)
    assert ffmpeg_tools.list_local_cameras("ffmpeg.exe") == ["Caméra intégrée", "Álvaro's USB cam"]


def test_a_cached_ffmpeg_that_fails_its_check_while_in_use_is_not_trusted(tmp_path, monkeypatch):
    """On Windows a running exe cannot be deleted: the copy that just failed
    its check was then adopted as an old copy, its hash recorded as trusted."""
    from turboadb.gui import ffmpeg_tools

    cache = tmp_path / "ffmpeg"
    cache.mkdir()
    exe = cache / "ffmpeg.exe"
    exe.write_bytes(b"MZ-changed-underneath")
    record = cache / "ffmpeg.exe.sha256"
    record.write_text("0" * 64 + "\n")  # the download's hash: no longer matches
    monkeypatch.setattr(ffmpeg_tools, "_CACHE", str(cache))
    monkeypatch.setattr(ffmpeg_tools, "_VERIFIED", {})
    monkeypatch.delenv("TURBOADB_FFMPEG_SHA256", raising=False)
    real_remove = ffmpeg_tools._remove_quietly

    def in_use(path):  # another camera stream runs this exe
        if os.path.normcase(path) != os.path.normcase(str(exe)):
            real_remove(path)

    monkeypatch.setattr(ffmpeg_tools, "_remove_quietly", in_use)
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError) as err:
        ffmpeg_tools.find_local_ffmpeg()
    assert "can't be replaced while it runs" in str(err.value)
    assert record.read_text().strip() == "0" * 64  # its record is kept...
    with pytest.raises(RuntimeError):
        ffmpeg_tools.find_local_ffmpeg()  # ...so it is never adopted later

    system = str(tmp_path / "bin" / "ffmpeg.exe")
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda *_a, **_k: system)
    assert ffmpeg_tools.find_local_ffmpeg() == system  # an ffmpeg on PATH still works
