"""End on the Phone page sends nothing while the device has no call.

End sends Android's End-call key.  With no call in progress Android takes that
key for its power button: pressing End while the pill said "Idle" put the head
unit's screen to sleep (or locked the phone), and the log said "End call
sent".  While the device is known to be idle End now asks it once more (a call
may have come in since the last poll) and sends the key only for a call.  Where
the call state is not known (a head unit that calls through a paired phone, a
page still checking) End sends the key as before.

Headless; a fake handler stands in for ADBHandler.
"""

import pytest

pytest.importorskip("PyQt5")

from test_phone_panel import (  # noqa: E402
    FakeHeadUnit,
    FakePhone,
    _pump,
    _process_events,
    _wait_threads,
)


@pytest.fixture
def page(qapp):
    made = []

    def make(handler):
        from turboadb.gui.phone_panel import PhonePanel

        panel = PhonePanel(handler)
        panel.logs = []
        panel.log.connect(panel.logs.append)
        made.append(panel)
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


def _loaded_page(qapp, page, handler, pill):
    """A page whose first load has finished, its pill at *pill*."""
    panel = page(handler)
    panel.refresh()
    assert _pump(qapp, lambda: panel.call_state_text() == pill and not panel._support_busy
                 and not panel._state_busy and not any(panel._loading.values()))
    return panel


def test_end_sends_no_key_while_the_device_is_idle(qapp, page):
    handler = FakePhone(state="idle")
    panel = _loaded_page(qapp, page, handler, "Idle")
    panel.btn_end.click()
    assert _pump(qapp, lambda: any("no call to end" in m for m in panel.logs))
    _pump(qapp, lambda: False, timeout=0.2)
    assert "end_call" not in handler.names()  # the key that would put it to sleep
    assert not any("End call sent" in m for m in panel.logs)
    assert not any(m.startswith(("[WARNING]", "[ERROR]")) for m in panel.logs)
    assert panel.dial_hint.text() == "No call to end."


def test_end_still_ends_a_call_that_came_in_since_the_last_poll(qapp, page):
    handler = FakePhone(state="idle")
    panel = _loaded_page(qapp, page, handler, "Idle")
    handler.state = "ringing"  # the pill has not caught up yet
    asked = handler.names().count("call_state")
    panel.btn_end.click()
    assert _pump(qapp, lambda: "end_call" in handler.names())
    names = handler.names()
    assert names.count("call_state") == asked + 1
    assert names[-2:] == ["call_state", "end_call"]  # asked once more, then the key
    assert _pump(qapp, lambda: any("[OK] End call sent" in m for m in panel.logs))


@pytest.mark.parametrize("state, pill", [("ringing", "Ringing"), ("in call", "In call")])
def test_end_goes_out_at_once_during_a_call(qapp, page, state, pill):
    handler = FakePhone(state=state)
    panel = _loaded_page(qapp, page, handler, pill)
    asked = handler.names().count("call_state")
    panel.btn_end.click()
    assert _pump(qapp, lambda: "end_call" in handler.names())
    assert handler.names().count("call_state") == asked  # no question first


def test_end_stays_a_key_where_the_device_has_no_call_state(qapp, page):
    """A head unit ends the call of the phone paired over Bluetooth."""
    handler = FakeHeadUnit()
    panel = _loaded_page(qapp, page, handler, "No telephony")
    panel.btn_end.click()
    assert _pump(qapp, lambda: "end_call" in handler.names())
    assert "call_state" not in handler.names()


def test_end_reports_a_device_that_does_not_answer_and_sends_nothing(qapp, page):
    handler = FakePhone(state="idle")
    panel = _loaded_page(qapp, page, handler, "Idle")
    handler.state_error = "adb: device offline"
    panel.btn_end.click()
    assert _pump(qapp, lambda: any(m.startswith("[ERROR] end call:") for m in panel.logs))
    assert "end_call" not in handler.names()
