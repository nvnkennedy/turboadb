"""The embedded device screen: typing on the video, recording beside a live
screen, and the shape of the idle "Screen is off" frame.  Headless (Qt
offscreen platform); no device, adb or network is used."""

from __future__ import annotations

import ctypes
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytest.importorskip("PyQt5")


class _Handler:
    serial = "mock"
    config = None


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Session:
    """A scrcpy session stand-in."""

    log_path = None

    def __init__(self, running=True):
        self.running = running
        self.stop_calls = []

    def read_log(self):
        return ""

    def stop(self, **kwargs):
        self.stop_calls.append(kwargs)
        self.running = False

    def wait(self, timeout=None):
        self.running = False
        return 0


def _panel(**kwargs):
    from turboadb.gui.mirror_panel import MirrorPanel

    return MirrorPanel(_Handler(), {"name": "mock"}, **kwargs)


def _close(panel):
    panel._scrcpy = None
    panel._rec_session = None
    panel._recording = False
    panel._child_hwnd = None
    panel.close_panel()
    panel.close()


# --------------------------------------------------------------------------- #
# typing on the embedded screen
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.name != "nt", reason="WM_PARENTNOTIFY is a Win32 message")
def test_container_reports_only_button_down_parent_notifications(app):
    from ctypes import wintypes

    from PyQt5 import sip
    from PyQt5.QtCore import QByteArray
    from turboadb.gui.mirror_panel import _EmbedContainer

    clicks = []
    container = _EmbedContainer(lambda *_args: None, on_child_click=lambda: clicks.append(True))
    generic = QByteArray(b"windows_generic_MSG")

    def send(message, low, high=0, kind=generic):
        msg = wintypes.MSG()
        msg.message = message
        msg.wParam = (high << 16) | low
        return container.nativeEvent(kind, sip.voidptr(ctypes.addressof(msg)))

    try:
        # left, right, middle and pen/touch presses over the scrcpy child
        for button in (0x0201, 0x0204, 0x0207, 0x0246):
            clicks.clear()
            handled = send(0x0210, button, high=7)
            assert clicks == [True], hex(button)
            assert not handled[0]  # the message still reaches Qt/DefWindowProc
        clicks.clear()
        send(0x0210, 0x0001)  # a child window was created
        send(0x0210, 0x0002)  # ... or destroyed
        send(0x0210, 0x0202)  # WM_LBUTTONUP is not a notification Windows sends
        send(0x0201, 0x0201)  # a plain button message, not WM_PARENTNOTIFY
        send(0x0210, 0x0201, kind=QByteArray(b"windows_dispatcher_MSG"))
        assert clicks == []
    finally:
        container.close()


def test_click_on_embedded_video_gives_the_container_the_keyboard(app, monkeypatch):
    import turboadb.gui.mirror_panel as mp_mod
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest

    claimed = []
    monkeypatch.setattr(mp_mod, "_claim_native_focus", lambda hwnd: claimed.append(hwnd) or True)
    panel = _panel()
    # the fake child HWNDs below must never reach a real MoveWindow
    panel._fit = lambda: None
    try:
        panel.resize(640, 480)
        panel.show()
        app.processEvents()
        focus = []
        panel.container.setFocus = lambda reason=None: focus.append(reason)

        panel._on_embedded_child_click()  # nothing embedded: nothing to type on
        QTest.qWait(80)
        assert focus == [] and claimed == []

        panel._child_hwnd = 4242
        panel._on_embedded_child_click()
        assert focus == []  # deferred: SDL is still handling its own click
        QTest.qWait(80)
        assert focus == [Qt.MouseFocusReason]
        assert claimed == [int(panel.container.winId())]
        # the later re-check only re-asserts focus the container still holds
        QTest.qWait(350)
        assert focus == [Qt.MouseFocusReason]

        # leaving the page cancels a pending click-focus
        panel._on_embedded_child_click()
        panel.yield_keyboard()
        QTest.qWait(80)
        assert focus == [Qt.MouseFocusReason]
    finally:
        _close(panel)


def test_page_show_never_grabs_the_keyboard(app, monkeypatch):
    import turboadb.gui.mirror_panel as mp_mod

    claimed = []
    monkeypatch.setattr(mp_mod, "_claim_native_focus", lambda hwnd: claimed.append(hwnd) or True)
    panel = _panel()
    panel._fit = lambda: None  # the fake child HWND must never reach MoveWindow
    try:
        panel._scrcpy = _Session()
        panel._embed_on = True
        panel._child_hwnd = 7
        panel.show()
        app.processEvents()
        panel._refresh_buttons()
        assert not panel.container.hasFocus()
        assert claimed == []
    finally:
        _close(panel)


def test_claim_native_focus_moves_win32_focus_to_the_container(monkeypatch):
    import turboadb.gui.mirror_panel as mp_mod

    class User32:
        focus = 99

        def GetFocus(self):
            return self.focus

        def SetFocus(self, hwnd):
            self.focus = hwnd
            return 99

    fake = User32()
    monkeypatch.setattr(mp_mod, "_IS_WIN", True)
    monkeypatch.setattr(mp_mod, "_win_api", lambda: (None, fake))
    assert mp_mod._claim_native_focus(1234) is True
    assert fake.focus == 1234
    assert mp_mod._claim_native_focus(None) is False


def test_reparent_lets_clicks_on_scrcpy_notify_the_container(monkeypatch):
    from turboadb.gui import mirror_panel as mp

    class User32:
        def __init__(self):
            self.longs = {
                mp._GWL_STYLE: mp._WS_POPUP | 0x10000000,
                mp._GWL_EXSTYLE: mp._WS_EX_NOPARENTNOTIFY | 0x00000008,
            }
            self.parent = 0

        def GetWindowLongPtrW(self, _hwnd, index):
            return self.longs[index]

        def SetWindowLongPtrW(self, _hwnd, index, value):
            self.longs[index] = value
            return 1

        def SetParent(self, _hwnd, parent):
            self.parent = parent
            return 1

        def GetAncestor(self, _hwnd, _flag):
            return self.parent

        def SetWindowPos(self, *_args):
            return 1

        def ShowWindow(self, *_args):
            return 1

    fake = User32()
    monkeypatch.setattr(mp, "_win_api", lambda: (None, fake))
    mp._reparent(1234, 5678, 400, 300)
    ex_style = fake.longs[mp._GWL_EXSTYLE]
    assert not ex_style & mp._WS_EX_NOPARENTNOTIFY
    assert ex_style & 0x00000008  # other extended styles are left alone
    assert fake.longs[mp._GWL_STYLE] & mp._WS_CHILD


