"""Webcam panel — view a camera on THIS machine (Local, which also covers running
TurboADB inside an RDP session), or on another Windows / RDP machine over WinRM
(Remote — no SSH needed). Pick a source + camera, watch it; snapshot, record,
pause, rotate / flip. Frames are decoded off the UI thread so the view stays
smooth, and the view fills the tab. Runs on its own threads — never touches adb /
shell / logcat work.
"""

from __future__ import annotations

import logging
import os
import queue
import time
import socket
import threading
import subprocess

from PyQt5.QtCore import QRect, Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QImage, QPainter, QPixmap, QTransform
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QCheckBox,
    QLineEdit,
    QMessageBox,
    QSizePolicy,
    QProgressDialog,
    QMenu,
)

from ..tools import NO_WINDOW as _NO_WINDOW
from . import ffmpeg_tools
from . import theme
from .icons import icon
from .logcat_view import terminal_icon_pixmap
from .qtutil import close_jobs, disconnect_signals, park_thread, run_job, thread_running

_log = logging.getLogger(__name__)


class _VideoView(QLabel):
    """The video well. Frames are shown as the label's pixmap; while there is
    no frame, a video icon is painted above the hint text (the text itself is
    still the label's ``text()``)."""

    ICON_SIZE = 44
    GAP = 14

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._icon_pm = None

    def idle(self) -> bool:
        pm = self.pixmap()
        return (pm is None or pm.isNull()) and bool(self.text())

    def paintEvent(self, event):
        if not self.idle():
            super().paintEvent(event)
            return
        ratio = self.devicePixelRatioF()
        if self._icon_pm is None or self._icon_pm.devicePixelRatio() != ratio:
            self._icon_pm = terminal_icon_pixmap("video", self.ICON_SIZE, "red", ratio)
        area = self.contentsRect().adjusted(16, 16, -16, -16)
        flags = int(Qt.AlignHCenter | Qt.TextWordWrap)
        text_rect = self.fontMetrics().boundingRect(
            QRect(0, 0, max(1, area.width()), 100000), flags, self.text()
        )
        total = self.ICON_SIZE + self.GAP + text_rect.height()
        top = area.top() + max(0, (area.height() - total) // 2)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.drawPixmap(area.center().x() - self.ICON_SIZE // 2, top, self._icon_pm)
        painter.setPen(QColor(theme.LOG_TIMESTAMP))
        painter.setFont(self.font())
        painter.drawText(
            QRect(area.left(), top + self.ICON_SIZE + self.GAP, area.width(), text_rect.height()),
            flags, self.text(),
        )
        painter.end()

# Cameras that rejected the requested frame rate this session; later starts go
# straight to the camera's native mode instead of failing and retrying again.
_NATIVE_FPS_CAMERAS = set()
# A local DirectShow start that fails (unsupported rate, camera busy) exits
# within a few hundred ms, so it is checked quickly; one that stays alive
# without producing a frame is given up on after this long.
_LOCAL_START_POLL_MS = 300
_LOCAL_NO_VIDEO_TIMEOUT_S = 12.0

_RES = {
    "480p (640×480)": (640, 480),
    "540p (960×540)": (960, 540),
    "720p (1280×720)": (1280, 720),
    "1080p (1920×1080)": (1920, 1080),
}


class _FrameReader(threading.Thread):
    """Reads an MJPEG byte stream (an ffmpeg pipe, or a TCP socket from a remote
    ffmpeg) via ``read_fn``, splits it into JPEG frames, DECODES the latest to a
    QImage here (off the UI thread), and tees raw bytes to a recorder when
    attached."""

    def __init__(self, read_fn):
        super().__init__(daemon=True)
        self._read = read_fn
        self._buf = bytearray()
        self._img = None
        self._raw = None
        self._lock = threading.Lock()
        self._alive = True
        self._record_fh = None
        self.frames = 0
        self.source_frames = 0

    def run(self):
        while self._alive:
            try:
                # A large blocking pipe read batches several JPEGs together.
                # That made a 30-fps source appear as roughly 10 fps because the
                # UI only received one decoded image per burst.  Small reads keep
                # latency low and publish frames as they arrive.
                data = self._read(16384)
            except Exception:
                break
            if data is None:
                break
            if not data:
                time.sleep(0.005)
                continue
            self._buf.extend(data)
            self._extract()

    def _extract(self):
        """Pull every COMPLETE JPEG out of the buffer in order. Each whole frame is
        tee'd to the recorder (clean SOI…EOI boundaries -> a valid MJPEG stream), and
        the LAST one is decoded for display."""
        buf = self._buf
        rec = self._record_fh
        last = None
        extracted = 0
        while True:
            start = buf.find(b"\xff\xd8")
            if start == -1:
                if len(buf) > 2 * 1024 * 1024:  # no SOI in a large buffer -> trim
                    del buf[: -512 * 1024]
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end == -1:
                if start:  # drop junk before the next SOI
                    del buf[:start]
                break
            frame = bytes(buf[start : end + 2])
            del buf[: end + 2]
            if rec is not None:
                rec(frame)  # _RecordWriter.put: queues, never blocks this reader
            last = frame
            extracted += 1
        if last is None:
            return
        img = QImage.fromData(last, "JPG")  # decode here, not on the UI thread
        if img.isNull():
            return
        with self._lock:
            self._img = img
            self._raw = last
            self.frames += 1
            self.source_frames += extracted

    def latest_image(self):
        with self._lock:
            return self._img

    def latest_raw(self):
        with self._lock:
            return self._raw

    def set_recorder(self, put):
        """Attach ``put(frame_bytes)`` (a :class:`_RecordWriter`), or None."""
        self._record_fh = put

    def stop(self):
        self._alive = False


class _RecordWriter(threading.Thread):
    """Feed recorded JPEG frames to the encoder's stdin on its own thread.

    Writing straight from :class:`_FrameReader` blocked the reader whenever the
    encoder fell behind (a full pipe), which froze the live view and back-pressured
    the capture. Frames are queued (bounded) and dropped when the encoder can't keep
    up — the recorder stamps wall-clock time, so a dropped frame shortens nothing."""

    def __init__(self, fh, max_frames: int = 90):
        super().__init__(daemon=True, name="webcam-record-writer")
        self._fh = fh
        self._q = queue.Queue(maxsize=max_frames)
        self._closed = threading.Event()
        self.dropped = 0
        self.error = None

    def put(self, frame: bytes) -> None:
        if self._closed.is_set():
            return
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        """No more frames: write what's queued, then close stdin (EOF -> ffmpeg finishes)."""
        self._closed.set()

    def run(self):
        while True:
            try:
                frame = self._q.get(timeout=0.2)
            except queue.Empty:
                if self._closed.is_set():
                    break
                continue
            if self.error is None:
                try:
                    self._fh.write(frame)
                except (OSError, ValueError) as exc:  # encoder exited / pipe closed
                    self.error = exc
        try:
            self._fh.close()
        except (OSError, ValueError):
            pass


class _FfmpegFinalizeThread(QThread):
    """Finish a recording off the GUI thread: end the input, then wait for ffmpeg to
    flush the encoder and write the MP4 trailer.

    ffmpeg's interactive ``q`` isn't available here — stdin IS the video input — so
    EOF on stdin is the graceful quit. ``+faststart`` then rewrites the whole file
    to move the index to the front, which takes time proportional to its size, so
    there's no short hard cap (a 10 s kill truncated long recordings): the ceiling
    grows with the file, and progress is reported while waiting."""

    done = pyqtSignal(str, bool)
    progress = pyqtSignal(str)

    BASE_S = 120.0
    SECONDS_PER_MB = 1.0  # tolerates a faststart rewrite as slow as 1 MB/s

    def __init__(self, proc, writer, path: str):
        super().__init__()
        self.proc, self.writer, self.path = proc, writer, path

    def _ceiling(self) -> float:
        try:
            mb = os.path.getsize(self.path) / (1024 * 1024)
        except OSError:
            mb = 0.0
        return self.BASE_S + mb * self.SECONDS_PER_MB

    def _end_input(self):
        if self.writer is not None:
            self.writer.close()  # the writer drains its queue, then closes stdin
            return
        try:
            self.proc.stdin.close()
        except (OSError, ValueError, AttributeError):
            pass

    def run(self):
        self._end_input()
        clean = True
        t0 = time.monotonic()
        reported = 0.0
        while True:
            try:
                self.proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            elapsed = time.monotonic() - t0
            if elapsed > self._ceiling():
                clean = False
                _log.warning("recording encoder didn't finish in %.0f s; killing it", elapsed)
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    _log.warning("couldn't kill the recording encoder: %s", exc)
                break
            if elapsed - reported >= 2.0:
                reported = elapsed
                self.progress.emit(f"Finalizing recording… {int(elapsed)} s")
        if self.writer is not None:
            self.writer.join(timeout=5)
            if self.writer.dropped:
                _log.info("recording dropped %d frame(s) the encoder couldn't keep up with",
                          self.writer.dropped)
        try:
            ok = clean and os.path.getsize(self.path) > 0
        except OSError:
            ok = False
        self.done.emit(self.path, ok)


class _LocalPrep(QThread):
    """Make sure ffmpeg is available (download once if needed) and list local
    cameras — off the UI thread so the app never freezes during the one-time fetch."""

    progress = pyqtSignal(str)
    done = pyqtSignal(str, list)  # ffmpeg path, cameras
    fail = pyqtSignal(str)

    def run(self):
        try:
            ff = ffmpeg_tools.ensure_local_ffmpeg(self.progress.emit)
            self.done.emit(ff, ffmpeg_tools.list_local_cameras(ff))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _RemotePrep(QThread):
    """List cameras on a remote Windows/RDP host over WinRM (NTLM), provisioning
    ffmpeg there (one-time download) if it's missing."""

    progress = pyqtSignal(str)
    done = pyqtSignal(str, list, str)  # remote ffmpeg path, cameras, diag
    fail = pyqtSignal(str)

    def __init__(self, host, login, password):
        super().__init__()
        self.host, self.login, self.password = host, login, password

    def run(self):
        try:
            from . import remote_webcam

            cams, ffmpeg, diag = remote_webcam.list_remote_cameras(
                self.host, self.login, self.password, log=self.progress.emit
            )
            self.done.emit(ffmpeg, cams, diag)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


def _close_socket(sock) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _reap_stream(proc, reader) -> None:
    """Worker: wait for a terminated capture ffmpeg and the reader thread to end."""
    if proc is not None:
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=3.0)
            except (OSError, subprocess.TimeoutExpired) as exc:
                _log.warning("webcam ffmpeg didn't exit: %s", exc)
    if reader is not None:
        reader.join(timeout=3.0)


