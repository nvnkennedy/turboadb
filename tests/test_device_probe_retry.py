"""A connect-time probe that got no device profile is run once more.

adbd on some head units refuses a second shell right after the connect, and a
device can be slow to serve its properties.  The tab used to give up at once
and treat the device as a phone for the whole session: no IVI Displays tab,
the phone defaults in Apps and Device Control.  It now asks one more time,
through the same probe and the tab's adb slots (no extra getprop processes).

The same goes for a profile without the feature list: a head unit that is
still starting answers with its properties but lists no features and no
display size, which reads as a phone."""

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


# --------------------------------------------------------------------------- #
# a profile without the feature list: the device is still starting
# --------------------------------------------------------------------------- #
# adbd answers and the properties are there, but the package and window
# services are not up yet: no feature list, no display size, no displays.
_STARTING = dict(features=(), wm="", displays="")
_HEAD_UNIT = dict(
    features=("android.hardware.bluetooth",), wm="Physical size: 1920x720",
    displays='DisplayInfo{"Centre, displayId 0", real 1920 x 720, x}\n'
             'DisplayInfo{"Cluster, displayId 2", real 1920 x 720, x}',
)


def _probed(device):
    """What the connect-time probe makes of *device* now."""
    from turboadb.gui.device_tab import _DeviceProbe

    return _DeviceProbe(device).run()


def test_a_head_unit_still_starting_is_asked_again_instead_of_taken_for_a_phone(qapp):
    device = _Device(probe_output=_probe_output(**_STARTING))
    first = _probed(device)
    assert first["info"]["kind"] == "phone" and first["features"] is False
    tab, messages = _retrying_tab(qapp, device)
    try:
        tab._on_probe_details(device, first)
        assert any("listed no features" in m and "asking the device again" in m for m in messages)
        assert not any(m.startswith("[OK] vivo V2318") for m in messages)  # not a phone yet
        device.probe_output = _probe_output(**_HEAD_UNIT)  # it has started by then
        assert _pump(qapp, lambda: tab._automotive)
        assert "ivi" in tab._subtabs and [d["id"] for d in tab.mirror_tab._displays] == [0, 2]
        assert any("Infotainment head unit" in m for m in messages)
        _pump(qapp, lambda: False, timeout=0.3)
        assert len(device.probes()) == 2  # the first one and its one repeat
    finally:
        _close(qapp, tab)


def test_a_device_that_never_lists_features_is_asked_once_more_then_taken_as_it_is(qapp):
    device = _Device(probe_output=_probe_output(**_STARTING))
    first = _probed(device)
    tab, messages = _retrying_tab(qapp, device)
    try:
        tab._on_probe_details(device, first)
        assert _pump(qapp, lambda: any(m.startswith("[OK] vivo V2318") for m in messages))
        _pump(qapp, lambda: False, timeout=0.3)
        assert len(device.probes()) == 2 and not tab._automotive
        assert sum("asking the device again" in m for m in messages) == 1
    finally:
        _close(qapp, tab)


def test_a_profile_with_its_feature_list_is_applied_at_once(qapp):
    device = _Device()
    result = _probed(device)
    assert result["features"] is True
    tab, messages = _retrying_tab(qapp, device)
    try:
        tab._on_probe_details(device, result)
        assert any(m.startswith("[OK] vivo V2318") for m in messages)
        _pump(qapp, lambda: False, timeout=0.3)
        assert len(device.probes()) == 1
        assert not any("asking the device again" in m for m in messages)
    finally:
        _close(qapp, tab)


def test_a_profile_without_features_is_kept_when_the_repeat_brings_none(qapp):
    """The profile of the first probe is better than none.  A head unit whose
    build says "automotive" was recognised from that alone; held back for the
    repeat, it was a phone with no profile at all once the repeat failed
    (adbd restarts while the unit starts)."""
    device = _Device(probe_output=_probe_output(characteristics="automotive", **_STARTING))
    first = _probed(device)
    assert first["info"]["automotive"] and first["features"] is False
    tab, messages = _retrying_tab(qapp, device)
    try:
        tab._on_probe_details(device, first)
        assert not tab._automotive  # asked again first
        device.probe_output = b"error: closed\n"  # this time it does not answer
        assert _pump(qapp, lambda: len(device.probes()) == 2)
        assert _pump(qapp, lambda: tab._automotive)
        assert "ivi" in tab._subtabs
        assert any(m.startswith("[OK] vivo V2318") for m in messages)
    finally:
        _close(qapp, tab)