# --------------------------------------------------------------------------- #
# recording beside a live screen
# --------------------------------------------------------------------------- #
@pytest.fixture
def rec(app, monkeypatch, tmp_path):
    import turboadb.gui.mirror_panel as mp_mod

    ns = types.SimpleNamespace(
        launches=[], stops=[], discards=[], viewer_stops=[], record_closes=[]
    )
    ns.path = str(tmp_path / "clip.mp4")

    class _Thread:
        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def isRunning(self):
            return False

        def isFinished(self):
            return True

    class Launch(_Thread):
        def __init__(self, handler, opts, compat, log_path):
            self.handler, self.opts, self.compat, self.log_path = handler, opts, compat, log_path
            self.done, self.fail = _Signal(), _Signal()
            self.started = self.cancelled = False
            ns.launches.append(self)

    class Stop(_Thread):
        def __init__(self, session, window_title=None, window_handle=None):
            self.session = session
            self.done = _Signal()
            self.started = False
            ns.stops.append(self)

    class Discard(_Thread):
        def __init__(self, session, path=None):
            self.session, self.path = session, path
            self.done = _Signal()
            self.started = False
            ns.discards.append(self)

    class ViewerStop(Stop):
        def __init__(self, session, window_title=None, window_handle=None):
            super().__init__(session)
            ns.stops.remove(self)
            ns.viewer_stops.append(self)

    class RecordClose(Stop):
        """Closes the scrcpy that recorded its own screen."""

        def __init__(self, session, window_title=None, window_handle=None):
            super().__init__(session)
            ns.stops.remove(self)
            ns.record_closes.append(self)

    monkeypatch.setattr(mp_mod, "_MirrorLaunchThread", Launch)
    monkeypatch.setattr(mp_mod, "_RecorderStopThread", Stop)
    monkeypatch.setattr(mp_mod, "_RecWaitThread", RecordClose)
    monkeypatch.setattr(mp_mod, "_RecorderDiscardThread", Discard)
    monkeypatch.setattr(mp_mod, "_MirrorStopThread", ViewerStop)
    monkeypatch.setattr(
        mp_mod.QFileDialog, "getSaveFileName", lambda *_a, **_k: (ns.path, "MP4 video (*.mp4)")
    )
    monkeypatch.setattr(mp_mod, "saved_dialog", lambda *_a, **_k: None)
    monkeypatch.setattr(mp_mod.QMessageBox, "warning", lambda *_a, **_k: None)
    return ns


def _live_panel():
    from turboadb.config import ScrcpyOptions

    panel = _panel()
    live = _Session()
    panel._scrcpy = live
    panel._embed_on = True
    panel._launch_spec = {
        "display_id": None,
        "compat": False,
        "embed": True,
        "record": None,
        "record_format": None,
        "camera": None,
    }
    panel._launch_opts = ScrcpyOptions(max_size=1600, bit_rate="12M", video_codec="h265", max_fps=45)
    calls = []
    panel.start = lambda *args, **kwargs: calls.append(("start", args, kwargs))
    panel.stop = lambda: calls.append(("stop",))
    return panel, live, calls


def test_record_beside_a_live_screen_never_stops_or_restarts_it(rec):
    panel, live, calls = _live_panel()
    try:
        panel._begin_record()
        assert panel._rec_mode == "parallel"
        assert panel._recording and panel._rec_parallel_starting
        assert not panel.btn_record.isEnabled()
        assert panel.btn_record.text() == "Starting recording…"

        (launch,) = rec.launches
        opts = launch.opts
        assert opts.record == rec.path and opts.record_format == "mp4"
        assert opts.no_playback and opts.no_control
        assert "--no-window" in opts.extra_args  # scrcpy 4 otherwise shows a window
        assert opts.window_title.startswith("turboadb-record-")
        assert opts.window_title != panel._win_title
        assert (opts.max_size, opts.bit_rate, opts.video_codec, opts.max_fps) == (
            1600, "12M", "h265", 45,
        )
        assert launch.log_path and launch.log_path != panel._log_path
        assert launch.compat == panel._compat

        recorder = _Session()
        launch.done.emit(recorder)
        assert panel._rec_session is recorder
        assert panel.btn_record.isEnabled() and panel.btn_record.text() == "Stop recording"
        assert calls == [] and live.stop_calls == []
        assert panel._scrcpy is live and panel._queued_start is None
        # changing displays does not have to wait for a parallel recording
        assert not panel._integrated_recording_blocks()
    finally:
        _close(panel)


def test_stopping_a_parallel_recording_finalises_only_the_recorder(rec):
    panel, live, calls = _live_panel()
    finished = []
    panel._record_finished = finished.append
    try:
        panel._begin_record()
        recorder = _Session()
        rec.launches[0].done.emit(recorder)
        panel._toggle_record()  # the toolbar's "Stop recording"
        (stop,) = rec.stops
        assert stop.session is recorder and stop.started
        assert panel._record_finalizing
        assert panel.btn_record.text() == "Saving recording…"
        assert panel._rec_session is None
        assert calls == [] and live.stop_calls == [] and panel._scrcpy is live
        stop.done.emit(True)
        assert finished == [[rec.path]]
    finally:
        _close(panel)


def test_recorder_stop_thread_waits_generously_for_the_mp4_index(app):
    from turboadb.gui.mirror_panel import _RecorderStopThread

    session = _Session()
    worker = _RecorderStopThread(session)
    results = []
    worker.done.connect(results.append)
    worker.run()
    assert session.stop_calls == [{"timeout": _RecorderStopThread.CLOSE_TIMEOUT}]
    assert _RecorderStopThread.CLOSE_TIMEOUT >= 15
    assert results == [True]