def _apply_orientation(img, deg, flip):
    """Rotate/mirror a QImage (thread-safe: no widget access)."""
    if img is None:
        return None
    if deg:
        img = img.transformed(QTransform().rotate(deg))
    if flip:
        img = img.mirrored(True, False)
    return img


class _RemoteStart(QThread):
    """Launch ffmpeg on the remote (WinRM) serving MJPEG on a TCP port, then connect
    a local socket to it — all off the UI thread (it takes a moment).

    Ownership of the started stream is handed over under a lock: if the panel
    stopped or closed meanwhile (:meth:`cancel`), the worker tears the remote
    ffmpeg down itself instead of emitting ``ok``; a result emitted just before the
    cancel is returned by :meth:`cancel` for the caller to discard."""

    ok = pyqtSignal(int, object)  # remote pid, connected socket
    fail = pyqtSignal(str)

    def __init__(self, host, login, password, camera, ffmpeg, w, h, fps, port):
        super().__init__()
        self.host, self.login, self.password = host, login, password
        self.camera, self.ffmpeg = camera, ffmpeg
        self.w, self.h, self.fps, self.port = w, h, fps, port
        self._lock = threading.Lock()
        self._cancelled = False
        self._result = None

    def cancel(self):
        """(UI thread) The stream is no longer wanted. Returns a ``(pid, sock)``
        already handed over but not yet taken — the caller must discard it."""
        with self._lock:
            self._cancelled = True
            res, self._result = self._result, None
        return res

    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def take_result(self):
        """(UI thread) Claim the delivered ``(pid, sock)``; None if already claimed."""
        with self._lock:
            res, self._result = self._result, None
        return res

    def _stop_remote(self, pid):
        from . import remote_webcam

        remote_webcam.stop_remote_stream(  # never raises
            self.host, self.login, self.password, pid, stream_port=self.port
        )

    def _deliver(self, pid, sock) -> bool:
        """Hand the stream to the UI — or, if cancelled, tear it down right here on
        the worker (never the UI thread)."""
        with self._lock:
            deliver = not self._cancelled
            if deliver:
                self._result = (pid, sock)
        if deliver:
            self.ok.emit(pid, sock)
            return True
        _close_socket(sock)
        self._stop_remote(pid)
        return False

    def run(self):
        pid = None
        try:
            from . import remote_webcam

            if self.cancelled():
                return
            pid = remote_webcam.start_remote_stream(
                self.host,
                self.login,
                self.password,
                self.camera,
                self.ffmpeg,
                width=self.w,
                height=self.h,
                fps=self.fps,
                stream_port=self.port,
            )
            # ffmpeg opens the camera THEN binds the listen socket, so give it a
            # generous window (camera init can take a few seconds) before giving up
            sock = None
            deadline = time.time() + 22
            last = ""
            while time.time() < deadline and not self.cancelled():
                try:
                    sock = socket.create_connection((self.host, self.port), timeout=4)
                    break
                except OSError as exc:
                    last = str(exc)
                    sock = None
                    time.sleep(0.6)
            if sock is None:
                # ffmpeg never started listening (or we were cancelled) — stop it
                # to free the camera and close the firewall rule
                stop_pid, pid = pid, None
                self._stop_remote(stop_pid)
                if self.cancelled():
                    return
                # ask ffmpeg WHY via a short capture probe, so the error is actionable
                diag = remote_webcam.probe_remote_camera(  # never raises
                    self.host, self.login, self.password, self.camera, self.ffmpeg
                )
                msg = f"couldn't connect to the remote video port {self.host}:{self.port} ({last})."
                if diag:
                    msg += f"\n\nffmpeg on the remote reported:\n{diag[:1200]}"
                raise RuntimeError(msg)
            sock.settimeout(1.0)
            owned, pid = pid, None  # ownership passes to _deliver
            self._deliver(owned, sock)
        except Exception as exc:
            if pid is not None:
                self._stop_remote(pid)
            if not self.cancelled():
                self.fail.emit(f"{type(exc).__name__}: {exc}")


