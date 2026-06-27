"""Webcam panel — view a camera on THIS machine (Local, which also covers running
TurboADB inside an RDP session), or on another Windows / RDP machine over WinRM
(Remote — no SSH needed). Pick a source + camera, watch it; snapshot, record,
pause, rotate / flip. Frames are decoded off the UI thread so the view stays
smooth, and the view fills the tab. Runs on its own threads — never touches adb /
shell / logcat work.
"""

from __future__ import annotations

import os
import time
import socket
import threading
import subprocess

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QImage, QPixmap, QTransform
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QComboBox, QCheckBox, QLineEdit, QFileDialog,
                             QMessageBox, QSizePolicy, QProgressDialog, QMenu)

from . import ffmpeg_tools
from . import theme

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_RES = {"480p (640×480)": (640, 480), "540p (960×540)": (960, 540),
        "720p (1280×720)": (1280, 720), "1080p (1920×1080)": (1920, 1080)}


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

    def run(self):
        while self._alive:
            try:
                data = self._read(131072)
            except Exception:
                break
            if data is None:
                break
            if not data:
                time.sleep(0.005); continue
            self._buf.extend(data)
            self._extract()

    def _extract(self):
        """Pull every COMPLETE JPEG out of the buffer in order. Each whole frame is
        tee'd to the recorder (clean SOI…EOI boundaries -> a valid MJPEG stream), and
        the LAST one is decoded for display."""
        buf = self._buf
        rec = self._record_fh
        last = None
        while True:
            start = buf.find(b"\xff\xd8")
            if start == -1:
                if len(buf) > 8 * 1024 * 1024:     # no SOI in a huge buffer -> trim
                    del buf[:-1024 * 1024]
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end == -1:
                if start:                          # drop junk before the next SOI
                    del buf[:start]
                break
            frame = bytes(buf[start:end + 2])
            del buf[:end + 2]
            if rec is not None:
                try:
                    rec.write(frame)
                except Exception:
                    pass
            last = frame
        if last is None:
            return
        img = QImage.fromData(last, "JPG")          # decode here, not on the UI thread
        if img.isNull():
            return
        with self._lock:
            self._img = img
            self._raw = last
            self.frames += 1

    def latest_image(self):
        with self._lock:
            return self._img

    def latest_raw(self):
        with self._lock:
            return self._raw

    def set_recorder(self, fh):
        self._record_fh = fh

    def stop(self):
        self._alive = False


