"""A TURBOADB_ADB (or TURBOADB_SCRCPY) that names no executable is reported as
such, and never starts a download.

Such a path is an error now, not a reason to run another adb.  But the engine
and the CLI still took the error for "adb is missing": they downloaded
platform-tools (a download can't change which adb the variable names) and
then said that TurboADB "could not install platform-tools", or that adb was
NOT FOUND, instead of naming the variable to correct.  The window used the
adb it found instead and said it was using the variable's."""
import os

import pytest

import turboadb.cli as cli
from turboadb import tools, toolsdl
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBNotFoundError

_EXE = "adb.exe" if os.name == "nt" else "adb"


@pytest.fixture
def stale_env(monkeypatch, tmp_path):
    """TURBOADB_ADB names a folder that is gone; a managed adb exists; auto-fetch
    is on, and a download is recorded instead of made."""
    managed = tmp_path / "managed" / _EXE
    managed.parent.mkdir()
    managed.write_text("")
    monkeypatch.setattr(tools, "_managed_candidates", lambda name: [str(managed)])
    monkeypatch.setattr(tools, "_bundled_candidates", lambda name: [])
    monkeypatch.setattr(tools, "_sdk_candidates", lambda name: [])
    monkeypatch.setattr(tools.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_gui_setting", lambda name: None)
    gone = str(tmp_path / "sdk" / "platform-tools" / _EXE)
    monkeypatch.setenv("TURBOADB_ADB", gone)
    monkeypatch.setenv("TURBOADB_AUTO_FETCH", "1")
    fetched = []
    monkeypatch.setattr(toolsdl, "ensure_tools", lambda **kw: fetched.append(kw) or {})
    tools.clear_tools_cache()
    yield gone, fetched
    tools.clear_tools_cache()


def test_the_error_says_which_path_was_given(stale_env):
    gone, _fetched = stale_env
    with pytest.raises(ADBNotFoundError) as info:
        tools.find_adb()
    assert info.value.configured == gone
    with pytest.raises(ADBNotFoundError) as info:
        tools.find_adb(gone)  # an explicit path, the same way
    assert info.value.configured == gone


def test_nothing_found_at_all_has_no_configured_path(stale_env, monkeypatch):
    monkeypatch.delenv("TURBOADB_ADB")
    monkeypatch.setattr(tools, "_managed_candidates", lambda name: [])
    tools.clear_tools_cache()
    with pytest.raises(ADBNotFoundError) as info:
        tools.find_adb()
    assert info.value.configured is None


def test_a_handler_does_not_download_for_a_stale_turboadb_adb(stale_env):
    gone, fetched = stale_env
    handler = ADBHandler(ADBConfig(serial="R58M1"))
    with pytest.raises(ADBNotFoundError, match="TURBOADB_ADB=") as info:
        handler.adb_path
    assert gone in str(info.value)
    assert "could not install platform-tools" not in str(info.value)
    assert fetched == []


def test_the_cli_names_the_variable_and_downloads_nothing(stale_env, capsys):
    gone, fetched = stale_env
    assert cli.main(["devices"]) == 3
    err = capsys.readouterr().err
    assert f"TURBOADB_ADB={gone}" in err
    assert fetched == []


def test_the_cli_names_a_stale_turboadb_scrcpy_for_a_mirror(stale_env, monkeypatch, tmp_path, capsys):
    gone, fetched = stale_env
    monkeypatch.setenv("TURBOADB_ADB", str(tmp_path / "managed" / _EXE))  # adb is fine
    missing = str(tmp_path / "scrcpy-gone" / ("scrcpy.exe" if os.name == "nt" else "scrcpy"))
    monkeypatch.setenv("TURBOADB_SCRCPY", missing)
    assert cli.main(["scrcpy"]) == 3
    assert f"TURBOADB_SCRCPY={missing}" in capsys.readouterr().err
    assert fetched == []


def test_an_adb_path_option_wins_over_the_variable(stale_env, tmp_path, monkeypatch):
    """--adb-path is used before TURBOADB_ADB, so a good one leaves no problem."""
    good = str(tmp_path / "managed" / _EXE)

    class Args:
        cmd = "devices"
        adb_path = good
        scrcpy_path = None

    assert cli._missing_explicit_tool(Args) is None


def test_doctor_names_the_variable_instead_of_a_download_link(stale_env, monkeypatch, capsys):
    gone, _fetched = stale_env
    monkeypatch.setattr(tools, "adb_server_version", lambda port=5037, timeout=0.25: None)
    monkeypatch.setattr(tools, "describe_adb_server", lambda adb, port=5037: None)
    monkeypatch.setattr(tools, "adb_server_env_note", lambda: None)
    monkeypatch.setattr(cli, "_install_report", lambda: {
        "version": "3.0.0", "path": "E:/work/turboadb", "installed": "3.0.0",
        "installed_path": "E:/work/turboadb", "same": True, "note": None})
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert f"TURBOADB_ADB={gone}" in out
    assert tools.ADB_DOWNLOAD not in out


def test_the_window_warns_that_turboadb_adb_is_not_used(stale_env, monkeypatch):
    pytest.importorskip("PyQt5")
    import turboadb.gui.main_window as mw

    gone, _fetched = stale_env

    class Window:
        _log_adb_environment = mw.MainWindow._log_adb_environment
        _log_stale_adb_setting = mw.MainWindow._log_stale_adb_setting

        def __init__(self):
            self.logged = []
            self.log_panel = self

        def _log(self, text):
            self.logged.append(text)

        append = _log

    monkeypatch.setattr(tools, "adb_server_env_note", lambda: None)
    window = Window()
    window._log_adb_environment()
    notes = [line for line in window.logged if "TURBOADB_ADB" in line]
    assert len(notes) == 1 and notes[0].startswith("[WARNING]") and gone in notes[0]
