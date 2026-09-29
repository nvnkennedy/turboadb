"""Addresses of a remote adb server and of a network device.

adb writes the server it talks to as ``tcp:HOST:PORT``, so an IPv6 host needs
its brackets on the command line; a network host typed with its port must be
split, not bracketed whole; and a server on another machine can be stopped
from here but never restarted, since adb only starts a server on this PC.
"""
import pytest

import turboadb.cli as cli
import turboadb.core as core
from turboadb import devices
from turboadb.config import ADBConfig, adb_server_args
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBError


# --------------------------------------------------------------------------- #
# IPv6 remote servers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("host, written", [
    ("10.0.0.9", "10.0.0.9"),
    ("lab-pc.local", "lab-pc.local"),
    ("fd00::10", "[fd00::10]"),
    ("[fd00::10]", "[fd00::10]"),
    ("::1", "[::1]"),
])
def test_server_flags_keep_an_ipv6_host_in_brackets(host, written):
    assert adb_server_args(host, 5038) == ["-H", written, "-P", "5038"]


def test_every_command_reaches_an_ipv6_server_on_its_own_port(fake_adb):
    handler = ADBHandler(ADBConfig(serial="R58M1", adb_server_host="[fd00::10]:5038"))
    handler._run(["get-state"])
    assert fake_adb.argv_after_adb()[:6] == ["-H", "[fd00::10]", "-P", "5038", "-s", "R58M1"]
    # stored bare: sockets and host lookups want the plain literal
    assert handler.config.adb_server_host == "fd00::10"


def test_the_device_list_reaches_an_ipv6_server_on_its_own_port(fake_adb, monkeypatch):
    monkeypatch.setattr(devices, "_list_devices_socket", lambda *a, **k: None)
    fake_adb.add("devices", stdout="List of devices attached\nR58M1\tdevice\n")
    found = devices.list_devices(server_host="fd00::10", server_port=5038)
    assert [d.serial for d in found] == ["R58M1"]
    assert fake_adb.argv_after_adb() == ["-H", "[fd00::10]", "-P", "5038", "devices", "-l"]


# --------------------------------------------------------------------------- #
# a network host given with its port
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("host, port, target", [
    ("10.0.0.5:5555", 5555, "10.0.0.5:5555"),
    ("10.0.0.5:5557", 5555, "10.0.0.5:5557"),  # the host's own port wins
    (" 10.0.0.5 ", 5556, "10.0.0.5:5556"),
    ("[fe80::1]:5555", 5555, "[fe80::1]:5555"),
    ("[fe80::1]", 5556, "[fe80::1]:5556"),
    ("fe80::1", 5555, "[fe80::1]:5555"),  # a bare IPv6 literal is never split
])
def test_a_host_given_with_its_port_is_split(host, port, target):
    assert ADBConfig(host=host, port=port).target == target


def test_a_blank_host_is_no_host():
    assert ADBConfig(host="  ", serial="R58M1").target == "R58M1"


def test_a_port_in_the_host_is_checked_like_any_other():
    with pytest.raises(ValueError):
        ADBConfig(host="10.0.0.5:70000")


def test_a_saved_host_with_its_port_connects(fake_adb, monkeypatch):
    """An imported target {"host": "10.0.0.5:5555"} used to run
    ``adb connect [10.0.0.5:5555]:5555``."""
    monkeypatch.setattr(core, "is_adb_server_alive", lambda **_k: True)
    fake_adb.add(lambda argv: "connect" in argv, stdout="connected to 10.0.0.5:5555")
    handler = ADBHandler(ADBConfig(host="10.0.0.5:5555"))
    handler.connect(safe=False)
    assert fake_adb.argv_after_adb(0) == ["connect", "10.0.0.5:5555"]
    assert handler.serial == "10.0.0.5:5555"


# --------------------------------------------------------------------------- #
# restarting and stopping a server on another machine
# --------------------------------------------------------------------------- #
def test_a_remote_server_is_never_restarted_from_here(fake_adb):
    handler = ADBHandler(ADBConfig(adb_server_host="10.0.0.5"))
    with pytest.raises(ADBError, match="10.0.0.5:5037.*on that machine"):
        handler.restart_server()
    result = handler.restart_server(safe=True)
    assert result.success is False and "10.0.0.5:5037" in str(result.error)
    assert fake_adb.calls == []  # no kill-server reached the shared server


def test_an_ipv6_remote_server_is_named_with_brackets(fake_adb):
    with pytest.raises(ADBError, match=r"\[fd00::10\]:5037"):
        ADBHandler(ADBConfig(adb_server_host="fd00::10")).restart_server()


def test_the_cli_refuses_a_remote_restart(fake_adb, capsys):
    assert cli.main(["--adb-host", "10.0.0.5", "restart-server"]) == 1
    assert "10.0.0.5:5037" in capsys.readouterr().err
    assert fake_adb.calls == []


def test_an_explicit_stop_still_reaches_the_remote_server(fake_adb):
    """Stopping the server is what a stop asks for, wherever it runs."""
    assert ADBHandler(ADBConfig(adb_server_host="10.0.0.5")).stop_server() is True
    assert [c[1:] for c in fake_adb.calls] == [["-H", "10.0.0.5", "-P", "5037", "kill-server"]]
