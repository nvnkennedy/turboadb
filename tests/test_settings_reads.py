"""Reading a setting never waits for a write: a write holds the settings lock
across another TurboADB's file lock (up to 5 s), an fsync and the replace
retries, and a read on the UI thread used to freeze the window meanwhile.
Also: the unused ribbon-density setting is gone."""

import json
import threading
import time

import pytest

from turboadb.gui import settings


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "_FILE", str(path))
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(settings, "_unreadable", None)
    return path


def _held_elsewhere(lock) -> bool:
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def test_a_read_does_not_wait_for_a_write_held_up_by_another_process(settings_file):
    settings_file.write_text(json.dumps({"theme": "light", "logcat_format": "brief"}),
                             encoding="utf-8")
    assert settings.get("theme") == "light"  # read once: cached
    # Another TurboADB (the CLI, say) holds the cross-process lock, so this
    # process's own write waits for it while holding the settings lock.
    other = settings._lock_acquire(str(settings_file) + ".lock", 1.0)
    assert other is not None
    writer = threading.Thread(target=settings.update, args=({"term_font_size": 15},),
                              daemon=True)
    try:
        writer.start()
        deadline = time.monotonic() + 5
        while not _held_elsewhere(settings._LOCK) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _held_elsewhere(settings._LOCK), "the write never started"
        got = []
        reader = threading.Thread(
            target=lambda: got.append((settings.get("logcat_format"), settings.load()["theme"])),
            daemon=True,
        )
        reader.start()
        reader.join(1.0)
        assert got == [("brief", "light")], "a settings read waited for the write"
    finally:
        settings._lock_release(other)
        writer.join(10)
    assert not writer.is_alive()
    # the write itself went through once the other process let go
    assert json.loads(settings_file.read_text(encoding="utf-8"))["term_font_size"] == 15
    assert settings.get("term_font_size") == 15


def test_a_changed_file_is_still_read_again(settings_file):
    settings_file.write_text(json.dumps({"theme": "light"}), encoding="utf-8")
    assert settings.get("theme") == "light"
    settings_file.write_text(json.dumps({"theme": "slate", "term_font_size": 18}),
                             encoding="utf-8")
    assert settings.get("theme") == "slate"
    assert settings.load()["term_font_size"] == 18


def test_the_unused_ribbon_density_setting_is_gone(settings_file):
    assert "compact_ribbon" not in settings.DEFAULTS
    settings.set("theme", "light")
    assert "compact_ribbon" not in json.loads(settings_file.read_text(encoding="utf-8"))
    # a file written by an older version still loads, key and all
    settings_file.write_text(json.dumps({"compact_ribbon": False, "theme": "slate"}),
                             encoding="utf-8")
    assert settings.get("theme") == "slate"
