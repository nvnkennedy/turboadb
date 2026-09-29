"""An adb path in Settings that names no adb is reported in the log panel.

TurboADB then uses another adb (the Settings path is a preference, see
``tools.find_adb``), and said so only in turboadb.log.  The main window now
shows the same warning at launch and when the path is changed.
"""
import logging
import os

import pytest

from turboadb import tools
from turboadb.gui import settings as settings_mod


@pytest.fixture
def adb_setting(monkeypatch, tmp_path):
    """Set Settings' adb path for this test only; a real adb exists elsewhere."""
    real = settings_mod.get
    value = {}
    monkeypatch.setattr(settings_mod, "get", lambda key, default=None:
                        value.get(key, default) if key in value else real(key, default))
    monkeypatch.delenv("TURBOADB_ADB", raising=False)
    other = tmp_path / ("adb.exe" if os.name == "nt" else "adb")
    other.write_text("")
    monkeypatch.setattr(tools, "_discover", lambda name: str(other))
    tools.clear_tools_cache()

    def use(path):
        value["adb_path"] = path
        return str(other)

    yield use
    tools.clear_tools_cache()


def test_a_stale_path_gets_the_warning_find_adb_logs(adb_setting, tmp_path, caplog):
    stale = str(tmp_path / "gone" / "adb.exe")
    other = adb_setting(stale)
    note = tools.stale_setting_note("adb")
    assert note == (f"The adb path in Settings ({stale}) does not exist; using {other} "
                    "instead. Correct or clear it in Settings → Tools.")
    with caplog.at_level(logging.WARNING, logger="turboadb.tools"):
        assert tools.find_adb() == other
    assert note in caplog.text  # one wording for the log file and the panel


def test_a_good_empty_or_overridden_path_gets_none(adb_setting, monkeypatch):
    other = adb_setting("")
    assert tools.stale_setting_note("adb") is None
    adb_setting(other)  # names an adb
    assert tools.stale_setting_note("adb") is None
    adb_setting("C:/gone/adb.exe")
    monkeypatch.setenv("TURBOADB_ADB", other)  # the environment wins: Settings unused
    assert tools.stale_setting_note("adb") is None


def test_the_main_window_shows_it_at_launch_and_when_the_path_changes(adb_setting, monkeypatch):
    pytest.importorskip("PyQt5")
    import turboadb.gui.main_window as mw

    class Window:
        _log_adb_environment = mw.MainWindow._log_adb_environment
        _log_stale_adb_setting = mw.MainWindow._log_stale_adb_setting
        _offer_adb_restart_for_new_binary = mw.MainWindow._offer_adb_restart_for_new_binary

        def __init__(self):
            self.logged = []

        def _log(self, text):
            self.logged.append(text)

    monkeypatch.setattr(mw.QMessageBox, "question", staticmethod(lambda *a, **k: mw.QMessageBox.No))
    monkeypatch.setattr(tools, "adb_server_env_note", lambda: None)
    window = Window()
    adb_setting("C:/gone/adb.exe")
    window._log_adb_environment()
    assert [line for line in window.logged if "Settings → Tools" in line] == [
        "[WARNING] " + tools.stale_setting_note("adb")]
    window.logged.clear()
    window._offer_adb_restart_for_new_binary()
    assert window.logged[0] == "[WARNING] " + tools.stale_setting_note("adb")

    window.logged.clear()
    adb_setting("")  # cleared: nothing to say
    window._log_adb_environment()
    assert not any("Settings → Tools" in line for line in window.logged)
