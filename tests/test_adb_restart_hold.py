"""A device tab held for an ADB restart or a tools update runs no adb by itself.

Restart ADB and a tools update stop this PC's adb server and start it again.
The tabs on it are held meanwhile, but only their shells and logcats were:

* a tab whose device was away (a reboot, an unplugged cable) kept asking
  ``adb get-state`` and ``adb connect`` every two seconds, and
* a visible Phone page kept asking for the call state every four.

One of those landed while the server was down, and adb started a server of its
own: one TurboADB never recorded (it outlived the app), or one run from the
tools folder being replaced (the update then failed).  The wait for the device
now ends with the hold, and one recovery starts once the server is back, for
the time the wait still had.  The Phone page asks nothing until then.
"""

import threading
import types

import pytest

pytest.importorskip("PyQt5")

from turboadb.results import OperationResult  # noqa: E402

from test_adb_processes import _Device, _close, _connect, _pump, _tab  # noqa: E402


class _AwayDevice(_Device):
    """A device whose adb state the test sets ("offline": it is away)."""

    state = "device"

    def get_state(self):
        return self._one_shot("get_state", self.state)


@pytest.fixture
def fast_waits(monkeypatch):
    """Waits for the device that poll at once and often; the list holds the
    time each was given."""
    from turboadb.gui import device_tab

    timeouts = []

    class Fast(device_tab._ReconnectThread):
        def __init__(self, handler, timeout=180, **_kwargs):
            timeouts.append(timeout)
            super().__init__(handler, timeout, initial_delay=0.0, poll_interval=0.02)

    monkeypatch.setattr(device_tab, "_ReconnectThread", Fast)
    return timeouts


def _settled_count(qapp, device, name):
    """How often *name* ran, once a command still in flight has finished."""
    _pump(qapp, lambda: False, timeout=0.2)
    return device.commands.count(name)


# --------------------------------------------------------------------------- #
# the wait for a device that is away
# --------------------------------------------------------------------------- #
def test_a_tab_waiting_for_its_device_asks_nothing_while_adb_restarts(qapp, fast_waits):
    device = _AwayDevice()
    device.state = "offline"
    tab = _tab(qapp)
    messages = []
    tab.log.connect(messages.append)
    try:
        _connect(tab, device)
        tab._wait_and_reconnect()  # the device went away: a reboot, a pulled cable
        assert _pump(qapp, lambda: device.commands.count("get_state") >= 3)

        tab.prepare_for_adb_restart()
        asked = _settled_count(qapp, device, "get_state")
        _pump(qapp, lambda: False, timeout=0.4)  # some twenty polls of the old wait
        assert device.commands.count("get_state") == asked
        assert not tab._reconnecting and tab._rc is None

        device.state = "device"  # back by the time the server is
        tab.finish_adb_restart(True)
        assert _pump(qapp, lambda: any("device back online" in m for m in messages))
        assert tab.status.full_text().startswith("Connected")
        # the one recovery waits as long as the wait it replaced still had
        assert len(fast_waits) == 2 and fast_waits[0] == 180
        assert 170 < fast_waits[1] <= 180
    finally:
        _close(qapp, tab)


def test_a_wait_asked_for_during_the_hold_starts_with_the_recovery(qapp, fast_waits):
    device = _AwayDevice()
    device.state = "offline"
    tab = _tab(qapp)
    try:
        _connect(tab, device)
        tab.prepare_for_adb_restart()
        tab._wait_and_reconnect(manual=True)  # e.g. Reconnect, pressed meanwhile
        _pump(qapp, lambda: False, timeout=0.3)
        assert "get_state" not in device.commands and fast_waits == []

        tab.finish_adb_restart(True)
        assert _pump(qapp, lambda: "get_state" in device.commands)
        assert fast_waits == [60]  # the minute a manual reconnect waits, not 25 s
    finally:
        _close(qapp, tab)


def test_a_failed_restart_leaves_no_wait_and_no_reboot_behind(qapp, fast_waits):
    device = _AwayDevice()
    device.state = "offline"
    tab = _tab(qapp)
    try:
        _connect(tab, device)
        tab._reboot_in_progress = True  # the reboot whose return the tab waits for
        tab._wait_and_reconnect()
        assert _pump(qapp, lambda: "get_state" in device.commands)
        tab.prepare_for_adb_restart()
        asked = _settled_count(qapp, device, "get_state")

        tab.finish_adb_restart(False)
        _pump(qapp, lambda: False, timeout=0.3)
        assert device.commands.count("get_state") == asked  # nothing reconnects by itself
        assert not tab.btn_reconnect.isHidden()
        # ...and the Reboot button beside it is not refused for a reboot under way
        assert not tab._reboot_in_progress and not tab._reconnecting
    finally:
        _close(qapp, tab)


