"""A connect-time probe that fails part-way gives back what it took.

The probe runs one ``adb shell`` inside one of the device tab's adb slots.  An
error while it read its first sections skipped its clean-up: the slot stayed
taken (every later background command of the tab then shared the slot left),
and the adb process was left to whoever called the probe.  The connect
worker's ``log`` signal, never emitted by anything, is gone as well.
"""
import threading

import pytest

pytest.importorskip("PyQt5")

from test_adb_processes import _Device  # noqa: E402


def _slot_is_free(gate) -> bool:
    """Whether *gate* (one slot) can be taken now, without blocking the test."""
    from turboadb.gui.device_tab import _GateClosed

    got = threading.Event()

    def take():
        try:
            gate.acquire()
        except _GateClosed:
            return
        got.set()
        gate.release()

    threading.Thread(target=take, daemon=True).start()
    free = got.wait(2)
    gate.close()  # wake the taker if the slot was never given back
    return free


@pytest.fixture
def unreadable(monkeypatch):
    from turboadb.gui.device_tab import _DeviceProbe

    def broken(self, section, timeout):
        raise ValueError("unreadable probe output")

    monkeypatch.setattr(_DeviceProbe, "read_until", broken)


def test_a_probe_that_fails_while_reading_ends_its_adb_and_frees_its_slot(unreadable):
    from turboadb.gui.device_tab import _AdbGate, _DeviceProbe

    device = _Device(probe_output=None)  # the probe's adb shell never answers
    gate = _AdbGate(slots=1)
    with pytest.raises(ValueError):
        _DeviceProbe(device).run(gate=gate)
    assert len(device.probes()) == 1 and device.alive() == []
    assert _slot_is_free(gate)


def test_the_tabs_probe_worker_reports_the_failure_and_leaves_nothing_behind(unreadable):
    from turboadb.gui.device_tab import _AdbGate, _ProbeThread

    device = _Device(probe_output=None)
    gate = _AdbGate(slots=1)
    worker = _ProbeThread(device, gate=gate)
    results = []
    worker.details.connect(results.append)
    worker.run()  # the worker's body, on this thread
    assert results[0]["info"] is None and "unreadable probe output" in results[0]["error"]
    assert device.alive() == []
    assert _slot_is_free(gate)


def test_the_connect_worker_has_no_signal_that_nothing_emits():
    from turboadb.gui.device_tab import _ConnectThread

    assert not hasattr(_ConnectThread, "log")
    assert hasattr(_ConnectThread, "trace")  # the engine's lines still reach the log panel
