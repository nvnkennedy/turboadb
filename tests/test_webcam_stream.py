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
# recording needs ffmpeg on this PC; a remote camera is watched without one
# --------------------------------------------------------------------------- #
@pytest.fixture
def remote_view(qapp, panel, monkeypatch, tmp_path):
    """A remote camera on screen, on a PC that has no ffmpeg of its own yet
    (the remote one has: nothing was fetched here to watch it)."""
    from turboadb.gui import camera_widget, ffmpeg_tools, fileutil
    from turboadb.gui import settings as settings_mod

    monkeypatch.setattr(settings_mod, "webcam_remote_password", lambda: "")
    panel.source.setCurrentIndex(panel.source.findData("remote"))
    reader = _stream(panel, [_jpeg()])
    assert _pump(qapp, lambda: reader.frames > 0 and panel._viewing_announced)
    view = panel
    view.have = [None]  # the ffmpeg this PC has
    view.asked, view.warned, view.encoders, view.fetches = [], [], [], []
    view.answer = camera_widget.QMessageBox.Yes
    view.clip = str(tmp_path / "webcam.mp4")
    monkeypatch.setattr(ffmpeg_tools, "cached_ffmpeg", lambda: view.have[0])
    monkeypatch.setattr(ffmpeg_tools, "_auto_download_supported", lambda: True)
    monkeypatch.setattr(
        camera_widget.QMessageBox, "question",
        lambda *args, **_k: view.asked.append(args[2]) or view.answer,
    )
    monkeypatch.setattr(
        camera_widget.QMessageBox, "warning", lambda *args, **_k: view.warned.append(args[2])
    )
    monkeypatch.setattr(fileutil, "ask_save_path", lambda *_a, **_k: view.clip)

    def encoder(argv, **_kwargs):
        view.encoders.append(list(argv))
        return _Encoder()

    monkeypatch.setattr(camera_widget.subprocess, "Popen", encoder)
    return view


def test_record_on_a_remote_camera_fetches_the_ffmpeg_this_pc_lacks(
    qapp, remote_view, monkeypatch
):
    """Record answered "ffmpeg isn't ready yet." every time: nothing in the
    Remote flow ever fetched an ffmpeg for this PC."""
    from turboadb.gui import ffmpeg_tools

    view = remote_view

    def fetch(log=lambda m: None, should_cancel=None):
        view.fetches.append(threading.get_ident())
        log("Downloading ffmpeg… 80 MB (50%)")
        view.have[0] = "C:/tools/ffmpeg.exe"
        return view.have[0]

    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg", fetch)
    view._toggle_record()
    assert len(view.asked) == 1 and "160 MB" in view.asked[0]  # asked before it downloads
    assert _pump(qapp, lambda: view._rec_proc is not None)
    assert view.fetches and view.fetches[0] != threading.get_ident()  # off the UI thread
    assert view.encoders[0][0] == "C:/tools/ffmpeg.exe" and view.encoders[0][-1] == view.clip
    assert view.rec_btn.text() == "Stop recording" and view.rec_btn.isEnabled()
    assert view._dl_dialog is None and view.warned == []
    assert view.reader is not None  # the stream was never stopped for it


def test_record_asks_nothing_when_ffmpeg_is_at_hand(qapp, remote_view):
    view = remote_view
    view.have[0] = "C:/tools/ffmpeg.exe"
    view._toggle_record()
    assert view.asked == [] and view._rec_proc is not None
    assert view.encoders[0][0] == "C:/tools/ffmpeg.exe"


def test_record_downloads_nothing_unless_the_user_agrees(qapp, remote_view, monkeypatch):
    from turboadb.gui import camera_widget, ffmpeg_tools

    view = remote_view
    monkeypatch.setattr(
        ffmpeg_tools, "ensure_local_ffmpeg",
        lambda *_a, **_k: view.fetches.append("download") or "C:/tools/ffmpeg.exe",
    )
    view.answer = camera_widget.QMessageBox.No
    view._toggle_record()
    qapp.processEvents()
    assert len(view.asked) == 1 and view.fetches == []
    assert view._rec_proc is None and view.rec_btn.isEnabled() and view.warned == []


