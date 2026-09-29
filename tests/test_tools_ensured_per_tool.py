"""The managed tools are seen to once per process, each on its own.

``ensure_tools()`` kept one "tools were ensured" flag.  A caller that asked for
adb alone (the CLI) set it, and a later caller in the same process that needs
scrcpy too (the GUI's startup check) got "already-ensured": scrcpy was never
fetched.  Each tool now has its own entry.
"""
import pytest

from turboadb import toolsdl


@pytest.fixture
def fetched(monkeypatch):
    """A fresh process whose cache has adb but no scrcpy; downloads are recorded."""
    monkeypatch.setattr(toolsdl, "_ensured", set())
    monkeypatch.setenv("TURBOADB_AUTO_FETCH", "1")
    monkeypatch.setattr(toolsdl, "_sync_scrcpy_adb", lambda: None)
    monkeypatch.setattr(toolsdl, "_write_stamp", lambda version: None)
    monkeypatch.setattr(toolsdl, "_read_stamp", lambda: "")
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: True)
    monkeypatch.setattr(toolsdl, "managed_adb", lambda: "/managed/adb")
    monkeypatch.setattr(toolsdl, "managed_scrcpy", lambda: None)
    downloads = []
    monkeypatch.setattr(toolsdl, "fetch_tools",
                        lambda **kw: downloads.append((kw["adb"], kw["scrcpy"])) or {"errors": {}})
    return downloads


def test_an_adb_only_check_leaves_scrcpy_to_the_next_caller(fetched):
    assert toolsdl.ensure_tools(scrcpy=False)["note"] == "up-to-date"  # adb is there
    assert fetched == []
    toolsdl.ensure_tools()  # the GUI's check: scrcpy is missing
    assert fetched == [(False, True)]
    assert toolsdl.ensure_tools()["note"] == "already-ensured"
    assert toolsdl.ensure_tools(scrcpy=False)["note"] == "already-ensured"
    assert fetched == [(False, True)]


def test_adb_is_asked_for_once_per_process(fetched, monkeypatch):
    monkeypatch.setattr(toolsdl, "managed_adb", lambda: None)  # its download failed
    toolsdl.ensure_tools(scrcpy=False)
    toolsdl.ensure_tools()
    assert fetched == [(True, False), (False, True)]


def test_a_check_of_both_covers_an_adb_only_one(fetched):
    toolsdl.ensure_tools()
    assert toolsdl.ensure_tools(scrcpy=False)["note"] == "already-ensured"
    assert fetched == [(False, True)]
