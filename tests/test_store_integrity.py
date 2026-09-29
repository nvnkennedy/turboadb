"""Saved targets and settings: a long-running window never writes back a stale
list, and a file that can't be read is kept (as .corrupt-<time>) before any
write replaces it."""
import glob
import json
import os
import warnings

import pytest

from turboadb.gui import sessions, settings


@pytest.fixture
def files(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    sessions_file = tmp_path / "sessions.json"
    monkeypatch.setattr(settings, "_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "_FILE", str(settings_file))
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(settings, "_unreadable", None)
    monkeypatch.setattr(sessions, "_DIR", str(tmp_path))
    monkeypatch.setattr(sessions, "_FILE", str(sessions_file))
    return settings_file, sessions_file


def _usb(name, serial):
    return {"name": name, "type": "usb", "serial": serial}


def _names(path):
    return [s["name"] for s in json.loads(path.read_text(encoding="utf-8"))]


def test_a_stale_store_does_not_bring_back_deleted_or_old_targets(files):
    _settings_file, sessions_file = files
    sessions_file.write_text(json.dumps([_usb("A", "1"), _usb("B", "2")]), encoding="utf-8")
    gui = sessions.SessionStore()             # a window that stays open
    cli = sessions.SessionStore()             # e.g. `turboadb targets ...`
    cli.delete("A")
    cli.save(_usb("B", "22"))
    gui.save(_usb("C", "3"))                  # an unrelated save from the window
    data = json.loads(sessions_file.read_text(encoding="utf-8"))
    assert [s["name"] for s in data] == ["B", "C"]
    assert data[0]["serial"] == "22"          # the other process's edit survives
    assert gui.names() == ["B", "C"]          # and the window now shows the file


def test_a_rename_applies_to_the_file_not_the_snapshot(files):
    _settings_file, sessions_file = files
    sessions_file.write_text(json.dumps([_usb("bench", "1"), _usb("lab", "2")]), encoding="utf-8")
    gui = sessions.SessionStore()
    sessions.SessionStore().save(_usb("desk", "9"))
    gui.save(dict(_usb("bench-2", "1"), previous_name="bench"))
    assert _names(sessions_file) == ["bench-2", "lab", "desk"]
    # renamed onto an existing name: that target is replaced
    gui.save(dict(_usb("lab", "1"), previous_name="bench-2"))
    assert _names(sessions_file) == ["lab", "desk"]


def test_renaming_a_target_someone_else_deleted_saves_it_under_the_new_name(files):
    _settings_file, sessions_file = files
    sessions_file.write_text(json.dumps([_usb("A", "1")]), encoding="utf-8")
    gui = sessions.SessionStore()
    sessions.SessionStore().delete("A")
    gui.save(dict(_usb("A2", "1"), previous_name="A"))
    assert _names(sessions_file) == ["A2"]


def test_import_upserts_onto_the_current_file(files, tmp_path):
    _settings_file, sessions_file = files
    sessions_file.write_text(json.dumps([_usb("A", "1")]), encoding="utf-8")
    gui = sessions.SessionStore()
    sessions.SessionStore().save(_usb("B", "2"))
    incoming = tmp_path / "in.json"
    incoming.write_text(json.dumps([_usb("A", "10"), _usb("C", "3")]), encoding="utf-8")
    assert gui.import_from(str(incoming)) == 2
    data = json.loads(sessions_file.read_text(encoding="utf-8"))
    assert [(s["name"], s["serial"]) for s in data] == [("A", "10"), ("B", "2"), ("C", "3")]


def test_a_corrupt_targets_file_is_kept_before_it_is_rewritten(files):
    _settings_file, sessions_file = files
    broken = '[{"name": "X", "type": "usb", "serial": "1"},]'   # trailing comma
    sessions_file.write_text(broken, encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        store = sessions.SessionStore()
        assert store.load_error
        store.save(_usb("Y", "2"))
    backups = glob.glob(str(sessions_file) + ".corrupt-*")
    assert len(backups) == 1
    with open(backups[0], encoding="utf-8") as fh:
        assert fh.read() == broken                    # X is recoverable
    assert _names(sessions_file) == ["Y"]


def test_a_partly_invalid_targets_file_is_kept_too(files):
    _settings_file, sessions_file = files
    raw = [_usb("ok", "1"), {"name": "bad", "type": "network", "host": "h", "port": "abc"}]
    sessions_file.write_text(json.dumps(raw), encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sessions.SessionStore().save(_usb("new", "2"))
    backups = glob.glob(str(sessions_file) + ".corrupt-*")
    assert len(backups) == 1 and json.loads(open(backups[0], encoding="utf-8").read()) == raw
    assert _names(sessions_file) == ["ok", "new"]


def test_remembering_a_device_goes_on_past_an_invalid_entry(files):
    """remember() (the auto-save of a connected device) refused the whole file
    for one invalid entry, so every connect failed to save; it now keeps a
    copy and goes on, as save() does."""
    _settings_file, sessions_file = files
    phone = {"name": "Phone", "type": "network", "host": "10.0.0.7", "port": 37123}
    raw = [_usb("ok", "1"), {"name": "bad", "type": "network", "host": "h", "port": "abc"}, phone]
    sessions_file.write_text(json.dumps(raw), encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        store = sessions.SessionStore()
        # already saved: nothing is written, and nothing needs a copy
        assert store.remember(_usb("ok again", "1"))[0] == "known"
        assert glob.glob(str(sessions_file) + ".corrupt-*") == []
        assert json.loads(sessions_file.read_text(encoding="utf-8")) == raw
        assert store.remember(_usb("new", "2")) == ("added", _usb("new", "2"))
    backups = glob.glob(str(sessions_file) + ".corrupt-*")
    assert len(backups) == 1 and json.loads(open(backups[0], encoding="utf-8").read()) == raw
    assert _names(sessions_file) == ["ok", "Phone", "new"]
    # the file is valid again: a new wireless-debugging port just updates it
    outcome, saved = store.remember(dict(phone, port=41555))
    assert (outcome, saved["port"]) == ("updated", 41555)
    assert len(glob.glob(str(sessions_file) + ".corrupt-*")) == 1


def test_remembering_a_device_leaves_a_file_that_is_no_list_of_targets_alone(files):
    _settings_file, sessions_file = files
    broken = '[{"name": "X", "type": "usb", "serial": "1"},]'
    sessions_file.write_text(broken, encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        store = sessions.SessionStore()
    with pytest.raises(ValueError, match="left untouched"):
        store.remember(_usb("Y", "2"))
    assert sessions_file.read_text(encoding="utf-8") == broken
    assert glob.glob(str(sessions_file) + ".corrupt-*") == []


def test_a_readable_file_makes_no_backup(files):
    _settings_file, sessions_file = files
    sessions_file.write_text(json.dumps([_usb("A", "1")]), encoding="utf-8")
    sessions.SessionStore().save(_usb("B", "2"))
    settings.set("theme", "light")
    assert glob.glob(str(sessions_file.parent / "*.corrupt-*")) == []


def test_a_corrupt_settings_file_is_kept_before_a_save_replaces_it(files):
    settings_file, _sessions_file = files
    broken = '{"theme": "light", "adb_path": "D:/sdk/adb.exe",}'
    settings_file.write_text(broken, encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert settings.get("theme") == settings.DEFAULTS["theme"]
        assert settings.load_problem()
        settings.set("term_font_size", 14)
    backups = glob.glob(str(settings_file) + ".corrupt-*")
    assert len(backups) == 1
    with open(backups[0], encoding="utf-8") as fh:
        assert fh.read() == broken                    # the custom adb path is recoverable
    assert json.loads(settings_file.read_text(encoding="utf-8"))["term_font_size"] == 14
    assert settings.load_problem() is None


def test_settings_refuse_to_write_when_the_copy_fails(files, monkeypatch):
    settings_file, _sessions_file = files
    settings_file.write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(settings, "keep_unreadable", lambda path, error: None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ValueError):
            settings.set("theme", "light")
    assert settings_file.read_text(encoding="utf-8") == "{oops"


def test_a_failed_settings_read_is_not_cached_as_defaults(files, monkeypatch):
    settings_file, _sessions_file = files
    settings_file.write_text(json.dumps({"theme": "light"}), encoding="utf-8")
    real_open = open
    calls = {"n": 0}

    def flaky_open(path, *a, **kw):
        if os.path.abspath(str(path)) == os.path.abspath(str(settings_file)) and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError("sharing violation")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky_open)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert settings.get("theme") == settings.DEFAULTS["theme"]   # the failed read
    assert settings.get("theme") == "light"                           # read again, not cached
