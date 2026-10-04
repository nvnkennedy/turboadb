"""``bugreport()`` returns the file adb wrote. adb adds ``.zip`` to a name that
lacks it, and inside an existing folder it names the file itself; the fake adb
here saves the report by the same rule."""
import os

import pytest

from turboadb import ADBConfig, ADBHandler
from turboadb.exceptions import ADBError

_DEVICE_NAME = "bugreport-car-RP1A.201005.004-2026-10-03-12-00-00.zip"


@pytest.fixture
def adb(fake_adb, monkeypatch):
    """The fake adb writes the report where adb would and says so."""

    def answer(argv):
        if "bugreport" not in argv:
            return 0, b"", b""
        path = argv[argv.index("bugreport") + 1]
        if os.path.isdir(path):
            path = os.path.join(path, _DEVICE_NAME)
        elif not path.lower().endswith(".zip"):
            path += ".zip"
        with open(path, "wb") as fh:
            fh.write(b"PK-report")
        return 0, f"Bug report copied to {path}\n".encode(), b""

    monkeypatch.setattr(fake_adb, "_answer", answer)
    return fake_adb


def _handler():
    return ADBHandler(ADBConfig(serial="x"))


def test_a_name_without_zip_is_saved_and_returned_with_it(adb, tmp_path):
    wanted = tmp_path / "reports" / "head-unit"
    saved = _handler().bugreport(str(wanted))
    assert saved == str(wanted) + ".zip"
    assert os.path.getsize(saved) == 9 and not wanted.exists()


@pytest.mark.parametrize("name", ["head-unit.zip", "HEAD-UNIT.ZIP"])
def test_a_zip_name_is_kept_as_it_is(adb, tmp_path, name):
    wanted = tmp_path / name
    assert _handler().bugreport(str(wanted)) == str(wanted)
    assert adb.argv_after_adb()[-2:] == ["bugreport", str(wanted)]


def test_an_existing_folder_gets_the_file_adb_names_in_it(adb, tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    assert _handler().bugreport(str(folder)) == str(folder / _DEVICE_NAME)


def test_a_report_adb_did_not_write_is_an_error(fake_adb, tmp_path):
    fake_adb.add("bugreport", stdout="", returncode=1, stderr="adb: device offline")
    with pytest.raises(ADBError, match="bugreport failed: adb: device offline"):
        _handler().bugreport(str(tmp_path / "head-unit"))
    fake_adb.rules.clear()  # adb says nothing and exits 0, yet no file is there
    with pytest.raises(ADBError, match="bugreport failed: no output"):
        _handler().bugreport(str(tmp_path / "head-unit"))
