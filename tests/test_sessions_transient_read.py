"""A targets read that fails for a moment must never drop the saved targets.

``SessionStore._update`` reads sessions.json, applies one change and writes
the result ("the file is the truth").  A read that could not open the file for
a moment (a sharing violation while a scanner or sync tool has it open) came
back as "no targets" plus an error: the file was copied aside as
``.corrupt-<time>`` (although nothing was wrong with it) and then replaced by
the one target being saved, and the window's list shrank to it.  The read is
now tried again; one that keeps failing leaves the file alone (OSError).
"""
import builtins
import glob
import json
import os
import warnings

import pytest

from turboadb.gui import sessions, settings


@pytest.fixture
def targets_file(tmp_path, monkeypatch):
    path = tmp_path / "sessions.json"
    monkeypatch.setattr(settings, "_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(sessions, "_DIR", str(tmp_path))
    monkeypatch.setattr(sessions, "_FILE", str(path))
    path.write_text(json.dumps([_usb("bench", "1"), _usb("lab", "2"), _usb("car", "3")]),
                    encoding="utf-8")
    return path


def _usb(name, serial):
    return {"name": name, "type": "usb", "serial": serial}


def _flaky_reads(monkeypatch, path, failures):
    real_open = builtins.open
    left = {"n": failures}

    def flaky(file, mode="r", *args, **kwargs):
        if (os.path.abspath(str(file)) == os.path.abspath(str(path)) and "r" in mode
                and left["n"] > 0):
            left["n"] -= 1
            raise PermissionError(13, "The process cannot access the file (sharing violation)")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky)
    monkeypatch.setattr(sessions, "_retry_pause", lambda _attempt: None)


def _names(path):
    with open(path, encoding="utf-8") as fh:
        return [s["name"] for s in json.load(fh)]


def test_a_passing_read_failure_keeps_every_saved_target(targets_file, monkeypatch):
    store = sessions.SessionStore()
    _flaky_reads(monkeypatch, targets_file, failures=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        store.save(_usb("desk", "9"))
    monkeypatch.undo()
    assert _names(targets_file) == ["bench", "lab", "car", "desk"]
    assert store.names() == ["bench", "lab", "car", "desk"]
    assert glob.glob(str(targets_file) + ".corrupt-*") == []  # nothing was wrong with it


def test_a_read_that_keeps_failing_leaves_the_file_alone(targets_file, monkeypatch):
    before = targets_file.read_text(encoding="utf-8")
    store = sessions.SessionStore()
    _flaky_reads(monkeypatch, targets_file, failures=10_000)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(OSError):
            store.delete("lab")
    monkeypatch.undo()
    assert targets_file.read_text(encoding="utf-8") == before
    assert store.names() == ["bench", "lab", "car"]