class CameraPanel(QWidget):
    log = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ffmpeg = None  # local ffmpeg path
        self._remote_ffmpeg = None  # remote ffmpeg path
        self._proc = None  # local ffmpeg process
        self._sock = None  # remote video socket
        self._remote_pid = None  # remote ffmpeg pid (to stop)
        self._remote_conn = None  # (host, login, password, port) of that stream
        self.reader = None
        self._rec_proc = None
        self._rec_writer = None
        self._rec_path = None
        self._finalizers = []  # recording finalize threads (several may overlap)
        self._jobs = []  # background stop jobs (remote ffmpeg / firewall cleanup)
        self._prep = None
        self._starter = None
        self._probe = None
        self._closing = False
        # bumped on every start/stop; delayed callbacks from an older stream
        # (diagnostic timers, a late remote start) compare it and bail out
        self._stream_gen = 0
        self._paused = False
        self._dl_dialog = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # --- page toolbar: Start/Scan left, capture settings middle,
        # capture actions right ---
        toolbar = QWidget()
        toolbar.setObjectName("pageToolbar")
        toolbar.setAttribute(Qt.WA_StyledBackground, True)
        from .flowlayout import ToolbarFlowLayout

        # Wraps instead of widening the window when the tab is narrow.
        top = ToolbarFlowLayout(toolbar, hspacing=8, vspacing=6)
        top.setContentsMargins(12, 8, 12, 8)
        self.source = QComboBox()
        self.source.addItem("Local — this PC", "local")
        self.source.addItem("Remote — Windows PC", "remote")
        self.source.setToolTip(
            "Local = this PC (also the camera of an RDP session you are in).\n"
            "Remote = another Windows / RDP machine's camera, over WinRM."
        )
        self.source.currentIndexChanged.connect(self._source_changed)
        self.camera = QComboBox()
        self.camera.setToolTip("Camera")
        self.res = QComboBox()
        self.res.addItems(list(_RES.keys()))
        self.res.setCurrentText("720p (1280×720)")
        self.res.setToolTip("Capture resolution")
        self.fps = QComboBox()
        self.fps.addItems(["15", "20", "25", "30"])
        self.fps.setCurrentText("25")
        self.fps.setToolTip("Frames per second")
        self.view_mode = QComboBox()
        self.view_mode.addItem("Fit (whole frame)", "fit")
        self.view_mode.addItem("Fill (no bars)", "fill")
        self.view_mode.addItem("Stretch", "stretch")
        # Preserve the entire camera picture by default.  Filling the pane is
        # useful for a wall display, but cropping is surprising for a webcam.
        self.view_mode.setCurrentIndex(0)
        self.view_mode.setToolTip(
            "Fill = no black bars, edges may be cropped.\n"
            "Fit = the whole frame, with thin bars where the "
            "shape doesn't match.\nStretch = fill exactly "
            "(slight distortion)."
        )
        self.view_mode.currentIndexChanged.connect(lambda *_: self._repaint_now())
        self.refresh_btn = QPushButton("Scan cameras")
        self.refresh_btn.setProperty("role", "ghost")
        self.refresh_btn.setIcon(icon("search", "accent"))
        self.refresh_btn.setToolTip("Scan this source for available cameras.")
        self.refresh_btn.clicked.connect(self._refresh)
        self.start_btn = QPushButton()
        self.start_btn.setProperty("role", "ok")
        self._show_start_button(False)
        self.start_btn.clicked.connect(self._toggle_start)
        # compact combos: the popups still show every item in full
        for _cb, chars in ((self.source, 19), (self.camera, 14), (self.res, 15),
                           (self.fps, 3), (self.view_mode, 16)):
            _cb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            _cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            _cb.setMinimumContentsLength(chars)
        self.camera.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        fps_label = QLabel("fps")
        fps_label.setObjectName("mutedHint")

        self.snap_btn = QPushButton("Snapshot")
        self.snap_btn.setProperty("role", "ghost")
        self.snap_btn.setIcon(icon("camera", "blue"))
        self.rec_btn = QPushButton("Record")
        self.rec_btn.setProperty("role", "ghost")
        self.rec_btn.setIcon(icon("record", "red"))
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setProperty("role", "ghost")
        self.pause_btn.setIcon(icon("pause", "amber"))
        for b in (self.snap_btn, self.rec_btn, self.pause_btn):
            b.setEnabled(False)
        self.snap_btn.clicked.connect(self._snapshot)
        self.rec_btn.clicked.connect(self._toggle_record)
        self.pause_btn.clicked.connect(self._toggle_pause)

        top.addWidget(self.start_btn)
        top.addWidget(self.refresh_btn)
        top.addSpacing(8)
        top.addWidget(self.source)
        top.addWidget(self.camera, 1)
        top.addWidget(self.res)
        top.addWidget(self.fps)
        top.addWidget(fps_label)
        top.addSpacing(8)
        top.addWidget(self.snap_btn)
        top.addWidget(self.rec_btn)
        top.addWidget(self.pause_btn)
        lay.addWidget(toolbar)

        # --- remote connection row (shown only when Source = Remote) ---
        self.remote_row = QWidget()
        self.remote_row.setObjectName("pageToolbar")
        self.remote_row.setAttribute(Qt.WA_StyledBackground, True)
        rl = ToolbarFlowLayout(self.remote_row, hspacing=8, vspacing=6)
        rl.setContentsMargins(12, 8, 12, 8)
        rl.addWidget(QLabel("RDP host"))
        # remember the last host/user/domain (never the password) so they don't have
        # to be retyped every session
        from . import settings as _s

        _cfg = _s.load()
        self.r_host = QLineEdit(_cfg.get("webcam_remote_host", ""))
        self.r_host.setPlaceholderText("remote machine IP / hostname")
        self.r_user = QLineEdit(_cfg.get("webcam_remote_user", ""))
        self.r_user.setPlaceholderText("user")
        self.r_domain = QLineEdit(_cfg.get("webcam_remote_domain", ""))
        self.r_domain.setPlaceholderText("domain (optional)")
        # password comes from the OS credential vault (keyring), not settings.json
        self.r_pass = QLineEdit(_s.webcam_remote_password())
        self.r_pass.setEchoMode(QLineEdit.Password)
        self.r_pass.setPlaceholderText("password")
        rl.addWidget(self.r_host, 2)
        rl.addSpacing(4)
        rl.addWidget(QLabel("User"))
        rl.addWidget(self.r_user, 1)
        rl.addSpacing(4)
        rl.addWidget(QLabel("Domain"))
        rl.addWidget(self.r_domain, 1)
        rl.addSpacing(4)
        rl.addWidget(QLabel("Password"))
        rl.addWidget(self.r_pass, 1)
        self.remote_row.setVisible(False)
        lay.addWidget(self.remote_row)

        body = QVBoxLayout()
        body.setContentsMargins(12, 12, 12, 8)
        body.setSpacing(8)
        lay.addLayout(body, 1)

        # --- the view (fills the tab) ---
        self.view = _VideoView(
            "Pick a source, Scan, then Start camera.\n\nLocal works over RDP "
            "(this session's camera); Remote drives another Windows "
            "machine's camera over WinRM."
        )
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.view.setMinimumSize(120, 90)  # small min so it resizes in a split
        self.view.setObjectName("cameraView")  # terminal-dark well (theme.py)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._view_menu)
        body.addWidget(self.view, 1)

        # --- view adjustments + status (under the video) ---
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(QLabel("View"))
        row.addWidget(self.view_mode)
        row.addSpacing(4)
        row.addWidget(QLabel("Rotate"))
        self.rotate = QComboBox()
        for label, deg in (("0°", 0), ("90°", 90), ("180°", 180), ("270°", 270)):
            self.rotate.addItem(label, deg)
        self.rotate.setToolTip(
            "Rotate the view (and snapshots) — useful for a camera mounted sideways or upside-down."
        )
        self.rotate.currentIndexChanged.connect(lambda *_: self._repaint_now())
        row.addWidget(self.rotate)
        self.flip = QCheckBox("Flip")
        self.flip.setToolTip("Flip the image horizontally (mirror, selfie-style).")
        self.flip.stateChanged.connect(lambda *_: self._repaint_now())
        row.addWidget(self.flip)
        self.fps_lbl = QLabel("")
        self.fps_lbl.setObjectName("mutedHint")
        row.addWidget(self.fps_lbl)
        row.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("cameraStatus")
        row.addWidget(self.status)
        self._set_status("Pick a source, Scan, then Start camera.", "idle")
        self.link = QLabel("")
        self.link.setOpenExternalLinks(True)
        self.link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        row.addWidget(self.link)
        body.addLayout(row)

        self._last_paint = 0
        self._fps_count = 0
        self._fps_t0 = time.time()
        self._requested_fps = 0
        self._timer = QTimer(self)
        self._timer.setInterval(16)  # 60 Hz presents a 30-fps camera frame-for-frame
        self._timer.timeout.connect(self._tick)
        # started in _after_start, stopped in _stop_stream (no idle 60 Hz wakeups)
        self._auto_scanned = False
        self._scan_quiet = False
        self._local_error_tail = []
        self._local_native_retry = False

        # Webcam discovery is intentionally automatic.  It happens in the
        # existing worker, so opening TurboADB stays responsive, but the camera
        # chooser is ready when the user first opens this page.
        QTimer.singleShot(150, self._auto_scan)

    def showEvent(self, event):
        super().showEvent(event)
        self._auto_scan()

    def _auto_scan(self):
        """Start one quiet local scan, whether this is a standalone or nested tab."""
        if self._auto_scanned:
            return
        self._auto_scanned = True
        self._refresh(quiet=True)

    def _show_start_button(self, running: bool) -> None:
        """Start camera (play) while idle, Stop camera (stop) while streaming."""
        self.start_btn.setText("Stop camera" if running else "Start camera")
        self.start_btn.setIcon(icon("stop" if running else "play", "on-accent"))

    # ---- status toast (coloured pill) ----
    def _set_status(self, text, kind="idle"):
        # idle / info (working) / ok (viewing) / rec / warn (attention) / error;
        # the chip colours are QLabel#cameraStatus[state=…] rules in theme.py
        state = kind if kind in theme.status_colors() else "idle"
        if self.status.property("state") != state:
            self.status.setProperty("state", state)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)
        self.status.setText(text)

    # ---- one-time ffmpeg setup popup (local download) ----
    def _on_progress(self, msg):
        self._set_status(msg, "info")
        low = msg.lower()
        if any(k in low for k in ("download", "extract")):
            self._ensure_dl_dialog()
            if self._dl_dialog is not None:
                self._dl_dialog.setLabelText(msg)
                import re

                m = re.search(r"\((\d+)%\)", msg)
                if m:
                    self._dl_dialog.setRange(0, 100)
                    self._dl_dialog.setValue(int(m.group(1)))
                else:
                    self._dl_dialog.setRange(0, 0)

    def _ensure_dl_dialog(self):
        if self._dl_dialog is not None:
            return
        dlg = QProgressDialog("Setting up ffmpeg (one-time, ~160 MB)…", None, 0, 100, self)
        dlg.setWindowTitle("TurboADB — camera setup")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setCancelButton(None)
        dlg.setValue(0)
        self._dl_dialog = dlg
        dlg.show()

    def _close_dl_dialog(self):
        if self._dl_dialog is not None:
            try:
                self._dl_dialog.close()
            except Exception:
                pass
            self._dl_dialog = None

    def _repaint_now(self):
        self._last_paint = -1

    def _res(self):
        return _RES.get(self.res.currentText(), (1280, 720))

    def _is_remote(self):
        return self.source.currentData() == "remote"

    def _remote_login(self):
        user = self.r_user.text().strip()
        dom = self.r_domain.text().strip()
        return f"{dom}\\{user}" if dom else user

    def _source_changed(self, *_):
        self._stop_stream()
        self.remote_row.setVisible(self._is_remote())
        self.camera.clear()
        self.start_btn.setEnabled(False)
        if self._is_remote():
            self._set_status("Enter the RDP machine's details, then Scan cameras.", "idle")
        else:
            self._set_status("Scan for a camera, then Start camera.", "idle")

    # ---- enumerate ----
    def _refresh(self, *, quiet=False):
        if thread_running(self._prep):
            return
        self._stop_stream()
        self.camera.clear()
        self.refresh_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self._scan_quiet = bool(quiet)
        if self._is_remote():
            host = self.r_host.text().strip()
            if not host or not self.r_user.text().strip():
                self._set_status("Enter the RDP host + user first.", "warn")
                self.refresh_btn.setEnabled(True)
                return
            self._set_status(f"Connecting to {host} over WinRM…", "info")
            self._prep = _RemotePrep(host, self._remote_login(), self.r_pass.text())
            self._prep.progress.connect(lambda m: self._set_status(m, "info"))
            self._prep.done.connect(self._remote_ready)
            self._prep.fail.connect(self._prep_fail)
            self._prep.start()
        else:
            self._set_status("Finding cameras…", "info")
            self._prep = _LocalPrep()
            self._prep.progress.connect(self._on_progress)
            self._prep.done.connect(self._local_ready)
            self._prep.fail.connect(self._prep_fail)
            self._prep.start()

    def _local_ready(self, ffmpeg, cams):
        self._close_dl_dialog()
        self.refresh_btn.setEnabled(True)
        self._ffmpeg = ffmpeg
        self._fill(cams, quiet=self._scan_quiet)

    def _remote_ready(self, ffmpeg, cams, diag):
        self.refresh_btn.setEnabled(True)
        self._remote_ffmpeg = ffmpeg
        self._save_remote_details()  # remember host/user/domain (not password)
        self._fill(cams, diag, quiet=self._scan_quiet)

    def _save_remote_details(self):
        try:
            from . import settings as _s

            _s.update(
                {
                    "webcam_remote_host": self.r_host.text().strip(),
                    "webcam_remote_user": self.r_user.text().strip(),
                    "webcam_remote_domain": self.r_domain.text().strip(),
                }
            )
            # password -> OS credential vault (never settings.json)
            _s.set_webcam_remote_password(self.r_pass.text())
        except Exception as exc:  # disk / keyring trouble mustn't break the scan result
            _log.warning("couldn't remember the remote webcam details: %s", exc)

    def _fill(self, cams, diag="", *, quiet=False):
        self.camera.clear()
        for c in cams:
            self.camera.addItem(c)
        if cams:
            self.camera.setCurrentIndex(0)
            self._set_status(f"{len(cams)} camera(s) — Start camera to view.", "ok")
            self.start_btn.setEnabled(True)
        elif self._is_remote():
            self._set_status("No cameras found on the remote machine.", "warn")
            if not quiet:
                QMessageBox.information(
                    self,
                    "No camera on the remote machine",
                    "ffmpeg didn't report a camera there. Its raw device listing is "
                    "below — check the camera is attached, Windows camera privacy allows "
                    "desktop apps, and nothing else is using it.\n\n" + (diag or "")[:3500],
                )
        else:
            self._set_status("No cameras found.", "warn")
            if not quiet:
                QMessageBox.information(
                    self,
                    "No camera found",
                    "ffmpeg didn't report a camera.\n\n"
                    "• Over Remote Desktop, enable camera redirection in the RDP client "
                    "(Local Resources → More… → Cameras) and reconnect.\n"
                    "• Turn ON Windows camera privacy ('Let desktop apps access your "
                    "camera').\n"
                    "• Make sure nothing else is using the camera.",
                )

    def _prep_fail(self, msg):
        self._close_dl_dialog()
        self.refresh_btn.setEnabled(True)
        self._set_status("Couldn't list cameras.", "error")
        if not self._scan_quiet:
            QMessageBox.warning(self, "Camera", f"Couldn't list cameras:\n\n{msg}")

    # ---- start / stop ----
    def _toggle_start(self):
        if self.reader is not None:
            self._stop_stream()
            self._show_start_button(False)
            return
        cam = self.camera.currentText().strip()
        if not cam:
            return
        w, h = self._res()
        fps = int(self.fps.currentText())
        self._requested_fps = fps
        if self._is_remote():
            self._start_remote(cam, w, h, fps)
        else:
            native = cam in _NATIVE_FPS_CAMERAS
            self._local_native_retry = native  # the native mode is already the fallback
            self._start_local(cam, w, h, fps, native_fps=native)

    def _start_local(self, cam, w, h, fps, *, native_fps=False):
        if not self._ffmpeg:
            self._refresh()
            return
        try:
            args = ffmpeg_tools.local_capture_args(
                self._ffmpeg,
                cam,
                width=w,
                height=h,
                fps=None if native_fps else fps,
            )
            # this stream's own list: a previous ffmpeg's stderr thread can't leak into it
            tail = self._local_error_tail = []
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=_NO_WINDOW,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Camera", f"Couldn't start the camera:\n\n{exc}")
            return
        self._stream_gen += 1

        def drain_stderr(proc, tail=tail):
            try:
                for raw in iter(proc.stderr.readline, b""):
                    text = raw.decode("utf-8", "replace").strip()
                    if text:
                        tail.append(text)
                        del tail[:-12]
            except (OSError, ValueError):  # pipe closed while stopping
                pass

        threading.Thread(target=drain_stderr, args=(self._proc,), daemon=True).start()

        def read_fn(n, p=self._proc):
            if p.poll() is not None:
                return None
            try:
                return os.read(p.stdout.fileno(), n)
            except (OSError, ValueError):
                return None

        self.reader = _FrameReader(read_fn)
        self.reader.start()
        self._after_start(cam)

    def _start_remote(self, cam, w, h, fps):
        if not self._remote_ffmpeg:
            self._refresh()
            return
        from . import remote_webcam

        self._cancel_starter()
        self.start_btn.setEnabled(False)
        self._set_status(f"Starting {cam} on the remote machine…", "info")
        self._stream_gen += 1
        gen = self._stream_gen
        starter = _RemoteStart(
            self.r_host.text().strip(),
            self._remote_login(),
            self.r_pass.text(),
            cam,
            self._remote_ffmpeg,
            w,
            h,
            fps,
            remote_webcam.DEFAULT_STREAM_PORT,
        )
        # the result is claimed via take_result(), so a stale/late one is discarded
        starter.ok.connect(lambda _pid, _sock, s=starter, c=cam, g=gen: self._remote_started(s, c, g))
        starter.fail.connect(lambda msg, s=starter: self._remote_start_fail(s, msg))
        park_thread(starter)  # never destroyed while running, even if replaced
        self._starter = starter
        starter.start()

    def _cancel_starter(self) -> bool:
        """Abandon a pending remote start; True if one was pending."""
        starter, self._starter = self._starter, None
        if starter is None:
            return False
        disconnect_signals(starter, ("ok", "fail"))
        res = starter.cancel()
        if res is not None:  # emitted just before the cancel, never claimed
            self._discard_remote(starter, *res)
        return True

    def _discard_remote(self, starter, pid, sock):
        """Tear down a remote stream nobody will watch: close the socket and stop
        the remote ffmpeg + firewall rule on a parked worker (WinRM is slow)."""
        _close_socket(sock)
        self._run_remote_stop(starter.host, starter.login, starter.password, pid, starter.port)

    def _run_remote_stop(self, host, login, pw, pid, port):
        def stop():
            from . import remote_webcam

            remote_webcam.stop_remote_stream(host, login, pw, pid, stream_port=port)

        run_job(self._jobs, stop)

    def _later(self, ms, fn):
        """Run *fn* after *ms* — only if the same stream is still current and the
        panel is open. The timer is a child, so it dies with the panel."""
        gen = self._stream_gen
        timer = QTimer(self)
        timer.setSingleShot(True)

        def fire():
            timer.deleteLater()
            self._run_if_current(gen, fn)

        timer.timeout.connect(fire)
        timer.start(ms)
        return timer

    def _run_if_current(self, gen, fn) -> bool:
        """Call *fn* only if stream generation *gen* is still current."""
        if self._closing or gen != self._stream_gen:
            return False
        fn()
        return True

    def _remote_started(self, starter, cam, gen):
        res = starter.take_result()
        if res is None:  # already discarded by a stop / close
            return
        pid, sock = res
        if self._closing or starter is not self._starter or gen != self._stream_gen:
            self._discard_remote(starter, pid, sock)
            return
        self._starter = None
        self.start_btn.setEnabled(True)
        self._remote_pid = pid
        self._remote_conn = (starter.host, starter.login, starter.password, starter.port)
        self._sock = sock

        def read_fn(n, s=sock):
            try:
                d = s.recv(n)
                return d if d else None  # b"" from recv = peer closed
            except socket.timeout:
                return b""
            except Exception:
                return None

        self.reader = _FrameReader(read_fn)
        self.reader.start()
        self._after_start(cam)
        self._later(8000, lambda c=cam: self._check_no_video(c))

    def _remote_start_fail(self, starter, msg):
        if self._closing or starter is not self._starter:
            return  # stopped / replaced meanwhile: nobody is waiting for this
        self._starter = None
        self.start_btn.setEnabled(True)
        self._set_status("Couldn't start the remote camera", "error")
        QMessageBox.warning(
            self,
            "Remote camera",
            f"Couldn't start the remote camera:\n\n{msg}\n\n"
            "Check WinRM is on (Enable-PSRemoting -Force), your "
            "account is a local admin, and ffmpeg is installed there.",
        )

    def _after_start(self, cam):
        self._show_start_button(True)
        for b in (self.snap_btn, self.rec_btn, self.pause_btn):
            b.setEnabled(True)
        # "Viewing" is only claimed once a frame is actually on screen (_tick).
        self._viewing_cam = cam
        self._viewing_announced = False
        self._stream_started_at = time.monotonic()
        self._set_status(f"Starting {cam}…", "info")
        pixmap = self.view.pixmap()
        if pixmap is None or pixmap.isNull():
            self.view.setText("Starting the camera…")
        self._fps_count = 0
        self._fps_t0 = time.time()
        self._last_paint = 0
        self._timer.start()
        if not self._is_remote():
            # A DirectShow failure (e.g. an unsupported frame rate) exits within
            # a few hundred ms: notice it quickly so the retry is seamless.
            self._later(_LOCAL_START_POLL_MS, lambda c=cam: self._check_no_video(c))

    def _check_no_video(self, cam):
        if self._closing or self.reader is None or self.reader.frames > 0:
            return
        if self._is_remote():
            if self._remote_conn is not None:
                host, login, pw, _port = self._remote_conn
            else:
                host, login, pw = (self.r_host.text().strip(), self._remote_login(), self.r_pass.text())
            ff = self._remote_ffmpeg
            self._stop_stream()  # release the camera before probing
            self._set_status("Diagnosing the remote camera…", "info")  # after stop
            gen = self._stream_gen
            probe = _RemoteProbe(host, login, pw, cam, ff)
            probe.done.connect(lambda out, c=cam, g=gen: self._remote_no_video(c, out, g))
            park_thread(probe)  # an older probe may still be running
            self._probe = probe
            probe.start()
        else:
            # A live DirectShow process can take a few seconds to warm up on a
            # USB/RDP camera. Only fail immediately when ffmpeg has exited;
            # otherwise grant it the original longer startup window.
            alive = self._proc is not None and self._proc.poll() is None
            elapsed = time.monotonic() - getattr(self, "_stream_started_at", 0.0)
            if alive and elapsed < _LOCAL_NO_VIDEO_TIMEOUT_S:
                # still warming up: USB and RDP-redirected cameras can take a while
                self._later(_LOCAL_START_POLL_MS, lambda c=cam: self._check_no_video(c))
                return
            detail = "\n".join(self._local_error_tail[-8:]).strip()
            # Some cameras do not advertise the requested frame rate.  Retry
            # once with their native mode, but only for a mode-negotiation
            # failure—not for privacy, unplugged, or device-in-use failures.
            mode_error = any(
                text in detail.lower()
                for text in ("frame rate", "framerate", "video options", "could not set")
            )
            if not alive and mode_error and not self._local_native_retry:
                width, height = self._res()
                requested = self._requested_fps
                # Restart quietly: this is still part of starting, not a user
                # stop, so the view must never flash "Camera stopped".
                self._stop_stream(announce=False)
                self._local_native_retry = True
                _NATIVE_FPS_CAMERAS.add(cam)
                self.log.emit(f"[INFO] {cam} does not support {requested} fps — using its native mode.")
                self._requested_fps = requested
                self._start_local(cam, width, height, requested, native_fps=True)
                return
            self._stop_stream(announce=False)
            self.view.setText("No video from the camera.")
            self._set_status("No video from the camera", "error")
            cause = f"\n\nffmpeg reported:\n{detail}" if detail else ""
            QMessageBox.information(
                self,
                "Camera — no video",
                f"The camera “{cam}” opened but produced no video.\n\n"
                "It's most likely in use by another app, blocked by Windows camera "
                "privacy, or — over RDP — not redirected into this session. Close "
                "other apps using it, check those settings, then Start again."
                + cause,
            )

    def _remote_no_video(self, cam, detail, gen=None):
        if self._closing or (gen is not None and gen != self._stream_gen):
            return  # a new stream started (or the panel closed) since the probe began
        self._set_status("Remote camera: no video", "error")
        QMessageBox.information(
            self,
            "Remote camera — no video",
            f"ffmpeg on the remote machine produced no video from “{cam}”. A short "
            f"diagnostic capture was run; its output is below.\n\n{detail}\n\n"
            "Most likely the camera is in use, blocked by Windows camera privacy, or "
            "is a camera redirected into someone's RDP session (only visible inside "
            "that session — a physical USB camera on the machine works headlessly).",
        )

    # ---- orientation (shared by view, snapshot, copy) ----
    def _orientation(self):
        """(rotation degrees, mirrored) — snapshot this before handing work to a thread."""
        return (self.rotate.currentData() or 0), self.flip.isChecked()

    def _orient(self, img):
        return _apply_orientation(img, *self._orientation())

    # ---- display (cheap: just scale an already-decoded QImage) ----
    def _tick(self):
        if self._paused or self.reader is None:
            return
        n = self.reader.frames
        if n == self._last_paint:  # no new frame -> don't rescale again
            return
        self._last_paint = n
        img = self._orient(self.reader.latest_image())
        if img is None:
            return
        target = self.view.size()
        mode = self.view_mode.currentData()
        # Smooth scaling is expensive at 720p/1080p and was the other common
        # reason a selected 30 fps was painted around 10 fps.  Fast scaling is
        # visually equivalent for motion at high target rates; keep Smooth for
        # deliberately low-rate preview where detail matters more.
        sm = Qt.FastTransformation if self._requested_fps >= 25 else Qt.SmoothTransformation
        if mode == "stretch":
            pm = QPixmap.fromImage(img).scaled(target, Qt.IgnoreAspectRatio, sm)
        elif mode == "fit":
            pm = QPixmap.fromImage(img).scaled(target, Qt.KeepAspectRatio, sm)
        else:
            pm = QPixmap.fromImage(img).scaled(target, Qt.KeepAspectRatioByExpanding, sm)
            if pm.width() > target.width() or pm.height() > target.height():
                x = max(0, (pm.width() - target.width()) // 2)
                y = max(0, (pm.height() - target.height()) // 2)
                pm = pm.copy(x, y, target.width(), target.height())
        self.view.setPixmap(pm)
        if not getattr(self, "_viewing_announced", True):
            self._viewing_announced = True
            self._set_status(f"● Viewing {self._viewing_cam}", "ok")
        self._fps_count += 1
        now = time.time()
        if now - self._fps_t0 >= 1.0:
            self.fps_lbl.setText(f"{self._fps_count} fps")
            self._fps_count = 0
            self._fps_t0 = now

    # ---- context menu / snapshot / copy ----
    def _view_menu(self, pos):
        m = QMenu(self)
        a_copy = m.addAction("Copy image to clipboard")
        a_snap = m.addAction("Save snapshot…")
        has = self.reader is not None and self.reader.latest_image() is not None
        a_copy.setEnabled(has)
        a_snap.setEnabled(has)
        chosen = m.exec_(self.view.mapToGlobal(pos))
        if chosen == a_copy:
            self._copy_frame()
        elif chosen == a_snap:
            self._snapshot()

    def _copy_frame(self):
        from PyQt5.QtWidgets import QApplication

        img = self._orient(self.reader.latest_image() if self.reader else None)
        if img is None:
            self._set_status("No frame to copy yet.", "warn")
            return
        QApplication.clipboard().setImage(img)
        self._set_status("Frame copied to clipboard ✓", "ok")
        self.log.emit("[OK] camera frame copied to clipboard")

    def _snapshot(self):
        raw = self.reader.latest_raw() if self.reader else None
        if not raw:
            return
        from .fileutil import save_output

        deg, flip = self._orientation()  # widget state, read here on the UI thread

        def write(path):  # runs on a worker: QImage only, no widgets
            img = _apply_orientation(QImage.fromData(raw, "JPG"), deg, flip)
            if img is not None and not img.isNull():
                if not img.save(path):
                    raise OSError("couldn't write the image (unsupported file type?)")
            else:  # undecodable frame: keep the camera's own JPEG bytes
                with open(path, "wb") as fh:
                    fh.write(raw)

        save_output(
            self,
            "Save snapshot",
            f"webcam-{int(time.time())}.jpg",
            write,
            what="snapshot",
            file_filter="Images (*.jpg *.png)",
            suffix=".jpg",
            on_saved=self._snapshot_saved,
            on_failed=lambda msg: self.log.emit(f"[ERROR] snapshot: {msg}"),
        )

    def _snapshot_saved(self, path):
        self._show_link(path)
        self.log.emit(f"[OK] snapshot saved: {path}")

    def _toggle_record(self):
        if self._rec_proc is not None:
            self._stop_record()
            return
        if self.reader is None:
            return
        ff = ffmpeg_tools.cached_ffmpeg()
        if not ff:
            QMessageBox.warning(self, "Recording", "ffmpeg isn't ready yet.")
            return
        from .fileutil import ask_save_path

        path = ask_save_path(
            self,
            "Record video to",
            f"webcam-{int(time.time())}.mp4",
            "Video (*.mp4)",
            ".mp4",
        )
        if not path or self.reader is None or self._rec_proc is not None:
            return  # cancelled, or the stream stopped while the dialog was open
        try:
            # Feed the clean MJPEG frames to ffmpeg and RE-ENCODE to H.264 MP4.
            # -use_wallclock_as_timestamps stamps each frame as it arrives, so the
            # recording runs at real speed even if the camera can't sustain the fps.
            self._rec_proc = subprocess.Popen(
                [
                    ff,
                    "-y",
                    "-f",
                    "mjpeg",
                    "-use_wallclock_as_timestamps",
                    "1",
                    "-i",
                    "-",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-pix_fmt",
                    "yuv420p",
                    "-fps_mode",
                    "vfr",
                    "-movflags",
                    "+faststart",
                    path,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
            self._rec_path = path
            writer = _RecordWriter(self._rec_proc.stdin)
            writer.start()
            self._rec_writer = writer
            self.reader.set_recorder(writer.put)
            self.rec_btn.setText("Stop recording")
            self.rec_btn.setIcon(icon("stop", "red"))
            self._set_status("● Recording…", "rec")
            self.log.emit(f"[OK] recording to {path}")
        except (OSError, ValueError) as exc:
            self._rec_proc = None
            self.log.emit(f"[ERROR] record: {exc}")

    def _stop_record(self):
        if self.reader:
            self.reader.set_recorder(None)
        proc, self._rec_proc = self._rec_proc, None
        writer, self._rec_writer = self._rec_writer, None
        path, self._rec_path = self._rec_path, None
        if proc is None:  # also makes a second stop a no-op (no double finalize)
            return
        if path:
            self.rec_btn.setEnabled(False)
            self.rec_btn.setText("Finalizing…")
            self.rec_btn.setIcon(icon("clock"))
            self._set_status("Finalizing recording…", "rec")
        # ending the input and waiting for the MP4 trailer happen on the worker
        fin = _FfmpegFinalizeThread(proc, writer, path or "")
        fin.done.connect(self._record_finalized)
        fin.progress.connect(self._record_progress)
        self._finalizers[:] = [f for f in self._finalizers if thread_running(f)]
        self._finalizers.append(fin)
        park_thread(fin)  # stays alive until finished — never dropped mid-run
        fin.start()

    def _record_progress(self, msg):
        if self._rec_proc is None:  # don't overwrite a newer "Recording…" status
            self._set_status(msg, "rec")

    def _record_finalized(self, path: str, ok: bool):
        if self._rec_proc is None:  # a newer recording owns the button otherwise
            self.rec_btn.setEnabled(self.reader is not None)
            self.rec_btn.setText("Record")
            self.rec_btn.setIcon(icon("record", "red"))
        if not path:
            return
        if ok:
            self._show_link(path)
            self.log.emit(f"[OK] recording saved: {path}")
            if self.reader is not None:
                self._set_status("Recording saved ✓ — still viewing", "ok")
            return
        self.log.emit(
            f"[ERROR] the recording saved no data → {path} "
            "(the ffmpeg encoder did not finish cleanly; if a custom/"
            "older ffmpeg is set in Settings → Tools, clear it so the "
            "downloaded one is used)"
        )
        self._set_status("Recording failed (empty file)", "error")

    def _toggle_pause(self):
        self._paused = not self._paused
        self.pause_btn.setText("Resume" if self._paused else "Pause")
        self.pause_btn.setIcon(icon("play", "green") if self._paused else icon("pause", "amber"))

    def _show_link(self, path):
        folder = os.path.dirname(os.path.abspath(path)).replace("\\", "/")
        self.link.setText(f'Saved — <a href="file:///{folder}">open folder</a>')

    # ---- teardown ----
    def _stop_stream(self, *, announce=True):
        """Stop viewing. Only signals and non-blocking calls run here; reaping the
        capture process and the remote WinRM cleanup happen on workers.

        ``announce=False`` is for internal restarts and failures whose caller
        sets the next view/status itself, so no "Camera stopped" text flashes."""
        self._stream_gen += 1  # invalidates pending diagnostic timers / starts
        self._timer.stop()
        starting = self._cancel_starter()
        was_viewing = any(
            value is not None
            for value in (self.reader, self._proc, self._sock, self._remote_pid)
        )
        self._stop_record()
        reader, self.reader = self.reader, None
        proc, self._proc = self._proc, None
        sock, self._sock = self._sock, None
        if reader is not None:
            reader.stop()
        if proc is not None:
            try:
                proc.terminate()
            except OSError:  # already exited
                pass
        _close_socket(sock)  # also unblocks the reader's recv()
        if proc is not None or reader is not None:
            threading.Thread(
                target=_reap_stream, args=(proc, reader), name="webcam-reap", daemon=True
            ).start()
        if self._remote_pid is not None:
            # kill the remote ffmpeg + firewall rule over WinRM, off the UI thread
            conn = self._remote_conn or (
                self.r_host.text().strip(), self._remote_login(), self.r_pass.text(), None
            )
            pid, self._remote_pid, self._remote_conn = self._remote_pid, None, None
            host, login, pw, port = conn
            if port is None:
                from . import remote_webcam

                port = remote_webcam.DEFAULT_STREAM_PORT
            self._run_remote_stop(host, login, pw, pid, port)
        if starting and not self._closing:
            self.start_btn.setEnabled(self.camera.count() > 0)
        for b in (self.snap_btn, self.rec_btn, self.pause_btn):
            b.setEnabled(False)
        self._paused = False
        self.pause_btn.setText("Pause")
        self.pause_btn.setIcon(icon("pause", "amber"))
        self._show_start_button(False)
        self.fps_lbl.setText("")
        self._requested_fps = 0
        self._local_native_retry = False
        if not announce:
            return
        self.view.clear()
        if was_viewing:
            self.view.setText("Camera stopped — select Start camera to view again.")
            self._set_status("Stopped.", "idle")
        else:
            # Automatic discovery calls this before the first stream exists;
            # calling that state "stopped" made the Camera tab look broken.
            self.view.setText("Select Start camera to view.")
            self._set_status("Ready to start the camera.", "idle")

    def close_panel(self):
        """Tear down without blocking: running workers are detached and parked.
        A recording still finalizes, and a remote start that completes later is
        torn down by its own worker (see _RemoteStart)."""
        if self._closing:
            return
        self._closing = True
        self._close_dl_dialog()
        self._timer.stop()
        self._stop_stream()  # also cancels a pending remote start
        for thread, names in ((self._prep, ("progress", "done", "fail")), (self._probe, ("done",))):
            disconnect_signals(thread, names)
            park_thread(thread)
        self._prep = self._probe = None
        close_jobs(self._finalizers, ("done", "progress"))
        close_jobs(self._jobs)

    # the main window closes tabs via close_session(); the device sub-tab uses
    # close_panel() — support both so a standalone Webcam tab tears down cleanly.
    def close_session(self):
        self.close_panel()


class _RemoteProbe(QThread):
    """Run a short verbose ffmpeg capture on the remote (WinRM) to explain a 'no
    video' case."""

    done = pyqtSignal(str)

    def __init__(self, host, login, password, camera, ffmpeg):
        super().__init__()
        self.host, self.login, self.password = host, login, password
        self.camera, self.ffmpeg = camera, ffmpeg

    def run(self):
        try:
            from . import remote_webcam

            out = remote_webcam.probe_remote_camera(
                self.host, self.login, self.password, self.camera, self.ffmpeg
            )
        except Exception as exc:
            out = f"{type(exc).__name__}: {exc}"
        self.done.emit(out or "ffmpeg produced no output.")
