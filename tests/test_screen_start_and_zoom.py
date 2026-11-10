"""A screen that is slow or fails is the user's to decide about; zoom shows detail.

scrcpy that has reached the device is not hung because its first picture is
late, so it is no longer killed for that.  Nothing moves to ADB screencap or a
separate window unless the user picks it.  Zoom makes the picture larger than
its place, which clips it, so the renderer really draws the extra pixels.

No device, adb or scrcpy is used.
"""
import time
import types

import pytest

pytest.importorskip("PyQt5")

from PyQt5 import sip  # noqa: E402
from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QColor, QImage, QMouseEvent, QWheelEvent  # noqa: E402

from turboadb.gui import mirror_panel as mp  # noqa: E402


def _pump(qapp, ms=40):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


@pytest.fixture
def panel(qapp, monkeypatch):
    monkeypatch.setattr(mp, "_IS_WIN", True)
    # (Qt's offscreen platform crashes in QMessageBox.open; a real desktop does not)
    monkeypatch.setattr(mp.QMessageBox, "open", lambda self: None)
    made = mp.MirrorPanel(types.SimpleNamespace(serial="s", config=None), {"name": "s"})
    made.resize(800, 600)
    yield made
    if sip.isdeleted(made):
        return
    made._closing = True
    made._host_sync_timer.stop()
    made._place_timer.stop()
    made._dismiss_next_step()
    made.setParent(None)
    made.deleteLater()
    _pump(qapp, 20)


class _Session:
    """A scrcpy that keeps running; ``log`` is what it has printed."""

    log_path = None
    tunnel_host = None

    def __init__(self, log=""):
        self.log = log
        self.running = True

    def read_log(self):
        return self.log


def _waiting(panel, log, ran=30.0):
    panel._scrcpy = _Session(log)
    panel._win_title = "t"
    panel._embed_on = True
    panel._became_ready = False
    panel._video_reported = True
    panel._start_t = time.time() - ran
    panel._launch_spec = {"display_id": 0, "compat": False, "embed": None}
    panel._scrcpy_window_up = lambda: False
    return panel._scrcpy


# ---- a late picture is not a hung scrcpy --------------------------------------
CONNECTED = "[server] INFO: Device: [vivo] V2318 (Android 16)\nINFO: Renderer: direct3d11\n"


def test_connected_and_waiting_is_not_killed(panel, qapp):
    session = _waiting(panel, CONNECTED)
    panel._check_alive()
    assert panel._scrcpy is session and session.running
    _pump(qapp)
    assert panel._next_step_box is not None
    assert "not sent a picture" in panel._next_step_box.text()


def test_it_is_asked_once_per_start(panel, qapp):
    _waiting(panel, CONNECTED)
    asked = []
    panel._offer_next_step = lambda *a, **k: asked.append(1)
    panel._check_alive()
    panel._check_alive()
    assert asked == [1]


def test_the_question_goes_when_the_picture_comes(panel, qapp):
    _waiting(panel, CONNECTED)
    panel._check_alive()
    _pump(qapp)
    assert panel._next_step_box is not None
    panel._scrcpy_window_up = lambda: True
    panel._check_alive()
    assert panel._next_step_box is None and panel._became_ready


def test_a_renderer_line_alone_is_no_picture(panel):
    """scrcpy creates its renderer before the first frame."""
    _waiting(panel, CONNECTED, ran=1.0)
    panel._check_alive()
    assert not panel._became_ready


def test_not_connected_after_the_timeout_is_still_restarted(panel, monkeypatch):
    """The first-launch server-push race leaves scrcpy waiting for ever."""
    _waiting(panel, "scrcpy 5.0.1\n")
    panel._stop_scrcpy_async = lambda *a, **k: None
    retried = []
    panel._retry_or_show_scrcpy_failure = lambda err: retried.append(err)
    panel._check_alive()
    assert panel._scrcpy is None and len(retried) == 1


# ---- the user chooses ---------------------------------------------------------
def test_a_failed_start_changes_nothing_by_itself(panel, qapp):
    panel._launch_spec = {"display_id": 0}
    panel._give_up_scrcpy_start("ERROR: boom\nno encoder", 3)
    _pump(qapp)
    assert panel.screen_backend() == "scrcpy" and panel._screencap is None
    labels = {b.text() for b in panel._next_step_box.buttons()}
    assert labels == {"Try again", "Switch to ADB screencap", "Open in a separate window",
                      "Show log", "Close"}
    assert "no encoder" in panel._next_step_box.informativeText()


