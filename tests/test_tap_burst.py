"""Rapid tap bursts: reading the touchscreen, building the device-side loop,
the engine's counting and fallbacks, and the CLI around them."""
import json

import pytest

from turboadb import touch
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBError

GETEVENT = """add device 1: /dev/input/event0
  name:     "gpio-keys"
  events:
    KEY (0001): KEY_VOLUMEUP          KEY_VOLUMEDOWN
add device 2: /dev/input/event5
  name:     "synaptics-pad"
  events:
    ABS (0003): ABS_MT_POSITION_X     : value 0, min 0, max 999, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y     : value 0, min 0, max 499, fuzz 0, flat 0, resolution 0
add device 3: /dev/input/event3
  name:     "goodix-ts"
  events:
    KEY (0001): BTN_TOUCH
    ABS (0003): ABS_MT_SLOT           : value 0, min 0, max 9, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_X     : value 0, min 0, max 4095, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y     : value 0, min 0, max 8191, fuzz 0, flat 0, resolution 0
                ABS_MT_TRACKING_ID    : value 0, min 0, max 65535, fuzz 0, flat 0, resolution 0
  input props:
    INPUT_PROP_DIRECT
"""

OLD_SCREEN = """add device 1: /dev/input/event2
  name:     "older-ts"
  events:
    ABS (0003): ABS_MT_POSITION_X     : value 0, min 0, max 1079, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y     : value 0, min 0, max 1919, fuzz 0, flat 0, resolution 0
  input props:
    INPUT_PROP_DIRECT
"""


# ---- reading the touchscreen ----------------------------------------------- #
def test_the_touchscreen_is_picked_over_keys_and_touchpads():
    devices = touch.parse_touch_devices(GETEVENT)
    assert [d.path for d in devices] == ["/dev/input/event5", "/dev/input/event3"]
    screen = touch.pick_touch_device(devices)
    assert screen.path == "/dev/input/event3" and screen.name == "goodix-ts"
    assert (screen.max_x, screen.max_y) == (4095, 8191)
    assert screen.protocol == "b" and screen.direct
    # a pad (no INPUT_PROP_DIRECT) is still listed, but never chosen first
    pad = devices[0]
    assert pad.protocol == "a" and not pad.direct


def test_a_screen_without_slots_uses_the_older_protocol():
    screen = touch.pick_touch_device(touch.parse_touch_devices(OLD_SCREEN))
    assert screen.protocol == "a" and (screen.max_x, screen.max_y) == (1079, 1919)
    assert touch.pick_touch_device([]) is None
    assert touch.parse_touch_devices("") == []


def test_points_are_scaled_into_the_touchscreens_own_units():
    screen = touch.pick_touch_device(touch.parse_touch_devices(GETEVENT))
    # a 4096x8192 digitiser under a 1080x2400 display
    assert touch.scale_point(540, 1200, (1080, 2400), screen) == (2048, 4096)
    assert touch.scale_point(0, 0, (1080, 2400), screen) == (0, 0)
    # never off the panel, whatever is asked for
    assert touch.scale_point(5000, 9000, (1080, 2400), screen) == (4095, 8191)
    same = touch.pick_touch_device(touch.parse_touch_devices(OLD_SCREEN))
    assert touch.scale_point(540, 960, (1080, 1920), same) == (540, 960)


def test_one_tap_is_a_complete_down_and_up():
    screen = touch.pick_touch_device(touch.parse_touch_devices(GETEVENT))
    events = touch.tap_events(screen, 100, 200)
    assert events[0] == (touch.EV_ABS, touch.ABS_MT_TRACKING_ID, 1)
    assert (touch.EV_ABS, touch.ABS_MT_POSITION_X, 100) in events
    assert (touch.EV_ABS, touch.ABS_MT_POSITION_Y, 200) in events
    assert events[-3:] == [(touch.EV_ABS, touch.ABS_MT_TRACKING_ID, -1),
                           (touch.EV_KEY, touch.BTN_TOUCH, 0),
                           (touch.EV_SYN, touch.SYN_REPORT, 0)]
    older = touch.pick_touch_device(touch.parse_touch_devices(OLD_SCREEN))
    old_events = touch.tap_events(older, 5, 6)
    assert (touch.EV_SYN, touch.SYN_MT_REPORT, 0) in old_events  # protocol A
    assert all(code != touch.ABS_MT_TRACKING_ID for _t, code, _v in old_events)
    line = touch.sendevent_tap(screen, 100, 200)
    assert line.startswith("sendevent /dev/input/event3 3 57 1 && ")
    assert line.count("sendevent") == len(events)  # chained, so a refusal ends the tap


