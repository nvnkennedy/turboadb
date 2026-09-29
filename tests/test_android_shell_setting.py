"""The ``android_shell_pty`` setting: the Android shell on a device terminal
(the default) or over plain pipes, from Settings → Appearance."""

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    from turboadb.gui import settings

    monkeypatch.setattr(settings, "_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", None)
    return tmp_path / "settings.json"


def test_the_device_terminal_is_the_default_and_the_dialog_turns_it_off(qapp, settings_file):
    from turboadb.gui import settings
    from turboadb.gui.settings_dialog import SettingsDialog

    assert settings.DEFAULTS["android_shell_pty"] is True
    dlg = SettingsDialog()
    try:
        assert dlg.android_pty.isChecked()
        assert dlg.changed_settings() == {}
        dlg.android_pty.setChecked(False)
        assert dlg.changed_settings() == {"android_shell_pty": False}
        dlg.accept()
    finally:
        dlg.deleteLater()
    assert settings.get("android_shell_pty") is False

    dlg = SettingsDialog()
    try:
        assert not dlg.android_pty.isChecked()
        dlg.android_pty.setChecked(True)
        dlg.accept()
    finally:
        dlg.deleteLater()
    assert settings.get("android_shell_pty") is True


def test_a_new_android_shell_follows_the_setting(qapp, settings_file):
    from turboadb.gui import settings
    from test_android_shell_pty import _close, _widget

    settings.set("android_shell_pty", False)
    widget, handler = _widget(qapp)
    try:
        assert handler.opened == [False] and widget.term._emulate_prompt
    finally:
        _close(widget)
    settings.set("android_shell_pty", True)
    widget, handler = _widget(qapp)
    try:
        assert handler.opened == [True] and not widget.term._emulate_prompt
    finally:
        _close(widget)
