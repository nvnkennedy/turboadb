"""The Phone page's call state while the device is away, and after it is back.

With the page on screen and the pill at "Idle", rebooting or unplugging the
phone made the next poll fail.  The page took that for a device without
telephony: a red "No telephony" pill, a warning that the device reports no
call state, and no more polls, so the pill stayed wrong after the device was
back, until Refresh.  A poll that adb or the device did not answer is now
tried again, and a device tab that reconnected loads its Phone page again.

Headless; a fake handler stands in for ADBHandler.
"""

import pytest

pytest.importorskip("PyQt5")

from turboadb.exceptions import ADBError  # noqa: E402
from turboadb.results import OperationResult  # noqa: E402

from test_adb_processes import _Device, _close, _connect, _tab  # noqa: E402
from test_phone_panel import FakePhone, _pump, _process_events, _wait_threads  # noqa: E402

OFFLINE = "adb: device offline"


class _Phone(FakePhone):
    """Records the mode each call-state query ran in."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.modes = []

    def call_state(self, *, safe=None):
        self.modes.append(safe)
        return super().call_state(safe=safe)


@pytest.fixture
def shown(qapp):
    """PhonePanels on fake handlers, shown (their polls run only then)."""
    made = []

    def make(handler):
        from turboadb.gui.phone_panel import PhonePanel

        panel = PhonePanel(handler)
        panel.logs = []
        panel.log.connect(panel.logs.append)
        made.append(panel)
        panel.show()
        return panel

    yield make
    threads = []
    for panel in made:
        threads.extend(panel._jobs)
        panel.close_panel()
    _wait_threads(threads)
    _process_events(qapp)
    for panel in made:
        panel.hide()
        panel.deleteLater()
    _process_events(qapp)


def _polled(qapp, panel, handler):
    """Run one poll tick and wait for its answer."""
    asked = handler.names().count("call_state")
    panel._poll.timeout.emit()
    assert _pump(qapp, lambda: handler.names().count("call_state") > asked
                 and not panel._state_busy)


def _warnings(panel):
    return [m for m in panel.logs if m.startswith(("[WARNING]", "[ERROR]"))]


def test_a_poll_the_device_did_not_answer_is_tried_again(qapp, shown):
    handler = _Phone()
    panel = shown(handler)
    assert _pump(qapp, lambda: panel.call_state_text() == "Idle" and panel._poll.isActive())

    handler.state_error = OFFLINE  # the phone reboots, or its cable is pulled
    _polled(qapp, panel, handler)
    assert panel.call_state_text() == "Checking…"
    assert (panel.state_pill.property("state") or "") == ""  # nothing red
    assert "device offline" in panel.state_pill.toolTip()
    assert _warnings(panel) == []  # and nothing to report: it asks again
    assert panel._poll.isActive()

    _polled(qapp, panel, handler)  # still away
    assert panel.call_state_text() == "Checking…" and _warnings(panel) == []

    handler.state_error, handler.state = None, "ringing"  # back, with a call coming in
    _polled(qapp, panel, handler)
    assert panel.call_state_text() == "Ringing" and panel._poll.isActive()


def test_polls_ask_quietly_and_a_load_the_user_asked_for_does_not(qapp, shown):
    handler = _Phone()
    panel = shown(handler)
    assert _pump(qapp, lambda: panel.call_state_text() == "Idle")
    _polled(qapp, panel, handler)
    # raw mode for the poll: a failure there is not logged as an engine error
    assert handler.modes[0] is True and handler.modes[1:] and not any(handler.modes[1:])


def test_a_load_the_device_did_not_answer_warns_once_and_keeps_asking(qapp, shown):
    handler = _Phone(state_error=OFFLINE)
    panel = shown(handler)
    assert _pump(qapp, lambda: "call_state" in handler.names() and not panel._state_busy)
    assert panel.call_state_text() == "Checking…"
    warnings = [m for m in panel.logs if "Call state" in m]
    assert warnings == ["[WARNING] Call state unavailable — ADBError: adb: device offline"]
    assert panel._poll.isActive()  # the answer may come yet

    asked = handler.names().count("call_state")
    panel._load_state()  # asked for again (Refresh): the same failure is not repeated
    assert _pump(qapp, lambda: handler.names().count("call_state") > asked
                 and not panel._state_busy)
    assert [m for m in panel.logs if "Call state" in m] == warnings

    handler.state_error = None
    _polled(qapp, panel, handler)
    assert panel.call_state_text() == "Idle"


def test_the_engines_own_error_is_reported_with_adbs_reason(qapp, shown, fake_adb):
    """The engine's error for a failed command names the command on its first
    line and says why on the next.  The warning and the pill's tip showed the
    first: "Command failed (exit=1): 'shell dumpsys telephony.registry'"."""
    from turboadb import ADBConfig, ADBHandler
    from turboadb.gui.phone_panel import friendly_failure

    fake_adb.add("dumpsys telephony.registry", returncode=1, stderr="error: device offline")
    panel = shown(ADBHandler(ADBConfig(serial="XYZ"), safe=True))
    assert _pump(qapp, lambda: any("Call state" in m for m in panel.logs))
    assert panel.call_state_text() == "Checking…"
    assert [m for m in panel.logs if "Call state" in m] == [
        "[WARNING] Call state unavailable — error: device offline"
    ]
    assert panel.state_pill.toolTip().startswith("error: device offline —")
    # a failure without adb's own words keeps its first line
    assert friendly_failure("ADBError: adb: device offline") == "ADBError: adb: device offline"
    assert friendly_failure(
        "ADBCommandError: Command failed (exit=1): 'shell x'\nstderr: "
    ) == "ADBCommandError: Command failed (exit=1): 'shell x'"


def test_a_device_without_a_call_state_still_stops_the_poll(qapp, shown):
    """What it answers, not that it did not: ``unknown`` and a missing
    telephony service are final, as before."""
    panel = shown(_Phone(state="unknown"))
    assert _pump(qapp, lambda: panel.call_state_text() == "No telephony")
    assert panel.state_pill.property("state") == "error" and not panel._poll.isActive()

    panel = shown(_Phone(state_error="Can't find service: telephony.registry"))
    assert _pump(qapp, lambda: panel.call_state_text() == "No telephony")
    assert (panel.state_pill.property("state") or "") == "" and not panel._poll.isActive()


def test_a_device_that_refuses_its_call_state_is_not_asked_again(qapp, shown):
    """A refusal is the device's answer: asking again would not change it."""
    denial = "java.lang.SecurityException: Permission Denial: can't dump telephony.registry"
    handler = _Phone(state_error=denial)
    panel = shown(handler)
    assert _pump(qapp, lambda: panel.call_state_text() == "No telephony")
    assert panel.state_pill.property("state") == "error" and not panel._poll.isActive()
    warnings = [m for m in panel.logs if "Call state" in m]
    assert warnings == ["[WARNING] Call state unavailable — "
                        "The device refused access (permission denied)."]


