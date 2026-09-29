"""A display tile removed while its scrcpy launch is running is destroyed.

A rescan that drops a display (a passenger screen turned off, say) closes its
tile, which cancels a launch still in flight. The launch worker then stopped
the session itself and said nothing, so the tile waited for a screen_stopped
that never came and stayed in memory, hidden, for the life of the device tab.
Headless (offscreen Qt); scrcpy is never run: the handler's mirror() stands in.
"""

import threading
import time

import pytest

pytest.importorskip("PyQt5")


def _pump_until(qapp, until, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return until()


class _NullDispatcher:
    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        return True

    def stop(self):
        pass


class _Session:
    """What handler.mirror() returns: a scrcpy session that can be stopped."""

    log_path = None

    def __init__(self):
        self.running = True
        self.stopped = threading.Event()

    def read_log(self):
        return ""

    def stop(self, timeout=None):
        self.running = False
        self.stopped.set()


class _SlowHandler:
    """mirror() blocks until the test releases it, like a scrcpy launch
    pushing its server to a slow head unit."""

    serial = "ivi"
    config = None

    def __init__(self):
        self.release = threading.Event()
        self.sessions = []

    def mirror(self, opts, compat=False, log_path=None, safe=None):
        self.release.wait(10)
        session = _Session()
        self.sessions.append(session)
        return session


def test_a_cancelled_launch_worker_stops_its_session_and_reports_the_end(qapp):
    from turboadb.config import ScrcpyOptions
    from turboadb.gui.mirror_panel import _MirrorLaunchThread

    session = _Session()

    class Handler:
        def mirror(self, *_args, **_kwargs):
            return session

    worker = _MirrorLaunchThread(Handler(), ScrcpyOptions(), False, "test.log")
    done, failed = [], []
    worker.done.connect(done.append)
    worker.fail.connect(failed.append)
    worker.cancel()  # the owner closed while scrcpy was starting
    worker.run()
    assert session.stopped.is_set()  # the worker closes what it started…
    assert done == [] and len(failed) == 1  # …and still says the launch is over

    class Broken:
        def mirror(self, *_args, **_kwargs):
            raise OSError("adb went away")

    worker = _MirrorLaunchThread(Broken(), ScrcpyOptions(), False, "test.log")
    failed = []
    worker.fail.connect(failed.append)
    worker.cancel()
    worker.run()
    assert failed == ["adb went away"]


def test_a_display_removed_while_its_screen_launches_is_destroyed_afterwards(qapp, monkeypatch):
    from turboadb.gui.display_wall import DisplayWall
    from turboadb.gui.mirror_panel import MirrorPanel

    # a tile that runs scrcpy, as on Windows (elsewhere it shows screencap frames)
    monkeypatch.setattr(MirrorPanel, "embeds_scrcpy", staticmethod(lambda: True))
    handler = _SlowHandler()
    wall = DisplayWall(handler, {"name": "ivi"}, automotive=True, dispatcher=_NullDispatcher())
    wall.set_displays([{"id": 0, "size": "1920x720"}, {"id": 2, "size": "1280x720"}])
    try:
        tile = wall._tiles[1]
        tile.panel.start()  # its scrcpy launch is under way (a real worker thread)
        assert tile.panel._launch_pending
        wall.set_displays([{"id": 0, "size": "1920x720"}])  # a rescan: display 2 is gone
        assert wall._retiring == [tile] and tile.isHidden()
        qapp.processEvents()
        assert wall._retiring == [tile]  # kept while the launch still runs
        handler.release.set()
        assert _pump_until(qapp, lambda: not wall._retiring)
        assert handler.sessions and handler.sessions[0].stopped.is_set()
        assert tile.parent() is None  # handed to deleteLater
    finally:
        handler.release.set()
        wall.close_panel()
        wall.close()
        wall.deleteLater()
        for _ in range(3):
            qapp.processEvents()


def test_closing_a_screen_during_its_launch_reports_no_failure(qapp):
    """The cancelled launch now reports its end; a closing panel takes that
    quietly: no retry, no error, no screen_failed."""
    from turboadb.gui.mirror_panel import MirrorPanel

    handler = _SlowHandler()
    panel = MirrorPanel(handler, {"name": "ivi"}, dispatcher=_NullDispatcher())
    failed, logs, stopped = [], [], []
    panel.screen_failed.connect(failed.append)
    panel.log.connect(logs.append)
    panel.screen_stopped.connect(lambda: stopped.append(True))
    try:
        panel.start()
        assert panel._launch_pending
        panel.close_panel()  # the device tab closes meanwhile
        handler.release.set()
        assert _pump_until(qapp, lambda: not panel._launch_pending)
        assert handler.sessions[0].stopped.is_set()
        assert stopped and failed == []
        assert not any(line.startswith("[ERROR]") for line in logs)
        assert not panel._retry_pending
    finally:
        handler.release.set()
        panel.close()
        panel.deleteLater()
        qapp.processEvents()
