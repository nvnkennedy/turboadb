"""A settings read that fails for a moment must never reset every setting.

``settings.update()`` (behind ``set()``, the Settings dialog, the theme menu,
the debounced zoom write) reads the file, applies the change and writes it
back.  When that read failed for a moment (Windows reports a sharing violation
while an antivirus scanner or indexer has settings.json open), the read gave
the defaults, and the write stored them: the theme, the adb path, the font
size and every other choice were gone.  No ``.corrupt-`` copy was kept either,
because the second read inside ``save()`` worked and cleared the "unreadable"
mark.  The read is now tried again, and a read that keeps failing saves
nothing (OSError) instead of writing the defaults.
"""
import builtins
import glob
import json
import os
import warnings

import pytest

from turboadb.gui import settings


@pytest.fixture
def settings_file():
    path = settings.settings_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"settings_version": settings.DEFAULTS["settings_version"],
                   "theme": "light", "adb_path": "D:/sdk/adb.exe", "term_font_size": 17}, fh)
    return path


def _flaky_reads(monkeypatch, path, failures):
    real_open = builtins.open
    left = {"n": failures}

    def flaky(file, *args, **kwargs):
        if os.path.abspath(str(file)) == os.path.abspath(path) and left["n"] > 0:
            left["n"] -= 1
            raise PermissionError(13, "The process cannot access the file (sharing violation)")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky)
    monkeypatch.setattr(settings, "_retry_pause", lambda _attempt: None)


def _saved(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_a_passing_read_failure_keeps_every_other_setting(settings_file, monkeypatch):
    _flaky_reads(monkeypatch, settings_file, failures=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        settings.set("logcat_format", "brief")
    monkeypatch.undo()
    data = _saved(settings_file)
    assert data["logcat_format"] == "brief"
    assert (data["theme"], data["adb_path"], data["term_font_size"]) == (
        "light", "D:/sdk/adb.exe", 17)
    assert glob.glob(settings_file + ".corrupt-*") == []  # nothing was wrong with it


def test_a_recent_list_entry_keeps_every_other_setting(settings_file, monkeypatch):
    _flaky_reads(monkeypatch, settings_file, failures=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        settings.add_recent("recent_remote_hosts", "10.0.0.9")
    monkeypatch.undo()
    data = _saved(settings_file)
    assert data["recent_remote_hosts"][0] == "10.0.0.9"
    assert data["theme"] == "light" and data["adb_path"] == "D:/sdk/adb.exe"


def test_a_read_that_keeps_failing_saves_nothing(settings_file, monkeypatch):
    with open(settings_file, encoding="utf-8") as fh:
        before = fh.read()
    _flaky_reads(monkeypatch, settings_file, failures=10_000)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(OSError):
            settings.set("logcat_format", "brief")
    monkeypatch.undo()
    with open(settings_file, encoding="utf-8") as fh:
        assert fh.read() == before


def test_the_debounced_write_reports_it_instead_of_raising(settings_file, monkeypatch):
    with open(settings_file, encoding="utf-8") as fh:
        before = fh.read()
    _flaky_reads(monkeypatch, settings_file, failures=10_000)
    settings._pending["logcat_format"] = "brief"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        settings.flush_pending()
    assert any("could not save settings" in str(w.message) for w in caught)
    monkeypatch.undo()
    with open(settings_file, encoding="utf-8") as fh:
        assert fh.read() == before
