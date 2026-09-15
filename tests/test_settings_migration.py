"""One-time settings upgrades."""
import json

from turboadb.gui import settings as settings_mod


def _use_file(monkeypatch, tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "_FILE", str(path))
    monkeypatch.setattr(settings_mod, "_cache", None)
    return path


def test_terminal_font_defaults_to_10pt_and_is_never_migrated(monkeypatch, tmp_path):
    assert settings_mod.DEFAULTS["term_font_size"] == 10
    for stored, version in ((10, None), (12, 2), (14, 3), (9, None)):
        content = {"term_font_size": stored}
        if version is not None:
            content["settings_version"] = version
        _use_file(monkeypatch, tmp_path, content)
        assert settings_mod.get("term_font_size") == stored


def test_old_default_bitrate_moves_to_16m(monkeypatch, tmp_path):
    # Older files wrote every default, including an 8M video bitrate.
    _use_file(monkeypatch, tmp_path, {"theme": "dark", "scrcpy_bit_rate": "8M"})
    assert settings_mod.get("scrcpy_bit_rate") == settings_mod.DEFAULTS["scrcpy_bit_rate"] == "16M"


def test_current_file_keeps_chosen_values(monkeypatch, tmp_path):
    path = _use_file(
        monkeypatch, tmp_path,
        {"settings_version": 3, "term_font_size": 13, "scrcpy_bit_rate": "8M"},
    )
    assert settings_mod.get("term_font_size") == 13
    assert settings_mod.get("scrcpy_bit_rate") == "8M"
    settings_mod.set("theme", "light")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["settings_version"] == 3 and stored["term_font_size"] == 13


def test_upgrade_is_recorded_so_a_later_choice_sticks(monkeypatch, tmp_path):
    path = _use_file(monkeypatch, tmp_path, {"settings_version": 2, "scrcpy_bit_rate": "8M"})
    assert settings_mod.get("scrcpy_bit_rate") == "16M"
    settings_mod.set("theme", "dark")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["settings_version"] == 3
    settings_mod.set("scrcpy_bit_rate", "8M")
    monkeypatch.setattr(settings_mod, "_cache", None)
    assert settings_mod.get("scrcpy_bit_rate") == "8M"
