"""Connecting: a device that has not authorised this PC yet, the adb that
starts the server, and what a handler forgets when it moves to another device.

``adb connect`` to a device that has never seen this PC's key answers "failed
to authenticate to HOST:PORT", but the connection is made: it is
'unauthorized' until someone accepts the "Allow USB debugging?" prompt, and
then it is 'device'. That used to be treated as a failed connect, so the first
wireless connect to a new PC always failed.
"""
import os
import threading
import time

import pytest

import turboadb.core as core
import turboadb.tools as tools
from turboadb import toolsdl
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.devices import Device
from turboadb.exceptions import ADBConnectionError, ADBNotFoundError
from turboadb.results import CommandResult

UNAUTHORIZED = (
    "error: device unauthorized.\nThis adb server's $ADB_VENDOR_KEYS is not set\n"
    "Try 'adb kill-server' if that seems wrong.\n"
    "Otherwise check for a confirmation dialog on your device.\n"
)


def _verbs(fake_adb):
    """The adb subcommand of every call (``connect``, ``get-state``, ...)."""
    verbs = []
    for argv in fake_adb.calls:
        args = argv[1:]
        while args and args[0] in ("-s", "-H", "-P"):
            args = args[2:]
        verbs.append(args[0] if args else "")
    return verbs


@pytest.fixture
def server_up(monkeypatch):
    """A local adb server that answers, without touching the real port 5037."""
    monkeypatch.setattr(core, "is_adb_server_alive", lambda **_kw: True)
    monkeypatch.setattr(ADBHandler, "_AUTH_POLL_S", 0.01)


def _device_asking(fake_adb, target="10.0.0.5:5555", *, accepted_after=None):
    """``adb connect`` finds a device showing its prompt; it is accepted at
    the *accepted_after*-th get-state (never, with None)."""
    fake_adb.add(lambda argv: "connect" in argv, stdout=f"failed to authenticate to {target}\n")
    looks = []

    def accepted(argv):
        if "get-state" not in argv:
            return False
        looks.append(argv)
        return accepted_after is not None and len(looks) >= accepted_after

    fake_adb.add(accepted, stdout="device\n")
    fake_adb.add("get-state", returncode=1, stderr=UNAUTHORIZED)
    return looks


# --------------------------------------------------------------------------- #
# a device that has not authorised this PC yet
# --------------------------------------------------------------------------- #
def test_connect_waits_for_the_prompt_to_be_accepted(fake_adb, server_up):
    lines = []
    handler = ADBHandler(ADBConfig(host="10.0.0.5"), log_callback=lines.append)
    _device_asking(fake_adb, accepted_after=3)
    handler.connect(safe=False)
    assert _verbs(fake_adb) == ["connect", "get-state", "get-state", "get-state"]
    assert handler.serial == "10.0.0.5:5555" and handler._connected
    # this connect made the connection, so it is the handler's to drop
    assert handler.owns_connection is True
    assert any("[WARNING]" in line and "Allow USB debugging" in line for line in lines)


def test_a_prompt_nobody_accepts_is_reported_as_such(fake_adb, server_up):
    handler = ADBHandler(ADBConfig(host="10.0.0.5", connect_timeout=0.05))
    _device_asking(fake_adb)
    with pytest.raises(ADBConnectionError, match="'unauthorized'") as info:
        handler.connect(safe=False)
    message = str(info.value)
    assert "Allow USB debugging" in message
    assert "adb tcpip" not in message  # not the "check the IP" advice of a failed connect
    # the connection this call made stays its own, so a closing tab can drop it
    assert handler.owns_connection is True


def test_without_auto_wait_the_state_is_reported_at_once(fake_adb, server_up):
    handler = ADBHandler(ADBConfig(host="10.0.0.5", auto_wait=False))
    _device_asking(fake_adb, accepted_after=2)
    with pytest.raises(ADBConnectionError, match="'unauthorized', not ready"):
        handler.connect(safe=False)
    assert _verbs(fake_adb) == ["connect", "get-state"]