# --------------------------------------------------------------------------- #
# the device tab: a reconnect loads the page again
# --------------------------------------------------------------------------- #
class _RebootingPhone(_Device):
    """A phone that can be away: its call state then fails as the engine
    reports a failed adb call, an error or (older) ``unknown``."""

    away = None  # None: there; "error" / "unknown": how a query fails

    def call_state(self, safe=None):
        if self.away == "error":
            error = ADBError(OFFLINE)
            if safe:
                return self._one_shot("call_state", OperationResult(False, "state", error=error))
            self._one_shot("call_state")
            raise error
        value = "unknown" if self.away else "idle"
        return self._one_shot("call_state", OperationResult(True, "state", value=value))


@pytest.fixture
def phone_tab(qapp, monkeypatch):
    from turboadb.gui import phone_panel

    monkeypatch.setattr(phone_panel.PhonePanel, "POLL_MS", 30)
    device = _RebootingPhone()
    tab = _tab(qapp)
    _connect(tab, device)
    tab.show()
    tab.show_subtab("phone")
    assert _pump(qapp, lambda: tab.phone.call_state_text() == "Idle")
    yield tab, device
    tab.hide()
    _close(qapp, tab)


@pytest.mark.parametrize(
    "failure, pill", [("error", "Checking…"), ("unknown", "No telephony")],
    ids=["an-error", "an-unknown-state"],
)
def test_the_pill_is_right_again_once_the_tab_has_reconnected(qapp, phone_tab, failure, pill):
    tab, device = phone_tab
    device.away = failure  # rebooted from the header, or unplugged
    assert _pump(qapp, lambda: tab.phone.call_state_text() == pill)

    device.away = None
    lists = device.commands.count("call_log")
    tab._on_reconnected(True)  # "device back online"
    assert _pump(qapp, lambda: tab.phone.call_state_text() == "Idle")
    assert _pump(qapp, lambda: device.commands.count("call_log") == lists + 1)
    polls = device.commands.count("call_state")
    assert _pump(qapp, lambda: device.commands.count("call_state") >= polls + 2)  # polling again


def _phone_queries(device):
    names = ("phone_support", "call_state", "call_log", "sms_list")
    return [name for name in device.commands if name in names]


def test_a_hidden_phone_page_loads_again_when_it_is_next_shown(qapp, phone_tab):
    tab, device = phone_tab
    tab.show_subtab("logcat")
    _pump(qapp, lambda: False, timeout=0.2)
    asked = _phone_queries(device)
    tab._on_reconnected(True)
    _pump(qapp, lambda: False, timeout=0.3)
    assert _phone_queries(device) == asked  # nothing is asked for a page that is not on screen

    tab.show_subtab("phone")
    assert _pump(qapp, lambda: device.commands.count("call_log") == asked.count("call_log") + 1)