def test_the_burst_loop_runs_on_the_device():
    script = touch.burst_script(touch.input_tap(5, 6), 1000, progress_every=50)
    assert script.startswith("i=0; while [ $i -lt 1000 ]; do input tap 5 6 || ")
    assert 'echo "@@$i"' in script and "sleep" not in script
    paced = touch.burst_script(touch.input_tap(5, 6, ["-d", "2"]), 10, sleep_s=0.05)
    assert "input -d 2 tap 5 6" in paced and "sleep 0.05" in paced
    assert touch.parse_progress("@@250") == 250
    assert touch.parse_progress("sendevent: Permission denied") is None
    assert touch.parse_progress("@@oops") is None


# ---- the engine ------------------------------------------------------------ #
@pytest.fixture
def handler(fake_adb):
    fake_adb.add("getevent -pl", stdout=GETEVENT)
    # the shell may write to the touchscreen: the probe prints its marker
    fake_adb.add("test -w /dev/input/event3", stdout="@@writable\n")
    fake_adb.add("dumpsys window displays",
                 stdout="  Display: mDisplayId=0\n  cur=1080x2400 app=1080x2400\n")
    return ADBHandler(ADBConfig(serial="S1"))


def _lines(handler, monkeypatch, *, taps=None, extra=()):
    """Capture the burst's adb arguments and answer with its progress lines."""
    calls = []

    def iter_lines(args, **kwargs):
        calls.append((list(args), kwargs))
        for line in extra:
            yield line
        if taps is not None:
            yield f"@@{taps}"

    monkeypatch.setattr(handler, "iter_lines", iter_lines)
    return calls


def test_touch_device_reports_the_screen_and_whether_it_is_writable(handler, fake_adb):
    info = handler.touch_device()
    assert info["path"] == "/dev/input/event3" and info["writable"] is True
    assert info["protocol"] == "b" and info["max_x"] == 4095
    reads = sum("getevent" in " ".join(c) for c in fake_adb.calls)
    handler.touch_device()  # cached: the same handler asks once
    assert sum("getevent" in " ".join(c) for c in fake_adb.calls) == reads
    handler.touch_device(refresh=True)
    assert sum("getevent" in " ".join(c) for c in fake_adb.calls) == reads + 1


def test_a_burst_taps_through_the_touchscreen_when_it_can(handler, monkeypatch):
    calls = _lines(handler, monkeypatch, taps=500)
    report = handler.tap_burst(540, 1200, count=500)
    args, kwargs = calls[0]
    assert args[0] == "shell" and "sendevent /dev/input/event3" in args[1]
    assert "3 53 2048" in args[1] and "3 54 4096" in args[1]  # scaled to the panel
    assert "while [ $i -lt 500 ]" in args[1] and "sleep" not in args[1]
    assert report["method"] == "events" and report["taps"] == 500
    assert report["device"] == "/dev/input/event3" and report["rate"] > 0
    assert kwargs["timeout"] >= 60


def test_a_burst_falls_back_to_input_when_the_screen_is_not_writable(fake_adb, monkeypatch):
    fake_adb.add("getevent -pl", stdout=GETEVENT)
    fake_adb.add("test -w", returncode=1)  # refused: no marker printed
    handler = ADBHandler(ADBConfig(serial="S1"))
    calls = _lines(handler, monkeypatch, taps=20)
    report = handler.tap_burst(10, 20, count=20)
    assert "input tap 10 20" in calls[0][0][1]
    assert report["method"] == "input" and report["device"] is None
    # asking for events explicitly says why it cannot
    with pytest.raises(ADBError, match="may not write"):
        handler.tap_burst(10, 20, count=5, method="events")


def test_a_burst_to_another_display_uses_input(handler, monkeypatch):
    calls = _lines(handler, monkeypatch, taps=5)
    handler.tap_burst(10, 20, count=5, display_id=2)
    assert "input -d 2 tap 10 20" in calls[0][0][1]
    with pytest.raises(ADBError, match="own display"):
        handler.tap_burst(10, 20, count=5, display_id=2, method="events")


