"""The address of a remote adb server in the environment of the programs
TurboADB starts.

adb reads ``ANDROID_ADB_SERVER_ADDRESS`` in place of a missing ``-H`` and joins
it with the port the same way, into ``tcp:HOST:PORT``.  For a bare IPv6 literal
scrcpy's adb and an ``adb`` typed in a PowerShell/CMD tab dialled
``2001:db8::1:5199`` on port 5555 (adb 37 says so in its error).  The literal
goes in brackets now, as on the command line; an IPv4 address or a host name
stays bare, since adb wraps it itself.
"""
import types

import pytest

from turboadb import devices, scrcpy
from turboadb.config import adb_server_address


@pytest.mark.parametrize("host, written", [
    ("192.168.1.50", "192.168.1.50"),
    ("lab-pc.local", "lab-pc.local"),
    ("2001:db8::1", "[2001:db8::1]"),
    ("[2001:db8::1]", "[2001:db8::1]"),
    (" ::1 ", "[::1]"),
])
def test_the_server_address_keeps_an_ipv6_literal_in_brackets(host, written):
    assert adb_server_address(host) == written


def test_scrcpys_adb_is_pointed_at_an_ipv6_server_in_brackets():
    env = scrcpy._server_env("2001:db8::1", 5199, "adb")
    assert env["ANDROID_ADB_SERVER_ADDRESS"] == "[2001:db8::1]"
    assert env["ANDROID_ADB_SERVER_PORT"] == "5199"
    assert env["ADB"] == "adb"
    # adb wraps a plain host itself: "tcp:10.0.0.4" would be wrapped twice
    assert scrcpy._server_env("10.0.0.4", 5037)["ANDROID_ADB_SERVER_ADDRESS"] == "10.0.0.4"


def test_a_mirror_through_an_ipv6_server_starts_scrcpy_with_the_bracketed_address(monkeypatch):
    started = []

    class Process:
        pid = 123

        @staticmethod
        def poll():
            return 0

    monkeypatch.setattr(scrcpy, "find_scrcpy", lambda _path=None: "scrcpy")
    monkeypatch.setattr(scrcpy, "is_local_host", lambda host: False)
    monkeypatch.setattr(scrcpy.subprocess, "Popen",
                        lambda cmd, **kw: started.append(kw["env"]) or Process())
    scrcpy.launch_scrcpy("one", adb_server_host="2001:db8::1", adb_server_port=5199)
    assert started[0]["ANDROID_ADB_SERVER_ADDRESS"] == "[2001:db8::1]"
    assert started[0]["ANDROID_ADB_SERVER_PORT"] == "5199"


def test_an_adb_typed_in_a_local_terminal_reaches_an_ipv6_server():
    lt = pytest.importorskip("turboadb.gui.local_terminal")
    env = lt.build_shell_env({}, adb_server_host="2001:db8::1", adb_server_port=5199)
    assert env["ANDROID_ADB_SERVER_ADDRESS"] == "[2001:db8::1]"
    assert env["ANDROID_ADB_SERVER_PORT"] == "5199"
    named = lt.build_shell_env({}, adb_server_host="lab-pc", adb_server_port=5199)
    assert named["ANDROID_ADB_SERVER_ADDRESS"] == "lab-pc"


def test_the_wireless_debugging_scan_reaches_an_ipv6_server(monkeypatch):
    ran = []
    monkeypatch.setattr(devices, "find_adb", lambda explicit=None: "adb")
    monkeypatch.setattr(devices.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(stdout=""))
    devices.mdns_devices("adb", server_host="2001:db8::1", server_port=5199)
    assert ran == [["adb", "-H", "[2001:db8::1]", "-P", "5199", "mdns", "services"]]
