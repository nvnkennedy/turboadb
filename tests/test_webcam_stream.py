"""The Webcam page: a stream that ends by itself, scans per source, and the
saved remote password.

- A camera unplugged mid-stream, a remote ffmpeg that ended or a network that
  went quiet left the last frame on screen as "Viewing", a recording running
  on with nothing to record. The stream is now ended, said so, and a recording
  finished with what it captured.
- A scan kept running after Source changed, and its cameras then filled the
  other source's list; a scan for the new source could not start meanwhile.
- The remote password was read from the OS credential vault on the UI thread
  when the page opened, and was filled in for whatever host was typed later.
- Opening the page downloaded ffmpeg (about 160 MB) without asking, and off
  Windows the Local scan blamed Remote Desktop settings for DirectShow being
  Windows-only.

Headless (offscreen Qt); no camera, ffmpeg, keyring or network is used.
"""

import threading
import time

import pytest

pytest.importorskip("PyQt5")


def _pump(qapp, until, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return until()


def _jpeg():
    from PyQt5.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt5.QtGui import QColor, QImage

    image = QImage(16, 16, QImage.Format_RGB32)
    image.fill(QColor("teal"))
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    assert image.save(buf, "JPG")
    return bytes(data)


class _Encoder:
    """The recording's ffmpeg: it finishes as soon as its input ends."""

    def __init__(self):
        import io

        self.stdin = io.BytesIO()

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.fixture
def panel(qapp, monkeypatch):
    from turboadb.gui import camera_widget

    monkeypatch.setattr(camera_widget.CameraPanel, "_auto_scan", lambda self: None)
    monkeypatch.setattr(camera_widget, "_LOCAL_CAMERAS", True)
    panel = camera_widget.CameraPanel()
    panel.logs = []
    panel.log.connect(panel.logs.append)
    try:
        yield panel
    finally:
        from PyQt5.QtCore import QCoreApplication, QEvent

        panel.close_panel()
        panel.close()
        qapp.processEvents()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()


def _stream(panel, reads):
    """Show a stream whose reader gets *reads* (bytes; None = the end)."""
    from turboadb.gui import camera_widget

    chunks = iter(reads)

    def read_fn(_n):
        try:
            chunk = next(chunks)
        except StopIteration:
            return b""  # a quiet source: nothing arrives, nothing ends
        return chunk

    panel.reader = camera_widget._FrameReader(read_fn)
    panel.reader.start()
    panel._after_start("Front camera")
    return panel.reader


# --------------------------------------------------------------------------- #
# a stream that ends by itself
# --------------------------------------------------------------------------- #
def test_a_stream_that_ends_says_so_and_saves_the_recording(qapp, panel, tmp_path):
    frame = _jpeg()
    reader = _stream(panel, [frame, frame, frame, None])  # then the camera is gone
    assert _pump(qapp, lambda: reader.frames > 0 and not reader.is_alive())
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 64)
    panel._rec_proc, panel._rec_writer, panel._rec_path = _Encoder(), None, str(path)
    assert panel._alive_timer.isActive()

    panel._check_stream()
    assert panel.reader is None and panel._rec_proc is None
    assert "stream ended" in panel.view.text()
    assert not panel._timer.isActive() and not panel._alive_timer.isActive()
    assert panel.fps_lbl.text() == "" and panel.start_btn.text() == "Start camera"
    assert any("the stream from Front camera ended" in line for line in panel.logs)
    assert any("recording stops where the stream did" in line for line in panel.logs)
    # the recording is finished and saved with what it captured
    assert _pump(qapp, lambda: any("recording saved" in line for line in panel.logs))
    assert panel.status.text() == "Recording saved ✓"


def test_a_stream_that_goes_quiet_ends_after_a_while(qapp, panel, monkeypatch):
    from turboadb.gui import camera_widget

    monkeypatch.setattr(camera_widget, "_STREAM_STALL_S", 0.3)
    reader = _stream(panel, [_jpeg()])  # one frame, then a socket that stays open
    assert _pump(qapp, lambda: reader.frames > 0)
    panel._check_stream()
    assert panel.reader is reader  # quiet for a moment only
    time.sleep(0.4)
    panel._check_stream()
    assert panel.reader is None and panel.status.text() == "Camera stream ended"
    assert any("sent no video for 0.3 s" in line for line in panel.logs)


def test_a_paused_view_is_not_a_stalled_stream(qapp, panel, monkeypatch):
    from turboadb.gui import camera_widget

    monkeypatch.setattr(camera_widget, "_STREAM_STALL_S", 0.3)
    frame = _jpeg()
    arrived = threading.Event()

    def slow_camera(_n):  # a frame every 0.1 s, as a slow camera sends them
        time.sleep(0.1)
        arrived.set()
        return frame

    panel.reader = camera_widget._FrameReader(slow_camera)
    panel.reader.start()
    panel._after_start("Front camera")
    assert arrived.wait(5)
    panel._toggle_pause()  # nothing is painted, frames still arrive
    for _ in range(8):
        panel._check_stream()
        time.sleep(0.1)
    assert panel.reader is not None and "ended" not in panel.status.text()
    panel._stop_stream()


def test_a_stream_that_never_showed_a_frame_is_left_to_the_no_video_check(qapp, panel):
    reader = _stream(panel, [None])  # ffmpeg exits before its first frame
    assert _pump(qapp, lambda: not reader.is_alive())
    panel._check_stream()
    # _check_no_video decides (a frame rate the camera refused is retried there)
    assert panel.reader is reader and "ended" not in panel.status.text()
    panel._stop_stream()


