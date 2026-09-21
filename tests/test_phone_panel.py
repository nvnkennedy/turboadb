"""Headless tests for the Phone page (turboadb/gui/phone_panel.py).

A fake handler stands in for ADBHandler, so no adb, device or network is used.
"""

from __future__ import annotations

import sys
import threading
import time


import pytest

pytest.importorskip("PyQt5")

from turboadb.exceptions import ADBError  # noqa: E402
from turboadb.results import OperationResult  # noqa: E402

NOW_MS = int(time.time() * 1000)

CALLS = [
    {"type": "1", "date": str(NOW_MS - 60_000), "duration": "125", "number": "+15550100"},
    {"type": "2", "date": str(NOW_MS - 120_000), "duration": "30", "number": "+15550101"},
    {"type": "3", "date": str(NOW_MS - 180_000), "duration": "0", "number": "+15550102"},
    {"type": "5", "date": str(NOW_MS - 240_000), "duration": "0", "number": "+15550103"},
    {"type": "3", "date": str(NOW_MS - 300_000), "duration": "0", "number": "-2"},
]
SMS = [
    {"type": "1", "date": str(NOW_MS - 60_000), "address": "+15550110", "body": "hello\nthere"},
    {"type": "2", "date": str(NOW_MS - 90_000), "address": "+15550111", "body": "on my way"},
]


def _ok(action, value):
    return OperationResult(success=True, action=action, value=value)


def _fail(action, message):
    return OperationResult(success=False, action=action, error=ADBError(message))


class FakePhone:
    """Records every handler call; each query can succeed, fail or block."""

    def __init__(self, *, state="idle", calls=None, sms=None, state_error=None,
                 calls_error=None, sms_error=None, block=None):
        self.invoked = []
        self.state = state
        self.calls = CALLS if calls is None else calls
        self.sms = SMS if sms is None else sms
        self.state_error = state_error
        self.calls_error = calls_error
        self.sms_error = sms_error
        self.block = block  # threading.Event the queries wait on

    def _wait(self):
        if self.block is not None:
            self.block.wait(5)

    def names(self):
        return [name for name, *_ in self.invoked]

    def dial(self, number, *, safe=None):
        self.invoked.append(("dial", number))
        return _ok("dial", True)

    def call(self, number, *, safe=None):
        self.invoked.append(("call", number))
        return _ok("call", True)

    def answer_call(self, *, safe=None):
        self.invoked.append(("answer_call",))
        return _ok("keyevent", True)

    def end_call(self, *, safe=None):
        self.invoked.append(("end_call",))
        return _ok("keyevent", True)

    def send_sms(self, number, body, *, safe=None):
        self.invoked.append(("send_sms", number, body))
        return _ok("send_sms", True)

    def call_state(self, *, safe=None):
        self.invoked.append(("call_state",))
        self._wait()
        if self.state_error:
            return _fail("call_state", self.state_error)
        return _ok("call_state", self.state)

    def call_log(self, limit=50, *, safe=None):
        self.invoked.append(("call_log", limit))
        self._wait()
        if self.calls_error:
            return _fail("call_log", self.calls_error)
        return _ok("call_log", list(self.calls))

    def sms_list(self, limit=50, *, safe=None):
        self.invoked.append(("sms_list", limit))
        self._wait()
        if self.sms_error:
            return _fail("sms_list", self.sms_error)
        return _ok("sms_list", list(self.sms))


def _process_events(qapp):
    """processEvents() that tolerates stale callbacks of widgets from earlier tests."""
    errors = []
    previous = sys.excepthook
    sys.excepthook = lambda etype, value, tb: errors.append(value)
    try:
        qapp.processEvents()
    finally:
        sys.excepthook = previous
    for error in errors:
        if not (isinstance(error, RuntimeError) and "has been deleted" in str(error)):
            raise error