def test_parallel_recorder_that_exits_early_falls_back_to_device_recording(rec):
    panel, live, calls = _live_panel()
    device, logs = [], []
    panel._record_device = lambda path, display_id=None: device.append((path, display_id))
    panel.log.connect(logs.append)
    try:
        panel._begin_record(display_id=2)
        recorder = _Session()
        rec.launches[0].done.emit(recorder)
        recorder.running = False  # scrcpy quit a moment after starting
        panel._poll_parallel_record()
        assert device == [(rec.path, 2)]
        (discard,) = rec.discards
        assert discard.session is recorder and discard.path == rec.path and discard.started
        assert any("[WARNING]" in line and "device-side" in line for line in logs)
        assert panel._rec_session is None and panel._recording
        assert calls == [] and live.stop_calls == []
    finally:
        _close(panel)


def test_parallel_recorder_without_data_or_launch_falls_back(rec):
    panel, _live, calls = _live_panel()
    device = []
    panel._record_device = lambda path, display_id=None: device.append(path)
    try:
        panel._begin_record()
        recorder = _Session()
        rec.launches[0].done.emit(recorder)
        panel._poll_parallel_record()  # still starting: keep waiting
        assert device == [] and rec.discards == []
        panel._rec_started_t = time.time() - 60  # no MP4 data for a minute
        panel._poll_parallel_record()
        assert device == [rec.path]
        assert [d.session for d in rec.discards] == [recorder]
    finally:
        _close(panel)

    panel, _live, calls = _live_panel()
    device = []
    panel._record_device = lambda path, display_id=None: device.append(path)
    try:
        panel._begin_record()
        rec.launches[-1].fail.emit("scrcpy not found")
        assert device == [rec.path]
        assert calls == []
    finally:
        _close(panel)


def test_confirmed_parallel_recorder_is_kept_and_saved_when_it_ends(rec):
    panel, _live, calls = _live_panel()
    device, finished = [], []
    panel._record_device = lambda path, display_id=None: device.append(path)
    panel._record_finished = finished.append
    try:
        panel._begin_record()
        recorder = _Session()
        rec.launches[0].done.emit(recorder)
        with open(rec.path, "wb") as fh:
            fh.write(b"\x00\x00\x00\x20ftypisom")
        panel._poll_parallel_record()
        assert panel._rec_confirmed and device == [] and rec.discards == []
        # much later the device disconnects; scrcpy closed the file itself
        panel._rec_started_t = time.time() - 120
        recorder.running = False
        panel._poll_parallel_record()
        assert device == [] and finished == [[rec.path]]
    finally:
        _close(panel)


_OLD_RECORDING = b"\x00\x00\x00\x20ftypisom" + b"yesterday's drive " * 2000


def _recording_from_yesterday(path):
    """The file the user chose to overwrite: a recording saved a day ago."""
    with open(path, "wb") as fh:
        fh.write(_OLD_RECORDING)
    past = time.time() - 86400
    os.utime(path, (past, past))


def test_a_file_being_overwritten_does_not_confirm_the_recording(rec):
    """Recording over an existing file: its old size was read as the first
    video data, so the recording counted as started before scrcpy had written
    anything, and a recorder that never did was never replaced."""
    _recording_from_yesterday(rec.path)
    panel, _live, calls = _live_panel()
    device, logs = [], []
    panel._record_device = lambda path, display_id=None: device.append(path)
    panel.log.connect(logs.append)
    try:
        panel._begin_record()
        recorder = _Session()
        rec.launches[0].done.emit(recorder)
        panel._poll_parallel_record()
        assert not panel._rec_confirmed  # scrcpy has not opened the file yet
        assert not any("recording started" in line for line in logs)
        panel._rec_started_t = time.time() - 60  # and still has not a minute later
        panel._poll_parallel_record()
        assert device == [rec.path]  # the device records instead
        assert [d.session for d in rec.discards] == [recorder]
        assert calls == []
    finally:
        _close(panel)


def test_a_recorder_that_wrote_nothing_is_not_saved_as_the_file_it_would_replace(rec):
    _recording_from_yesterday(rec.path)
    panel, _live, _calls = _live_panel()
    device, finished = [], []
    panel._record_device = lambda path, display_id=None: device.append(path)
    panel._record_finished = finished.append
    try:
        panel._begin_record()
        recorder = _Session()
        rec.launches[0].done.emit(recorder)
        panel._poll_parallel_record()
        panel._rec_started_t = time.time() - 30  # well past the start-up grace
        recorder.running = False  # it ended without ever opening the file
        panel._poll_parallel_record()
        assert finished == []  # yesterday's recording is not today's
        assert device == [rec.path]
    finally:
        _close(panel)


def test_an_overwritten_file_confirms_the_recording_once_scrcpy_writes_it(rec):
    _recording_from_yesterday(rec.path)
    panel, _live, _calls = _live_panel()
    device, logs = [], []
    panel._record_device = lambda path, display_id=None: device.append(path)
    panel.log.connect(logs.append)
    try:
        panel._begin_record()
        recorder = _Session()
        rec.launches[0].done.emit(recorder)
        with open(rec.path, "wb"):
            pass  # scrcpy opened it: empty until the first video data
        panel._poll_parallel_record()
        assert not panel._rec_confirmed
        with open(rec.path, "wb") as fh:
            fh.write(b"\x00\x00\x00\x20ftypisom")
        panel._poll_parallel_record()
        assert panel._rec_confirmed and device == []
        assert any("recording started" in line for line in logs)
    finally:
        _close(panel)


