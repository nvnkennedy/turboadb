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
    fake_adb.add(lambda argv: "test" in argv and "-w" in argv, returncode=1)
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