def test_a_burst_without_a_touchscreen_still_taps(fake_adb, monkeypatch):
    fake_adb.add("getevent -pl", stdout="add device 1: /dev/input/event0\n  name: \"keys\"\n")
    handler = ADBHandler(ADBConfig(serial="S1"))
    calls = _lines(handler, monkeypatch, taps=3)
    assert handler.tap_burst(1, 2, count=3)["method"] == "input"
    assert "input tap 1 2" in calls[0][0][1]
    with pytest.raises(ADBError, match="no touchscreen"):
        handler.tap_burst(1, 2, count=3, method="events")


def test_rate_and_duration_decide_how_long_the_burst_runs(handler, monkeypatch):
    calls = _lines(handler, monkeypatch, taps=120)
    report = handler.tap_burst(5, 5, rate=20, duration=6, method="input")
    assert "while [ $i -lt 120 ]" in calls[0][0][1] and "sleep 0.05" in calls[0][0][1]
    assert report["taps"] == 120
    # a rate-limited burst gets time for its own sleeps
    assert calls[0][1]["timeout"] >= 120 * 0.05
    with pytest.raises(ValueError, match="needs a rate"):
        handler.tap_burst(5, 5, duration=10)
    for bad in ({"count": 0}, {"rate": 0, "duration": 5}, {"count": 10 ** 7}):
        with pytest.raises(ValueError):
            handler.tap_burst(5, 5, **bad)
    assert handler.tap_burst(5, 5, count=0, safe=True).success is False


def test_progress_is_reported_and_an_unfinished_burst_is_an_error(handler, monkeypatch):
    seen = []
    monkeypatch.setattr(handler, "iter_lines", lambda args, **kw: iter(["@@50", "@@100"]))
    handler.tap_burst(5, 5, count=100, on_progress=lambda done, total: seen.append((done, total)))
    assert seen == [(50, 100), (100, 100)]

    monkeypatch.setattr(handler, "iter_lines",
                        lambda args, **kw: iter(["@@40", "sendevent: Permission denied"]))
    with pytest.raises(ADBError, match="Permission denied"):
        handler.tap_burst(5, 5, count=100)
    monkeypatch.setattr(handler, "iter_lines", lambda args, **kw: iter([]))
    with pytest.raises(ADBError, match="stopped after 0 of 100"):
        handler.tap_burst(5, 5, count=100)


def test_a_failing_tap_stops_the_loop_instead_of_running_it_out(handler, monkeypatch):
    calls = _lines(handler, monkeypatch, taps=10)
    handler.tap_burst(5, 5, count=10, method="input")
    script = calls[0][0][1]
    assert "||" in script and "exit" in script  # the loop gives up on the first failure
    assert 'echo "tap failed after $i taps"' in script  # and says how far it got
    assert touch.refused_after("tap failed after 12 taps") == 12
    assert touch.refused_after("@@12") is None


# ---- a turned display ------------------------------------------------------ #
PHONE_PANEL = """add device 4: /dev/input/event2
  name:     "sec_touchscreen"
  events:
    KEY (0001): BTN_TOUCH
    ABS (0003): ABS_MT_SLOT           : value 0, min 0, max 9, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_X     : value 0, min 0, max 1079, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y     : value 0, min 0, max 2399, fuzz 0, flat 0, resolution 0
                ABS_MT_TRACKING_ID    : value 0, min 0, max 65535, fuzz 0, flat 0, resolution 0
  input props:
    INPUT_PROP_DIRECT
"""


def _dumpsys_input(field="SurfaceOrientation: 1"):
    """``dumpsys input`` as Android prints it: a keyboard, then the touchscreen."""
    return ("Input Reader State (Nums of device: 2):\n"
            "  Device 1: gpio-keys\n"
            "    Sources: 0x00000101\n"
            "      SurfaceOrientation: 3\n"  # never another device's
            "  Device 4: sec_touchscreen\n"
            "    Touch Input Mapper (mode - DIRECT):\n"
            "      Raw Touch Axes:\n"
            "        X: min=0, max=1079, flat=0, fuzz=0, resolution=0\n"
            "      Viewport INTERNAL: displayId=0, orientation=1, logicalFrame=[0, 0, 2400, 1080]\n"
            f"      {field}\n"
            "Input Dispatcher State:\n")