def _pump(qapp, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _process_events(qapp)
        if predicate():
            return True
        time.sleep(0.01)
    _process_events(qapp)
    return predicate()


def _wait_threads(threads, timeout_ms=5000):
    """Block until every job thread has really finished (deleted ones count as done)."""
    from turboadb.gui.qtutil import thread_running

    for thread in threads:
        if thread_running(thread):
            thread.wait(timeout_ms)


@pytest.fixture
def make_panel(qapp):
    """Build PhonePanels on fake handlers and tear them down deterministically:
    release blocked queries, close, wait for every job thread, flush events,
    then delete and flush again, so nothing runs after the test."""
    created = []

    def _make(handler):
        from turboadb.gui.phone_panel import PhonePanel

        panel = PhonePanel(handler)
        logs = []
        panel.log.connect(logs.append)
        panel.logs = logs
        created.append((panel, handler))
        return panel

    yield _make
    threads = []
    for panel, handler in created:
        if handler.block is not None:
            handler.block.set()
        threads.extend(panel._jobs)  # a plain Python list, readable even after deletion
        try:
            panel.close_panel()
        except RuntimeError:  # already deleted by the test
            pass
    _wait_threads(threads)
    _process_events(qapp)
    for panel, _handler in created:
        try:
            panel.hide()
            panel.deleteLater()
        except RuntimeError:
            pass
    _process_events(qapp)


def _loaded(panel):
    return panel._counts["calls"] is not None and panel._counts["sms"] is not None


def test_keypad_builds_the_number(make_panel):
    handler = FakePhone()
    panel = make_panel(handler)
    for digit in "15*0#":
        panel.keys[digit].click()
    assert panel.number.text() == "15*0#"
    panel.btn_backspace.click()
    assert panel.number.text() == "15*0"
    # Holding 0 inserts "+" instead of "0".
    key = panel.keys["0"]
    key._long_fired = False
    key.setDown(True)
    key._on_hold()
    key.setDown(False)
    key._on_clicked()
    assert panel.number.text() == "15*0+"
    assert handler.invoked == []  # typing never touches the device


def test_number_field_strips_pasted_junk(make_panel):
    panel = make_panel(FakePhone())
    panel.number.setText("tel:+1 (555) 0100")
    panel.number.textEdited.emit(panel.number.text())  # as a paste would
    assert panel.number.text() == "+1 (555) 0100"


def test_enter_opens_the_dialler_and_never_calls(qapp, make_panel):
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest

    handler = FakePhone()
    panel = make_panel(handler)
    panel.number.setText("+15550100")
    QTest.keyClick(panel.number, Qt.Key_Return)
    assert _pump(qapp, lambda: ("dial", "+15550100") in handler.invoked)
    assert _pump(qapp, lambda: any("[OK] Dialler opened" in m for m in panel.logs))
    assert "call" not in handler.names()


def test_enter_with_empty_number_does_nothing(qapp, make_panel):
    handler = FakePhone()
    panel = make_panel(handler)
    assert not panel.btn_call.isEnabled()
    panel.number.returnPressed.emit()
    _pump(qapp, lambda: False, timeout=0.1)
    assert handler.invoked == []


def test_call_end_answer_buttons_invoke_the_right_methods(qapp, make_panel):
    handler = FakePhone()
    panel = make_panel(handler)
    panel.number.setText("+15550100")
    assert panel.btn_call.isEnabled()
    panel.btn_call.click()
    assert _pump(qapp, lambda: ("call", "+15550100") in handler.invoked)
    panel.btn_end.click()
    assert _pump(qapp, lambda: ("end_call",) in handler.invoked)
    panel.btn_answer.click()
    assert _pump(qapp, lambda: ("answer_call",) in handler.invoked)
    panel.btn_dial.click()
    assert _pump(qapp, lambda: ("dial", "+15550100") in handler.invoked)
    assert handler.names().count("call") == 1


def test_call_log_rows_render_with_direction(qapp, make_panel):
    from turboadb.gui.phone_panel import ROW_ROLE

    handler = FakePhone()
    panel = make_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: _loaded(panel))
    assert panel.calls.count() == len(CALLS)
    assert panel.calls_stack.currentWidget() is panel.calls
    rows = [panel.calls.item(i).data(ROW_ROLE) for i in range(panel.calls.count())]
    assert [(r["kind"], r["tone"], r["icon"]) for r in rows] == [
        ("incoming", "green", "phone-incoming"),
        ("outgoing", "blue", "phone-outgoing"),
        ("missed", "red", "phone-missed"),
        ("rejected", "amber", "phone-off"),
        ("missed", "red", "phone-missed"),
    ]
    assert rows[0]["title"] == "+15550100"
    assert rows[0]["detail"] == "2:05"
    assert rows[0]["when"].startswith(("Today", "Yesterday"))
    assert rows[4]["title"] == "Private number"
    assert panel.count.text() == "5 calls"
    assert panel.sms.count() == len(SMS)
    assert panel.sms.item(0).data(ROW_ROLE)["detail"] == "hello there"
    assert panel.sms.item(1).data(ROW_ROLE)["detail"] == "You: on my way"
    assert any("[OK] 5 recent calls" in m for m in panel.logs)


