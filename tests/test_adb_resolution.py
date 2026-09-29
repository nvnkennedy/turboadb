"""An adb path set on purpose is the adb TurboADB runs, or an error.

find_adb went on to the managed, PATH and SDK copies when the path it was
given (``ADBConfig(adb_path=...)``, ``--adb-path``, ``TURBOADB_ADB``) named no
executable, so a moved SDK quietly ran another adb, which restarted the server
the chosen one had started, and nothing said why.  The path in Settings is a
preference: a stale one still falls back, with a warning.
"""
import logging
import os
import types

import pytest

from turboadb import tools, toolsdl
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBNotFoundError

_EXE = "adb.exe" if os.name == "nt" else "adb"


@pytest.fixture
def adbs(monkeypatch, tmp_path):
    """One adb auto-detection finds (the managed copy); no setting, no env."""
    managed = tmp_path / "managed" / _EXE
    managed.parent.mkdir()
    managed.write_text("")
    monkeypatch.setattr(tools, "_managed_candidates", lambda name: [str(managed)])
    monkeypatch.setattr(tools, "_bundled_candidates", lambda name: [])
    monkeypatch.setattr(tools, "_sdk_candidates", lambda name: [])
    monkeypatch.setattr(tools.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_gui_setting", lambda name: None)
    monkeypatch.delenv("TURBOADB_ADB", raising=False)
    tools.clear_tools_cache()
    yield types.SimpleNamespace(
        managed=str(managed), gone=str(tmp_path / "sdk" / "platform-tools" / _EXE)
    )
    tools.clear_tools_cache()


def test_a_given_path_that_is_gone_is_an_error_not_another_adb(adbs):
    with pytest.raises(ADBNotFoundError, match="platform-tools"):
        tools.find_adb(adbs.gone)
    assert tools.adb_available(adbs.gone) is False
    assert tools.find_adb() == adbs.managed  # nothing given: auto-detection as before


def test_turboadb_adb_that_is_gone_is_an_error(adbs, monkeypatch):
    monkeypatch.setenv("TURBOADB_ADB", adbs.gone)
    with pytest.raises(ADBNotFoundError, match="TURBOADB_ADB="):
        tools.find_adb()


def test_a_handler_never_swaps_its_adb_for_another(adbs, monkeypatch):
    """core._resolve_adb refuses to replace an explicit path; that guard could
    not fire while find_adb fell back by itself."""
    monkeypatch.setattr(toolsdl, "ensure_tools", lambda **kw: pytest.fail("downloaded tools"))
    handler = ADBHandler(ADBConfig(adb_path=adbs.gone))
    with pytest.raises(ADBNotFoundError):
        handler.adb_path


def test_a_stale_settings_path_warns_and_falls_back(adbs, monkeypatch, caplog):
    monkeypatch.setattr(tools, "_gui_setting", lambda name: adbs.gone)
    with caplog.at_level(logging.WARNING, logger="turboadb.tools"):
        assert tools.find_adb() == adbs.managed
    assert "Settings" in caplog.text and adbs.gone in caplog.text
    # the GUI hands its Settings path on as an explicit one: still a preference
    tools.clear_tools_cache()
    assert tools.find_adb(adbs.gone) == adbs.managed


def test_a_path_that_exists_is_used(adbs, tmp_path, monkeypatch):
    chosen = tmp_path / "chosen" / _EXE
    chosen.parent.mkdir()
    chosen.write_text("")
    assert tools.find_adb(str(chosen)) == str(chosen)
    assert tools.find_adb(str(chosen.parent)) == str(chosen)  # a folder holding adb
    monkeypatch.setenv("TURBOADB_ADB", str(chosen))
    assert tools.find_adb() == str(chosen)
