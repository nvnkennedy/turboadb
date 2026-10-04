"""A pull saves every file under a name this PC can store.

Android allows names Windows does not: with a ``:`` in it, NTFS puts the
data into a hidden stream of another, empty file (``Screenshot_12`` for
``Screenshot_12:30.png``), and the pull still looked like a success. Windows
also drops a trailing dot or space, and ``nul`` or ``con.txt`` are devices
there. The engine now picks the name itself, as the Files tab does."""
import os
import posixpath

import pytest

import turboadb.core as core
from turboadb import ADBConfig, ADBHandler

from test_transfer_outcome import _Adb  # noqa: E402


class _AdbPull(_Adb):
    """``adb pull SRC DST`` that leaves five bytes where adb would: as DST, or
    under SRC's name inside DST when that is a folder (for ``SRC/.``, a file
    in DST itself)."""

    def __init__(self):
        super().__init__([], work=self._pull)

    def _pull(self):
        src, dst = self.cmd[-2:]
        if os.path.isdir(dst):
            name = posixpath.basename(src.rstrip("/"))
            dst = os.path.join(dst, "inside.txt" if name == "." else name)
        with open(dst, "wb") as fh:
            fh.write(b"video")


@pytest.fixture
def adb(fake_adb, monkeypatch):
    """A fake ``adb pull``, with Windows' file names in force on any system."""
    monkeypatch.setattr(core, "_WINDOWS_NAMES", True)
    pull = _AdbPull()
    monkeypatch.setattr(core.subprocess, "Popen", pull)
    return pull


def _handler():
    return ADBHandler(ADBConfig(serial="x"))


# --------------------------------------------------------------------------- #
# the name Windows can store
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name, stored", [
    ("Screenshot_12:30.png", "Screenshot_12_30.png"),
    ('what?<a>|"b"*.txt', "what__a___b__.txt"),
    ("notes.", "notes_"),
    ("draft ", "draft_"),
    ("tab\there.txt", "tab_here.txt"),
    ("nul", "_nul"),
    ("CON.txt", "_CON.txt"),
    ("com1.log", "_com1.log"),
    ("console.txt", "console.txt"),
    ("holiday.mp4", "holiday.mp4"),
    ("...", "___"),
])
def test_a_name_is_made_storable_on_windows(name, stored):
    assert core._windows_file_name(name) == stored


# --------------------------------------------------------------------------- #
# pulling a file whose name Windows cannot store
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.name != "nt", reason="NTFS streams exist only on Windows")
def test_a_name_with_a_colon_is_pulled_into_a_file_of_its_own(fake_adb, monkeypatch, tmp_path):
    pull = _AdbPull()
    monkeypatch.setattr(core.subprocess, "Popen", pull)
    out = tmp_path / "out"
    out.mkdir()
    res = _handler().pull("/sdcard/Screenshot_12:30.png", str(out))
    # not an empty "Screenshot_12" with the picture hidden in its stream
    assert os.listdir(out) == ["Screenshot_12_30.png"]
    assert (out / "Screenshot_12_30.png").read_bytes() == b"video" and res.size_bytes == 5


def _warnings(lines):
    return [line for line in lines if line.startswith("[WARNING]")]