def test_record_says_how_to_get_ffmpeg_where_it_cannot_be_downloaded(
    qapp, remote_view, monkeypatch
):
    from turboadb.gui import ffmpeg_tools

    view = remote_view
    monkeypatch.setattr(ffmpeg_tools, "_auto_download_supported", lambda: False)
    view._toggle_record()
    assert view.asked == [] and view._rec_proc is None
    assert len(view.warned) == 1
    assert "PATH" in view.warned[0] and "Settings → ffmpeg path" in view.warned[0]


def test_a_failed_ffmpeg_fetch_says_why_and_gives_record_back(qapp, remote_view, monkeypatch):
    from turboadb.gui import ffmpeg_tools

    view = remote_view

    def offline(log=lambda m: None, should_cancel=None):
        raise RuntimeError("Couldn't download ffmpeg (offline).")

    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg", offline)
    view._toggle_record()
    assert _pump(qapp, lambda: bool(view.warned))
    assert "Couldn't download ffmpeg (offline)." in view.warned[0]
    assert view._rec_proc is None and view.rec_btn.isEnabled()
    assert view.rec_btn.text() == "Record"


def test_a_cancelled_ffmpeg_fetch_is_no_error(qapp, remote_view, monkeypatch):
    from turboadb.gui import ffmpeg_tools

    view = remote_view
    started = threading.Event()

    def slow(log=lambda m: None, should_cancel=None):
        log("Downloading ffmpeg… 5 MB (3%)")
        started.set()
        deadline = time.monotonic() + 10
        while not should_cancel() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise ffmpeg_tools.DownloadCancelled("cancelled")

    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg", slow)
    view._toggle_record()
    assert started.wait(5)
    assert _pump(qapp, lambda: view._dl_dialog is not None)  # the usual setup dialog
    view._cancel_download()  # its Cancel button
    assert _pump(qapp, lambda: view.rec_btn.isEnabled())
    assert view.warned == [] and view._rec_proc is None and view._dl_dialog is None
    assert "cancelled" in view.status.text()


# --------------------------------------------------------------------------- #
# several streams from one remote PC
# --------------------------------------------------------------------------- #
class _Socket:
    """The video socket of a remote stream that sends nothing for now."""

    def __init__(self):
        self.closed = False

    def settimeout(self, _seconds):
        pass

    def recv(self, _n):
        import socket

        if self.closed:
            return b""
        time.sleep(0.01)
        raise socket.timeout()

    def close(self):
        self.closed = True


@pytest.fixture
def remote_pc(monkeypatch):
    """A remote PC whose WinRM calls are recorded: ``started`` and ``stopped``
    hold (camera or pid, stream port)."""
    from turboadb.gui import camera_widget
    from turboadb.gui import remote_webcam as rw
    from turboadb.gui import settings as settings_mod

    world = {"started": [], "stopped": [], "fail": None}

    def start(host, login, password, camera, ffmpeg, *, stream_port, **_kwargs):
        if world["fail"]:
            raise RuntimeError(world["fail"])
        world["started"].append((camera, stream_port))
        return 5000 + stream_port  # its ffmpeg's pid there

    def stop(host, login, password, pid, *, stream_port, **_kwargs):
        world["stopped"].append((pid, stream_port))

    monkeypatch.setattr(rw, "_ports_in_use", set())
    monkeypatch.setattr(rw, "start_remote_stream", start)
    monkeypatch.setattr(rw, "stop_remote_stream", stop)
    monkeypatch.setattr(rw, "probe_remote_camera", lambda *_a, **_k: "")
    monkeypatch.setattr(
        camera_widget.socket, "create_connection", lambda _address, timeout=None: _Socket()
    )
    monkeypatch.setattr(settings_mod, "webcam_remote_password", lambda: "")
    monkeypatch.setattr(camera_widget.CameraPanel, "_auto_scan", lambda self: None)
    return world


def _watch_remote(panel, camera):
    """Source = Remote on lab-pc, scanned, then Start camera."""
    panel.source.setCurrentIndex(panel.source.findData("remote"))
    panel.r_host.setText("lab-pc")
    panel.r_user.setText("me")
    panel._remote_ffmpeg = r"C:\ffmpeg\bin\ffmpeg.exe"
    panel.camera.addItem(camera)
    panel._toggle_start()