def test_call_back_fills_the_number_without_calling(qapp, make_panel):
    handler = FakePhone()
    panel = make_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: _loaded(panel))
    before = list(handler.invoked)

    panel.calls.setCurrentRow(2)
    assert panel.btn_call_back.isEnabled()
    panel.btn_call_back.click()
    assert panel.number.text() == "+15550102"

    panel.number.clear()
    item = panel.calls.item(1)
    panel.calls.itemActivated.emit(item)  # double-click / Enter on a row
    assert panel.number.text() == "+15550101"

    # A private number can't be called back.
    panel.calls.setCurrentRow(4)
    assert not panel.btn_call_back.isEnabled()

    _pump(qapp, lambda: False, timeout=0.2)
    assert handler.invoked == before  # nothing was dialled or called


def test_selected_message_prefills_compose_and_composes(qapp, make_panel):
    handler = FakePhone()
    panel = make_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: _loaded(panel))
    panel.show_page(panel.PAGE_MESSAGES)
    panel.sms.setCurrentRow(1)
    assert panel.sms_to.text() == "+15550111"
    panel.sms_body.setText("see you soon")
    panel.btn_compose.click()
    assert _pump(qapp, lambda: ("send_sms", "+15550111", "see you soon") in handler.invoked)


def test_call_state_failure_shows_no_telephony(qapp, make_panel):
    # No telephony service at all (a head unit): shown calmly, without a warning.
    handler = FakePhone(state_error="Can't find service: telephony.registry")
    panel = make_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: panel.call_state_text() == "No telephony")
    assert (panel.state_pill.property("state") or "") == ""
    assert not any(m.startswith(("[WARNING]", "[ERROR]")) for m in panel.logs)


def test_unexpected_call_state_failure_warns_once(qapp, make_panel):
    handler = FakePhone(state_error="adb: device offline")
    panel = make_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: panel.call_state_text() == "No telephony")
    assert panel.state_pill.property("state") == "error"
    warnings = [m for m in panel.logs if m.startswith("[WARNING]")]
    assert len(warnings) == 1 and "Call state unavailable" in warnings[0]
    assert not any(m.startswith("[ERROR]") for m in panel.logs)


class FakeHeadUnit(FakePhone):
    """A customised head unit: no telephony, no dialler, no call log, its own phone app."""

    NO_CALL_LOG = (
        "Error while accessing provider:call_log\n"
        "java.lang.IllegalArgumentException: Unknown authority call_log"
    )

    def __init__(self, support=None, **kwargs):
        kwargs.setdefault("calls_error", self.NO_CALL_LOG)
        kwargs.setdefault("sms_error", "Error while accessing provider:sms")
        kwargs.setdefault("state_error", "Can't find service: telephony.registry")
        super().__init__(**kwargs)
        self.support = support if support is not None else {
            "telephony": False, "dialer": "", "caller": "", "messages": "",
            "phone_apps": ["com.oem.btphone"],
        }

    def phone_support(self, *, safe=None):
        self.invoked.append(("phone_support",))
        return _ok("phone_support", dict(self.support))

    def start_app(self, package, *, safe=None):
        self.invoked.append(("start_app", package))
        return _ok("start_app", True)


def test_head_unit_without_a_phone_app_shows_no_errors(qapp, make_panel):
    """Issue: on a customised head unit the Phone tab showed errors (no phone app)."""
    handler = FakeHeadUnit()
    panel = make_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: panel.calls_empty.title.text() == "No call history on this device"
                 and panel.sms_empty.title.text() == "No messages on this device")
    assert panel.call_state_text() == "No telephony"
    assert (panel.state_pill.property("state") or "") == ""  # expected, not a fault
    assert "call_state" not in handler.names()  # no telephony: nothing to poll
    assert not any(m.startswith(("[ERROR]", "[WARNING]")) for m in panel.logs)

    panel.number.setText("+15550100")
    assert not panel.btn_call.isEnabled() and not panel.btn_dial.isEnabled()
    panel.number.returnPressed.emit()  # Enter doesn't try the missing dialler
    assert not panel.app_notice.isHidden()
    assert panel.btn_phone_app.text() == "Open com.oem.btphone"
    panel.btn_phone_app.click()
    assert _pump(qapp, lambda: ("start_app", "com.oem.btphone") in handler.invoked)
    assert "dial" not in handler.names() and "call" not in handler.names()

    panel.show_page(panel.PAGE_MESSAGES)
    panel.sms_to.setText("+15550100")
    assert not panel.btn_compose.isEnabled()


