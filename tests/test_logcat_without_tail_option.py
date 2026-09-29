"""Live logcat on a device whose logcat has no -T (an older Android).

"Live from now" (-T 1, the default) and "Last N + live" (-T N) stopped at once
there with logcat's usage.  The Logcat tab now sees that, remembers it for the
device, says so, and follows without -T: it reads the buffer once, keeps the
last lines the mode asks for, and leaves the rest of the buffer out of the
stream, which starts with the whole buffer there.  A capture resumed at a
time leaves out what came before it the same way.
"""
import time

import pytest

pytest.importorskip("PyQt5")

from test_logcat_panel import _Handler, _chunks  # noqa: E402

USAGE = [
    "logcat: unknown option -- T",
    "Unrecognized Option",
    "Usage: logcat [options] [filterspecs]",
    "options include:",
    "  -t <count>      print only the most recent <count> lines (implies -d)",
]
BUFFER = [
    "09-27 10:00:00.100  1234  5678 I Tag: first",
    "09-27 10:00:01.200  1234  5678 I Tag: second",
    "09-27 10:00:02.300  1234  5678 I Tag: third",
]
LIVE = [
    "09-27 10:00:03.400  1234  5678 I Tag: fourth",
    "09-27 10:00:04.500  1234  5678 I Tag: fifth",
]


def _pump(qapp, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return until()


@pytest.fixture
def panel(qapp):
    from turboadb.gui.logcat_view import LogcatPanel

    made = []

    def make(handler, mode="Live from now"):
        p = LogcatPanel(handler)
        p.logged = []
        p.log.connect(p.logged.append)
        p.hist.setCurrentIndex(p.hist.findText(mode))
        made.append(p)
        return p

    yield make
    for p in made:
        p.close_panel()


def _refused():
    return {"out": _chunks(USAGE)}


def _dump():
    return {"out": _chunks(["--------- beginning of main"] + BUFFER)}


def _stream():
    return {"out": _chunks(["--------- beginning of main"] + BUFFER + LIVE), "live": True}


def _shown(qapp, p):
    p._render_pending()
    qapp.processEvents()
    return [line for line in p.view.toPlainText().splitlines() if " Tag: " in line]


def _tag_lines(lines):
    return [line.split("Tag: ")[1] for line in lines]


@pytest.mark.parametrize("mode, history", [
    ("Live from now", ["third"]),  # -T 1: the newest line, then live
    ("Last 1,000 + live", ["first", "second", "third"]),
])
def test_a_live_mode_follows_without_minus_t_and_shows_only_what_it_asks_for(
        qapp, panel, mode, history):
    handler = _Handler(_refused(), _dump(), _stream())
    p = panel(handler, mode)
    p.start()
    assert _pump(qapp, lambda: len(handler.spawned) == 3)
    first, dump, stream = (proc.args for proc in handler.spawned)
    assert "-T" in first
    assert "-d" in dump and "-T" not in dump
    assert "-d" not in stream and "-T" not in stream
    assert _pump(qapp, lambda: _tag_lines(_shown(qapp, p)) == history + ["fourth", "fifth"])
    assert p._no_tail_option
    assert any(m.startswith("[WARNING] logcat: this device's logcat has no -T") for m in p.logged)
    assert not [m for m in p.logged if "ended by itself" in m]
    assert "no -T" in p.view.toPlainText()  # said in the view as well


def test_the_next_capture_goes_without_minus_t_at_once(qapp, panel):
    handler = _Handler(_refused(), _dump(), _stream(), _dump(), _stream())
    p = panel(handler)
    p.start()
    assert _pump(qapp, lambda: len(handler.spawned) == 3)
    p.stop()
    assert _pump(qapp, lambda: p.thread is None)
    p.start()
    assert _pump(qapp, lambda: len(handler.spawned) == 5)
    assert "-d" in handler.spawned[3].args and "-T" not in handler.spawned[4].args


def test_a_resumed_capture_leaves_out_what_it_had_already(qapp, panel):
    handler = _Handler(_stream())
    p = panel(handler)
    p._no_tail_option = True  # found out earlier on this device
    spec = p._spec_from_controls()
    stamp = "09-27 10:00:02.300"
    p._last = (stamp, [BUFFER[2]], time.monotonic())
    p._resume = {"spec": spec, "reboot": False}
    p.resume_after_reconnect()
    assert _pump(qapp, lambda: len(handler.spawned) == 1)
    assert "-T" not in handler.spawned[0].args and "-d" not in handler.spawned[0].args
    assert _pump(qapp, lambda: _tag_lines(_shown(qapp, p)) == ["fourth", "fifth"])


def test_another_failure_is_not_taken_for_a_missing_minus_t(qapp, panel):
    handler = _Handler({"out": _chunks(["error: device offline"]), "code": 1})
    p = panel(handler)
    p.start()
    assert _pump(qapp, lambda: p.thread is None)
    _pump(qapp, lambda: False, timeout=0.2)
    assert len(handler.spawned) == 1 and not p._no_tail_option
    assert any("logcat ended (exit 1)" in m for m in p.logged)


def test_a_new_year_is_not_taken_for_an_older_time():
    from turboadb.gui.logcat_view import _stamp_before

    assert _stamp_before("09-27 10:00:01.000", "09-27 10:00:02.000")
    assert not _stamp_before("09-27 10:00:03.000", "09-27 10:00:02.000")
    assert not _stamp_before("01-01 00:00:01.000", "12-31 23:59:59.000")
