"""The Phone page shares its device tab's adb slots.

Opening the page started phone_support, then call_state, call_log and
sms_list on separate workers at once (up to 4 adb.exe with the Terminal's
shell), each list asked ``am get-current-user`` on its own, and every Refresh
click started another query per list: ten clicks ran 23 device calls at once.
"""
import threading
import time

import pytest

from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.results import OperationResult


# --------------------------------------------------------------------------- #
# engine: one `am get-current-user` for the call log and the messages
# --------------------------------------------------------------------------- #
def test_concurrent_queries_ask_the_foreground_user_once(fake_adb, monkeypatch):
    import turboadb.core as core

    fake_adb.add("get-current-user", stdout="10\n")
    fake_run = core.subprocess.run

    def slow_run(cmd, **kwargs):
        if "get-current-user" in cmd:
            time.sleep(0.3)  # both workers are inside _content_user meanwhile
        return fake_run(cmd, **kwargs)

    monkeypatch.setattr(core.subprocess, "run", slow_run)
    handler = ADBHandler(ADBConfig(serial="car"))
    workers = [threading.Thread(target=handler.call_log), threading.Thread(target=handler.sms_list)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    asks = [argv for argv in fake_adb.calls if "get-current-user" in argv]
    assert len(asks) == 1
    queries = [argv for argv in fake_adb.calls if "query" in argv]
    assert len(queries) == 2 and all("--user" in q and "10" in q for q in queries)


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #
class _Phone:
    """A fake handler whose device calls take *delay* s and count how many run
    at once (every call is one adb process on a real device)."""

    serial = "PHONE1"
    config = None

    def __init__(self, delay=0.15, block=None):
        self.delay = delay
        self.block = block  # threading.Event the list queries wait on
        self.calls = []
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()

    def _call(self, name, value):
        with self._lock:
            self.calls.append(name)
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            if self.block is not None and name in ("call_log", "sms_list", "phone_support"):
                self.block.wait(10)
            time.sleep(self.delay)
            return OperationResult(True, name, value=value)
        finally:
            with self._lock:
                self.running -= 1

    # never used while the Phone page is the one on screen; an exception from
    # a missing method inside a Qt slot would abort the test run
    def open_shell(self, tty=True, safe=None):
        return OperationResult(False, "open_shell", error=RuntimeError("no shell here"))

    def shell(self, command, timeout=None, safe=None, **_kw):
        return OperationResult(False, "shell", error=RuntimeError("no shell here"))

    def phone_support(self, safe=None):
        return self._call("phone_support", {"telephony": True, "dialer": "d/.D",
                                            "caller": "c/.C", "messages": "m/.M"})

    def call_state(self, safe=None):
        return self._call("call_state", "idle")

    def call_log(self, limit=100, safe=None):
        return self._call("call_log", [])

    def sms_list(self, limit=100, safe=None):
        return self._call("sms_list", [])

    def answer_call(self, safe=None):
        return self._call("answer_call", True)

    def end_call(self, safe=None):
        return self._call("end_call", True)

    def dial(self, number, safe=None):
        return self._call("dial", True)


def _pump(qapp, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return until()


def _loaded(panel):
    return panel._counts["calls"] is not None and panel._counts["sms"] is not None


@pytest.fixture
def panels(qapp):
    from turboadb.gui.qtutil import thread_running

    made = []

    def make(handler, gate=None):
        from turboadb.gui.phone_panel import PhonePanel

        panel = PhonePanel(handler, adb_gate=gate)
        panel.logs = []
        panel.log.connect(panel.logs.append)
        made.append((panel, handler, gate))
        return panel

    yield make
    for panel, handler, gate in made:
        if handler.block is not None:
            handler.block.set()
        jobs = list(panel._jobs)
        panel.close_panel()
        if gate is not None:
            gate.close()
        for job in jobs:
            if thread_running(job):
                job.wait(5000)
        panel.deleteLater()
    qapp.processEvents()


def test_opening_the_phone_page_stays_within_the_tabs_adb_slots(qapp):
    from turboadb.gui.device_tab import DeviceTab

    phone = _Phone(delay=0.2)
    tab = DeviceTab({"name": "phone", "serial": "PHONE1"})
    tab._started_connect = True
    try:
        tab._on_connected(phone, {"_probe_pending": True})
        tab.resize(1280, 820)
        tab.show_subtab("phone")  # the Phone page is the one on screen
        tab.show()
        assert _pump(qapp, lambda: {"call_log", "sms_list", "call_state"} <= set(phone.calls)
                     and phone.running == 0, timeout=10)
        assert phone.peak <= DeviceTab.ADB_SLOTS == 2  # before: 3 at once
    finally:
        tab.hide()
        tab.close_session()
        tab.close()
        qapp.processEvents()


def test_a_refresh_burst_runs_one_query_per_list_at_a_time(qapp, panels):
    block = threading.Event()
    phone = _Phone(delay=0.0, block=block)
    panel = panels(phone)
    panel._support = {"telephony": True}  # checked already
    for _ in range(10):
        panel.refresh()
    assert _pump(qapp, lambda: phone.calls.count("call_log") == 1, timeout=2)
    block.set()
    assert _pump(qapp, lambda: _loaded(panel) and not any(panel._loading.values()), timeout=5)
    # the running one and a single follow-up, not ten
    assert phone.calls.count("call_log") == 2 and phone.calls.count("sms_list") == 2


def test_the_device_check_runs_once_however_often_refresh_is_clicked(qapp, panels):
    block = threading.Event()
    phone = _Phone(delay=0.0, block=block)
    panel = panels(phone)
    for _ in range(5):
        panel.refresh()
    _pump(qapp, lambda: False, timeout=0.2)
    block.set()
    assert _pump(qapp, lambda: _loaded(panel), timeout=5)
    assert phone.calls.count("phone_support") == 1


def test_every_device_job_waits_for_a_slot_but_the_call_keys(qapp, panels):
    from turboadb.gui.device_tab import _AdbGate

    gate = _AdbGate(2)
    gate.acquire()
    gate.acquire()  # two slow commands are running in the tab
    phone = _Phone(delay=0.0)
    panel = panels(phone, gate)
    released = 0
    try:
        panel.refresh()
        panel.number.setText("+15550100")
        panel._dial()
        _pump(qapp, lambda: False, timeout=0.3)
        assert phone.calls == []  # the check, the lists and Dial all wait

        panel._answer_call()
        panel._end_call()  # a ringing call must not wait behind a listing
        assert _pump(qapp, lambda: {"answer_call", "end_call"} <= set(phone.calls))
        assert "dial" not in phone.calls and "phone_support" not in phone.calls

        gate.release()
        gate.release()
        released = 2
        assert _pump(qapp, lambda: "dial" in phone.calls and _loaded(panel), timeout=5)
    finally:
        for _ in range(2 - released):
            gate.release()


def test_closing_the_tab_ends_waiting_phone_jobs_quietly(qapp, panels):
    from turboadb.gui.device_tab import _AdbGate

    gate = _AdbGate(1)
    gate.acquire()  # a slow command holds the only slot
    phone = _Phone(delay=0.0)
    panel = panels(phone, gate)
    panel._support = {"telephony": True}
    panel.refresh()
    jobs = list(panel._jobs)
    assert jobs
    gate.close()  # what DeviceTab.close_session does first...
    panel.close_panel()  # ...then it closes the pages
    for job in jobs:
        job.wait(5000)
    _pump(qapp, lambda: False, timeout=0.2)
    assert phone.calls == []  # nothing started adb for the closed tab
    assert not any(line.startswith(("[ERROR]", "[WARNING]")) for line in panel.logs)
