"""The Crashes preset on a device whose logcat has no crash buffer.

Android 4 and older have no crash buffer: their logcat stopped at once with
"Unable to open log device '/dev/log/crash'" and the preset showed nothing.
It now reads the main and system buffers there, where those devices log
crashes, says so, and remembers it for the next capture."""

import time

import pytest

pytest.importorskip("PyQt5")

from test_logcat_panel import _Handler, _chunks  # noqa: E402

NO_CRASH = "Unable to open log device '/dev/log/crash': No such file or directory"


def _pump(qapp, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return until()


def _buffers(args):
    return [args[i + 1] for i, word in enumerate(args) if word == "-b"]


@pytest.fixture
def panel(qapp):
    from turboadb.gui.logcat_view import LogcatPanel

    made = []

    def make(handler):
        p = LogcatPanel(handler)
        p.logged = []
        p.log.connect(p.logged.append)
        p.hist.setCurrentIndex(p.hist.findText("Full buffer + live"))
        made.append(p)
        return p

    yield make
    for p in made:
        p.close_panel()


def test_the_crash_preset_falls_back_to_main_and_system(qapp, panel):
    handler = _Handler({"out": _chunks([NO_CRASH])}, {"live": True}, {"live": True})
    p = panel(handler)
    p._crash_preset()
    p.start()
    assert _pump(qapp, lambda: len(handler.spawned) == 2)
    assert _buffers(handler.spawned[0].args) == ["crash", "main", "system"]
    retry = handler.spawned[1].args
    assert _buffers(retry) == ["main", "system"] and "*:E" in retry  # still Error level
    assert p._no_crash_buffer
    p._render_pending()
    assert any("no crash buffer" in line for line in p.view.toPlainText().splitlines())
    assert any(m.startswith("[WARNING] logcat: no crash buffer") for m in p.logged)
    assert not [m for m in p.logged if "ended by itself" in m]
    p.stop()
    assert _pump(qapp, lambda: p.thread is None)
    p._crash_preset()
    p.start()  # the next capture goes to main and system at once
    assert _pump(qapp, lambda: len(handler.spawned) == 3)
    assert _buffers(handler.spawned[2].args) == ["main", "system"]


def test_other_failures_of_a_crash_capture_are_reported_as_they_are(qapp, panel):
    handler = _Handler({"out": _chunks(["error: device offline"]), "code": 1})
    p = panel(handler)
    p._crash_preset()
    p.start()
    assert _pump(qapp, lambda: p.thread is None)
    _pump(qapp, lambda: False, timeout=0.2)
    assert len(handler.spawned) == 1 and not p._no_crash_buffer
    assert any("logcat ended (exit 1)" in m for m in p.logged)