class _LocalPrep(QThread):
    """Make sure ffmpeg is available (download once if needed) and list local
    cameras — off the UI thread so the app never freezes during the one-time fetch."""
    progress = pyqtSignal(str)
    done = pyqtSignal(str, list)        # ffmpeg path, cameras
    fail = pyqtSignal(str)

    def run(self):
        try:
            ff = ffmpeg_tools.ensure_local_ffmpeg(self.progress.emit)
            self.done.emit(ff, ffmpeg_tools.list_local_cameras(ff))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _RemotePrep(QThread):
    """List cameras on a remote Windows/RDP host over WinRM (NTLM)."""
    done = pyqtSignal(str, list, str)   # remote ffmpeg path, cameras, diag
    fail = pyqtSignal(str)

    def __init__(self, host, login, password):
        super().__init__()
        self.host, self.login, self.password = host, login, password

    def run(self):
        try:
            from . import remote_webcam
            cams, ffmpeg, diag = remote_webcam.list_remote_cameras(
                self.host, self.login, self.password)
            self.done.emit(ffmpeg, cams, diag)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _RemoteStart(QThread):
    """Launch ffmpeg on the remote (WinRM) serving MJPEG on a TCP port, then connect
    a local socket to it — all off the UI thread (it takes a moment)."""
    ok = pyqtSignal(int, object)        # remote pid, connected socket
    fail = pyqtSignal(str)

    def __init__(self, host, login, password, camera, ffmpeg, w, h, fps, port):
        super().__init__()
        self.host, self.login, self.password = host, login, password
        self.camera, self.ffmpeg = camera, ffmpeg
        self.w, self.h, self.fps, self.port = w, h, fps, port

    def run(self):
        pid = None
        try:
            from . import remote_webcam
            pid = remote_webcam.start_remote_stream(
                self.host, self.login, self.password, self.camera, self.ffmpeg,
                width=self.w, height=self.h, fps=self.fps, stream_port=self.port)
            # ffmpeg with ?listen=1 needs a moment to bind before we connect
            sock = None
            deadline = time.time() + 12
            last = ""
            while time.time() < deadline:
                try:
                    sock = socket.create_connection((self.host, self.port), timeout=4)
                    break
                except Exception as exc:
                    last = str(exc); sock = None; time.sleep(0.6)
            if sock is None:
                raise RuntimeError(f"couldn't connect to the remote video port "
                                   f"{self.host}:{self.port} ({last})")
            sock.settimeout(1.0)
            self.ok.emit(pid, sock)
        except Exception as exc:
            if pid is not None:
                try:
                    from . import remote_webcam
                    remote_webcam.stop_remote_stream(self.host, self.login,
                                                     self.password, pid)
                except Exception:
                    pass
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class CameraPanel(QWidget):
    log = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ffmpeg = None              # local ffmpeg path
        self._remote_ffmpeg = None       # remote ffmpeg path
        self._proc = None                # local ffmpeg process
        self._sock = None                # remote video socket
        self._remote_pid = None          # remote ffmpeg pid (to stop)
        self.reader = None
        self._rec_proc = None
        self._rec_path = None
        self._paused = False
        self._dl_dialog = None

        lay = QVBoxLayout(self)

        # --- source + camera row ---
        top = QHBoxLayout()
        top.addWidget(QLabel("Source:"))
        self.source = QComboBox()
        self.source.addItem("Local (this PC / RDP session)", "local")
        self.source.addItem("Remote (RDP / Windows machine)", "remote")
        self.source.currentIndexChanged.connect(self._source_changed)
        top.addWidget(self.source)
        top.addWidget(QLabel("Camera:"))
        self.camera = QComboBox(); top.addWidget(self.camera, 2)
        top.addWidget(QLabel("Quality:"))
        self.res = QComboBox(); self.res.addItems(list(_RES.keys()))
        self.res.setCurrentText("720p (1280×720)")
        top.addWidget(self.res)
        self.fps = QComboBox(); self.fps.addItems(["15", "20", "25", "30"])
        self.fps.setCurrentText("25")
        top.addWidget(self.fps)
        top.addWidget(QLabel("View:"))
        self.view_mode = QComboBox()
        self.view_mode.addItem("Fill (no bars)", "fill")
        self.view_mode.addItem("Fit (whole frame)", "fit")
        self.view_mode.addItem("Stretch", "stretch")
        self.view_mode.setToolTip("Fill = no black bars, edges may be cropped.\n"
                                  "Fit = the whole frame, with thin bars where the "
                                  "shape doesn't match.\nStretch = fill exactly "
                                  "(slight distortion).")
        self.view_mode.currentIndexChanged.connect(lambda *_: self._repaint_now())
        top.addWidget(self.view_mode)
        self.refresh_btn = QPushButton("🔍 Scan cameras")
        self.refresh_btn.setProperty("role", "ghost")
        self.refresh_btn.setToolTip("Scan this source for available cameras.")
        self.refresh_btn.clicked.connect(self._refresh)
        self.start_btn = QPushButton("▶ Start"); self.start_btn.setProperty("role", "ok")
        self.start_btn.clicked.connect(self._toggle_start)
        top.addWidget(self.refresh_btn); top.addWidget(self.start_btn)
        for _cb in (self.source, self.camera, self.res, self.fps, self.view_mode):
            _cb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.source.setMinimumWidth(150); self.camera.setMinimumWidth(140)
        self.res.setMinimumWidth(95); self.fps.setMinimumWidth(55)
        self.view_mode.setMinimumWidth(95)
        lay.addLayout(top)

        # --- remote connection row (shown only when Source = Remote) ---
        self.remote_row = QWidget()
        rl = QHBoxLayout(self.remote_row); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("RDP host:"))
        self.r_host = QLineEdit()
        self.r_host.setPlaceholderText("remote machine IP / hostname")
        self.r_user = QLineEdit(); self.r_user.setPlaceholderText("user")
        self.r_domain = QLineEdit(); self.r_domain.setPlaceholderText("domain (optional)")
        self.r_pass = QLineEdit(); self.r_pass.setEchoMode(QLineEdit.Password)
        self.r_pass.setPlaceholderText("password")
        rl.addWidget(self.r_host, 2); rl.addWidget(QLabel("user:")); rl.addWidget(self.r_user, 1)
        rl.addWidget(QLabel("domain:")); rl.addWidget(self.r_domain, 1)
        rl.addWidget(QLabel("pass:")); rl.addWidget(self.r_pass, 1)
        self.remote_row.setVisible(False)
        lay.addWidget(self.remote_row)

        # --- the view (fills the tab) ---
        self.view = QLabel("Pick a source, Scan, then Start.\n\nLocal works over RDP "
                           "(this session's camera); Remote drives another Windows "
                           "machine's camera over WinRM.")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.view.setMinimumSize(120, 90)        # small min so it resizes in a split
        self.view.setStyleSheet("background:#0d1014; color:#8a8a8a; border-radius:6px;")
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._view_menu)
        lay.addWidget(self.view, 1)

        # --- bottom controls ---
        row = QHBoxLayout()
        self.snap_btn = QPushButton("📷 Snapshot"); self.snap_btn.setProperty("role", "ghost")
        self.rec_btn = QPushButton("⏺ Record"); self.rec_btn.setProperty("role", "ghost")
        self.pause_btn = QPushButton("⏸ Pause"); self.pause_btn.setProperty("role", "ghost")
        for b in (self.snap_btn, self.rec_btn, self.pause_btn):
            b.setEnabled(False)
        self.snap_btn.clicked.connect(self._snapshot)
        self.rec_btn.clicked.connect(self._toggle_record)
        self.pause_btn.clicked.connect(self._toggle_pause)
        row.addWidget(self.snap_btn); row.addWidget(self.rec_btn); row.addWidget(self.pause_btn)
        row.addWidget(QLabel("Rotate:"))
        self.rotate = QComboBox()
        for label, deg in (("0°", 0), ("90°", 90), ("180°", 180), ("270°", 270)):
            self.rotate.addItem(label, deg)
        self.rotate.setToolTip("Rotate the view (and snapshots) — useful for a camera "
                               "mounted sideways or upside-down.")
        self.rotate.currentIndexChanged.connect(lambda *_: self._repaint_now())
        row.addWidget(self.rotate)
        self.flip = QCheckBox("Flip")
        self.flip.setToolTip("Flip the image horizontally (mirror, selfie-style).")
        self.flip.stateChanged.connect(lambda *_: self._repaint_now())
        row.addWidget(self.flip)
        self.fps_lbl = QLabel(""); self.fps_lbl.setStyleSheet("color:#8a8a8a;")
        row.addWidget(self.fps_lbl)
        row.addStretch(1)
        self.status = QLabel("")
        row.addWidget(self.status)
        self._set_status("Pick a source, Scan, then Start.", "idle")
        self.link = QLabel(""); self.link.setOpenExternalLinks(True)
        self.link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        row.addWidget(self.link)
        lay.addLayout(row)

        self._last_paint = 0
        self._fps_count = 0
        self._fps_t0 = time.time()
        self._timer = QTimer(self); self._timer.timeout.connect(self._tick)
        self._timer.start(30)
        # NOTE: no auto-scan on construction — the webcam touches ffmpeg/cameras
        # only when the user clicks Scan or Start, so opening a device starts nothing.

    # ---- status toast (coloured pill) ----
    _STATUS = {
        "idle":  ("#9aa0a6", "transparent"),
        "info":  ("#ffffff", "#1f6feb"),     # blue   — working / connecting
        "ok":    ("#ffffff", "#238636"),     # green  — viewing / found
        "rec":   ("#ffffff", "#cf222e"),     # red    — recording
        "warn":  ("#241a00", "#d29922"),     # amber  — nothing found / attention
        "error": ("#ffffff", "#cf222e"),     # red    — failure
    }

    def _set_status(self, text, kind="idle"):
        fg, bg = self._STATUS.get(kind, self._STATUS["idle"])
        if bg == "transparent":
            self.status.setStyleSheet(f"color:{fg}; padding:2px 4px;")
        else:
            self.status.setStyleSheet(f"color:{fg}; background:{bg}; padding:2px 10px;"
                                      f"border-radius:9px; font-weight:600;")
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
        dlg.setAutoClose(False); dlg.setAutoReset(False)
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
            self._set_status("Scan for a camera, then Start.", "idle")

    # ---- enumerate ----
    def _refresh(self):
        self._stop_stream()
        self.camera.clear()
        self.refresh_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        if self._is_remote():
            host = self.r_host.text().strip()
            if not host or not self.r_user.text().strip():
                self._set_status("Enter the RDP host + user first.", "warn")
                self.refresh_btn.setEnabled(True)
                return
            self._set_status(f"Connecting to {host} over WinRM…", "info")
            self._prep = _RemotePrep(host, self._remote_login(), self.r_pass.text())
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
        self._fill(cams)

    def _remote_ready(self, ffmpeg, cams, diag):
        self.refresh_btn.setEnabled(True)
        self._remote_ffmpeg = ffmpeg
        self._fill(cams, diag)

    def _fill(self, cams, diag=""):
        self.camera.clear()
        for c in cams:
            self.camera.addItem(c)
        if cams:
            self.camera.setCurrentIndex(0)
            self._set_status(f"{len(cams)} camera(s) — Start to view.", "ok")
            self.start_btn.setEnabled(True)
        elif self._is_remote():
            self._set_status("No cameras found on the remote machine.", "warn")
            QMessageBox.information(
                self, "No camera on the remote machine",
                "ffmpeg didn't report a camera there. Its raw device listing is "
                "below — check the camera is attached, Windows camera privacy allows "
                "desktop apps, and nothing else is using it.\n\n" + (diag or "")[:3500])
        else:
            self._set_status("No cameras found.", "warn")
            QMessageBox.information(
                self, "No camera found",
                "ffmpeg didn't report a camera.\n\n"
                "• Over Remote Desktop, enable camera redirection in the RDP client "
                "(Local Resources → More… → Cameras) and reconnect.\n"
                "• Turn ON Windows camera privacy ('Let desktop apps access your "
                "camera').\n"
                "• Make sure nothing else is using the camera.")

    def _prep_fail(self, msg):
        self._close_dl_dialog()
        self.refresh_btn.setEnabled(True)
        self._set_status("Couldn't list cameras.", "error")
        QMessageBox.warning(self, "Camera", f"Couldn't list cameras:\n\n{msg}")

    # ---- start / stop ----
    def _toggle_start(self):
        if self.reader is not None:
            self._stop_stream(); self.start_btn.setText("▶ Start"); return
        cam = self.camera.currentText().strip()
        if not cam:
            return
        w, h = self._res(); fps = int(self.fps.currentText())
        if self._is_remote():
            self._start_remote(cam, w, h, fps)
        else:
            self._start_local(cam, w, h, fps)

    def _start_local(self, cam, w, h, fps):
        if not self._ffmpeg:
            self._refresh(); return
        try:
            args = ffmpeg_tools.local_capture_args(self._ffmpeg, cam,
                                                   width=w, height=h, fps=fps)
            self._proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL,
                                          bufsize=0, creationflags=_NO_WINDOW)
        except Exception as exc:
            QMessageBox.warning(self, "Camera", f"Couldn't start the camera:\n\n{exc}")
            return
        self.reader = _FrameReader(lambda n, p=self._proc: p.stdout.read(n))
        self.reader.start()
        self._after_start(cam)

    def _start_remote(self, cam, w, h, fps):
        if not self._remote_ffmpeg:
            self._refresh(); return
        from . import remote_webcam
        self.start_btn.setEnabled(False)
        self._set_status(f"Starting {cam} on the remote machine…", "info")
        self._starter = _RemoteStart(
            self.r_host.text().strip(), self._remote_login(), self.r_pass.text(),
            cam, self._remote_ffmpeg, w, h, fps, remote_webcam.DEFAULT_STREAM_PORT)
        self._starter.ok.connect(lambda pid, sock, c=cam: self._remote_started(pid, sock, c))
        self._starter.fail.connect(self._remote_start_fail)
        self._starter.start()

    def _remote_started(self, pid, sock, cam):
        self.start_btn.setEnabled(True)
        self._remote_pid = pid
        self._sock = sock

        def read_fn(n, s=sock):
            try:
                d = s.recv(n)
                return d if d else None       # b"" from recv = peer closed
            except socket.timeout:
                return b""
            except Exception:
                return None
        self.reader = _FrameReader(read_fn)
        self.reader.start()
        self._after_start(cam)
        QTimer.singleShot(8000, lambda c=cam: self._check_no_video(c))

    def _remote_start_fail(self, msg):
        self.start_btn.setEnabled(True)
        self._set_status("Couldn't start the remote camera", "error")
        QMessageBox.warning(self, "Remote camera",
                            f"Couldn't start the remote camera:\n\n{msg}\n\n"
                            "Check WinRM is on (Enable-PSRemoting -Force), your "
                            "account is a local admin, and ffmpeg is installed there.")

    def _after_start(self, cam):
        self.start_btn.setText("⏹ Stop")
        for b in (self.snap_btn, self.rec_btn, self.pause_btn):
            b.setEnabled(True)
        self._set_status(f"● Viewing {cam}", "ok")
        self._fps_count = 0; self._fps_t0 = time.time()
        if not self._is_remote():
            QTimer.singleShot(6000, lambda c=cam: self._check_no_video(c))

    def _check_no_video(self, cam):
        if self.reader is None or self.reader.frames > 0:
            return
        if self._is_remote():
            host, login, pw = (self.r_host.text().strip(), self._remote_login(),
                               self.r_pass.text())
            ff = self._remote_ffmpeg
            self._stop_stream()                 # release the camera before probing
            self._set_status("Diagnosing the remote camera…", "info")  # after stop
            self._probe = _RemoteProbe(host, login, pw, cam, ff)
            self._probe.done.connect(lambda out, c=cam: self._remote_no_video(c, out))
            self._probe.start()
        else:
            self._set_status("No video from the camera", "error")
            QMessageBox.information(
                self, "Camera — no video",
                f"The camera “{cam}” opened but produced no video.\n\n"
                "It's most likely in use by another app, blocked by Windows camera "
                "privacy, or — over RDP — not redirected into this session. Close "
                "other apps using it, check those settings, then Start again.")

    def _remote_no_video(self, cam, detail):
        self._set_status("Remote camera: no video", "error")
        QMessageBox.information(
            self, "Remote camera — no video",
            f"ffmpeg on the remote machine produced no video from “{cam}”. A short "
            f"diagnostic capture was run; its output is below.\n\n{detail}\n\n"
            "Most likely the camera is in use, blocked by Windows camera privacy, or "
            "is a camera redirected into someone's RDP session (only visible inside "
            "that session — a physical USB camera on the machine works headlessly).")

    # ---- orientation (shared by view, snapshot, copy) ----
    def _orient(self, img):
        if img is None:
            return None
        deg = self.rotate.currentData() or 0
        if deg:
            img = img.transformed(QTransform().rotate(deg))
        if self.flip.isChecked():
            img = img.mirrored(True, False)
        return img

    # ---- display (cheap: just scale an already-decoded QImage) ----
    def _tick(self):
        if self._paused or self.reader is None:
            return
        n = self.reader.frames
        if n == self._last_paint:            # no new frame -> don't rescale again
            return
        self._last_paint = n
        img = self._orient(self.reader.latest_image())
        if img is None:
            return
        target = self.view.size()
        mode = self.view_mode.currentData()
        sm = Qt.SmoothTransformation
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
        self._fps_count += 1
        now = time.time()
        if now - self._fps_t0 >= 1.0:
            self.fps_lbl.setText(f"{self._fps_count} fps")
            self._fps_count = 0; self._fps_t0 = now

    # ---- context menu / snapshot / copy ----
    def _view_menu(self, pos):
        m = QMenu(self)
        a_copy = m.addAction(theme.emoji_icon("📋"), "Copy image to clipboard")
        a_snap = m.addAction(theme.emoji_icon("📷"), "Save snapshot…")
        has = self.reader is not None and self.reader.latest_image() is not None
        a_copy.setEnabled(has); a_snap.setEnabled(has)
        chosen = m.exec_(self.view.mapToGlobal(pos))
        if chosen == a_copy:
            self._copy_frame()
        elif chosen == a_snap:
            self._snapshot()

    def _copy_frame(self):
        from PyQt5.QtWidgets import QApplication
        img = self._orient(self.reader.latest_image() if self.reader else None)
        if img is None:
            self._set_status("No frame to copy yet.", "warn"); return
        QApplication.clipboard().setImage(img)
        self._set_status("Frame copied to clipboard ✓", "ok")
        self.log.emit("[OK] camera frame copied to clipboard")

    def _snapshot(self):
        raw = self.reader.latest_raw() if self.reader else None
        if not raw:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save snapshot",
                                              f"webcam-{int(time.time())}.jpg",
                                              "Images (*.jpg *.png)")
        if not path:
            return
        try:
            img = self._orient(QImage.fromData(raw, "JPG"))
            if img is not None:
                img.save(path)
            else:
                with open(path, "wb") as fh:
                    fh.write(raw)
            self._show_link(path)
            self.log.emit(f"[OK] snapshot saved: {path}")
        except Exception as exc:
            self.log.emit(f"[ERROR] snapshot: {exc}")

    def _toggle_record(self):
        if self._rec_proc is not None:
            self._stop_record(); return
        if self.reader is None:
            return
        ff = ffmpeg_tools.cached_ffmpeg()
        if not ff:
            QMessageBox.warning(self, "Recording", "ffmpeg isn't ready yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Record video to",
                                              f"webcam-{int(time.time())}.mp4",
                                              "Video (*.mp4)")
        if not path:
            return
        try:
            # Feed the clean MJPEG frames to ffmpeg and RE-ENCODE to H.264 MP4.
            # -use_wallclock_as_timestamps stamps each frame as it arrives, so the
            # recording runs at real speed even if the camera can't sustain the fps.
            self._rec_proc = subprocess.Popen(
                [ff, "-y",
                 "-f", "mjpeg", "-use_wallclock_as_timestamps", "1", "-i", "-",
                 "-an", "-c:v", "libx264", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p", "-fps_mode", "vfr",
                 "-movflags", "+faststart", path],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
            self._rec_path = path
            self.reader.set_recorder(self._rec_proc.stdin)
            self.rec_btn.setText("⏺ Stop recording")
            self._set_status("● Recording…", "rec")
            self.log.emit(f"[OK] recording to {path}")
        except Exception as exc:
            self._rec_proc = None
            self.log.emit(f"[ERROR] record: {exc}")

    def _stop_record(self):
        if self.reader:
            self.reader.set_recorder(None)
        proc, self._rec_proc = self._rec_proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()           # flush remaining frames + write the trailer
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self.rec_btn.setText("⏺ Record")
        path, self._rec_path = self._rec_path, None
        if path:
            self._show_link(path)
            self.log.emit(f"[OK] recording saved: {path}")
            if self.reader is not None:
                self._set_status("Recording saved ✓ — still viewing", "ok")

    def _toggle_pause(self):
        self._paused = not self._paused
        self.pause_btn.setText("▶ Resume" if self._paused else "⏸ Pause")

    def _show_link(self, path):
        folder = os.path.dirname(os.path.abspath(path)).replace("\\", "/")
        self.link.setText(f'Saved — <a href="file:///{folder}">open folder</a>')

    # ---- teardown ----
    def _stop_stream(self):
        self._stop_record()
        if self.reader:
            try:
                self.reader.stop()
            except Exception:
                pass
        self.reader = None
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._remote_pid is not None:
            # kill the remote ffmpeg over WinRM, off the UI thread
            host, login, pw, pid = (self.r_host.text().strip(), self._remote_login(),
                                    self.r_pass.text(), self._remote_pid)
            self._remote_pid = None
            threading.Thread(target=self._remote_kill, args=(host, login, pw, pid),
                             daemon=True).start()
        for b in (self.snap_btn, self.rec_btn, self.pause_btn):
            b.setEnabled(False)
        self._paused = False
        self.pause_btn.setText("⏸ Pause")
        self.start_btn.setText("▶ Start")
        self.fps_lbl.setText("")
        self.view.clear()
        self.view.setText("Camera stopped — Start to view again.")
        self._set_status("Stopped.", "idle")

    @staticmethod
    def _remote_kill(host, login, pw, pid):
        try:
            from . import remote_webcam
            remote_webcam.stop_remote_stream(host, login, pw, pid)
        except Exception:
            pass

    def close_panel(self):
        self._close_dl_dialog()
        try:
            self._timer.stop()
        except Exception:
            pass
        self._stop_stream()
        for attr in ("_prep", "_starter", "_probe"):
            t = getattr(self, attr, None)
            if t is not None:
                try:
                    t.wait(2000)
                except Exception:
                    pass

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
                self.host, self.login, self.password, self.camera, self.ffmpeg)
        except Exception as exc:
            out = f"{type(exc).__name__}: {exc}"
        self.done.emit(out or "ffmpeg produced no output.")