def test_every_remote_stream_gets_a_port_of_its_own(monkeypatch):
    from turboadb.gui import remote_webcam as rw

    monkeypatch.setattr(rw, "_ports_in_use", set())
    first, second = rw.claim_stream_port(), rw.claim_stream_port()
    assert first == rw.DEFAULT_STREAM_PORT and second not in (first, None)
    rw.release_stream_port(first)
    assert rw.claim_stream_port() == first  # free again once its stream is gone
    taken = {first, second}
    with pytest.raises(RuntimeError) as err:
        while True:
            port = rw.claim_stream_port()
            assert port not in taken
            taken.add(port)
    assert "stop one" in str(err.value) and len(taken) >= 8


def test_two_panels_on_one_remote_pc_keep_their_own_streams(qapp, remote_pc):
    """The Webcam tab and a device tab's Webcam page on the same remote PC:
    both streams used port 28100, so starting the second ended the first (its
    ffmpeg looked stale), and stopping either ended the other and removed
    the firewall rule they shared."""
    from turboadb.gui import camera_widget
    from turboadb.gui import remote_webcam as rw

    panels = [camera_widget.CameraPanel(), camera_widget.CameraPanel()]
    try:
        _watch_remote(panels[0], "Front camera")
        assert _pump(qapp, lambda: panels[0].reader is not None)
        _watch_remote(panels[1], "Rear camera")
        assert _pump(qapp, lambda: panels[1].reader is not None)
        ports = [panel._remote_conn[3] for panel in panels]
        assert ports[0] == rw.DEFAULT_STREAM_PORT and ports[1] != ports[0]
        assert remote_pc["started"] == [("Front camera", ports[0]), ("Rear camera", ports[1])]

        panels[0]._stop_stream()
        assert _pump(qapp, lambda: bool(remote_pc["stopped"]))
        # only its own ffmpeg, by its own port and rule
        assert remote_pc["stopped"] == [(5000 + ports[0], ports[0])]
        assert panels[1].reader is not None and panels[1]._remote_pid == 5000 + ports[1]
        # ...and that port is free for the next stream once the remote side is stopped
        assert _pump(qapp, lambda: ports[0] not in rw._ports_in_use)
        assert ports[1] in rw._ports_in_use
    finally:
        for panel in panels:
            panel.close_panel()
            panel.close()
            qapp.processEvents()
            panel.deleteLater()
    assert _pump(qapp, lambda: not rw._ports_in_use)


def test_a_remote_stream_that_never_started_gives_its_port_back(
    qapp, panel, remote_pc, monkeypatch
):
    from turboadb.gui import camera_widget
    from turboadb.gui import remote_webcam as rw

    warned = []
    monkeypatch.setattr(
        camera_widget.QMessageBox, "warning", lambda *args, **_k: warned.append(args[2])
    )
    remote_pc["fail"] = "the remote refused to launch ffmpeg"
    _watch_remote(panel, "Front camera")
    assert _pump(qapp, lambda: bool(warned))
    assert "refused to launch ffmpeg" in warned[0]
    assert _pump(qapp, lambda: not rw._ports_in_use)
    assert panel.start_btn.isEnabled()


def test_a_streams_remote_script_names_only_its_own_port(monkeypatch):
    from turboadb.gui import remote_webcam as rw

    scripts = []
    monkeypatch.setattr(
        rw, "_run_ps",
        lambda host, login, password, script, winrm_port=5985:
        scripts.append(script) or (0, "PID:77\nRC:0\n", ""),
    )
    other = rw.DEFAULT_STREAM_PORT
    mine = other + 1
    rw.start_remote_stream("lab-pc", "u", "p", "Cam", "ffmpeg.exe", stream_port=mine,
                           client_ip="10.0.0.9")
    rw.stop_remote_stream("lab-pc", "u", "p", 77, stream_port=mine)
    for script in scripts:
        assert f"tcp://0.0.0.0:{mine}?listen=1" in script
        assert f'"TurboADB Webcam {mine}"' in script
        assert str(other) not in script  # the other panel's ffmpeg and rule are left alone


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
