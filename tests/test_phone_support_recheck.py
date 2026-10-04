"""The Phone page asks what the device offers again when the first check failed.

The page first asks the device what it has (telephony, a dialler, whether it
is a car), so that a head unit's missing call log or refused messages show
calmly instead of as errors.  A check that failed, or that the device answered
with nothing because it was busy, away or still starting, was kept as the
answer for the whole session: Refresh never asked again, and the head unit
stayed a phone.  Such a check is now repeated by Refresh and when the page is
next shown.

Headless; a fake handler stands in for ADBHandler.
"""

import pytest

pytest.importorskip("PyQt5")

from turboadb.exceptions import ADBError  # noqa: E402
from turboadb.results import OperationResult  # noqa: E402

from test_phone_panel import _pump, _process_events, _wait_threads  # noqa: E402
from test_phone_automotive import DENIAL, FakeCar  # noqa: E402

# What phone_support makes of a device that printed nothing.
NOTHING = {"telephony": None, "dialer": None, "caller": None, "messages": None,
           "phone_apps": [], "kind": "phone", "automotive": False, "phone_connected": None}


class _BusyCar(FakeCar):
    """A head unit (its call log refused, as on a car) whose first *failures*
    support checks fail, or are answered with *nothing*."""

    def __init__(self, failures=1, nothing=False):
        super().__init__(link=None, calls_error=DENIAL, sms_error=DENIAL)
        self.failures = failures
        self.nothing = nothing

    def phone_support(self, *, safe=None):
        self.invoked.append(("phone_support", safe))
        if self.failures > 0:
            self.failures -= 1
            if self.nothing:
                return OperationResult(True, "phone_support", value=dict(NOTHING))
            error = ADBError("adb command timed out after 30s: 'shell echo @@features; …'")
            return OperationResult(False, "phone_support", error=error)
        return OperationResult(True, "phone_support", value=dict(self.support))


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


def _settled(qapp, panel):
    assert _pump(qapp, lambda: not panel._support_busy and not any(panel._loading.values())
                 and not panel._state_busy)


def _asked(handler):
    return handler.names().count("phone_support")


@pytest.mark.parametrize("nothing", [False, True], ids=["check-failed", "answered-nothing"])
def test_refresh_asks_a_device_again_that_could_not_say_what_it_has(qapp, page, nothing):
    handler = _BusyCar(failures=2, nothing=nothing)
    panel = page(handler)
    panel.refresh()
    _settled(qapp, panel)
    # handled as a phone for now: a refused call log is a warning there
    assert _asked(handler) == 1 and not panel._car
    assert any(m.startswith("[WARNING] Recent calls unavailable") for m in panel.logs)

    panel.btn_refresh.click()
    _settled(qapp, panel)
    assert _asked(handler) == 2 and not panel._car  # still busy

    del panel.logs[:]
    panel.btn_refresh.click()
    _settled(qapp, panel)
    assert _asked(handler) == 3 and panel._car  # the head unit answered
    assert panel.calls_empty.title.text() == "Call history not available on this head unit"
    assert not any(m.startswith(("[WARNING]", "[ERROR]")) for m in panel.logs)

    panel.btn_refresh.click()  # what it offers is known now: not asked again
    _settled(qapp, panel)
    assert _asked(handler) == 3


def test_showing_the_page_again_asks_a_device_that_could_not_say(qapp, page):
    handler = _BusyCar(failures=1)
    panel = page(handler)
    panel.show()
    _settled(qapp, panel)
    assert _asked(handler) == 1 and not panel._car

    panel.hide()
    panel.show()  # back on the Phone tab
    _settled(qapp, panel)
    assert _asked(handler) == 2 and panel._car

    panel.hide()
    panel.show()
    _settled(qapp, panel)
    assert _asked(handler) == 2  # answered: showing the page asks no more


def test_a_page_without_a_device_check_is_not_reloaded_by_showing_it(qapp, page):
    """A handler that cannot be asked (no ``phone_support``) loads once."""
    from test_phone_panel import FakePhone

    handler = FakePhone()
    panel = page(handler)
    panel.show()
    assert _pump(qapp, lambda: handler.names().count("call_log") == 1
                 and not any(panel._loading.values()))
    panel.hide()
    panel.show()
    _pump(qapp, lambda: False, timeout=0.2)
    assert handler.names().count("call_log") == 1