def test_device_with_standard_phone_apps_keeps_the_dialler(qapp, make_panel):
    handler = FakeHeadUnit(
        support={
            "telephony": True,
            "dialer": "com.android.dialer/.Main",
            "caller": "com.android.server.telecom/.UserCallActivity",
            "messages": "com.android.messaging/.Main",
            "phone_apps": ["com.android.dialer"],
        },
        calls_error=None, sms_error=None, state_error=None,
    )
    panel = make_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: _loaded(panel))
    assert panel.app_notice.isHidden()
    assert _pump(qapp, lambda: "call_state" in handler.names())
    panel.number.setText("+15550100")
    assert panel.btn_call.isEnabled() and panel.btn_dial.isEnabled()


def test_unknown_call_state_also_shows_no_telephony(qapp, make_panel):
    panel = make_panel(FakePhone(state="unknown"))
    panel.refresh()
    assert _pump(qapp, lambda: panel.call_state_text() == "No telephony")


def test_call_states_map_to_pill(qapp, make_panel):
    handler = FakePhone(state="ringing")
    panel = make_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: panel.call_state_text() == "Ringing")
    assert panel.state_pill.property("state") == "warn"
    handler.state = "in call"
    panel._load_state()
    assert _pump(qapp, lambda: panel.call_state_text() == "In call")
    assert panel.state_pill.property("state") == "ok"


def test_permission_failure_shows_empty_state_and_one_warning(qapp, make_panel):
    denial = "java.lang.SecurityException: Permission Denial: reading CallLogProvider"
    handler = FakePhone(calls_error=denial, sms_error=denial)
    panel = make_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: panel.calls_empty.title.text() == "Recent calls unavailable"
                 and panel.sms_empty.title.text() == "Messages unavailable")
    assert panel.calls_stack.currentWidget() is panel.calls_empty
    assert panel.sms_stack.currentWidget() is panel.sms_empty
    assert "permission denied" in panel.calls_empty.detail.text()
    # A second refresh with the same failure doesn't repeat the warning.
    panel.refresh()
    assert _pump(qapp, lambda: handler.names().count("call_log") == 2)
    _pump(qapp, lambda: False, timeout=0.3)
    warnings = [m for m in panel.logs if m.startswith("[WARNING]")]
    assert len(warnings) == 2  # one for calls, one for messages
    assert not any(m.startswith("[ERROR]") for m in panel.logs)


def test_empty_history_shows_empty_state(qapp, make_panel):
    panel = make_panel(FakePhone(calls=[], sms=[]))
    panel.refresh()
    assert _pump(qapp, lambda: _loaded(panel))
    assert panel.calls_empty.title.text() == "No recent calls"
    assert panel.calls_stack.currentWidget() is panel.calls_empty


def test_loads_lazily_on_first_show(qapp, make_panel):
    handler = FakePhone()
    panel = make_panel(handler)
    _pump(qapp, lambda: False, timeout=0.1)
    assert handler.invoked == []  # constructing the page never queries the device
    panel.show()
    assert _pump(qapp, lambda: "call_log" in handler.names() and "sms_list" in handler.names())
    panel.hide()


def test_layout_is_side_by_side_when_wide_and_stacked_when_narrow(qapp, make_panel):
    panel = make_panel(FakePhone())
    panel.resize(1200, 700)
    panel._apply_width(1200)
    assert panel.is_wide()
    panel._apply_width(700)
    assert not panel.is_wide()
    panel.resize(700, 700)
    panel.show()
    _pump(qapp, lambda: False, timeout=0.2)
    assert panel.scroll.horizontalScrollBar().maximum() == 0
    assert panel.scroll.widget().minimumSizeHint().width() <= 700
    panel.hide()


def test_close_panel_while_a_job_is_running_does_not_crash(qapp, make_panel):
    gate = threading.Event()
    handler = FakePhone(block=gate)
    panel = make_panel(handler)
    panel.refresh()
    assert _pump(qapp, lambda: "call_log" in handler.names())
    jobs = list(panel._jobs)
    assert jobs  # queries are still running
    panel.close_panel()
    assert panel._jobs == []  # detached and parked, no waiting on the UI thread
    logs_at_close = list(panel.logs)
    panel.deleteLater()
    _process_events(qapp)
    gate.set()  # let the parked threads finish after the widget is gone
    _wait_threads(jobs)
    _process_events(qapp)
    assert panel.logs == logs_at_close  # no callback reached the closed page
    panel.refresh()  # a stale reference is inert after close
