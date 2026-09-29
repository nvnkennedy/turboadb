"""A connect-time probe that got no device profile is run once more.

adbd on some head units refuses a second shell right after the connect, and a
device can be slow to serve its properties.  The tab used to give up at once
and treat the device as a phone for the whole session: no IVI Displays tab,
the phone defaults in Apps and Device Control.  It now asks one more time,
through the same probe and the tab's adb slots (no extra getprop processes)."""

import pytest

pytest.importorskip("PyQt5")

from test_adb_processes import _Device, _close, _connect, _probe_output, _pump, _tab  # noqa: E402

_FAILED = {"info": None, "prompt": None, "displays": None, "error": "error: closed"}


def _retrying_tab(qapp, device):
    tab = _tab(qapp)
    tab.PROBE_RETRY_MS = 50
    messages = []
    tab.log.connect(messages.append)
    _connect(tab, device)
    return tab, messages


def test_a_probe_without_a_profile_is_repeated_and_its_answer_applied(qapp):
    device = _Device(probe_output=_probe_output(characteristics="automotive"))
    tab, messages = _retrying_tab(qapp, device)
    try:
        tab._on_probe_details(device, dict(_FAILED))
        assert any("asking the device again" in m for m in messages)
        assert device.probes() == []  # not at once: the device gets a moment
        assert _pump(qapp, lambda: len(device.probes()) == 1)
        assert _pump(qapp, lambda: tab._automotive)
        assert "ivi" in tab._subtabs  # the head unit got its displays tab after all
        # The first failure started the panel's own display scan; the repeat
        # brought the display list itself, so no second scan ran.
        assert device.commands.count("list_displays") == 1
    finally:
        _close(qapp, tab)


def test_the_probe_is_repeated_only_once(qapp):
    device = _Device(probe_output=b"error: device offline\n")
    tab, messages = _retrying_tab(qapp, device)
    try:
        tab._on_probe_details(device, dict(_FAILED))
        assert _pump(qapp, lambda: len(device.probes()) == 1)
        assert _pump(qapp, lambda: any(
            m.startswith("[INFO] Device details are still unavailable")
            and "asking the device again" not in m for m in messages
        ))
        _pump(qapp, lambda: False, timeout=0.3)
        assert len(device.probes()) == 1
        assert not tab._automotive
        assert device.commands.count("list_displays") == 1
    finally:
        _close(qapp, tab)


def test_a_tab_closed_before_the_repeat_starts_no_probe(qapp):
    device = _Device()
    tab, _messages = _retrying_tab(qapp, device)
    tab._on_probe_details(device, dict(_FAILED))
    _close(qapp, tab)
    _pump(qapp, lambda: False, timeout=0.3)
    assert device.probes() == []


def test_a_repeat_waits_while_the_device_is_away(qapp):
    device = _Device()
    tab, _messages = _retrying_tab(qapp, device)
    try:
        tab._on_probe_details(device, dict(_FAILED))
        tab._reboot_in_progress = True
        _pump(qapp, lambda: False, timeout=0.3)
        assert device.probes() == []  # a reboot owns the device meanwhile
        tab._reboot_in_progress = False
        assert _pump(qapp, lambda: len(device.probes()) == 1)
    finally:
        _close(qapp, tab)


def test_a_probe_that_raises_still_reports_a_result(monkeypatch):
    from turboadb.gui.device_tab import _DeviceProbe, _ProbeThread

    def broken(self, gate=None, on_identity=None, identity_timeout=1.5):
        raise ValueError("unexpected probe output")

    monkeypatch.setattr(_DeviceProbe, "run", broken)
    results = []
    thread = _ProbeThread(_Device())
    thread.details.connect(results.append)
    thread.run()  # synchronously: an escaping exception reached the error popup
    assert len(results) == 1 and results[0]["info"] is None
    assert "unexpected probe output" in results[0]["error"]