def _press(panel, qapp, label):
    button = next(b for b in panel._next_step_box.buttons() if b.text() == label)
    button.click()
    _pump(qapp)


def test_switching_to_screencap_is_the_users_choice(panel, qapp):
    panel._launch_spec = {"display_id": 2}
    started = []
    panel._start_screencap = lambda display_id, requested: started.append(display_id)
    panel._give_up_scrcpy_start("boom", 3)
    _pump(qapp)
    _press(panel, qapp, "Switch to ADB screencap")
    assert panel.screen_backend() == "screencap" and started == [2]


def test_try_again_repeats_the_same_start(panel, qapp):
    """The same display and window, with the compat mode the user's Options
    say: the last automatic retry turns compat on for itself."""
    panel._launch_spec = {"display_id": 2, "compat": True, "embed": None, "_retry": True}
    panel._give_up_scrcpy_start("boom", 3)
    _pump(qapp)
    calls = []
    panel.start = lambda **spec: calls.append(spec)
    _press(panel, qapp, "Try again")
    assert calls == [{"display_id": 2, "embed": None}]


def test_retries_never_open_a_separate_window(panel):
    panel._launch_spec = {"display_id": 0}
    panel._retry_count = 1
    scheduled = []
    panel._schedule_retry = lambda delay, callback: scheduled.append(callback)
    panel._retry_or_show_scrcpy_failure("boom")
    seen = []
    panel._retry_start = lambda **spec: seen.append(spec)
    scheduled[0]()
    assert seen == [{"compat": True}]


def test_a_tile_asks_on_its_status_line(panel, qapp):
    panel._fixed_display = {"id": 3}
    notices = []
    panel._tile_notice = lambda text: notices.append(text) or True
    panel._give_up_scrcpy_start("boom", 3)
    _pump(qapp)
    assert panel._next_step_box is None
    assert "#use-screencap" in notices[0] and "#scrcpy-retry" in notices[0]


def test_a_tile_link_switches_it(panel, qapp):
    panel._launch_spec = {"display_id": 3}
    started = []
    panel._start_screencap = lambda display_id, requested: started.append(display_id)
    panel._on_status_link("#use-screencap")
    _pump(qapp)
    assert started == [3]


def test_waiting_for_a_window_never_gives_up_on_it(panel, monkeypatch):
    """After 27 s the hidden window was shown as a blank separate one."""
    panel._scrcpy = _Session()
    panel._win_title = "t"
    panel._embed_tries = 500
    panel._embed_timer = None
    panel.container.resize(400, 400)
    monkeypatch.setattr(mp, "_find_window", lambda title: None)
    panel._rescue_offscreen = lambda hwnd=None: pytest.fail("moved to a window of its own")
    panel._try_embed()


# ---- zoom ---------------------------------------------------------------------
@pytest.mark.parametrize("area, aspect, zoom, pan, expected", [
    ((800, 600), 0.5, 1.0, (0.5, 0.5), (0, 0, 800, 600)),  # fit: the renderer letter-boxes
    ((800, 600), None, 2.0, (0.5, 0.5), (0, 0, 800, 600)),  # shape unknown
    # a tall picture: fitted 300x600, doubled 600x1200, width still fits
    ((800, 600), 0.5, 2.0, (0.5, 0.0), (100, 0, 600, 1200)),
    ((800, 600), 0.5, 2.0, (0.5, 1.0), (100, -600, 600, 1200)),
    ((800, 600), 0.5, 2.0, (0.5, 0.5), (100, -300, 600, 1200)),
    # a wide picture: fitted 800x400, tripled
    ((800, 600), 2.0, 3.0, (0.0, 0.5), (0, -300, 2400, 1200)),
    ((800, 600), 2.0, 3.0, (1.0, 0.5), (-1600, -300, 2400, 1200)),
])
def test_where_the_zoomed_picture_goes(area, aspect, zoom, pan, expected):
    assert mp._zoomed_rect(*area, aspect, zoom, *pan) == expected


def test_a_zoomed_picture_never_exceeds_what_a_gpu_takes():
    _x, _y, w, h = mp._zoomed_rect(4000, 2000, 2.0, 8.0)
    assert max(w, h) <= mp._MAX_ZOOMED_EDGE


