"""The connect path of a device tab: no redundant adb one-shots, and a closed
tab never leaves its connect's adb child running.

A tab used to run ``adb connect`` (network targets), ``adb wait-for-device``
and ``adb get-state`` before its probe, even for a device the main window's
tracker had just reported as online, and closing the tab only set a flag: a
``wait-for-device`` for an absent device ran on for up to 20 s.
"""
import subprocess
import sys
import threading
import time

import pytest

import turboadb.core as core
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBConnectionError

try:  # conftest stubs _ConnectThread.run for every test; keep the real one
    from turboadb.gui.device_tab import _ConnectThread as _RealConnectThread

    _REAL_CONNECT_RUN = _RealConnectThread.run
except Exception:  # PyQt5 is not installed: the GUI tests below skip
    _REAL_CONNECT_RUN = None


def _adb_verbs(fake_adb):
    """The adb subcommand of every call (``connect``, ``wait-for-device``, ...)."""
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


# --------------------------------------------------------------------------- #
# known_state: the tracker already saw the device online
# --------------------------------------------------------------------------- #
def test_a_usb_device_the_tracker_reports_online_needs_no_wait_or_get_state(
        fake_adb, server_up):
    handler = ADBHandler(ADBConfig(serial="R58M123"))
    handler.connect(known_state="device", safe=False)
    assert fake_adb.calls == []  # before: wait-for-device + get-state
    assert handler._connected is True


def test_without_a_known_state_a_successful_wait_is_enough(fake_adb, server_up):
    handler = ADBHandler(ADBConfig(serial="R58M123"))
    handler.connect(safe=False)
    assert _adb_verbs(fake_adb) == ["wait-for-device"]  # its exit 0 means "device"


def test_a_failed_wait_still_asks_for_the_state_and_reports_it(fake_adb, server_up):
    fake_adb.add("wait-for-device", returncode=1, stderr="error: device unauthorized.")
    fake_adb.add("get-state", returncode=1, stderr="error: device unauthorized.")
    handler = ADBHandler(ADBConfig(serial="R58M123"))
    with pytest.raises(ADBConnectionError, match="unauthorized"):
        handler.connect(safe=False)
    assert _adb_verbs(fake_adb) == ["wait-for-device", "get-state"]


def test_a_state_other_than_device_is_checked_as_before(fake_adb, server_up):
    handler = ADBHandler(ADBConfig(serial="R58M123"))
    handler.connect(known_state="unauthorized", safe=False)
    assert _adb_verbs(fake_adb) == ["wait-for-device"]


def test_without_auto_wait_get_state_decides(fake_adb, server_up):
    fake_adb.add("get-state", stdout="device\n")
    handler = ADBHandler(ADBConfig(serial="R58M123", auto_wait=False))
    handler.connect(safe=False)
    assert _adb_verbs(fake_adb) == ["get-state"]


def test_an_already_connected_network_target_skips_the_wait(fake_adb, server_up):
    fake_adb.add(lambda argv: "connect" in argv, stdout="already connected to 10.0.0.5:5555")
    handler = ADBHandler(ADBConfig(host="10.0.0.5"))
    handler.connect(known_state="device", safe=False)
    # adb connect still runs: it is what tells a connection of our own from one
    # that another program made
    assert _adb_verbs(fake_adb) == ["connect"]
    assert handler.owns_connection is False


def test_a_connection_just_made_is_still_waited_for(fake_adb, server_up):
    """A tracker entry is stale when adb connect has only now made the
    connection: the device may still be authorizing, so the wait runs."""
    fake_adb.add(lambda argv: "connect" in argv, stdout="connected to 10.0.0.5:5555")
    handler = ADBHandler(ADBConfig(host="10.0.0.5"))
    handler.connect(known_state="device", safe=False)
    assert _adb_verbs(fake_adb) == ["connect", "wait-for-device"]
    assert handler.owns_connection is True


def test_binding_to_the_only_online_device_needs_no_wait(fake_adb, server_up, monkeypatch):
    from turboadb.devices import Device

    monkeypatch.setattr(core, "list_devices", lambda *_a, **_k: [
        Device("emulator-5554", "device"), Device("R58M9", "unauthorized")])
    handler = ADBHandler(ADBConfig())
    handler.connect(safe=False)
    assert handler.serial == "emulator-5554"
    assert fake_adb.calls == []  # the server has just listed it online