def test_stopping_before_the_recorder_wrote_never_reports_the_old_file_as_saved(
    rec, monkeypatch
):
    import turboadb.gui.mirror_panel as mp_mod

    _recording_from_yesterday(rec.path)
    saved, warned, logs = [], [], []
    monkeypatch.setattr(mp_mod, "saved_dialog", lambda *args, **_k: saved.append(args[1]))
    monkeypatch.setattr(
        mp_mod.QMessageBox, "warning", lambda *args, **_k: warned.append(args[2])
    )
    panel, _live, _calls = _live_panel()
    panel.log.connect(logs.append)
    try:
        panel._begin_record()
        recorder = _Session()
        rec.launches[0].done.emit(recorder)
        panel._toggle_record()  # Stop recording, before scrcpy had opened the file
        rec.stops[0].done.emit(True)
        assert saved == [] and not any(line.startswith("[OK]") for line in logs)
        assert any(line.startswith("[ERROR]") and "no data" in line for line in logs)
        assert not any("MP4 index" in line for line in logs)  # (that is the old file's)
        assert len(warned) == 1 and "has not been changed" in warned[0]
        with open(rec.path, "rb") as fh:
            assert fh.read() == _OLD_RECORDING
        assert not panel._recording and not panel._record_finalizing
    finally:
        _close(panel)


def test_a_recording_saved_over_an_old_file_is_reported_as_saved(rec, monkeypatch):
    import turboadb.gui.mirror_panel as mp_mod

    _recording_from_yesterday(rec.path)
    saved, logs = [], []
    monkeypatch.setattr(mp_mod, "saved_dialog", lambda *args, **_k: saved.append(args[1]))
    panel, _live, _calls = _live_panel()
    panel.log.connect(logs.append)
    try:
        panel._begin_record()
        recorder = _Session()
        rec.launches[0].done.emit(recorder)
        with open(rec.path, "wb") as fh:
            fh.write(b"\x00\x00\x00\x20ftypisom")
        panel._toggle_record()
        rec.stops[0].done.emit(True)
        assert saved == [rec.path]
        assert any(line == f"[OK] recording saved → {rec.path}" for line in logs)
    finally:
        _close(panel)


def test_closing_the_tab_mid_parallel_recording_finalises_the_file(rec):
    panel, live, _calls = _live_panel()
    del panel.stop  # the real viewer stop runs on close
    try:
        panel._begin_record()
        recorder = _Session()
        rec.launches[0].done.emit(recorder)
        panel.close_panel()
        assert [s.session for s in rec.stops] == [recorder]
        assert rec.stops[0].started
        assert panel._record_finalizing
    finally:
        panel._recording = False
        panel._scrcpy = None
        panel.close()


# --------------------------------------------------------------------------- #
# Record pressed while scrcpy is still starting, or the old screen closing
# --------------------------------------------------------------------------- #
def _spin(app):
    for _ in range(3):  # a queued start runs from the event loop
        app.processEvents()


def test_another_screen_request_never_replaces_a_recording_start_that_waits(rec, app):
    """Record while scrcpy was still launching, then Device camera: the camera
    start took the place of the start that carried the recording, and was then
    refused because a recording was "running". Record stayed on "Starting
    recording…" and the screen could not be started until the tab was closed."""
    panel = _panel()
    try:
        panel.start()  # scrcpy is launching
        (launch,) = rec.launches
        panel._begin_record()
        assert panel._rec_integrated_starting and panel._queued_start["record"] == rec.path
        panel._mirror_camera()  # Options → Device camera
        assert panel._queued_start["record"] == rec.path  # still the recording's start
        assert "Stop the current recording" in panel.status.text()
        launch.done.emit(_Session())  # the launch Record cancelled reports
        (closing,) = rec.viewer_stops
        closing.done.emit(True)
        _spin(app)
        assert len(rec.launches) == 2 and rec.launches[1].opts.record == rec.path
        rec.launches[1].done.emit(_Session())
        assert panel._recording and not panel._rec_integrated_starting
        assert panel.btn_record.isEnabled() and panel.btn_record.text() == "Stop recording"
    finally:
        _close(panel)


def test_a_recording_start_behind_a_closing_screen_survives_another_start(rec, app):
    panel = _panel()
    try:
        panel.start()
        rec.launches[0].done.emit(_Session())
        panel.stop()  # the screen is closing
        (closing,) = rec.viewer_stops
        assert panel._stopping_mirror
        panel._begin_record()
        panel.start(embed=False)  # the tab menu's "Open screen in a separate window"
        assert panel._queued_start["record"] == rec.path
        closing.done.emit(True)
        _spin(app)
        assert rec.launches[-1].opts.record == rec.path
    finally:
        _close(panel)


def test_a_retry_never_replaces_a_recording_start_that_waits(rec, app):
    panel = _panel()
    try:
        panel.start()
        rec.launches[0].done.emit(_Session())
        panel._scrcpy_window_up = lambda: False
        panel._start_t = time.time() - 60  # no window in time: closed, to be retried
        panel._check_alive()
        assert panel._stopping_mirror and panel._retry_pending
        panel._begin_record()  # Record takes the retry's place
        assert not panel._retry_pending
        panel._retry_start()  # (a retry that fired all the same)
        assert panel._queued_start["record"] == rec.path
        rec.viewer_stops[0].done.emit(True)
        _spin(app)
        assert len(rec.launches) == 2 and rec.launches[1].opts.record == rec.path
    finally:
        _close(panel)


def test_stop_cancels_a_recording_start_and_leaves_the_panel_usable(rec, app):
    """Separate window, Record, Stop, Device camera, and then the launch
    fails: the camera start was taken for the start with the recording."""
    panel = _panel()
    try:
        panel._popout_mirror()  # scrcpy is launching
        panel._begin_record()
        panel.stop()  # neither is wanted any more
        assert not panel._recording and not panel._rec_integrated_starting
        assert panel.btn_record.isEnabled() and panel.btn_record.text() == "Record…"
        panel._mirror_camera()  # asked for while the cancelled launch still runs
        rec.launches[0].fail.emit("adb: device offline")
        _spin(app)
        assert not panel._recording and panel.btn_record.isEnabled()
        assert len(rec.launches) == 2
        assert rec.launches[1].opts.video_source == "camera"  # what was asked for last
        assert rec.launches[1].opts.record is None
    finally:
        _close(panel)