def _android_maps(raw, turns, panel):
    """Android's own mapping of a panel point to display pixels (the inverse
    of what scale_point does), for a panel whose units are its pixels."""
    x, y = raw
    return {0: (x, y), 1: (y, panel.max_x - x), 2: (panel.max_x - x, panel.max_y - y),
            3: (panel.max_y - y, x)}[turns]


def test_a_point_on_a_turned_display_lands_where_android_puts_it():
    panel = touch.pick_touch_device(touch.parse_touch_devices(PHONE_PANEL))
    for turns, screen in ((0, (1080, 2400)), (1, (2400, 1080)), (2, (1080, 2400)),
                          (3, (2400, 1080))):
        for point in ((2000 % screen[0], 500), (10, 20), (screen[0] - 1, screen[1] - 1)):
            raw = touch.scale_point(*point, screen, panel, turns)
            assert _android_maps(raw, turns, panel) == point, (turns, point)
    # the case that went wrong: landscape, a portrait panel, tapped near the right edge
    assert touch.scale_point(2000, 500, (2400, 1080), panel, 1) == (579, 2000)
    assert touch.scale_point(2000, 500, (2400, 1080), panel) == (900, 1111)  # unturned: wrong


def test_the_rotation_comes_from_the_touchscreens_own_section_of_dumpsys_input():
    assert touch.input_rotation(_dumpsys_input(), "sec_touchscreen") == 1
    assert touch.input_rotation(_dumpsys_input("SurfaceOrientation: 0"), "sec_touchscreen") == 0
    # Android 14 names the rotation instead
    assert touch.input_rotation(_dumpsys_input("InputDeviceOrientation: Rotation270"),
                                "sec_touchscreen") == 3
    assert touch.input_rotation(_dumpsys_input("InputDeviceOrientation: Rotation0"),
                                "sec_touchscreen") == 0
    assert touch.input_rotation(_dumpsys_input(""), "sec_touchscreen") is None
    assert touch.input_rotation(_dumpsys_input(), "goodix-ts") is None  # not listed
    assert touch.input_rotation("", "sec_touchscreen") is None


def test_the_panels_shape_tells_a_quarter_turn_when_dumpsys_cannot():
    panel = touch.pick_touch_device(touch.parse_touch_devices(PHONE_PANEL))
    assert touch.looks_rotated((2400, 1080), panel) is True
    assert touch.looks_rotated((1080, 2400), panel) is False
    square = touch.TouchDevice("/dev/input/event1", "ts", 4095, 4095, "b", True)
    assert touch.looks_rotated((1920, 720), square) is None  # a square grid can't tell


@pytest.fixture
def landscape(fake_adb):
    """A rooted phone held in landscape: its panel reports in portrait."""
    fake_adb.add("getevent -pl", stdout=PHONE_PANEL)
    fake_adb.add("test -w /dev/input/event2", stdout="@@writable\n")
    fake_adb.add("dumpsys window displays",
                 stdout="  Display: mDisplayId=0\n  init=1080x2400 cur=2400x1080\n")
    return fake_adb


def test_auto_taps_a_turned_display_through_input(landscape, monkeypatch):
    landscape.add("dumpsys input", stdout=_dumpsys_input("SurfaceOrientation: 1"))
    handler = ADBHandler(ADBConfig(serial="S1"))
    calls = _lines(handler, monkeypatch, taps=100)
    report = handler.tap_burst(2000, 500, count=100)
    assert "input tap 2000 500" in calls[0][0][1] and "sendevent" not in calls[0][0][1]
    assert report["method"] == "input"


def test_events_on_a_turned_display_turn_the_point_back(landscape, monkeypatch):
    landscape.add("dumpsys input", stdout=_dumpsys_input("SurfaceOrientation: 1"))
    handler = ADBHandler(ADBConfig(serial="S1"))
    calls = _lines(handler, monkeypatch, taps=100)
    handler.tap_burst(2000, 500, count=100, method="events")
    script = calls[0][0][1]
    assert "sendevent /dev/input/event2 3 53 579 " in script  # X on the panel
    assert "sendevent /dev/input/event2 3 54 2000 " in script  # Y on the panel