@pytest.mark.parametrize("cancelled_in", ["connect_tcp", "get_state", "shell"])
def test_a_wait_ended_during_an_adb_command_runs_no_other(cancelled_in):
    from turboadb.gui.device_tab import _ReconnectThread

    calls = []

    class Handler:
        config = types.SimpleNamespace(host="10.0.0.5", port=5555)

        def _ran(self, name):
            calls.append(name)
            if name == cancelled_in:
                thread.cancel()  # the hold began while this command ran

        def connect_tcp(self, host, port, safe=None):
            self._ran("connect_tcp")

        def get_state(self):
            self._ran("get_state")
            return "device"

        def shell(self, command, timeout=None, safe=None):
            self._ran("shell")
            return OperationResult(True, "shell", value=types.SimpleNamespace(ok=True))

    thread = _ReconnectThread(Handler(), timeout=5, initial_delay=0.0, poll_interval=0.01)
    results = []
    thread.done.connect(results.append)
    thread.run()  # in this thread: the result, if any, arrives at once
    order = ["connect_tcp", "get_state", "shell"]
    assert calls == order[: order.index(cancelled_in) + 1]
    assert results == []  # a wait that was ended reports nothing


def test_a_wait_knows_how_long_it_has_left():
    from turboadb.gui.device_tab import _ReconnectThread

    release = threading.Event()

    class Handler:
        config = None

        def get_state(self):
            release.set()
            return "offline"

    thread = _ReconnectThread(Handler(), timeout=60, initial_delay=0.0, poll_interval=0.01)
    assert thread.time_left() == 60  # all of it, until it starts polling
    thread.start()
    try:
        assert release.wait(5)
        assert 55 < thread.time_left() <= 60
    finally:
        thread.cancel()
        assert thread.wait(5000)


# --------------------------------------------------------------------------- #
# the Phone page
# --------------------------------------------------------------------------- #
@pytest.fixture
def phone_tab(qapp, monkeypatch, fast_waits):
    """A connected tab whose Phone page is on screen and polls every 30 ms."""
    from turboadb.gui import phone_panel

    monkeypatch.setattr(phone_panel.PhonePanel, "POLL_MS", 30)
    device = _AwayDevice()
    tab = _tab(qapp)
    _connect(tab, device)
    tab.show()
    tab.show_subtab("phone")
    assert _pump(qapp, lambda: device.commands.count("call_state") >= 2, timeout=5)
    yield tab, device
    tab.hide()
    _close(qapp, tab)


_PHONE_QUERIES = ("phone_support", "call_state", "call_log", "sms_list")


def _phone_queries(device):
    return [name for name in device.commands if name in _PHONE_QUERIES]


def test_the_phone_page_asks_nothing_while_adb_restarts(qapp, phone_tab):
    tab, device = phone_tab
    tab.prepare_for_adb_restart()
    _pump(qapp, lambda: False, timeout=0.2)  # a query in flight may still finish
    asked = _phone_queries(device)
    _pump(qapp, lambda: False, timeout=0.5)  # some fifteen polls, before
    assert _phone_queries(device) == asked

    tab.phone.btn_refresh.click()  # it loads once the device is back instead
    tab.show_subtab("logcat")
    tab.show_subtab("phone")  # nor does showing the page again ask
    _pump(qapp, lambda: False, timeout=0.3)
    assert _phone_queries(device) == asked

    tab.finish_adb_restart(True)  # the server answers: the tab reconnects
    lists = asked.count("call_log")
    assert _pump(qapp, lambda: device.commands.count("call_log") == lists + 1, timeout=5)
    polls = device.commands.count("call_state")
    assert _pump(qapp, lambda: device.commands.count("call_state") >= polls + 2, timeout=5)
    assert tab.phone.call_state_text() == "Idle"


def test_a_failed_restart_gives_the_phone_page_back_without_asking(qapp, phone_tab):
    tab, device = phone_tab
    tab.prepare_for_adb_restart()
    _pump(qapp, lambda: False, timeout=0.2)
    asked = _phone_queries(device)

    tab.finish_adb_restart(False)  # no server: nothing asks by itself
    _pump(qapp, lambda: False, timeout=0.4)
    assert _phone_queries(device) == asked
    tab.phone.btn_refresh.click()  # the user's Refresh does
    assert _pump(qapp, lambda: device.commands.count("call_log") == asked.count("call_log") + 1)


