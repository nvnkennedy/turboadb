"""A failed screen start is retried a bounded number of times, never behind
the user's back.

- The startup watchdog kills a scrcpy that shows no window in time. Its retry
  used to wait for that kill and then replay as a fresh start, which reset the
  retry count: a hung scrcpy was retried forever and its error never shown.
- A display tile nobody can see (its tab is hidden, or another display is
  maximized) reports a failed start at once instead of retrying it.
- Stop (a tile's own, or the displays tab's Stop all) also cancels a retry
  that waits for the previous scrcpy to close.

Headless (offscreen Qt); no device, adb or scrcpy is used.
"""

import time

import pytest

pytest.importorskip("PyQt5")


def _pump(qapp, ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Handler:
    serial = "ivi"
    config = None


class _NullDispatcher:
    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        return True

    def stop(self):
        pass


class _Launch:
    """_MirrorLaunchThread stand-in: records each launch and runs nothing; the
    test reports how it went through ``done`` / ``fail``."""

    made = []

    def __init__(self, handler, opts, compat, log_path):
        self.opts, self.compat = opts, compat
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


class _SlowStop:
    """_MirrorStopThread stand-in that finishes only when the test says so:
    closing a hung scrcpy takes WM_CLOSE, a 5 s wait, then a kill."""

    made = []

    def __init__(self, session, window_title=None, window_handle=None):
        self.done = _Signal()
        self.running = True
        _SlowStop.made.append(self)

    def start(self):
        pass

    def finish(self):
        self.running = False
        self.done.emit(True)

    def isRunning(self):
        return self.running

    def isFinished(self):
        return not self.running


class _HungSession:
    """A scrcpy process that runs but never shows a window."""

    log_path = None
    running = True

    def read_log(self):
        return "INFO: Device: [Acme] Car (Android 14)"

    def stop(self, timeout=None):
        self.running = False


@pytest.fixture
def mp(monkeypatch):
    import turboadb.gui.mirror_panel as mirror_panel

    _Launch.made = []
    _SlowStop.made = []
    monkeypatch.setattr(mirror_panel, "_MirrorLaunchThread", _Launch)
    monkeypatch.setattr(mirror_panel, "_MirrorStopThread", _SlowStop)
    monkeypatch.setattr(mirror_panel, "park_thread", lambda thread: None)
    # The tiles here run scrcpy, as they do on Windows (off Windows a tile
    # shows ADB screencap frames instead, which has no retry ladder).
    monkeypatch.setattr(mirror_panel.MirrorPanel, "embeds_scrcpy", staticmethod(lambda: True))
    return mirror_panel


def _instrument(panel, monkeypatch):
    """Retries without the 900 ms wait, a scrcpy that never shows a window,
    and the log dialog recorded instead of opened."""
    opened, failed, logs = [], [], []
    schedule = panel._schedule_retry
    monkeypatch.setattr(panel, "_schedule_retry", lambda _ms, callback: schedule(0, callback))
    monkeypatch.setattr(panel, "_scrcpy_window_up", lambda: False)
    monkeypatch.setattr(panel, "_open_scrcpy_log", opened.append)
    panel.screen_failed.connect(failed.append)
    panel.log.connect(logs.append)
    return opened, failed, logs


def _hang(panel):
    """The running scrcpy has shown no window for longer than the watchdog allows."""
    panel._start_t = time.time() - panel._STARTUP_TIMEOUT - 5


def _tile_wall(qapp, shown):
    from PyQt5.QtCore import Qt
    from turboadb.gui.display_wall import DisplayWall

    wall = DisplayWall(_Handler(), {"name": "ivi"}, automotive=True, dispatcher=_NullDispatcher())
    wall.set_displays([{"id": 2, "size": "1920x720", "name": "Cluster"}])
    wall.setAttribute(Qt.WA_DontShowOnScreen, True)
    wall.resize(900, 600)
    if shown:
        wall.show()
        _pump(qapp, 50)
    return wall


def _close_wall(qapp, wall):
    wall.close_panel()
    wall.hide()
    wall.close()
    wall.deleteLater()
    for _ in range(3):
        qapp.processEvents()


def test_a_scrcpy_the_watchdog_kills_still_climbs_the_retry_ladder(qapp, mp, monkeypatch):
    panel = mp.MirrorPanel(_Handler(), {"name": "car"}, dispatcher=_NullDispatcher())
    opened, failed, logs = _instrument(panel, monkeypatch)
    try:
        panel.start()
        _Launch.made[0].done.emit(_HungSession())
        _hang(panel)
        panel._check_alive()  # the startup watchdog kills it…
        assert panel._stopping_mirror and panel._retry_count == 1
        _pump(qapp, 100)  # …and its retry comes while that kill still runs
        assert len(_Launch.made) == 1  # it waits for the old scrcpy to close
        assert panel._queued_start and panel._queued_start["_retry"] is True
        _SlowStop.made[-1].finish()
        _pump(qapp, 100)
        assert len(_Launch.made) == 2
        assert panel._retry_count == 1  # still the first retry, not a fresh start

        _Launch.made[-1].fail.emit("adb: device offline")  # the retry fails too
        _pump(qapp, 100)
        assert len(_Launch.made) == 3 and _Launch.made[-1].compat is True  # IVI rung
        _Launch.made[-1].fail.emit("adb: device offline")  # so does the last rung
        _pump(qapp, 100)
        assert len(_Launch.made) == 3  # no fourth try: the error is shown
        assert failed == ["adb: device offline"] and panel._retry_count == 0
        assert any("could not start after 3 attempts" in line for line in logs)
        assert opened and not panel.is_active() and not panel.btn_mirror.isHidden()
    finally:
        panel.close_panel()
        panel.close()


def test_stop_cancels_a_retry_that_waits_for_the_old_scrcpy_to_close(qapp, mp, monkeypatch):
    panel = mp.MirrorPanel(_Handler(), {"name": "car"}, dispatcher=_NullDispatcher())
    _instrument(panel, monkeypatch)
    try:
        panel.start()
        _Launch.made[0].done.emit(_HungSession())
        _hang(panel)
        panel._check_alive()
        _pump(qapp, 100)
        assert panel._stopping_mirror and panel._queued_start is not None
        assert panel.btn_stop.isEnabled()  # a start waits: Stop can still cancel it
        panel.btn_stop.click()
        assert panel._queued_start is None and not panel.btn_stop.isEnabled()
        _SlowStop.made[-1].finish()
        _pump(qapp, 100)
        assert len(_Launch.made) == 1  # the screen did not come back by itself
        assert not panel.is_active()
    finally:
        panel.close_panel()
        panel.close()


def test_stop_all_sticks_for_a_tile_whose_retry_waits_for_the_old_scrcpy(qapp, mp, monkeypatch):
    wall = _tile_wall(qapp, shown=True)
    panel = wall._tiles[0].panel
    _instrument(panel, monkeypatch)
    try:
        panel.start()
        _Launch.made[0].done.emit(_HungSession())
        _hang(panel)
        panel._check_alive()
        _pump(qapp, 100)
        assert panel._queued_start is not None and wall.btn_stop_all.isEnabled()
        wall.stop_all()
        _SlowStop.made[-1].finish()
        _pump(qapp, 100)
        assert len(_Launch.made) == 1 and not panel.is_active()
        assert wall.summary.text() == "1 display — stopped"
    finally:
        _close_wall(qapp, wall)


def test_a_tile_nobody_can_see_reports_a_failed_start_instead_of_retrying(qapp, mp, monkeypatch):
    wall = _tile_wall(qapp, shown=False)  # its tab is hidden (Start all carries on)
    panel = wall._tiles[0].panel
    opened, failed, logs = _instrument(panel, monkeypatch)
    try:
        panel.start()
        _Launch.made[0].fail.emit("ERROR: Could not open video stream")
        assert failed == ["ERROR: Could not open video stream"]
        assert panel._retry_count == 0 and not panel._retry_pending
        _pump(qapp, 100)
        assert len(_Launch.made) == 1  # not retried behind the user's back
        assert any("after 1 attempt (not retried" in line for line in logs)
        assert "Show log" in panel.status.text() and opened == []  # no dialog either
        assert not panel.is_active()  # its Start is offered again

        wall.show()  # the user comes back and presses Start: the full ladder again
        _pump(qapp, 50)
        panel.start()
        _Launch.made[-1].fail.emit("adb server race")
        assert panel._retry_pending and len(failed) == 1
        _pump(qapp, 100)
        assert len(_Launch.made) == 3
    finally:
        _close_wall(qapp, wall)


def test_a_retry_due_after_the_tab_was_hidden_is_not_run_unseen(qapp, mp, monkeypatch):
    wall = _tile_wall(qapp, shown=True)
    panel = wall._tiles[0].panel
    _opened, failed, logs = _instrument(panel, monkeypatch)
    try:
        panel.start()
        _Launch.made[0].fail.emit("adb server race")  # on screen: a retry is due
        assert panel._retry_pending
        wall.hide()  # the user switches to another tab before it runs
        _pump(qapp, 100)
        assert len(_Launch.made) == 1
        assert failed == ["adb server race"] and panel._retry_count == 0
        assert not panel.is_active()
        assert any("after 1 attempt (not retried" in line for line in logs)
    finally:
        _close_wall(qapp, wall)


def test_a_retry_waiting_for_the_old_scrcpy_is_dropped_once_the_tab_is_hidden(
    qapp, mp, monkeypatch
):
    wall = _tile_wall(qapp, shown=True)
    panel = wall._tiles[0].panel
    _opened, failed, logs = _instrument(panel, monkeypatch)
    try:
        panel.start()
        _Launch.made[0].done.emit(_HungSession())
        _hang(panel)
        panel._check_alive()  # on screen: killed, and its retry waits for the kill
        _pump(qapp, 100)
        assert panel._queued_start and panel._queued_start["_retry"] is True
        wall.hide()  # the user leaves the tab while the old scrcpy closes
        _SlowStop.made[-1].finish()
        _pump(qapp, 100)
        assert len(_Launch.made) == 1  # the retry is reported, not run
        assert failed and not panel.is_active()
        assert any("after 1 attempt (not retried" in line for line in logs)
    finally:
        _close_wall(qapp, wall)


def test_the_watchdog_on_a_hidden_tile_gives_up_without_a_retry(qapp, mp, monkeypatch):
    wall = _tile_wall(qapp, shown=False)
    panel = wall._tiles[0].panel
    _opened, failed, logs = _instrument(panel, monkeypatch)
    try:
        panel.start()
        _Launch.made[0].done.emit(_HungSession())
        _hang(panel)
        panel._check_alive()
        assert any("within" in line and "giving up" in line for line in logs)
        assert failed and not panel._retry_pending and panel._queued_start is None
        _SlowStop.made[-1].finish()
        _pump(qapp, 100)
        assert len(_Launch.made) == 1 and not panel.is_active()
    finally:
        _close_wall(qapp, wall)
