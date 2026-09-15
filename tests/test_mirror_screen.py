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


_APP = []  # a QApplication whose only Python reference dies is destroyed


@pytest.fixture(scope="session")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    if not _APP:
        _APP.append(QApplication.instance() or QApplication(["test-mirror-screen"]))
    return _APP[0]


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

    ns = types.SimpleNamespace(launches=[], stops=[], discards=[], viewer_stops=[])
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

    monkeypatch.setattr(mp_mod, "_MirrorLaunchThread", Launch)
    monkeypatch.setattr(mp_mod, "_RecorderStopThread", Stop)
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