def test_a_cancelled_launch_with_another_start_waiting_still_ends_the_recording_start(rec):
    """Whatever start waits, only one that carries the recording keeps it."""
    panel = _panel()
    try:
        panel.start()
        panel._begin_record()
        panel._queued_start = {"embed": False}  # a start without the recording
        panel._clear_cancelled_record_start()
        assert not panel._recording and not panel._rec_integrated_starting
        assert panel._rec_mode is None
    finally:
        _close(panel)


# --------------------------------------------------------------------------- #
# a display picked while a screen start is under way
# --------------------------------------------------------------------------- #
_TWO_DISPLAYS = [{"id": 0, "size": "1080x2400"}, {"id": 2, "size": "1920x720"}]


def _pick(panel, display_id):
    """Choose a display in the picker, as a click on its list does."""
    index = panel.cmb_display.findData(display_id)
    panel.cmb_display.setCurrentIndex(index)
    panel._on_display_combo_changed(index)


def test_a_display_picked_while_scrcpy_launches_is_the_one_that_starts(rec, app):
    """The picker moved, but the launch under way went on for the display
    chosen before: scrcpy came up on one display while the picker showed
    another."""
    panel = _panel()
    try:
        panel._got_displays(_TWO_DISPLAYS)
        panel.start()  # the default display; scrcpy is still launching
        assert rec.launches[0].opts.display_id is None
        _pick(panel, 2)
        assert (panel._queued_start or {}).get("display_id") == 2
        rec.launches[0].done.emit(_Session())  # the launch for display 0 reports
        (closing,) = rec.viewer_stops  # ...and is closed again
        closing.done.emit(True)
        _spin(app)
        assert len(rec.launches) == 2 and rec.launches[1].opts.display_id == 2
        assert panel.cmb_display.currentData() == 2
    finally:
        _close(panel)


def test_a_display_picked_while_the_old_screen_closes_is_the_one_that_starts(rec, app):
    panel = _panel()
    try:
        panel._got_displays(_TWO_DISPLAYS)
        panel.start()
        rec.launches[0].done.emit(_Session())  # display 0 is on screen
        _pick(panel, 2)  # closes it, display 2 waits for that
        assert panel._stopping_mirror and panel._queued_start["display_id"] == 2
        _pick(panel, None)  # back to display 0 meanwhile
        assert panel._queued_start["display_id"] is None
        rec.viewer_stops[0].done.emit(True)
        _spin(app)
        assert len(rec.launches) == 2 and rec.launches[1].opts.display_id is None
    finally:
        _close(panel)


def test_a_display_picked_while_a_retry_waits_is_the_one_that_starts(rec, app):
    panel = _panel()
    try:
        panel._got_displays(_TWO_DISPLAYS)
        panel.start()
        rec.launches[0].fail.emit("adb: device offline")  # retried in a moment
        assert panel._retry_pending
        _pick(panel, 2)
        assert not panel._retry_pending
        assert len(rec.launches) == 2 and rec.launches[1].opts.display_id == 2
    finally:
        _close(panel)


def test_a_display_picked_after_stop_is_only_selected(rec, app):
    """Stop means stopped: a display picked while the stopped screen is still
    closing (or its launch still being cancelled) starts nothing by itself."""
    panel = _panel()
    try:
        panel._got_displays(_TWO_DISPLAYS)
        panel.start()
        panel.stop()  # while scrcpy was still launching
        _pick(panel, 2)
        rec.launches[0].done.emit(_Session())
        rec.viewer_stops[0].done.emit(True)
        _spin(app)
        assert len(rec.launches) == 1 and not panel.is_active()
        assert panel.cmb_display.currentData() == 2

        panel.start()
        rec.launches[1].done.emit(_Session())
        panel.stop()  # a running screen, which is now closing
        _pick(panel, None)
        rec.viewer_stops[1].done.emit(True)
        _spin(app)
        assert len(rec.launches) == 2 and not panel.is_active()
        assert panel.cmb_display.currentData() is None
    finally:
        _close(panel)


def test_recording_another_display_never_moves_the_picker_off_a_starting_screen(
    rec, app, monkeypatch
):
    import turboadb.gui.mirror_panel as mp_mod

    panel = _panel()
    device = []
    panel._record_device = lambda path, display_id=None: device.append(display_id)
    try:
        panel._got_displays(_TWO_DISPLAYS)
        panel.start()  # display 0 is starting
        chosen = [("", "")]  # the file dialog, cancelled
        monkeypatch.setattr(
            mp_mod.QFileDialog, "getSaveFileName", lambda *_a, **_k: chosen[0]
        )
        panel._select_and_record(2)  # the display manager's Record for display 2
        assert panel.cmb_display.currentData() is None  # display 0 is what starts
        assert not panel._recording

        # recorded after all: the one scrcpy shows what it records
        chosen[0] = (rec.path, "MP4 video (*.mp4)")
        panel._select_and_record(2)
        assert panel._queued_start["display_id"] == 2 and panel._queued_start["record"]
        assert panel.cmb_display.currentData() == 2
    finally:
        _close(panel)


def test_a_pick_refused_for_a_recording_returns_to_the_display_being_recorded(rec, app):
    panel = _panel()
    try:
        panel._got_displays(_TWO_DISPLAYS)
        panel.start()  # display 0 is starting
        panel._select_and_record(2)  # scrcpy will show, and record, display 2 instead
        assert panel.cmb_display.currentData() == 2
        _pick(panel, None)  # refused: the recording would end
        assert "Stop the current recording" in panel.status.text()
        assert panel.cmb_display.currentData() == 2
        assert panel._queued_start["record"] == rec.path
    finally:
        _close(panel)


