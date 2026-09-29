"""OpenGL is given up only when scrcpy says its renderer failed.

The screen asks scrcpy for OpenGL (trilinear filtering keeps small text sharp)
and falls back to scrcpy's default renderer on a PC where OpenGL cannot be
used. The check read the whole session log, whose first lines are TurboADB's
own header with the command line (``--render-driver=opengl``), so ANY failed
start (an unplugged device, a slow encoder, the startup watchdog) turned
OpenGL off for the rest of the tab's life. Headless; scrcpy is never run.
"""

import time

import pytest

pytest.importorskip("PyQt5")

_HEADER = (
    "TurboADB launched scrcpy as:\n"
    "  scrcpy.exe --serial=ivi --video-bit-rate=16M --render-driver=opengl "
    "--window-title=turboadb-embed-1\n"
    + "-" * 60
    + "\n"
)


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Launch:
    """_MirrorLaunchThread stand-in: records the options, runs nothing."""

    made = []

    def __init__(self, handler, opts, compat, log_path):
        self.opts = opts
        self.done, self.fail = _Signal(), _Signal()
        _Launch.made.append(self)

    def start(self):
        pass

    def cancel(self):
        pass

    def isRunning(self):
        return False

    def isFinished(self):
        return True


class _Stop:
    def __init__(self, session, window_title=None, window_handle=None):
        self.done = _Signal()

    def start(self):
        self.done.emit(True)

    def isRunning(self):
        return False

    def isFinished(self):
        return True


class _Session:
    """A scrcpy that runs and never shows a window; its log is *log*."""

    log_path = None
    running = True

    def __init__(self, log):
        self.log = log

    def read_log(self):
        return self.log

    def stop(self, timeout=None):
        self.running = False


class _Handler:
    serial = "ivi"
    config = None


class _NullDispatcher:
    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        return True

    def stop(self):
        pass


@pytest.fixture
def panel(qapp, monkeypatch):
    import turboadb.gui.mirror_panel as mp

    _Launch.made = []
    monkeypatch.setattr(mp, "_MirrorLaunchThread", _Launch)
    monkeypatch.setattr(mp, "_MirrorStopThread", _Stop)
    monkeypatch.setattr(mp, "park_thread", lambda thread: None)
    panel = mp.MirrorPanel(_Handler(), {"name": "car"}, dispatcher=_NullDispatcher())
    panel._opt["soft"] = False  # a PC with a GPU (not a Remote Desktop session)
    retries = []
    monkeypatch.setattr(panel, "_schedule_retry", lambda _ms, callback: retries.append(callback))
    monkeypatch.setattr(panel, "_scrcpy_window_up", lambda: False)
    monkeypatch.setattr(panel, "_open_scrcpy_log", lambda _text: None)
    panel.retries = retries
    yield panel
    panel._scrcpy = None
    panel._launch_pending = False
    panel.close_panel()
    panel.close()


def _run_retry(panel):
    panel.retries.pop(0)()
    return _Launch.made[-1].opts


def test_only_scrcpys_own_renderer_errors_count():
    from turboadb.gui.mirror_panel import _renderer_failed

    healthy_then_lost = (
        _HEADER
        + "INFO: Renderer: opengl\nINFO: OpenGL version: 4.6.0 NVIDIA 551.23\n"
        + "INFO: Trilinear filtering enabled\nERROR: Could not open video stream\n"
    )
    assert not _renderer_failed(healthy_then_lost)
    assert not _renderer_failed(_HEADER)  # a start the watchdog ended
    assert not _renderer_failed("scrcpy startup timed out")
    assert _renderer_failed(
        _HEADER + "ERROR: Could not create renderer: Couldn't find matching render driver\n"
    )
    assert _renderer_failed(_HEADER + "ERROR: Failed to initialize OpenGL context\n")
    # without the header (a log that could not be written) the output still counts
    assert _renderer_failed("ERROR: Could not create renderer: WGL: no pixel format\n")


def test_a_start_the_watchdog_ended_keeps_opengl(panel):
    panel.start()
    assert _Launch.made[0].opts.render_driver == "opengl"
    _Launch.made[0].done.emit(_Session(_HEADER + "INFO: Device: [Acme] Car (Android 14)\n"))
    panel._start_t = time.time() - panel._STARTUP_TIMEOUT - 5
    panel._check_alive()  # no window in time: killed, and retried
    assert panel.retries and panel._avoid_opengl is False
    assert _run_retry(panel).render_driver == "opengl"


def test_a_failed_start_with_opengl_in_its_output_keeps_opengl(panel):
    panel.start()
    _Launch.made[0].fail.emit(
        _HEADER + "INFO: Renderer: opengl\nINFO: OpenGL version: 4.6.0\n"
        "ERROR: Device disconnected\n"
    )
    assert panel._avoid_opengl is False
    assert _run_retry(panel).render_driver == "opengl"


def test_a_renderer_that_cannot_be_created_falls_back_to_scrcpys_default(panel):
    logs = []
    panel.log.connect(logs.append)
    panel.start()
    _Launch.made[0].fail.emit(
        _HEADER + "ERROR: Could not create renderer: Couldn't find matching render driver\n"
    )
    assert panel._avoid_opengl is True
    assert "default renderer" in panel.status.text()
    assert any("could not use OpenGL" in line for line in logs)
    assert _run_retry(panel).render_driver is None  # scrcpy's default
    # the PC has no working OpenGL: a later start does not fail on it again
    panel._launch_pending = False
    panel.start()
    assert _Launch.made[-1].opts.render_driver is None


def test_a_renderer_error_without_opengl_changes_nothing(panel):
    panel._opt["soft"] = True  # Remote Desktop: software rendering was asked for
    panel.start()
    assert _Launch.made[0].opts.render_driver == "software"
    _Launch.made[0].fail.emit(_HEADER + "ERROR: Could not create renderer: out of memory\n")
    assert panel._avoid_opengl is False
