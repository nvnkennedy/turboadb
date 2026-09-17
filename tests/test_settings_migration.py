"""One-time settings upgrades."""
import json

from turboadb.gui import settings as settings_mod


def _use_file(monkeypatch, tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "_FILE", str(path))
    monkeypatch.setattr(settings_mod, "_cache", None)
    return path


def test_terminal_font_defaults_to_12pt_and_only_the_old_default_moves(monkeypatch, tmp_path):
    assert settings_mod.DEFAULTS["term_font_size"] == 12
    # (stored size, stored version) -> size used
    cases = (
        ((10, None), 12),  # the old 10 pt default moves to 12 pt once
        ((10, 3), 12),
        ((10, 4), 10),  # 10 pt chosen after the upgrade stays
        ((12, 2), 12),
        ((14, 3), 14),  # any other size is the user's choice
        ((9, None), 9),
    )
    for (stored, version), expected in cases:
        content = {"term_font_size": stored}
        if version is not None:
            content["settings_version"] = version
        _use_file(monkeypatch, tmp_path, content)
        assert settings_mod.get("term_font_size") == expected, (stored, version)


def test_a_new_install_uses_12pt_terminals_and_the_scrcpy_renderer(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(settings_mod, "_cache", None)
    assert settings_mod.get("term_font_size") == 12
    assert settings_mod.get("screen_backend") == "scrcpy"


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
    assert stored["settings_version"] == settings_mod.DEFAULTS["settings_version"]
    assert stored["term_font_size"] == 13


def test_upgrade_is_recorded_so_a_later_choice_sticks(monkeypatch, tmp_path):
    path = _use_file(monkeypatch, tmp_path, {"settings_version": 2, "scrcpy_bit_rate": "8M"})
    assert settings_mod.get("scrcpy_bit_rate") == "16M"
    settings_mod.set("theme", "dark")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["settings_version"] == settings_mod.DEFAULTS["settings_version"] == 4
    settings_mod.set("scrcpy_bit_rate", "8M")
    settings_mod.set("term_font_size", 10)
    monkeypatch.setattr(settings_mod, "_cache", None)
    assert settings_mod.get("scrcpy_bit_rate") == "8M"
    assert settings_mod.get("term_font_size") == 10
