"""A device list that could not be asked for is not an empty one.

With no adb installed, or with a local adb server that would not start,
``list_devices`` returned ``[]``: ``turboadb devices`` printed "No devices
found on USB. Plug in…" and exited 0 (``[]`` with --json), and the window's
device scan blamed "another ADB version" on the port for an adb that was not
there.  No test here runs adb: the launcher and the socket probes are fakes.
"""
import os

import pytest

import turboadb.cli as cli
from turboadb import devices, tools, toolsdl
from turboadb.exceptions import ADBNotFoundError


@pytest.fixture
def no_server(monkeypatch):
    """Nothing answers on the adb server's port."""
    monkeypatch.delenv("ANDROID_ADB_SERVER_PORT", raising=False)
    monkeypatch.setattr(devices, "_list_devices_socket", lambda *a, **k: None)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: False)


@pytest.fixture
def no_adb(monkeypatch):
    """No adb anywhere, and no download; the downloads asked for (returned)."""
    monkeypatch.setattr(tools, "_discover", lambda name: None)
    monkeypatch.setattr(tools, "_gui_setting", lambda name: None)
    monkeypatch.delenv("TURBOADB_ADB", raising=False)
    fetched = []
    monkeypatch.setattr(toolsdl, "ensure_tools", lambda **kw: fetched.append(kw) or {})
    tools.clear_tools_cache()
    yield fetched
    tools.clear_tools_cache()


@pytest.fixture
def adb_that_cannot_start_a_server(monkeypatch, tmp_path):
    """An adb whose ``start-server`` ends at once with an error."""
    adb = tmp_path / ("adb.exe" if os.name == "nt" else "adb")
    adb.write_text("")
    monkeypatch.setenv("TURBOADB_ADB", str(adb))
    monkeypatch.setattr(tools, "_LAUNCH_EXIT_GRACE", 0.05)

    class Launcher:
        def __init__(self, cmd, stdout=None, **kw):
            stdout.write(b"* cannot bind 'tcp:5037': Only one usage of each socket address")

        def poll(self):
            return 1

    monkeypatch.setattr(tools.subprocess, "Popen", Launcher)
    tools.clear_tools_cache()
    yield str(adb)
    tools.clear_tools_cache()


def test_no_adb_is_not_an_empty_device_list(no_server, no_adb):
    with pytest.raises(ADBNotFoundError, match="Could not find 'adb'"):
        devices.list_devices()
    with pytest.raises(ADBNotFoundError):
        devices.first_online()
    # the window's scan: the missing adb, not "the port is held by another ADB version"
    with pytest.raises(ADBNotFoundError):
        devices.list_devices(strict=True)


@pytest.mark.parametrize("argv", [["devices"], ["devices", "--json"]])
def test_the_devices_command_exits_3_without_adb(no_server, no_adb, capsys, argv):
    assert cli.main(argv) == 3
    out, err = capsys.readouterr()
    assert out == "" and "ERROR: Could not find 'adb'" in err
    assert "No devices found" not in err


def test_a_running_server_is_listed_without_any_adb(no_adb, monkeypatch):
    """The server is asked over its socket: no adb is needed for that, and
    listing downloads none (the command line asks for a missing adb before
    any device command, as it always did)."""
    monkeypatch.setattr(devices, "_list_devices_socket",
                        lambda *a, **k: [devices.Device("R58M1", "device")])
    assert [d.serial for d in devices.list_devices()] == ["R58M1"]
    assert no_adb == []
    assert cli.main(["devices"]) == 0


def test_a_server_that_will_not_start_says_why(no_server, adb_that_cannot_start_a_server):
    with pytest.raises(ConnectionError, match="could not start on port 5037: .*cannot bind"):
        devices.list_devices(strict=True)
    # callers that asked for no errors keep getting an empty list
    assert devices.list_devices() == []
    assert devices.first_online() is None


@pytest.mark.parametrize("argv", [["devices"], ["devices", "--json"],
                                  ["shell", "--all", "--", "id"]])
def test_the_cli_reports_a_server_that_will_not_start(no_server, adb_that_cannot_start_a_server,
                                                      capsys, argv):
    assert cli.main(argv) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "ERROR: ADB server could not start on port 5037" in err and "cannot bind" in err
    assert "No devices found" not in err and "No online devices" not in err
