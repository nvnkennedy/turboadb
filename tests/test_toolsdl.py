"""Tests for the tool-download install path: zip extraction (with zip-slip
guard), the atomic directory swap, and integrity checks — the machinery behind
the 1.0.15 "adb update silently did nothing" fix. No network required."""

import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from turboadb import toolsdl
from turboadb.exceptions import ADBError


def _make_zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)


def test_extract_strip_top_flattens(tmp_path):
    zp = tmp_path / "s.zip"
    _make_zip(zp, {"scrcpy-win64/scrcpy.exe": "x", "scrcpy-win64/lib/a.dll": "y"})
    dest = tmp_path / "flat"
    toolsdl._extract_zip(str(zp), str(tmp_path), strip_top_to=str(dest))
    assert (dest / "scrcpy.exe").read_text() == "x"
    assert (dest / "lib" / "a.dll").exists()


def test_extract_blocks_zip_slip(tmp_path):
    zp = tmp_path / "evil.zip"
    _make_zip(zp, {"top/ok.txt": "ok", "top/../escape.txt": "bad"})
    dest = tmp_path / "out"
    toolsdl._extract_zip(str(zp), str(tmp_path), strip_top_to=str(dest))
    assert (dest / "ok.txt").exists()
    # the '..' member must NOT have escaped the destination directory
    assert not (tmp_path / "escape.txt").exists()


def test_check_zip_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip at all")
    with pytest.raises(ADBError):
        toolsdl._check_zip(str(bad))


def test_swap_dir_replaces_and_cleans(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "adb.exe").write_text("old")
    (live / "stale.dll").write_text("stale")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "adb.exe").write_text("new")
    toolsdl._swap_dir(str(staged), str(live))
    assert (live / "adb.exe").read_text() == "new"
    assert not (live / "stale.dll").exists()  # old content fully replaced


def test_swap_dir_into_empty_target(tmp_path):
    dest = tmp_path / "dest"  # does not exist yet
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "adb.exe").write_text("new")
    toolsdl._swap_dir(str(staged), str(dest))
    assert (dest / "adb.exe").read_text() == "new"


@pytest.mark.skipif(os.name != "nt", reason="only Windows locks an open file's directory")
def test_swap_dir_locked_file_raises_and_rolls_back(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    fh = open(live / "adb.exe", "w")
    fh.write("old")
    fh.flush()
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "adb.exe").write_text("new")
    try:
        with pytest.raises(ADBError):
            toolsdl._swap_dir(str(staged), str(live))
        # rolled back: the original is intact, not half-swapped
        assert (live / "adb.exe").read_text() == "old"
    finally:
        fh.close()


def test_decide_unknown_when_latest_missing():
    assert toolsdl._decide("37.0.0", None) is None
