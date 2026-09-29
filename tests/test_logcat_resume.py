"""A live logcat that ended with the adb server comes back with the shell.

When the adb server goes away under TurboADB (another tool's adb of a
different version restarts it, a tools update, an ``adb kill-server`` typed in
a terminal), the Terminal's shell and the Logcat capture end together.  The
Logcat page says "it resumes when the device is back" and waits for the device
tab's device-back hook.  The tab's recovery only called that hook after a full
wait-for-device reconnect: when the device was still (or again) online, the
shell was restarted on the spot and the capture stayed stopped for good.

The shell's "device is back" can also arrive while the ended capture's reader
is still reaping its adb (it reports the end, and asks to be resumed, a moment
later); the capture must then resume as soon as it has reported.
"""
import threading

import pytest

pytest.importorskip("PyQt5")

from turboadb.core import ADBHandler  # noqa: E402


class _Stream:
    """One logcat line, then EOF once *ended* is set (the server went away)."""

    def __init__(self, ended):
        self._ended = ended
        self._sent = False

    def read(self, _size=-1):
        if not self._sent:
            self._sent = True
            return b"09-27 12:00:00.000  100  100 I Tag: hello\n"
        self._ended.wait(10)
        return b""

    def close(self):
        pass


class _Logcat:
    """``adb logcat``: its stdout ends with *ended*; reaping it waits for
    *reaped* (set at once unless a test holds it back)."""

    def __init__(self, argv):
        self.argv = list(argv)
        self.ended = threading.Event()
        self.reaped = threading.Event()
        self.reaped.set()
        self.stdout = _Stream(self.ended)
        self.stderr = None

    def poll(self):
        return 0 if self.ended.is_set() and self.reaped.is_set() else None

    def wait(self, timeout=None):
        self.ended.wait(timeout)
        self.reaped.wait(timeout)
        return 0

    def kill(self):
        self.ended.set()
        self.reaped.set()

    terminate = kill


@pytest.fixture
def logcat_tab(qapp):
    from test_adb_processes import _close, _connect, _Device, _pump, _tab

    class Device(_Device):
        def popen(self, args):
            if list(args)[:1] != ["logcat"]:
                return super().popen(args)
            proc = _Logcat(args)
            if self.hold_reap:
                proc.reaped.clear()
            self.logcats.append(proc)
            return proc

    device = Device()
    device.logcats = []
    device.hold_reap = False
    tab = _tab(qapp)
    try:
        _connect(tab, device)
        tab.show()
        tab.show_subtab("logcat")
        assert _pump(qapp, lambda: tab._lazy_pages["logcat"].page is not None)
        yield tab, device, _pump
    finally:
        _close(qapp, tab)


def _started(qapp, tab, device, pump):
    tab.logcat.start()
    assert pump(qapp, lambda: len(device.logcats) == 1)
    thread = tab.logcat.thread
    assert pump(qapp, lambda: thread.received == 1)
    return thread


def test_a_capture_that_ended_with_the_shell_resumes_when_the_shell_is_back(qapp, logcat_tab):
    tab, device, pump = logcat_tab
    _started(qapp, tab, device, pump)

    device.logcats[0].ended.set()  # the server went away
    assert pump(qapp, lambda: tab.logcat.thread is None)
    assert tab.logcat._resume is not None  # "it resumes when the device is back"

    # The Terminal's shell ended too; the device is online again: the tab
    # restarts the shell at once rather than waiting for a reconnect.
    tab._on_shell_lost_state(tab.handler, "device")
    assert pump(qapp, lambda: len(device.logcats) == 2)
    # from the last line's timestamp: nothing in between is lost
    assert device.logcats[1].argv == ADBHandler.logcat_args(tail="09-27 12:00:00.000")


def test_the_device_back_before_the_capture_has_reported_its_end(qapp, logcat_tab):
    tab, device, pump = logcat_tab
    device.hold_reap = True
    thread = _started(qapp, tab, device, pump)

    device.logcats[0].ended.set()  # the server went away; adb is still being reaped
    pump(qapp, lambda: False, timeout=0.2)
    assert thread.isRunning()
    tab._on_shell_lost_state(tab.handler, "device")  # the shell is back first

    device.logcats[0].reaped.set()  # the capture ends and reports it
    assert pump(qapp, lambda: len(device.logcats) == 2)
    assert device.logcats[1].argv == ADBHandler.logcat_args(tail="09-27 12:00:00.000")
    assert tab.logcat._resume is None


def test_a_device_back_while_the_capture_carries_on_does_not_resume_a_later_end(
        qapp, logcat_tab):
    """"The device is back" only counts for a capture that was ending then:
    one that carried on and ends by itself later waits for the next one."""
    tab, device, pump = logcat_tab
    thread = _started(qapp, tab, device, pump)
    tab._on_shell_lost_state(tab.handler, "device")  # a shell blip; logcat carries on
    assert thread.isRunning() and not tab.logcat._resume_when_ended

    device.logcats[0].ended.set()  # now the device goes away
    assert pump(qapp, lambda: tab.logcat.thread is None)
    pump(qapp, lambda: False, timeout=0.3)
    assert len(device.logcats) == 1
    assert tab.logcat._resume is not None  # still waiting for the device


def test_stop_forgets_a_device_back_note(qapp, logcat_tab):
    tab, device, pump = logcat_tab
    device.hold_reap = True
    _started(qapp, tab, device, pump)
    device.logcats[0].ended.set()
    tab._on_shell_lost_state(tab.handler, "device")
    tab.logcat.stop()  # the user stops the capture: nothing resumes it
    device.logcats[0].reaped.set()
    assert pump(qapp, lambda: tab.logcat.thread is None)
    pump(qapp, lambda: False, timeout=0.3)
    assert len(device.logcats) == 1