def test_a_refresh_pressed_during_the_hold_runs_when_it_ends(qapp, phone_tab):
    tab, device = phone_tab
    tab.prepare_for_adb_restart()
    _pump(qapp, lambda: False, timeout=0.2)
    asked = _phone_queries(device)
    tab.phone.btn_refresh.click()
    _pump(qapp, lambda: False, timeout=0.3)
    assert _phone_queries(device) == asked
    tab.finish_adb_restart(False)  # however the hold ends: the user asked
    assert _pump(qapp, lambda: device.commands.count("call_log") == asked.count("call_log") + 1)


def test_a_device_that_does_not_come_back_gives_the_phone_page_back(qapp, phone_tab):
    tab, device = phone_tab
    tab.prepare_for_adb_restart()
    _pump(qapp, lambda: False, timeout=0.2)
    asked = _phone_queries(device)
    tab._adb_restart_in_progress = False
    tab._on_reconnected(False)  # the wait after the restart timed out
    _pump(qapp, lambda: False, timeout=0.3)
    assert _phone_queries(device) == asked
    tab.phone.btn_refresh.click()
    assert _pump(qapp, lambda: device.commands.count("call_log") == asked.count("call_log") + 1)


def test_a_phone_page_first_shown_during_the_hold_loads_after_it(qapp, fast_waits):
    device = _AwayDevice()
    tab = _tab(qapp)
    try:
        _connect(tab, device)
        tab.show()
        tab.prepare_for_adb_restart()  # a Phone page never shown stays unbuilt
        assert tab._lazy_pages["phone"].page is None
        tab.show_subtab("phone")
        assert _pump(qapp, lambda: tab._lazy_pages["phone"].page is not None)
        _pump(qapp, lambda: False, timeout=0.3)
        assert _phone_queries(device) == []

        # (a tab held before its page existed tells the page as it is built)
        tab.finish_adb_restart(True)
        assert _pump(qapp, lambda: "call_log" in device.commands, timeout=5)
    finally:
        tab.hide()
        _close(qapp, tab)


def test_the_hold_ends_with_an_adbd_restart_that_outlasted_the_reconnect(qapp, phone_tab):
    """adb root (or making files writable) was running when ADB restarted: the
    reconnect leaves the pages to the end of the adbd restart."""
    tab, device = phone_tab
    tab._hold_terminals("adb root — adbd restarting")
    tab.prepare_for_adb_restart()
    _pump(qapp, lambda: False, timeout=0.2)
    asked = _phone_queries(device)
    tab._adb_restart_in_progress = False
    tab._on_reconnected(True)  # the reconnect after the restart, adbd still held
    _pump(qapp, lambda: False, timeout=0.3)
    assert _phone_queries(device) == asked

    tab._release_terminals()
    assert _pump(qapp, lambda: device.commands.count("call_log") == asked.count("call_log") + 1)


def test_a_cars_phone_connection_check_waits_for_the_hold_too(qapp):
    """On a head unit the page re-checks every so often whether a phone is
    connected: that poll is adb as well."""
    from test_phone_automotive import FakeCar
    from turboadb.gui.phone_panel import PhonePanel
    from turboadb.gui.qtutil import thread_running

    car = FakeCar(link=False)
    panel = PhonePanel(car)
    try:
        panel.show()
        assert _pump(qapp, lambda: panel._link_poll.isActive() and not panel._support_busy)
        panel._state_soon.start()  # as after Answer or End
        asked = list(car.names())

        panel.suspend_for_device_loss()
        assert not panel._link_poll.isActive() and not panel._state_soon.isActive()
        panel._link_poll.timeout.emit()  # a tick already on its way
        panel._check_phone_link(reload=True)
        _pump(qapp, lambda: False, timeout=0.2)
        assert car.names() == asked

        panel.resume_after_reconnect()  # the tab reconnected: is a phone there now?
        assert _pump(qapp, lambda: car.names().count("phone_connected") == 1)
        assert _pump(qapp, lambda: panel._link_poll.isActive())
    finally:
        jobs = list(panel._jobs)
        panel.close_panel()
        for job in jobs:
            if thread_running(job):
                job.wait(5000)
        panel.hide()
        panel.deleteLater()
        qapp.processEvents()