def test_adb_is_handed_the_storable_name_and_the_log_says_so(adb, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    logged = []
    handler = ADBHandler(ADBConfig(serial="x"), log_callback=logged.append)
    res = handler.pull("/sdcard/Screenshot_12:30.png", str(out))
    assert adb.cmd[-2:] == ["/sdcard/Screenshot_12:30.png",
                            str(out / "Screenshot_12_30.png")]
    assert (res.files, res.size_bytes) == (1, 5)
    (warning,) = _warnings(logged)
    assert "'Screenshot_12:30.png'" in warning and "'Screenshot_12_30.png'" in warning


def test_a_name_windows_can_store_goes_to_adb_as_before(adb, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    logged = []
    handler = ADBHandler(ADBConfig(serial="x"), log_callback=logged.append)
    res = handler.pull("/sdcard/holiday.mp4", str(out))
    assert adb.cmd[-2:] == ["/sdcard/holiday.mp4", str(out)]  # adb names the file itself
    assert res.size_bytes == 5 and not _warnings(logged)


def test_a_device_file_named_like_a_windows_device_is_not_written_to_it(adb, tmp_path):
    _handler().pull("/sdcard/logs/nul", str(tmp_path))
    assert adb.cmd[-1] == str(tmp_path / "_nul") and (tmp_path / "_nul").read_bytes() == b"video"


def test_a_local_name_typed_with_a_colon_is_stored_under_a_name_that_works(adb, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    res = _handler().pull("/sdcard/a.png", str(out / "Shot 12:30.png"))
    assert adb.cmd[-1] == str(out / "Shot 12_30.png")
    assert res.size_bytes == 5 and os.listdir(out) == ["Shot 12_30.png"]


def test_a_device_name_typed_as_the_destination_is_left_alone(adb, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _handler().pull("/sdcard/big.bin", "NUL")  # a copy thrown away on purpose
    assert adb.cmd[-1] == "NUL"


def test_a_folder_pulled_again_goes_into_the_folder_it_was_stored_as(adb, tmp_path):
    _handler().pull("/sdcard/logs:old", str(tmp_path))
    assert adb.cmd[-2:] == ["/sdcard/logs:old", str(tmp_path / "logs_old")]
    (tmp_path / "logs_old").unlink()
    (tmp_path / "logs_old").mkdir()  # as the first pull of the folder left it
    _handler().pull("/sdcard/logs:old/", str(tmp_path))
    # its contents into that folder: adb would put a "logs:old" inside it
    assert adb.cmd[-2:] == ["/sdcard/logs:old/.", str(tmp_path / "logs_old")]
    assert os.listdir(tmp_path / "logs_old") == ["inside.txt"]


def test_a_merge_into_a_folder_keeps_its_dot(adb, tmp_path):
    _handler().pull("/sdcard/Download/.", str(tmp_path))
    assert adb.cmd[-2:] == ["/sdcard/Download/.", str(tmp_path)]


def test_other_systems_keep_the_device_name(fake_adb, monkeypatch, tmp_path):
    monkeypatch.setattr(core, "_WINDOWS_NAMES", False)
    pull = _Adb([])
    monkeypatch.setattr(core.subprocess, "Popen", pull)
    _handler().pull("/sdcard/Screenshot_12:30.png", str(tmp_path))
    assert pull.cmd[-2:] == ["/sdcard/Screenshot_12:30.png", str(tmp_path)]


# --------------------------------------------------------------------------- #
# a name typed for the copy, and what its caller does with it afterwards
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("typed, read_as", [
    ("shot.", "shot"), ("clip.mp4 ", "clip.mp4"), ("notes.. ", "notes"),
])
def test_a_typed_name_goes_to_adb_as_windows_reads_it(adb, tmp_path, typed, read_as):
    """Windows drops a trailing dot or space itself: "clip.mp4 " is clip.mp4,
    never clip.mp4_, and nothing is said about it."""
    logged = []
    handler = ADBHandler(ADBConfig(serial="x"), log_callback=logged.append)
    res = handler.pull("/sdcard/a.png", os.path.join(str(tmp_path), typed))
    assert adb.cmd[-1] == str(tmp_path / read_as) and not _warnings(logged)
    assert res.size_bytes == 5 and (tmp_path / read_as).read_bytes() == b"video"


@pytest.mark.skipif(os.name != "nt", reason="only Windows reads a name without its last space")
def test_the_path_as_typed_reaches_the_copy_afterwards(adb, tmp_path):
    target = os.path.join(str(tmp_path), "clip.mp4 ")
    _handler().pull("/sdcard/a.png", target)
    with open(target, "rb") as fh:
        assert fh.read() == b"video"


class _AdbPullsAFolder(_Adb):
    """``adb pull FOLDER DST`` for a DST that is not there yet: DST becomes the
    folder, and adb writes each file as DST, a separator and its name."""

    def __init__(self):
        super().__init__([], work=self._pull)

    def _pull(self):
        folder = self.cmd[-1]
        os.makedirs(folder)
        with open(folder + os.sep + "1.jpg", "wb") as fh:
            fh.write(b"photo")


def test_a_folder_pulled_to_a_name_typed_with_a_trailing_space_arrives(fake_adb, monkeypatch,
                                                                      tmp_path):
    """Windows drops the space of the folder's own name, but not of a name
    inside a path: adb made "Pictures" and then could not reach the files it
    wrote into "Pictures "."""
    monkeypatch.setattr(core, "_WINDOWS_NAMES", True)
    pull = _AdbPullsAFolder()
    monkeypatch.setattr(core.subprocess, "Popen", pull)
    res = _handler().pull("/sdcard/DCIM", str(tmp_path / "Pictures") + " ")
    assert pull.cmd[-1] == str(tmp_path / "Pictures")
    assert (res.files, res.size_bytes) == (1, 5)
    assert (tmp_path / "Pictures" / "1.jpg").read_bytes() == b"photo"


def test_a_file_pulled_into_a_folder_typed_with_a_trailing_space_arrives(adb, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    _handler().pull("/sdcard/a.png", str(out) + " ")
    assert adb.cmd[-2:] == ["/sdcard/a.png", str(out)]
    assert (out / "a.png").read_bytes() == b"video"


def test_the_result_names_the_file_a_typed_name_was_stored_as(adb, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    res = _handler().pull("/sdcard/a.png", str(out / "Shot 12:30.png"))
    assert res.dest == str(out / "Shot 12_30.png") and os.path.isfile(res.dest)
    assert str(res).startswith(f"Pulled /sdcard/a.png -> {out / 'Shot 12_30.png'}  (")
    # a name Windows takes, and a folder, are named as they were given
    assert _handler().pull("/sdcard/a.png", str(out / "plain.png")).dest == str(out / "plain.png")
    assert _handler().pull("/sdcard/Screenshot_12:30.png", str(out)).dest == str(out)


def test_a_device_file_named_with_dots_alone_is_stored_too(adb, tmp_path):
    _handler().pull("/sdcard/...", str(tmp_path))
    assert adb.cmd[-1] == str(tmp_path / "___") and (tmp_path / "___").read_bytes() == b"video"


class _AdbRecordAndPull(_AdbPull):
    """Also the ``adb shell screenrecord`` child, which has ended already."""

    def _pull(self):
        if "pull" in self.cmd:
            super()._pull()


@pytest.fixture
def recorder(fake_adb, monkeypatch):
    monkeypatch.setattr(core, "_WINDOWS_NAMES", True)
    adb = _AdbRecordAndPull()
    monkeypatch.setattr(core.subprocess, "Popen", adb)
    return adb


@pytest.fixture
def clips(tmp_path):
    folder = tmp_path / "clips"
    folder.mkdir()
    return folder


def test_a_recording_is_returned_under_the_name_it_was_stored_as(recorder, clips):
    saved = _handler().screen_record(str(clips / "drive 12:30.mp4"), time_limit=1)
    assert saved == str(clips / "drive 12_30.mp4") == recorder.cmd[-1]
    assert os.listdir(clips) == ["drive 12_30.mp4"]
    with open(saved, "rb") as fh:
        assert fh.read() == b"video"


def test_the_parts_of_a_long_recording_are_returned_as_stored_too(recorder, clips):
    paths = _handler().screen_record_continuous(str(clips / "drive 12:30.mp4"))
    assert paths == [str(clips / "drive 12_30.mp4")] and os.path.isfile(paths[0])


def test_a_recording_typed_with_a_trailing_space_is_saved(recorder, clips):
    saved = _handler().screen_record(str(clips / "clip.mp4") + " ", time_limit=1)
    assert saved == str(clips / "clip.mp4") and os.listdir(clips) == ["clip.mp4"]
    with open(saved, "rb") as fh:
        assert fh.read() == b"video"


def _edit(fake_adb, monkeypatch, tmp_path, device_file):
    """``edit_file(device_file)`` with a temp folder of its own; returns what
    the editor was handed and what the folder holds afterwards."""
    import tempfile

    fake_adb.add("stat -L", stdout="5 644 regular file\n%s\n" % device_file)
    work = tmp_path / "temp"
    work.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(work))
    seen = []

    def editor(path):
        # a name every editor can open: not all of them drop a last dot or space
        assert os.path.basename(path) in os.listdir(work)
        with open(path, "rb") as fh:
            seen.append(fh.read())
        return 0

    out = _handler().edit_file(device_file, editor)
    assert out == {"changed": False, "path": device_file, "editor_exit": 0}
    return seen, os.listdir(work)


@pytest.mark.parametrize("device_file", [
    "/sdcard/notes.", "/sdcard/todo.txt ", "/sdcard/trace.12:30", "/sdcard/a.b?c",
])
def test_the_editor_gets_the_content_of_a_file_whose_ending_windows_cannot_store(
        fake_adb, recorder, monkeypatch, tmp_path, device_file):
    """The local copy is named after the device file's ending, so that the
    editor knows the kind of file: an ending Windows changes named two files,
    the empty one the editor opened and the one adb wrote."""
    seen, left = _edit(fake_adb, monkeypatch, tmp_path, device_file)
    assert seen == [b"video"]
    assert left == []  # and no copy stays behind


@pytest.mark.parametrize("typed", ["clips ", "clips..", "clips " + os.sep])
def test_a_recording_into_a_folder_typed_with_a_trailing_space_goes_into_it(recorder, clips,
                                                                           typed):
    """The engine reads the folder's name as Windows does before it looks it
    up, so this holds on a system that takes "clips " for a name of its own."""
    # joined as text: a Path would drop the separator the last one ends with
    saved = _handler().screen_record(os.path.join(str(clips.parent), typed), time_limit=1)
    assert os.path.dirname(saved) == str(clips) and os.listdir(clips.parent / "clips") == [
        os.path.basename(saved)]
    assert sorted(p.name for p in clips.parent.iterdir() if p.is_dir()) == ["clips"]


@pytest.mark.skipif(os.name == "nt", reason="Windows reads the name without its last space")
def test_other_systems_save_a_recording_under_the_name_as_typed(recorder, clips, monkeypatch):
    monkeypatch.setattr(core, "_WINDOWS_NAMES", False)
    typed = str(clips) + " "  # a file of that name there, next to the folder
    saved = _handler().screen_record(typed, time_limit=1)
    assert saved == typed == recorder.cmd[-1] and os.listdir(clips) == []
    with open(saved, "rb") as fh:
        assert fh.read() == b"video"


def test_a_path_with_the_long_path_prefix_is_taken_as_it_is():
    # under that prefix Windows drops nothing: the name is stored as typed
    for path in (r"\\?\C:\out\lit.", "//?/C:/out/lit ."):
        assert core._as_windows_reads(path) == path
    assert core._as_windows_reads(os.path.join("out", "lit. ")) == os.path.join("out", "lit")
    for no_name in ("..", os.path.join("out", "...")):
        assert core._as_windows_reads(no_name) == no_name