def test_a_display_picked_while_a_recording_is_saved_stays_with_the_screen(rec, app):
    """The screen is not restarted until a device recording is on this PC, so
    the picker must not move off the display that is on screen."""
    panel = _panel()
    try:
        panel._got_displays(_TWO_DISPLAYS)
        panel.start()
        rec.launches[0].done.emit(_Session())  # display 0 is on screen
        panel._recording, panel._rec_mode = True, "device"
        panel._stop_record()  # its recording is being pulled
        _pick(panel, 2)
        assert panel.cmb_display.currentData() is None
        assert "finish saving" in panel.status.text()
        panel._select_and_mirror(2)  # the display manager's Start
        assert panel.cmb_display.currentData() is None
        assert rec.viewer_stops == [] and len(rec.launches) == 1  # still on screen
    finally:
        panel._record_finalizing = False
        _close(panel)


def test_the_view_that_returns_after_a_recording_is_the_pickers_display(rec, app):
    """Stop recording closes the scrcpy that recorded and brings the view
    back on the recorded display: a pick made while it is saved has to wait."""
    panel = _panel()
    try:
        panel._got_displays(_TWO_DISPLAYS)
        _pick(panel, 2)
        panel.start()
        panel._begin_record()  # while scrcpy launches: it will record itself
        rec.launches[0].done.emit(_Session())
        rec.viewer_stops[0].done.emit(True)
        _spin(app)
        rec.launches[1].done.emit(_Session())  # display 2, recording
        panel._toggle_record()  # Stop recording: the MP4 is being finished
        assert panel._record_finalizing
        _pick(panel, None)
        assert panel.cmb_display.currentData() == 2
        assert "finish saving" in panel.status.text()
        with open(rec.path, "wb") as fh:
            fh.write(b"\x00\x00\x00\x20ftypisom")
        rec.record_closes[0].done.emit(True)
        _spin(app)
        assert rec.launches[-1].opts.display_id == 2 and rec.launches[-1].opts.record is None
        assert panel.cmb_display.currentData() == 2
    finally:
        _close(panel)


def test_recording_another_display_while_a_retry_waits_keeps_the_pickers_display(rec, app):
    panel = _panel()
    device = []
    panel._record_device = lambda path, display_id=None: device.append(display_id)
    try:
        panel._got_displays(_TWO_DISPLAYS)
        panel.start()
        rec.launches[0].fail.emit("adb: device offline")  # display 0 is retried
        panel._select_and_record(2)
        assert device == [2]  # recorded on the device, beside the screen to come
        assert panel.cmb_display.currentData() is None
    finally:
        _close(panel)


@pytest.fixture
def frames(monkeypatch):
    """The screencap renderer without its capture worker: the list holds the
    display of every view that was started."""
    from turboadb.gui import screencap_view as sv

    started = []
    monkeypatch.setattr(sv.ScreencapView, "start", lambda view: started.append(view.display_id))
    return started


def test_a_renderer_change_keeps_the_display_picked_while_scrcpy_launches(rec, app, frames):
    """The renderer was changed (Options, or Settings) while a display picked
    during the launch waited for it: the restart for the new renderer took the
    display of the launch under way, and the picker showed another."""
    panel = _panel()
    try:
        panel._got_displays(_TWO_DISPLAYS)
        panel.start()  # the default display; scrcpy is still launching
        _pick(panel, 2)  # waits for that launch to be closed
        panel.set_screen_backend("screencap")
        assert (panel._queued_start or {}).get("display_id") == 2
        rec.launches[0].done.emit(_Session())
        rec.viewer_stops[0].done.emit(True)
        _spin(app)
        assert frames == [2] and len(rec.launches) == 1  # screencap frames of display 2
        assert panel.cmb_display.currentData() == 2
    finally:
        _close(panel)


def test_a_renderer_change_keeps_a_separate_window_that_waits(rec, app, frames):
    panel = _panel()
    try:
        panel.start()
        panel._popout_mirror()  # asked for while scrcpy was still launching
        panel.set_screen_backend("screencap")
        rec.launches[0].done.emit(_Session())
        rec.viewer_stops[0].done.emit(True)
        _spin(app)
        assert frames == []  # a separate window is scrcpy's
        assert len(rec.launches) == 2 and panel._launch_spec["embed"] is False
    finally:
        _close(panel)


def test_a_renderer_change_brings_no_screen_back_after_stop(rec, app, frames):
    """Stop while scrcpy was still launching, then another renderer: the
    cancelled launch counted as a running screen and was started again."""
    panel = _panel()
    try:
        panel.start()
        panel.stop()
        panel.set_screen_backend("screencap")
        rec.launches[0].done.emit(_Session())  # the cancelled launch reports
        rec.viewer_stops[0].done.emit(True)
        _spin(app)
        assert frames == [] and len(rec.launches) == 1
        assert not panel.is_active()
    finally:
        _close(panel)


def test_a_renderer_change_moves_a_launch_under_way_to_the_new_renderer(rec, app, frames):
    panel = _panel()
    try:
        panel._got_displays(_TWO_DISPLAYS)
        _pick(panel, 2)
        panel.start()  # scrcpy is launching for display 2
        panel.set_screen_backend("screencap")
        rec.launches[0].done.emit(_Session())
        rec.viewer_stops[0].done.emit(True)
        _spin(app)
        assert frames == [2] and len(rec.launches) == 1
    finally:
        _close(panel)


def test_mp4_finalised_check_reads_the_box_headers(tmp_path):
    from turboadb.gui.mirror_panel import _mp4_is_finalised

    def box(kind, payload=b""):
        return (8 + len(payload)).to_bytes(4, "big") + kind + payload

    killed = tmp_path / "killed.mp4"
    killed.write_bytes(box(b"ftyp", b"isom") + box(b"free") + b"\x00\x00\x00\x00mdat" + b"x" * 64)
    good = tmp_path / "good.mp4"
    good.write_bytes(box(b"ftyp", b"isom") + box(b"mdat", b"x" * 64) + box(b"moov", b"y" * 16))
    assert not _mp4_is_finalised(str(killed))
    assert _mp4_is_finalised(str(good))
    assert not _mp4_is_finalised(str(tmp_path / "missing.mp4"))


