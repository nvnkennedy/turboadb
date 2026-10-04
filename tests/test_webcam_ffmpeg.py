"""The webcam's ffmpeg: the local camera list and the cached copy's check.

- ffmpeg writes DirectShow camera names in UTF-8, and a name goes back to it
  as ``video=<name>``. Read with the Windows code page, "Caméra intégrée"
  came back garbled and could not be opened, and a byte that code page lacks
  lost the whole list.
- A cached ffmpeg that failed its checksum but could not be deleted (another
  camera stream runs it) was adopted as an old copy with no record, its hash
  then recorded as trusted.
- An ffmpeg "on PATH" was asked of ``shutil.which``, which looks in the
  current folder before PATH on Windows (and for an empty or relative PATH
  entry anywhere): an ``ffmpeg.bat`` lying in the folder TurboADB was started
  from was run for the camera scan.

No ffmpeg, camera or network is used.
"""

import os
import stat
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
    monkeypatch.setattr(ffmpeg_tools, "_on_path", lambda name: None)
    with pytest.raises(RuntimeError) as err:
        ffmpeg_tools.find_local_ffmpeg()
    assert "can't be replaced while it runs" in str(err.value)
    assert record.read_text().strip() == "0" * 64  # its record is kept...
    with pytest.raises(RuntimeError):
        ffmpeg_tools.find_local_ffmpeg()  # ...so it is never adopted later

    system = str(tmp_path / "bin" / "ffmpeg.exe")
    monkeypatch.setattr(ffmpeg_tools, "_on_path", lambda name: system)
    assert ffmpeg_tools.find_local_ffmpeg() == system  # an ffmpeg on PATH still works


def _stand_in(folder, name="ffmpeg"):
    """A program called *name* in *folder*, as a console finds it there (a
    batch file on Windows); its path."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + (".bat" if os.name == "nt" else ""))
    path.write_text("")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def no_ffmpeg_yet(tmp_path, monkeypatch):
    """Neither a Settings path nor a downloaded ffmpeg, and a working folder
    of the test's own (returned)."""
    from turboadb.gui import ffmpeg_tools, settings

    monkeypatch.setattr(ffmpeg_tools, "_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(ffmpeg_tools, "_VERIFIED", {})
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def test_an_ffmpeg_in_the_working_folder_is_not_used(no_ffmpeg_yet, tmp_path, monkeypatch):
    from turboadb.gui import ffmpeg_tools

    _stand_in(no_ffmpeg_yet)
    _stand_in(no_ffmpeg_yet / "bin")
    empty = tmp_path / "on-path"
    empty.mkdir()
    # "." and an empty entry mean the current folder, as a relative one does
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin", str(empty)]))
    assert ffmpeg_tools.cached_ffmpeg() is None
    assert ffmpeg_tools.find_local_ffmpeg() is None


def test_an_ffmpeg_in_a_path_folder_is_used_by_its_absolute_path(no_ffmpeg_yet, tmp_path,
                                                                 monkeypatch):
    from turboadb.gui import ffmpeg_tools

    _stand_in(no_ffmpeg_yet)  # the one in the working folder loses
    wanted = _stand_in(tmp_path / "tools")
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(tmp_path / "tools")]))
    for found in (ffmpeg_tools.cached_ffmpeg(), ffmpeg_tools.find_local_ffmpeg()):
        assert os.path.isabs(found) and os.path.samefile(found, wanted)