def _showing(panel, monkeypatch, aspect=0.5):
    panel._child_hwnd = 1234
    panel._video_aspect = aspect
    monkeypatch.setattr(panel, "_fit", lambda: None)
    panel._sync_zoom_controls()


def test_zoom_is_off_with_no_screen(panel):
    assert not panel.btn_zoom_in.isEnabled() and not panel.btn_zoom.isEnabled()


def test_the_zoom_buttons_step(panel, monkeypatch):
    _showing(panel, monkeypatch)
    assert panel.btn_zoom.text() == "Fit" and not panel.btn_zoom_out.isEnabled()
    panel.btn_zoom_in.click()
    assert panel._zoom == 1.25 and panel.btn_zoom.text() == "125%" and panel.btn_zoom_out.isEnabled()
    panel.btn_zoom_in.click()
    assert panel._zoom == 1.5
    panel.btn_zoom_out.click()
    panel.btn_zoom_out.click()
    assert panel._zoom == 1.0 and panel.btn_zoom.text() == "Fit"
    panel.btn_zoom_out.click()
    assert panel._zoom == 1.0


def test_zoom_stops_at_the_last_step(panel, monkeypatch):
    _showing(panel, monkeypatch)
    panel._zoom_to(100)
    assert panel._zoom == mp.ZOOM_STEPS[-1] and not panel.btn_zoom_in.isEnabled()


def test_scroll_bars_appear_for_what_does_not_fit(panel, qapp, monkeypatch):
    panel.show()
    _pump(qapp)
    _showing(panel, monkeypatch, aspect=0.5)
    assert panel._pan_v.isHidden() and panel._pan_h.isHidden()
    panel._zoom_to(2.0)
    assert not panel._pan_v.isHidden()  # twice as tall as its place
    assert panel._pan_h.isHidden()  # and still narrower than it


def test_a_scroll_bar_moves_the_picture(panel, qapp, monkeypatch):
    panel.show()
    _pump(qapp)
    fits = []
    _showing(panel, monkeypatch)
    monkeypatch.setattr(panel, "_fit", lambda: fits.append(list(panel._pan)))
    panel._zoom_to(2.0)
    fits.clear()
    panel._pan_v.setValue(panel._pan_v.maximum())
    assert fits == [[0.5, 1.0]]


def test_actual_pixels_is_one_to_one(panel, monkeypatch):
    panel.container.resize(500, 400)
    monkeypatch.setattr(panel, "_video_pixels", lambda: (2500, 1000))
    monkeypatch.setattr(panel, "devicePixelRatioF", lambda: 1.0)
    assert panel._actual_pixels_zoom() == 5.0


def test_a_video_smaller_than_its_place_stays_at_fit(panel, monkeypatch):
    panel.container.resize(1000, 1000)
    monkeypatch.setattr(panel, "_video_pixels", lambda: (640, 480))
    monkeypatch.setattr(panel, "devicePixelRatioF", lambda: 1.0)
    assert panel._actual_pixels_zoom() == 1.0


def test_the_video_size_comes_from_the_display_when_scrcpy_does_not_say(panel):
    """Hardware decoding prints no "Texture:" line."""
    panel._scrcpy = _Session("INFO: Video decoding: d3d11va\n")
    panel._launch_spec = {"display_id": 0}
    panel._displays = [{"id": 0, "size": "2560x1600"}]
    assert panel._video_pixels() == (2560, 1600)


def test_stopping_goes_back_to_fit(panel, monkeypatch):
    _showing(panel, monkeypatch)
    panel._zoom_to(3.0)
    panel._pan = [0.1, 0.9]
    panel._release_viewer()
    assert panel._zoom == 1.0 and panel._pan == [0.5, 0.5]
    assert panel._pan_v.isHidden() and not panel.btn_zoom.isEnabled()


def test_zoom_moves_a_screencap_view_as_well(panel, monkeypatch):
    placed = []
    panel._screencap = types.SimpleNamespace(setGeometry=lambda *a: placed.append(a))
    panel._video_aspect = 0.5
    panel.container.resize(800, 600)
    monkeypatch.setattr(panel, "_fit", lambda: None)
    panel._zoom_to(2.0)
    panel._screencap = None
    assert placed[-1] == (100, -300, 600, 1200)