def test_a_turn_nobody_reports_is_never_guessed(landscape, monkeypatch):
    # dumpsys input says nothing, but a portrait panel under a landscape display
    handler = ADBHandler(ADBConfig(serial="S1"))
    calls = _lines(handler, monkeypatch, taps=100)
    assert handler.tap_burst(2000, 500, count=100)["method"] == "input"
    assert "input tap 2000 500" in calls[0][0][1]
    with pytest.raises(ADBError, match="could not tell how the display is turned"):
        handler.tap_burst(2000, 500, count=100, method="events")


# ---- Android 6 and older: every adb shell exits 0 ------------------------- #
def test_writability_is_read_from_the_probe_not_its_exit_code(fake_adb, monkeypatch):
    fake_adb.add("getevent -pl", stdout=GETEVENT)
    # `test -w` refused, yet adb (no shell protocol) exits 0 and prints nothing
    fake_adb.add("test -w", stdout="", returncode=0)
    fake_adb.add("dumpsys window displays",
                 stdout="  Display: mDisplayId=0\n  cur=1080x2400 app=1080x2400\n")
    handler = ADBHandler(ADBConfig(serial="S1"))
    assert handler.touch_device()["writable"] is False
    probe = next(" ".join(c) for c in fake_adb.calls if "test -w" in " ".join(c))
    assert "&& echo @@writable" in probe
    calls = _lines(handler, monkeypatch, taps=20)
    assert handler.tap_burst(10, 20, count=20)["method"] == "input"
    assert "input tap 10 20" in calls[0][0][1]


def _refusing_panel(handler, monkeypatch, *, after=0):
    """sendevent is refused (after *after* taps); input taps work."""
    runs = []

    def iter_lines(args, **_kwargs):
        runs.append(args[1])
        if "sendevent" in args[1]:
            yield "sendevent: /dev/input/event3: Permission denied"
            yield f"tap failed after {after} taps"
        else:
            yield "@@30"

    monkeypatch.setattr(handler, "iter_lines", iter_lines)
    return runs


def test_auto_goes_on_with_input_when_the_panel_refuses_the_first_event(handler, monkeypatch):
    runs = _refusing_panel(handler, monkeypatch)
    report = handler.tap_burst(5, 5, count=30)
    assert report["method"] == "input" and report["taps"] == 30 and report["device"] is None
    assert "sendevent" in runs[0] and "input tap 5 5" in runs[1]
    assert handler.touch_device()["writable"] is False  # never tried again on this device


def test_taps_already_made_are_never_made_again(handler, monkeypatch):
    runs = _refusing_panel(handler, monkeypatch, after=12)
    with pytest.raises(ADBError, match="tap failed after 12 taps"):
        handler.tap_burst(5, 5, count=30)
    assert len(runs) == 1
    # and "events" asked for by name is never swapped for input
    runs = _refusing_panel(handler, monkeypatch)
    with pytest.raises(ADBError, match="Permission denied"):
        handler.tap_burst(5, 5, count=30, method="events")
    assert len(runs) == 1


# ---- slow devices ------------------------------------------------------------ #
def test_a_slow_burst_runs_as_long_as_the_device_reports_progress(handler, monkeypatch):
    import time

    monkeypatch.setattr(ADBHandler, "_BURST_TAP_SECONDS", {"input": 0.001, "events": 0.001})
    monkeypatch.setattr(ADBHandler, "_BURST_QUIET_SLACK_S", 0.6)

    def iter_lines(args, *, stop_event=None, **_kwargs):
        for done in range(5, 101, 5):  # 20 reports, 0.2 s apart: 4 s in all
            time.sleep(0.2)
            if stop_event is not None and stop_event.is_set():
                return
            yield f"@@{done}"

    monkeypatch.setattr(handler, "iter_lines", iter_lines)
    report = handler.tap_burst(5, 5, count=100, method="input")
    assert report["taps"] == 100 and report["seconds"] >= 4


def test_a_burst_that_goes_quiet_is_ended_and_says_why(handler, monkeypatch):
    monkeypatch.setattr(ADBHandler, "_BURST_TAP_SECONDS", {"input": 0.001, "events": 0.001})
    monkeypatch.setattr(ADBHandler, "_BURST_QUIET_SLACK_S", 0.3)
    ended = []

    def iter_lines(args, *, stop_event=None, **_kwargs):
        yield "@@5"
        ended.append(stop_event.wait(10))  # the device hangs until the burst is ended

    monkeypatch.setattr(handler, "iter_lines", iter_lines)
    with pytest.raises(ADBError, match=r"no progress for 0\.3\d* s \(after 5 of 100 taps\)"):
        handler.tap_burst(5, 5, count=100, method="input")
    assert ended == [True]