def test_scrcpy_session_stop_passes_its_timeout(monkeypatch):
    import turboadb.scrcpy as sc

    waits, breaks = [], []

    class Proc:
        pid = 99
        code = None

        def poll(self):
            return self.code

        def wait(self, timeout=None):
            waits.append(timeout)
            self.code = 0
            return 0

        def terminate(self):
            self.code = 1

        def kill(self):
            self.code = 1

    real_send_ctrl_break = sc._send_ctrl_break
    monkeypatch.setattr(sc, "_post_close_to_pid", lambda _pid: False)
    monkeypatch.setattr(sc, "_send_ctrl_break", lambda pid: breaks.append(pid) or True)
    sc.ScrcpySession(Proc(), "serial").stop(timeout=17.0)
    assert waits[0] == 17.0
    if os.name == "nt":
        assert breaks == [99]  # a windowless recorder has no window to close
    assert real_send_ctrl_break(0) is False  # no process: nothing is signalled


# --------------------------------------------------------------------------- #
# the idle "Screen is off" frame
# --------------------------------------------------------------------------- #
def test_display_size_parsing():
    from turboadb.gui.mirror_panel import _parse_display_size

    assert _parse_display_size("1920x720") == (1920, 720)
    assert _parse_display_size("1080 × 2400") == (1080, 2400)
    assert _parse_display_size((800, 480)) == (800, 480)
    assert _parse_display_size((0, 480)) is None
    assert _parse_display_size("primary") is None
    assert _parse_display_size(None) is None


def test_idle_frame_is_a_phone_only_for_portrait_non_automotive_screens(app):
    panel = _panel()
    try:
        idle = panel.empty_state
        idle.resize(1200, 900)
        idle._place()
        assert not idle.landscape and idle.glyph.icon_name == "smartphone"
        assert idle.aspect == pytest.approx(9 / 19.5)
        frame = idle.frame_rect()
        assert frame.height() > frame.width()

        panel._got_displays(
            [
                {"id": 0, "size": "1080x2400"},
                {"id": 1, "size": "1920x720"},
                {"id": 2, "size": "1000x1000"},
                {"id": 3, "size": "7680x1080"},
                {"id": 4, "size": ""},
            ]
        )
        # the default display is display 0: a portrait phone at its real ratio
        assert not idle.landscape and idle.aspect == pytest.approx(1080 / 2400)

        panel.cmb_display.setCurrentIndex(panel.cmb_display.findData(1))
        assert idle.landscape and idle.glyph.icon_name == "monitor"
        assert idle.aspect == pytest.approx(1920 / 720)
        frame = idle.frame_rect()
        assert frame.width() > frame.height()
        assert "the device" in idle.hint.text()

        panel._sync_display_selection(2)  # a square display
        assert idle.landscape and idle.aspect == pytest.approx(1.0)
        panel._sync_display_selection(3)  # ultra-wide: clamped to 32:9
        assert idle.aspect == pytest.approx(32 / 9)
        panel._sync_display_selection(4)  # size unknown: back to a phone
        assert not idle.landscape and idle.glyph.icon_name == "smartphone"
    finally:
        _close(panel)


def test_idle_frame_for_automotive_is_a_head_unit_display(app):
    panel = _panel(automotive=True)
    try:
        idle = panel.empty_state
        assert idle.landscape and idle.automotive
        assert idle.glyph.icon_name == "car"
        assert idle.aspect == pytest.approx(16 / 9)  # unknown size
        assert "the head unit display" in idle.hint.text()
        panel.act_embed.setChecked(False)
        assert "head unit display" in idle.hint.text() and "own scrcpy window" in idle.hint.text()
        panel.act_embed.setChecked(True)

        panel._got_displays([{"id": 0, "size": "1920x720"}])
        assert idle.glyph.icon_name == "car"
        assert idle.aspect == pytest.approx(1920 / 720)
        panel._got_displays([{"id": 0, "size": "1080x1920"}])  # a portrait head unit
        assert idle.landscape and idle.aspect == pytest.approx(1.0)

        panel.set_automotive_default(False)
        assert not idle.landscape and idle.glyph.icon_name == "smartphone"
        assert "the device" in idle.hint.text()
    finally:
        _close(panel)


def test_idle_frame_uses_a_known_default_size_and_keeps_the_old_api(app):
    from turboadb.gui.mirror_panel import _IdleScreen

    panel = _panel()
    try:
        idle = panel.empty_state
        panel.set_default_display_size("2560x1600")
        assert idle.landscape and idle.glyph.icon_name == "monitor"
        assert idle.aspect == pytest.approx(1.6)
        panel.set_default_display_size(None)
        assert not idle.landscape
        idle.set_landscape(True)
        assert idle.landscape
    finally:
        _close(panel)
    standalone = _IdleScreen(landscape=True)
    try:
        assert standalone.landscape and standalone.aspect == pytest.approx(16 / 9)
    finally:
        standalone.close()


# --------------------------------------------------------------------------- #
# the screen toolbar: frame rate beside the picture, a status line that clears
# --------------------------------------------------------------------------- #
def _solid(width, height, colour="#3366aa"):
    from PyQt5.QtGui import QColor, QImage

    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor(colour))
    return image


@pytest.fixture
def screencap_panel(app, monkeypatch):
    """A shown panel whose screencap renderer shows a portrait frame at once
    (no adb, no worker thread)."""
    from PyQt5.QtCore import Qt
    import turboadb.gui.mirror_panel as mp_mod
    from turboadb.gui import screencap_view as sv

    def fake_start(view):
        view._image = _solid(108, 240)
        view._frame_size = (1080, 2400)
        view._fps = 4.5
        view._message = ""
        view.frame_shape_changed.emit()
        view.first_frame.emit()
        view.stats_changed.emit()

    monkeypatch.setattr(sv.ScreencapView, "start", fake_start)
    monkeypatch.setattr(mp_mod, "saved_dialog", lambda *_a, **_k: None)
    monkeypatch.setattr(mp_mod.QMessageBox, "warning", lambda *_a, **_k: None)
    panel = _panel()
    panel.setAttribute(Qt.WA_DontShowOnScreen, True)
    panel.set_screen_backend("screencap")
    panel.resize(420, 700)
    panel.show()
    panel.start()
    for _ in range(5):
        app.processEvents()
    yield panel
    panel.hide()
    _close(panel)