# --------------------------------------------------------------------------- #
# scans per source
# --------------------------------------------------------------------------- #
def test_changing_source_during_a_scan_keeps_its_cameras_out_of_the_new_list(
    qapp, panel, monkeypatch
):
    from turboadb.gui import ffmpeg_tools, remote_webcam

    release = threading.Event()

    def slow_local(log=None, should_cancel=None):
        release.wait(5)
        return "C:/tools/ffmpeg.exe"

    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg", slow_local)
    monkeypatch.setattr(ffmpeg_tools, "list_local_cameras", lambda ff: ["Integrated Camera"])
    monkeypatch.setattr(
        remote_webcam, "list_remote_cameras",
        lambda host, login, password, log=None: (["Remote Cam"], "C:/ff/ffmpeg.exe", ""),
    )
    panel._refresh()  # Scan cameras, Local
    local_scan = panel._prep
    assert not panel.refresh_btn.isEnabled()
    panel.source.setCurrentIndex(panel.source.findData("remote"))
    assert panel.refresh_btn.isEnabled()  # a scan of Remote may start at once
    release.set()
    assert _pump(qapp, lambda: local_scan.isFinished())
    qapp.processEvents()
    assert panel.camera.count() == 0 and not panel.start_btn.isEnabled()
    # a stale result that was already on its way is ignored too
    panel._local_ready("C:/tools/ffmpeg.exe", ["Integrated Camera"], local_scan)
    assert panel.camera.count() == 0

    panel.r_host.setText("rdp-host")
    panel.r_user.setText("me")
    panel._refresh()
    assert _pump(qapp, lambda: panel.camera.count() == 1)
    assert panel.camera.itemText(0) == "Remote Cam" and panel.start_btn.isEnabled()


def test_opening_the_page_never_downloads_ffmpeg(qapp, panel, monkeypatch):
    from turboadb.gui import ffmpeg_tools

    def download(*_args, **_kwargs):
        raise AssertionError("the automatic scan must not download ffmpeg")

    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg", download)
    monkeypatch.setattr(ffmpeg_tools, "find_local_ffmpeg", lambda log=None: None)
    panel._refresh(quiet=True)  # the scan when the page opens
    assert _pump(qapp, lambda: "one-time download" in panel.status.text())
    assert panel._dl_dialog is None and panel.refresh_btn.isEnabled()

    monkeypatch.setattr(ffmpeg_tools, "find_local_ffmpeg", lambda log=None: "C:/ff/ffmpeg.exe")
    monkeypatch.setattr(ffmpeg_tools, "list_local_cameras", lambda ff: ["USB Camera"])
    panel._refresh(quiet=True)  # an ffmpeg already at hand is used as before
    assert _pump(qapp, lambda: panel.camera.count() == 1)


def test_off_windows_the_local_source_says_it_needs_windows(qapp, panel, monkeypatch):
    from turboadb.gui import camera_widget

    monkeypatch.setattr(camera_widget, "_LOCAL_CAMERAS", False)
    panel._refresh(quiet=True)
    assert panel._prep is None and "Windows only" in panel.status.text()
    assert panel.refresh_btn.isEnabled()
    panel.source.setCurrentIndex(panel.source.findData("remote"))
    panel.source.setCurrentIndex(panel.source.findData("local"))
    assert "Windows only" in panel.status.text()


# --------------------------------------------------------------------------- #
# the saved remote password
# --------------------------------------------------------------------------- #
def test_the_saved_password_is_read_off_the_ui_thread_and_only_for_its_host(qapp, monkeypatch):
    from turboadb.gui import camera_widget
    from turboadb.gui import settings as settings_mod

    reads = []

    def vault():
        reads.append(threading.get_ident())
        return "s3cret"

    monkeypatch.setattr(settings_mod, "webcam_remote_password", vault)
    monkeypatch.setattr(camera_widget.CameraPanel, "_auto_scan", lambda self: None)
    settings_mod.update(
        {"webcam_remote_host": "pc1", "webcam_remote_user": "me", "webcam_remote_domain": ""}
    )
    panel = camera_widget.CameraPanel()
    try:
        assert reads == [] and panel.r_pass.text() == ""  # opening the page reads nothing
        panel.source.setCurrentIndex(panel.source.findData("remote"))
        assert _pump(qapp, lambda: panel.r_pass.text() == "s3cret")
        assert len(reads) == 1 and reads[0] != threading.get_ident()

        panel.r_host.setText("pc2")  # another host: not sent there unasked
        assert panel.r_pass.text() == ""
        panel.r_host.setText("pc1")
        assert panel.r_pass.text() == "s3cret"
        panel.r_user.setText("admin")
        assert panel.r_pass.text() == ""
        panel.r_pass.setText("typed for admin")  # the user's own typing stays
        panel.r_user.setText("me")
        assert panel.r_pass.text() == "typed for admin"
        panel.source.setCurrentIndex(panel.source.findData("local"))
        panel.source.setCurrentIndex(panel.source.findData("remote"))
        qapp.processEvents()
        assert len(reads) == 1  # read once
    finally:
        settings_mod.update(
            {"webcam_remote_host": "", "webcam_remote_user": "", "webcam_remote_domain": ""}
        )
        panel.close_panel()
        panel.close()
        qapp.processEvents()