# ---- Remote Desktop and very large displays -----------------------------------
def test_remote_desktop_starts_as_scrcpy_starts_itself(qapp, monkeypatch):
    """Forced software rendering and a 1920 cap turned 7200x1178 into 1920x314."""
    monkeypatch.setattr(mp, "is_remote_session", lambda: True)
    made = mp.MirrorPanel(types.SimpleNamespace(serial="s", config=None), {"name": "s"})
    try:
        assert made._rdp is True and made._opt["soft"] is False
        opts = made._tune(mp.ScrcpyOptions(no_audio=True), native=True)
        assert opts.render_driver is None and opts.max_size is None and opts.max_fps is None
    finally:
        made._closing = True
        made._host_sync_timer.stop()
        made._place_timer.stop()
        made.deleteLater()
        _pump(qapp, 10)


def test_a_default_renderer_that_cannot_run_falls_back_to_software(panel):
    panel._launch_opts = mp.ScrcpyOptions()
    panel._launch_spec = {"display_id": 0}
    panel._schedule_retry = lambda delay, callback: None
    panel._retry_or_show_scrcpy_failure("ERROR: Could not create renderer: no driver")
    assert panel._opt["soft"] is True and panel._retry_count == 1


def test_other_failures_leave_the_renderer_alone(panel):
    panel._launch_opts = mp.ScrcpyOptions()
    panel._launch_spec = {"display_id": 0}
    panel._schedule_retry = lambda delay, callback: None
    panel._retry_or_show_scrcpy_failure("ERROR: Could not open video stream")
    assert panel._opt["soft"] is False


def test_software_failing_does_not_loop(panel):
    panel._opt["soft"] = True
    panel._launch_opts = mp.ScrcpyOptions(render_driver="software")
    panel._launch_spec = {"display_id": 0}
    logged = []
    panel.log.connect(logged.append)
    panel._schedule_retry = lambda delay, callback: None
    panel._retry_or_show_scrcpy_failure("ERROR: Could not create renderer")
    assert not any("using software rendering instead" in line for line in logged)


@pytest.mark.parametrize("text, size", [
    ("INFO: Texture: 1920x314\n", (1920, 314)),
    ("INFO: Texture (D3D11VA): 4096x672\n", (4096, 672)),  # hardware decoding
    ("INFO: Texture: 1080x2400\nINFO: Texture: 2400x1080\n", (2400, 1080)),  # rotated since
    ("INFO: Video decoding: d3d11va\n", None),
    ("", None),
])
def test_the_video_size_is_read_from_every_kind_of_log(text, size):
    assert mp._video_size_in_log(text) == size


def test_actual_pixels_uses_what_the_encoder_really_makes(panel, monkeypatch):
    """A 7200-wide display the encoder could only make 4096 wide."""
    panel._scrcpy = _Session("INFO: Texture (D3D11VA): 4096x672\n")
    panel._displays = [{"id": 0, "size": "7200x1178"}]
    panel._launch_spec = {"display_id": 0}
    panel.container.resize(2048, 800)
    monkeypatch.setattr(panel, "devicePixelRatioF", lambda: 1.0)
    assert panel._actual_pixels_zoom() == 2.0


def test_hardware_decoding_counts_as_a_picture(panel):
    _waiting(panel, "INFO: Renderer: direct3d11\nINFO: Texture (D3D11VA): 4096x672\n", ran=1.0)
    panel._check_alive()
    assert panel._became_ready


def test_fill_covers_the_window_with_a_very_wide_display(panel):
    panel.container.resize(1500, 900)
    panel._video_aspect = 7200 / 1178
    zoom = panel._fill_zoom()
    assert zoom == pytest.approx(3.67, abs=0.01)  # the picture is now 900 high
    x, y, w, h = mp._zoomed_rect(1500, 900, panel._video_aspect, zoom)
    assert h == 900 and w > 5000 and y == 0


def test_fill_with_no_picture_shape_stays_at_fit(panel):
    panel._video_aspect = None
    assert panel._fill_zoom() == 1.0