def test_the_quiet_limit_covers_the_taps_between_two_reports(handler, monkeypatch):
    calls = _lines(handler, monkeypatch, taps=1000)
    handler.tap_burst(5, 5, count=1000, method="input")
    # a head unit's `input tap` can take a second: 1000 of them are far from the backstop
    assert calls[0][1]["timeout"] >= 1000 * 1.0
    calls = _lines(handler, monkeypatch, taps=1000)
    handler.tap_burst(5, 5, count=1000, method="input", timeout=90)
    assert calls[0][1]["timeout"] == 90  # an explicit cap is kept


# ---- the CLI --------------------------------------------------------------- #
class _Dev:
    config = ADBConfig()

    def __init__(self, **returns):
        self.calls = []
        self.returns = returns

    def connect(self, *args, **kwargs):
        return True

    def tap_burst(self, x, y, **kwargs):
        self.calls.append((x, y, kwargs))
        progress = kwargs.get("on_progress")
        if progress:
            progress(kwargs.get("count") or 1, kwargs.get("count") or 1)
        return self.returns.get("tap_burst", {"method": "events", "taps": kwargs.get("count", 1),
                                              "seconds": 2.0, "rate": 250.0,
                                              "device": "/dev/input/event3"})

    def touch_device(self):
        return self.returns.get("touch_device")

    def stop_server(self):
        self.calls.append(("stop_server", {}))
        return self.returns.get("stop_server", True)


@pytest.fixture
def run_cli(monkeypatch, capsys):
    import turboadb.cli as cli

    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: True)
    monkeypatch.setattr(cli, "scrcpy_available", lambda *a, **k: True)

    def go(argv, dev):
        monkeypatch.setattr(cli, "_handler", lambda args, **kw: dev)
        rc = cli.main(argv)
        out, err = capsys.readouterr()
        return rc, out, err

    return go


def test_cli_tap_burst_passes_its_options_and_reports(run_cli):
    dev = _Dev()
    rc, out, err = run_cli(["-s", "S1", "tap-burst", "540", "1200", "--count", "5000",
                            "--rate", "20", "--method", "events"], dev)
    assert rc == 0
    x, y, kwargs = dev.calls[0]
    assert (x, y) == (540, 1200)
    assert kwargs["count"] == 5000 and kwargs["rate"] == 20 and kwargs["method"] == "events"
    assert kwargs["duration"] is None and kwargs["display_id"] is None
    assert "5000 taps in 2s (250/s, events through /dev/input/event3)" in out
    assert "5000/5000 taps" in err  # progress while it runs
    rc, out, _err = run_cli(["-s", "S1", "tap-burst", "1", "2", "--json"], _Dev())
    assert rc == 0 and json.loads(out)["result"]["method"] == "events"


def test_cli_tap_burst_timeout_caps_the_whole_burst(run_cli):
    dev = _Dev()
    assert run_cli(["-s", "S1", "tap-burst", "1", "2"], dev)[0] == 0
    assert dev.calls[0][2]["timeout"] is None  # no cap: it runs while the device reports
    dev = _Dev()
    assert run_cli(["-s", "S1", "tap-burst", "1", "2", "--timeout", "900"], dev)[0] == 0
    assert dev.calls[0][2]["timeout"] == 900


def test_cli_touch_device_reports_or_fails(run_cli):
    dev = _Dev(touch_device={"path": "/dev/input/event3", "name": "goodix-ts", "max_x": 4095,
                             "max_y": 8191, "protocol": "b", "direct": True, "writable": False})
    rc, out, _err = run_cli(["-s", "S1", "touch-device"], dev)
    assert rc == 0 and "/dev/input/event3  goodix-ts" in out
    assert "4096x8192 event units" in out and "no (try adb root)" in out
    rc, _out, err = run_cli(["-s", "S1", "touch-device"], _Dev(touch_device=None))
    assert rc == 1 and "no touchscreen" in err


def test_cli_stop_server(run_cli):
    rc, out, _err = run_cli(["stop-server"], _Dev())
    assert rc == 0 and "adb server stopped" in out
    rc, _out, err = run_cli(["stop-server"], _Dev(stop_server=False))
    assert rc == 1 and "still running" in err