def test_closing_the_tab_ends_the_wait(fake_adb, server_up, monkeypatch):
    monkeypatch.setattr(ADBHandler, "_AUTH_POLL_S", 0.05)
    handler = ADBHandler(ADBConfig(host="10.0.0.5", connect_timeout=30))
    # the connect's own cancellable adb child is covered by the tab's
    # connect tests; here it answers at once
    run_global = handler._run_global
    handler._run_global = lambda args, **kw: (
        CommandResult(" ".join(args), 0, "failed to authenticate to 10.0.0.5:5555\n", "", 0.0)
        if args[0] == "connect" else run_global(args, **kw))
    fake_adb.add("get-state", returncode=1, stderr=UNAUTHORIZED)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    result = handler.connect(cancel=cancel, safe=True)
    assert not result.success and "cancelled" in str(result.error)
    assert time.monotonic() - started < 5.0  # not the 30 s connect_timeout


def test_connect_tcp_waits_for_the_prompt_too(fake_adb, server_up):
    handler = ADBHandler(ADBConfig())
    _device_asking(fake_adb, "10.0.0.7:5555", accepted_after=2)
    assert handler.connect_tcp("10.0.0.7", 5555) == "10.0.0.7:5555"
    assert _verbs(fake_adb) == ["connect", "get-state", "get-state"]
    assert handler.owns_connection is True


def test_go_wireless_waits_for_the_prompt_instead_of_retrying(fake_adb, server_up):
    fake_adb.add("ip route", stdout="192.168.1.0/24 dev wlan0 proto kernel src 192.168.1.20\n")
    fake_adb.add("tcpip", stdout="restarting in TCP mode port: 5555\n")
    _device_asking(fake_adb, "192.168.1.20:5555", accepted_after=2)
    handler = ADBHandler(ADBConfig(serial="R58M1"))
    assert handler.go_wireless() == "192.168.1.20:5555"
    assert _verbs(fake_adb).count("connect") == 1
    assert handler.serial == "192.168.1.20:5555" and handler.owns_connection


def test_a_prompt_nobody_accepts_leaves_the_handler_on_its_device(fake_adb, server_up):
    """A device tab runs go_wireless on its own handler: a switch that fails
    must leave it on the USB device, as any other failed connect does. (By
    default the wait outlasts go_wireless's 10 s of retries for adbd.)"""
    _device_asking(fake_adb, "192.168.1.20:5555")
    handler = ADBHandler(ADBConfig(serial="R58M1", connect_timeout=0.05))
    handler._touch = ("the USB device's touchscreen", True)
    with pytest.raises(ADBConnectionError, match="'unauthorized'"):
        handler.connect_tcp("192.168.1.20", 5555)
    assert handler.serial == "R58M1" and handler.config.host is None
    assert handler._touch == ("the USB device's touchscreen", True)
    assert handler.owns_connection is False


def test_an_ordinary_failed_connect_still_fails_at_once(fake_adb, server_up):
    fake_adb.add(lambda argv: "connect" in argv,
                 stdout="failed to connect to '10.0.0.5:5555': Connection refused\n")
    with pytest.raises(ADBConnectionError, match="adb tcpip"):
        ADBHandler(ADBConfig(host="10.0.0.5")).connect(safe=False)
    assert _verbs(fake_adb) == ["connect"]


# --------------------------------------------------------------------------- #
# the adb that starts the local server
# --------------------------------------------------------------------------- #
def test_a_missing_adb_is_fetched_and_reported_not_a_server_that_never_started(monkeypatch):
    monkeypatch.setattr(core, "is_adb_server_alive", lambda **_kw: False)

    def missing(explicit=None):
        raise ADBNotFoundError("Could not find 'adb' (Android Platform-Tools).")

    monkeypatch.setattr(core, "find_adb", missing)
    monkeypatch.setattr(tools, "find_adb", missing)
    fetched, started = [], []
    monkeypatch.setattr(toolsdl, "ensure_tools",
                        lambda **kw: fetched.append(kw) or {"errors": {"adb": "offline"}})
    monkeypatch.setattr(tools, "ensure_adb_server",
                        lambda adb, timeout=15.0, port=5037: started.append(adb) or False)
    with pytest.raises(ADBNotFoundError, match="could not install platform-tools"):
        ADBHandler(ADBConfig(serial="R58M1")).connect(safe=False)
    assert len(fetched) == 1  # the one-time download was tried
    assert started == []  # and no server start without an adb


def test_the_server_is_started_with_the_resolved_adb(fake_adb, monkeypatch):
    monkeypatch.setattr(core, "is_adb_server_alive", lambda **_kw: False)
    started = []
    monkeypatch.setattr(tools, "ensure_adb_server",
                        lambda adb, timeout=15.0, port=5037: started.append(adb) or True)
    ADBHandler(ADBConfig(serial="R58M1")).connect(safe=False)
    assert started == [os.environ["TURBOADB_ADB"]]


