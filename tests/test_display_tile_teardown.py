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


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


_VIDEO = b"\x00\x00\x00\x20ftypisom" + b"video" * 20


def _destroyed(qapp, widget):
    """Carry out the deletes that are due, and say whether *widget* is gone."""
    from PyQt5 import sip
    from PyQt5.QtCore import QEvent

    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    return sip.isdeleted(widget)


def test_a_display_removed_mid_recording_is_kept_until_its_scrcpy_has_closed(
    qapp, monkeypatch, tmp_path
):
    """scrcpy recording its own screen is closed by the recording's worker,
    which gives it fifteen seconds to finish the MP4.  The panel does not
    count as active meanwhile: the tile was destroyed with scrcpy's window
    still inside it, and with nobody left to report the recording."""
    import turboadb.gui.mirror_panel as mp
    from turboadb.gui.display_wall import DisplayWall

    closes = []

    class Close:
        """The worker that closes the scrcpy that recorded; the test says when."""

        def __init__(self, session, window_title=None, window_handle=None):
            self.session, self.done, self.closed = session, _Signal(), False
            closes.append(self)

        def start(self):
            pass

        def isRunning(self):
            return not self.closed

        def isFinished(self):  # (nothing to keep alive: never parked)
            return True

        def finish(self):
            self.closed = True
            self.done.emit(True)

    monkeypatch.setattr(mp, "_RecWaitThread", Close)
    monkeypatch.setattr(mp.MirrorPanel, "embeds_scrcpy", staticmethod(lambda: True))
    wall = DisplayWall(_SlowHandler(), {"name": "ivi"}, automotive=True,
                       dispatcher=_NullDispatcher())
    wall.set_displays([{"id": 0, "size": "1920x720"}, {"id": 2, "size": "1280x720"}])
    logs = []
    wall.log.connect(logs.append)
    try:
        tile = wall._tiles[1]
        panel = tile.panel
        clip = tmp_path / "cluster.mp4"
        panel._scrcpy, panel._embed_on = _Session(), True  # its screen is on...
        panel._recording, panel._rec_mode = True, "integrated"  # ...and scrcpy records it
        panel._rec_path = str(clip)
        wall.set_displays([{"id": 0, "size": "1920x720"}])  # a rescan: display 2 is gone
        (closing,) = closes
        assert not _destroyed(qapp, tile) and tile.isHidden()  # scrcpy is still closing
        assert wall._retiring == [tile]
        assert closing in wall.shutdown_threads()

        clip.write_bytes(_VIDEO)
        closing.finish()
        assert any(line.startswith("[OK] recording saved") for line in logs)
        assert _pump_until(qapp, lambda: _destroyed(qapp, tile))
        assert wall._retiring == []
    finally:
        wall.close_panel()
        wall.close()
        wall.deleteLater()
        for _ in range(3):
            qapp.processEvents()


def test_a_display_removed_while_its_recording_is_pulled_is_kept_until_it_is_saved(
    qapp, monkeypatch, tmp_path
):
    """A device recording is stopped and pulled by a worker.  The tile stays
    until that is done: the result is reported, and an exit meanwhile finds
    the recording to wait for."""
    import turboadb.gui.mirror_panel as mp
    from turboadb.gui.display_wall import DisplayWall

    release = threading.Event()

    class Recorder:
        serial = "ivi"
        config = None

        def screen_record_continuous(self, path, *, stop_event=None, on_part=None, **_kwargs):
            stop_event.wait(30)
            release.wait(30)  # the pull
            with open(path, "wb") as fh:
                fh.write(_VIDEO)
            on_part(path)
            return [path]

    clip = str(tmp_path / "cluster.mp4")
    monkeypatch.setattr(mp.QFileDialog, "getSaveFileName", lambda *_a, **_k: (clip, ""))
    monkeypatch.setattr(mp.MirrorPanel, "embeds_scrcpy", staticmethod(lambda: True))
    wall = DisplayWall(Recorder(), {"name": "ivi"}, automotive=True,
                       dispatcher=_NullDispatcher())
    wall.set_displays([{"id": 0, "size": "1920x720"}, {"id": 2, "size": "1280x720"}])
    logs = []
    wall.log.connect(logs.append)
    try:
        tile = wall._tiles[1]
        tile.panel._begin_record()  # no screen runs: the device records
        assert tile.panel._rec_mode == "device"
        wall.set_displays([{"id": 0, "size": "1920x720"}])
        assert not _destroyed(qapp, tile) and wall._retiring == [tile]
        assert mp._recordings_being_saved(wall) == [(tile.panel, clip)]
        release.set()
        assert _pump_until(qapp, lambda: _destroyed(qapp, tile))
        assert any(line.startswith("[OK] recording saved") for line in logs)
        assert wall._retiring == []
    finally:
        release.set()
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