def test_the_frame_rate_sits_in_the_toolbar_outside_the_picture(app, screencap_panel):
    from PyQt5.QtCore import QRect
    from turboadb.gui import screencap_view as sv

    panel = screencap_panel
    view = panel.screencap_view()
    chip = panel.lbl_renderer
    assert not chip.isHidden() and chip.text() == "screencap · 4.5 fps"
    left, top, width, height = (int(v) for v in sv.fit_rect(108, 240, view.width(), view.height()))
    picture = QRect(view.mapTo(panel, QRect(left, top, 1, 1).topLeft()), QRect(0, 0, width, height).size())
    badge = QRect(chip.mapTo(panel, chip.rect().topLeft()), chip.size())
    assert not badge.intersects(picture)
    assert chip.parentWidget() is panel._toolbar_widget  # in the toolbar row itself
    panel.stop()
    assert chip.isHidden()  # no renderer running, no frame rate


def test_a_saved_screencap_recording_clears_its_status_line(app, screencap_panel, tmp_path):
    """Issue: "Recording stopped. Saving the MP4 to your PC…" stayed under the
    live screen after the recording had been saved."""
    panel = screencap_panel
    assert panel.screencap_view() is not None and panel.status.isHidden()
    path = tmp_path / "screen.mp4"
    path.write_bytes(b"video data")
    panel._rec_path = str(path)
    panel._recording, panel._rec_mode = True, "device"
    panel._stop_record()
    assert panel.status.text() == "Recording stopped. Saving the MP4 to your PC…"
    panel._record_finished([str(path)])
    assert panel.status.isHidden() and panel.status.text() == ""
    assert not panel._recording and not panel._record_finalizing

    # a failed save clears it too, but never another message that replaced it
    panel._recording, panel._rec_mode = True, "device"
    panel._stop_record()
    panel.status.setText("Screen capture ended. Retry")
    panel.status.show()
    panel._record_failed("pull failed")
    assert panel.status.text() == "Screen capture ended. Retry"


def test_a_saved_scrcpy_recording_clears_its_status_line(rec):
    panel, _live, _calls = _live_panel()
    try:
        panel._begin_record()
        recorder = _Session()
        rec.launches[0].done.emit(recorder)
        assert panel.status.text().startswith("● Recording")
        panel.status.show()  # e.g. shown while the screen was still starting
        panel._toggle_record()
        assert panel.status.text() == "Recording stopped. Saving the MP4 to your PC…"
        with open(rec.path, "wb") as fh:
            fh.write(b"\x00\x00\x00\x20ftypisom")
        rec.stops[0].done.emit(True)
        assert panel.status.isHidden() and panel.status.text() == ""
        assert not panel._record_finalizing
    finally:
        _close(panel)


def test_text_typed_for_the_device_never_reaches_the_log(qapp, monkeypatch):
    """"Type into the device" may carry a password: the log, status bar and
    toasts only ever see how many characters were sent."""
    from turboadb.gui import mirror_panel as mp_mod

    sent = []

    class Handler:
        serial = "dev"
        config = None

        def input_text(self, text, **kwargs):
            sent.append(text)
            return True

    class Now:
        def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
            result = fn()
            if on_done is not None:
                on_done(result)
            return True

        def stop(self):
            pass

    monkeypatch.setattr(mp_mod.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("hunter2 secret", True)))
    panel = mp_mod.MirrorPanel(Handler(), {"name": "dev"}, dispatcher=Now())
    lines = []
    panel.log.connect(lines.append)
    try:
        panel._type_text()
        assert sent == ["hunter2 secret"]
        assert lines and not any("hunter2" in line or "secret" in line for line in lines)
        assert lines[0] == "[INFO] Sending 14 characters to the device…"
    finally:
        panel.close_panel()
        panel.deleteLater()
        qapp.processEvents()


# --------------------------------------------------------------------------- #
# a paste goes to the DEVICE, so this window has to say so
# --------------------------------------------------------------------------- #
def test_pasting_into_the_device_screen_reports_itself(qapp):
    """Ctrl+V on the mirror sends the clipboard to the device. The text lands
    there, not here, so with no message a paste that worked and one that did
    nothing looked identical."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtWidgets import QApplication

    from turboadb.gui.mirror_panel import _forward_device_key, device_paste_message

    sent, told = [], []
    event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_V, Qt.ControlModifier, "v")

    QApplication.clipboard().setText("hello device")
    handled = _forward_device_key(
        event, lambda kind, payload, **k: sent.append((kind, payload)), notify=told.append
    )
    assert handled
    assert sent == [("text", "hello device")]
    assert told == ["hello device"]
    assert device_paste_message(told[0]) == "[OK] Pasted 12 characters to the device"

    # an empty clipboard must say so rather than look like a silent success
    sent.clear()
    told.clear()
    QApplication.clipboard().setText("")
    assert _forward_device_key(
        event, lambda kind, payload, **k: sent.append((kind, payload)), notify=told.append
    )
    assert sent == []          # nothing was sent to the device
    assert told == [""]        # but the caller was still told
    assert device_paste_message("") == "[WARNING] Nothing on the clipboard to paste"

    assert device_paste_message("x") == "[OK] Pasted 1 character to the device"


def test_the_embed_container_routes_its_paste_to_the_panel_log(qapp):
    """_EmbedContainer has no signals of its own; MirrorPanel hands it the
    callback so the paste reaches the normal log/toast path."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtWidgets import QApplication

    from turboadb.gui.mirror_panel import _EmbedContainer, device_paste_message

    logged = []
    container = _EmbedContainer(lambda *a, **k: None, on_paste=logged.append)
    try:
        QApplication.clipboard().setText("abc")
        container.keyPressEvent(
            QKeyEvent(QKeyEvent.KeyPress, Qt.Key_V, Qt.ControlModifier, "v")
        )
        assert logged == ["abc"]
        assert device_paste_message(logged[0]) == "[OK] Pasted 3 characters to the device"
    finally:
        container.deleteLater()