# --------------------------------------------------------------------------- #
# cancel: the adb child is killed, not left to run out its timeout
# --------------------------------------------------------------------------- #
@pytest.fixture
def slow_adb(monkeypatch):
    """Every adb command is a real process that would sleep for 30 s; records
    each Popen so the test can check it is gone."""
    spawned = []
    real_popen = subprocess.Popen

    class Recording(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spawned.append(self)

    monkeypatch.setattr(core.subprocess, "Popen", Recording)
    monkeypatch.setattr(core, "is_adb_server_alive", lambda **_kw: True)
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    monkeypatch.setattr(ADBHandler, "_base", lambda self, target=True: list(sleeper))
    yield spawned
    for proc in spawned:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_cancel_kills_a_waiting_wait_for_device(slow_adb):
    handler = ADBHandler(ADBConfig(serial="absent", connect_timeout=30))
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    result = handler.connect(cancel=cancel, safe=True)
    took = time.monotonic() - started
    assert not result.success and "cancelled" in str(result.error)
    assert took < 5.0  # not the 30 s the child would have run
    assert len(slow_adb) == 1 and slow_adb[0].poll() is not None  # the child is gone


def test_cancel_before_the_wait_starts_no_process(slow_adb):
    handler = ADBHandler(ADBConfig(serial="absent"))
    cancel = threading.Event()
    cancel.set()
    assert not handler.connect(cancel=cancel, safe=True).success
    assert slow_adb == []


def test_a_cancellable_wait_still_times_out_and_kills_its_child(slow_adb):
    handler = ADBHandler(ADBConfig(serial="absent", connect_timeout=0.5))
    with pytest.raises(ADBConnectionError, match="Timed out"):
        handler.connect(cancel=threading.Event(), safe=False)
    assert slow_adb and all(proc.poll() is not None for proc in slow_adb)


def test_a_cancellable_command_returns_its_output(monkeypatch):
    handler = ADBHandler(ADBConfig(serial="x"))
    code = "import sys; print('device'); sys.stderr.write('note'); sys.exit(3)"
    monkeypatch.setattr(ADBHandler, "_base", lambda self, target=True: [sys.executable, "-c", code])
    res = handler._run(["get-state"], timeout=20, cancel=threading.Event())
    assert (res.exit_code, res.text, res.stderr.strip()) == (3, "device", "note")


# --------------------------------------------------------------------------- #
# the device tab: what it passes to connect, and what cancel does
# --------------------------------------------------------------------------- #
class _Tracked:
    """Stands in for a Device from the main window's tracker."""

    def __init__(self, serial, state):
        self.serial, self.state = serial, state


@pytest.fixture
def window(qapp):
    """A top-level widget with the main window's tracker list; tabs go inside."""
    from PyQt5.QtWidgets import QWidget

    host = QWidget()
    host._live_devices = [_Tracked("R58M123", "device"), _Tracked("10.0.0.5:5555", "device"),
                          _Tracked("R58M999", "unauthorized")]
    yield host
    host.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("session, expected", [
    ({"name": "usb", "serial": "R58M123"}, "device"),
    ({"name": "slow", "serial": "R58M999"}, "unauthorized"),
    ({"name": "net", "type": "network", "host": "10.0.0.5", "port": 5555}, "device"),
    ({"name": "gone", "serial": "OTHER"}, None),
    ({"name": "only"}, None),  # "the only device": no serial to look up
    # the tracker watches this PC's adb server, not a remote one
    ({"name": "far", "type": "remote", "serial": "R58M123", "adb_host": "10.9.9.9"}, None),
])
def test_start_connect_hands_the_trackers_state_to_the_connect(window, session, expected):
    from turboadb.gui.device_tab import DeviceTab

    tab = DeviceTab(session, parent=window)  # window() is the one with the tracker
    started = []
    tab._ct.start = lambda: started.append(tab._ct.known_state)
    try:
        tab.start_connect()
        assert started == [expected]
    finally:
        tab.close_session()


def test_the_connect_worker_passes_the_state_and_its_cancel_event(qapp):
    from turboadb.gui.device_tab import _ConnectThread

    seen = {}

    class Handler:
        def connect(self, **kwargs):
            seen.update(kwargs)
            return self

    worker = _ConnectThread(ADBConfig(serial="R58M123"), fetch_identity=False,
                            known_state="device")
    worker._make_handler = Handler
    connected = []
    worker.ok.connect(lambda handler, _info: connected.append(handler))
    _REAL_CONNECT_RUN(worker)  # run() itself, on this thread
    assert seen == {"known_state": "device", "cancel": worker._cancel_event}
    assert len(connected) == 1


def test_closing_the_tab_cancels_its_connect(qapp):
    """close_session -> _ConnectThread.cancel() sets the event the engine's
    wait-for-device watches."""
    from turboadb.gui.device_tab import DeviceTab

    tab = DeviceTab({"name": "absent", "serial": "absent"})
    event = tab._ct._cancel_event
    assert not event.is_set()
    tab.close_session()
    assert event.is_set()
    tab.close()