# ---- the webcam ---------------------------------------------------------------
@pytest.fixture
def camera(qapp, monkeypatch):
    from turboadb.gui import camera_widget

    # It scans for cameras 150 ms after it is made, in a thread: a panel
    # destroyed while that runs takes the whole process down.
    monkeypatch.setattr(camera_widget.CameraPanel, "_auto_scan", lambda self: None)
    made = camera_widget.CameraPanel()
    made.resize(800, 600)
    yield made
    made.close_panel()
    made.deleteLater()
    _pump(qapp, 10)


def _picture(w=400, h=200):
    """Left half red, right half blue."""
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor("red"))
    for x in range(w // 2, w):
        for y in range(h):
            img.setPixelColor(x, y, QColor("blue"))
    return img


def test_the_webcam_starts_unzoomed(camera):
    img = _picture()
    assert camera._zoomed(img) is img and camera.zoom_lbl.text() == "100%"


def test_webcam_zoom_crops_the_middle(camera):
    camera.zoom_slider.setValue(200)
    assert camera.zoom_lbl.text() == "200%"
    part = camera._zoomed(_picture())
    assert (part.width(), part.height()) == (200, 100)
    assert part.pixelColor(10, 50) == QColor("red") and part.pixelColor(190, 50) == QColor("blue")


def test_webcam_pan_shows_another_part(camera):
    camera.zoom_slider.setValue(200)
    camera._zoom_pan = [0.0, 0.5]
    assert camera._zoomed(_picture()).pixelColor(190, 50) == QColor("red")
    camera._zoom_pan = [1.0, 0.5]
    assert camera._zoomed(_picture()).pixelColor(10, 50) == QColor("blue")


def test_the_wheel_zooms_the_webcam(camera, qapp):
    wheel = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, 120),
                        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    assert camera.eventFilter(camera.view, wheel)
    assert camera.zoom_slider.value() == 125


def test_dragging_moves_the_zoomed_webcam(camera):
    camera.zoom_slider.setValue(200)
    camera.view.resize(400, 200)

    def mouse(kind, x, y):
        return QMouseEvent(kind, QPointF(x, y), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

    camera.eventFilter(camera.view, mouse(QEvent.MouseButtonPress, 100, 100))
    camera.eventFilter(camera.view, mouse(QEvent.MouseMove, 300, 100))  # picture follows right
    assert camera._zoom_pan[0] == 0.0  # so the left part shows
    camera.eventFilter(camera.view, mouse(QEvent.MouseButtonRelease, 300, 100))
    camera.eventFilter(camera.view, mouse(QEvent.MouseMove, 100, 100))
    assert camera._zoom_pan[0] == 0.0  # no button, no drag


def test_double_click_goes_back(camera):
    camera.zoom_slider.setValue(300)
    camera._zoom_pan = [0.0, 0.0]
    dbl = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(5, 5), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    assert camera.eventFilter(camera.view, dbl)
    assert camera.zoom_slider.value() == 100 and camera._zoom_pan == [0.5, 0.5]


def _wheel(dx, dy):
    return QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(dx, dy),
                       Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)


def test_a_sideways_scroll_does_not_zoom_the_webcam(camera):
    assert not camera.eventFilter(camera.view, _wheel(120, 0))
    assert camera.zoom_slider.value() == 100


def test_a_touchpad_s_small_turns_add_up_to_one_step(camera):
    for _ in range(3):
        camera.eventFilter(camera.view, _wheel(0, 30))
    assert camera.zoom_slider.value() == 100
    camera.eventFilter(camera.view, _wheel(0, 30))
    assert camera.zoom_slider.value() == 125
    camera.eventFilter(camera.view, _wheel(0, 60))
    camera.eventFilter(camera.view, _wheel(0, -120))  # the other way: a step back at once
    assert camera.zoom_slider.value() == 100


# ---- a device turned while it shows ---------------------------------------------
def test_the_zoom_follows_a_turned_device(panel, monkeypatch):
    shapes = []
    panel.display_shape_changed.connect(lambda: shapes.append(panel._video_aspect))
    monkeypatch.setattr(panel, "_fit", lambda: None)
    _waiting(panel, "INFO: Texture: 1080x2400\n", ran=40.0)
    panel._became_ready, panel._child_hwnd = True, 1234
    panel._video_aspect = 1080 / 2400
    panel._check_alive()
    assert shapes == []  # the same shape: nothing to do
    panel._scrcpy.log += "INFO: Texture: 2400x1080\n"
    panel._check_alive()
    assert shapes == [pytest.approx(2400 / 1080)]