def test_a_server_that_does_not_start_says_why(fake_adb, monkeypatch):
    monkeypatch.setattr(core, "is_adb_server_alive", lambda **_kw: False)
    monkeypatch.setattr(tools, "ensure_adb_server", lambda *a, **k: False)
    monkeypatch.setattr(tools, "last_adb_server_error",
                        lambda: "cannot bind 'tcp:5037': Only one usage of each socket address")
    with pytest.raises(ADBConnectionError, match="port 5037: cannot bind 'tcp:5037'"):
        ADBHandler(ADBConfig(serial="R58M1")).connect(safe=False)


def test_the_only_device_is_found_with_the_resolved_adb(fake_adb, monkeypatch):
    monkeypatch.setattr(core, "is_adb_server_alive", lambda **_kw: True)
    asked = []
    monkeypatch.setattr(core, "list_devices",
                        lambda adb, **_kw: asked.append(adb) or [Device("R58M1", "device")])
    handler = ADBHandler(ADBConfig())
    handler.connect(safe=False)
    assert asked == [os.environ["TURBOADB_ADB"]] and handler.serial == "R58M1"


def test_a_remote_server_is_listed_without_resolving_a_local_adb(monkeypatch):
    """A remote server answers over its socket: a PC without adb must not
    download one just to list that server's devices."""
    asked = []
    monkeypatch.setattr(core, "list_devices",
                        lambda adb, **_kw: asked.append(adb) or [Device("R58M1", "device")])
    monkeypatch.setattr(ADBHandler, "_resolve_adb",
                        lambda self: pytest.fail("no local adb is needed to list a remote server"))
    handler = ADBHandler(ADBConfig(adb_server_host="10.0.0.9"))
    handler.connect(safe=False)
    assert asked == [None] and handler.serial == "R58M1"


# --------------------------------------------------------------------------- #
# per-device details when the handler moves to another device
# --------------------------------------------------------------------------- #
PANEL_A = """add device 1: /dev/input/event3
  name:     "goodix-ts"
  events:
    ABS (0003): ABS_MT_SLOT           : value 0, min 0, max 9, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_X     : value 0, min 0, max 4095, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y     : value 0, min 0, max 8191, fuzz 0, flat 0, resolution 0
  input props:
    INPUT_PROP_DIRECT
"""

PANEL_B = """add device 1: /dev/input/event2
  name:     "atmel-ts"
  events:
    ABS (0003): ABS_MT_POSITION_X     : value 0, min 0, max 1079, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y     : value 0, min 0, max 1919, fuzz 0, flat 0, resolution 0
  input props:
    INPUT_PROP_DIRECT
"""


def _two_devices(fake_adb, connect_answer):
    fake_adb.add(lambda argv: "10.0.0.5:5555" in argv and "getevent" in argv, stdout=PANEL_A)
    fake_adb.add(lambda argv: "10.0.0.6:5555" in argv and "getevent" in argv, stdout=PANEL_B)
    fake_adb.add(lambda argv: "connect" in argv, stdout=connect_answer)


def test_another_device_gets_its_own_touchscreen(fake_adb):
    """tap_burst sent device A's /dev/input node to device B."""
    _two_devices(fake_adb, "connected to 10.0.0.6:5555")
    handler = ADBHandler(ADBConfig(host="10.0.0.5"))
    assert handler.touch_device()["path"] == "/dev/input/event3"
    handler._screencap_ids[2] = 4619827551948147201  # A's physical display id
    handler._cap_method = 2
    handler.connect_tcp("10.0.0.6", 5555)
    assert handler.touch_device()["path"] == "/dev/input/event2"
    assert handler._screencap_ids == {} and handler._cap_method is None


def test_reconnecting_the_same_device_keeps_what_was_learnt(fake_adb):
    _two_devices(fake_adb, "already connected to 10.0.0.5:5555")
    handler = ADBHandler(ADBConfig(host="10.0.0.5"))
    handler.touch_device()
    handler._screencap_ids[2] = 4619827551948147201
    handler._cap_method = 2
    handler.connect_tcp("10.0.0.5", 5555)  # the tab's reconnect after a reboot
    handler.touch_device()
    assert sum("getevent" in argv for argv in fake_adb.calls) == 1  # not read again
    assert handler._screencap_ids == {2: 4619827551948147201} and handler._cap_method == 2
