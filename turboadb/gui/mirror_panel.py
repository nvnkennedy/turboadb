"""Embedded scrcpy mirror — the scrcpy window lives INSIDE a tab instead of a
separate floating window.

On Windows this launches scrcpy borderless, finds its window by a unique title,
and reparents it (Win32 ``SetParent``) into a Qt container that follows resizes.
If embedding can't work (non-Windows, scrcpy not found, or — common over Remote
Desktop — no GPU/decoder for scrcpy), it cleanly falls back to an external
window and says so, so you're never stuck."""

from __future__ import annotations

import os
import sys
import time
import subprocess

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QStandardPaths
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QComboBox, QCheckBox, QInputDialog,
                             QMessageBox, QFileDialog, QToolButton, QMenu)

from ..config import ScrcpyOptions
from ..results import OperationResult
from ..scrcpy import is_remote_session, is_local_host
from ..tools import scrcpy_available
from . import settings as settings_mod

_IS_WIN = os.name == "nt"


# --------------------------------------------------------------------------- #
# Win32 window reparenting helpers (best-effort, guarded)
# --------------------------------------------------------------------------- #
def _win_api():
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.FindWindowW.restype = wintypes.HWND
    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.SetParent.restype = wintypes.HWND
    u.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    u.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.BOOL]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    return ctypes, u


def _reparent(child_hwnd, parent_hwnd, w, h):
    # Minimal + safe: SetParent + MoveWindow only (no window-style rewriting,
    # which is the part most likely to destabilise a foreign SDL window).
    _ctypes, u = _win_api()
    u.SetParent(child_hwnd, parent_hwnd)
    u.MoveWindow(child_hwnd, 0, 0, max(1, w), max(1, h), True)
    u.ShowWindow(child_hwnd, 5)   # SW_SHOW


def _find_window(title):
    _ctypes, u = _win_api()
    return u.FindWindowW(None, title)


def _post_close(title) -> bool:
    """Ask a window (by title) to close politely — WM_CLOSE. scrcpy treats this
    like Ctrl+C and **finalizes the recording file** before exiting, so the mp4
    is never corrupt. Returns True if the window was found and messaged."""
    from ctypes import wintypes
    _ctypes, u = _win_api()
    hwnd = u.FindWindowW(None, title)
    if not hwnd:
        return False
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                               wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    u.PostMessageW(hwnd, 0x0010, 0, 0)        # WM_CLOSE
    return True


def _bitrate_to_bps(value) -> str:
    """Normalise a bitrate like '16M' / '8m' / '16000000' to a bits-per-second
    integer string — what device-side ``screenrecord --bit-rate`` expects (older
    builds reject the 'M' suffix)."""
    s = str(value).strip().lower()
    try:
        if s.endswith("m"):
            return str(int(float(s[:-1]) * 1_000_000))
        if s.endswith("k"):
            return str(int(float(s[:-1]) * 1_000))
        return str(int(float(s)))
    except Exception:
        return "16000000"


def _default_save_dir() -> str:
    d = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
    return d or os.path.expanduser("~")


def _open_path(path) -> None:
    try:
        if _IS_WIN:
            os.startfile(path)                # noqa: type-defined on Windows
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def _reveal_path(path) -> None:
    try:
        if _IS_WIN:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
    except Exception:
        pass


class _StayOpenMenu(QMenu):
    """A menu that stays open when you toggle a CHECKABLE item, so several
    options can be set in one go. Non-checkable actions (the methods) close it
    and run normally."""
    def mouseReleaseEvent(self, e):
        act = self.activeAction()
        if act is not None and act.isCheckable() and act.isEnabled():
            act.toggle()
            e.accept()
            return
        super().mouseReleaseEvent(e)


# --------------------------------------------------------------------------- #
class _DisplaysThread(QThread):
    done = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler

    def run(self):
        try:
            self.done.emit(self.handler.list_displays(safe=False))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _LiveThread(QThread):
    """Stream the screen by polling ``screencap`` over the EXISTING adb
    connection. Unlike scrcpy this needs no video tunnel and no GPU, so it works
    over a remote adb server / RDP / head units where scrcpy can't. Lower FPS."""
    frame = pyqtSignal(bytes)
    note = pyqtSignal(str)

    def __init__(self, handler, fps: float = 2.0):
        super().__init__()
        self.handler = handler
        self._stop = False
        self.interval = max(0.2, 1.0 / max(0.5, fps))

    def run(self):
        first = True
        while not self._stop:
            t0 = time.time()
            try:
                data = self.handler.screenshot(None, safe=False)   # PNG bytes
                if isinstance(data, (bytes, bytearray)) and data[:4] == b"\x89PNG":
                    self.frame.emit(bytes(data))
                    if first:
                        self.note.emit("[OK] live view streaming")
                        first = False
                elif first:
                    self.note.emit("[WARNING] live view: screencap returned no "
                                   "image on this device")
                    first = False
            except Exception as exc:
                if first:
                    self.note.emit(f"[ERROR] live view: {exc}")
                    first = False
            rem = self.interval - (time.time() - t0)
            while rem > 0 and not self._stop:
                time.sleep(min(0.05, rem)); rem -= 0.05

    def stop(self):
        self._stop = True


class _LiveView(QLabel):
    """Shows live frames scaled to fit, and turns clicks/drags on the image into
    device taps/swipes (emitted as fractions 0..1 of the image)."""
    tapped = pyqtSignal(float, float)
    swiped = pyqtSignal(float, float, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#000;")
        self.setMouseTracking(False)
        self._pm = None
        self._press = None

    def set_frame(self, pm):
        self._pm = pm
        self._draw()

    def _draw(self):
        if self._pm is not None and not self._pm.isNull():
            self.setPixmap(self._pm.scaled(self.size(), Qt.KeepAspectRatio,
                                           Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e); self._draw()

    def _frac(self, pos):
        if self._pm is None or self._pm.isNull():
            return None
        s = self._pm.size().scaled(self.size(), Qt.KeepAspectRatio)
        ox = (self.width() - s.width()) / 2
        oy = (self.height() - s.height()) / 2
        if s.width() <= 0 or s.height() <= 0:
            return None
        fx = (pos.x() - ox) / s.width()
        fy = (pos.y() - oy) / s.height()
        if 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
            return (fx, fy)
        return None

    def mousePressEvent(self, e):
        self._press = self._frac(e.pos())

    def mouseReleaseEvent(self, e):
        rel = self._frac(e.pos())
        if self._press and rel:
            dx = abs(rel[0] - self._press[0]); dy = abs(rel[1] - self._press[1])
            if dx < 0.02 and dy < 0.02:
                self.tapped.emit(rel[0], rel[1])
            else:
                self.swiped.emit(self._press[0], self._press[1], rel[0], rel[1])
        self._press = None


class _RecWaitThread(QThread):
    """Wait for the recording scrcpy process to finish writing the file after a
    polite WM_CLOSE. If it doesn't exit in time, force it. Emits whether it ended
    cleanly (clean = the file was finalized properly)."""
    done = pyqtSignal(bool)

    def __init__(self, session, graceful: bool):
        super().__init__()
        self.session = session
        self.graceful = graceful

    def run(self):
        try:
            self.session.wait(timeout=10 if self.graceful else 4)
            self.done.emit(True)
            return
        except Exception:
            pass
        # never exited on its own — force it (file may be truncated)
        try:
            self.session.stop()
        except Exception:
            pass
        try:
            self.session.wait(timeout=3)
        except Exception:
            pass
        self.done.emit(False)


class _ShotThread(QThread):
    done = pyqtSignal(str)
    fail = pyqtSignal(str)

    def __init__(self, handler, path):
        super().__init__()
        self.handler = handler
        self.path = path

    def run(self):
        try:
            self.handler.screenshot(self.path, safe=False)
            self.done.emit(self.path)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _RecordThread(QThread):
    """Record the screen on the DEVICE itself (``adb shell screenrecord``) and
    pull the file. No video tunnel and no GPU needed, so it works while using
    Live View, over a remote adb server / RDP / IVI — anywhere the adb
    connection works. (screenrecord caps at ~3 min and has no audio.)"""
    done = pyqtSignal(list)      # the saved part file(s)
    fail = pyqtSignal(str)
    part = pyqtSignal(int)       # a new part started (Android's 3-min cap)

    def __init__(self, handler, path, stop_event, bit_rate=None):
        super().__init__()
        self.handler = handler
        self.path = path
        self.stop_event = stop_event
        self.bit_rate = bit_rate

    def run(self):
        # A single device-side screenrecord is capped at ~3 min by Android. Rather
        # than just stopping mid-capture, record back-to-back parts until the user
        # stops — each a valid, sharp clip (explicit high bitrate + native size).
        base, ext = os.path.splitext(self.path)
        ext = ext or ".mp4"
        parts, n = [], 0
        try:
            while not self.stop_event.is_set():
                seg = self.path if n == 0 else f"{base}-part{n + 1:02d}{ext}"
                if n > 0:
                    self.part.emit(n + 1)
                self.handler.screen_record(seg, time_limit=180,
                                           bit_rate=self.bit_rate,
                                           stop_event=self.stop_event, safe=False)
                if os.path.exists(seg) and os.path.getsize(seg) > 0:
                    parts.append(seg)
                n += 1
                # if the user didn't press stop, the 3-min cap ended this segment —
                # loop straight into the next part instead of stopping
            self.done.emit(parts)
        except Exception as exc:
            if parts:
                self.done.emit(parts)        # keep whatever was captured
            else:
                self.fail.emit(f"{type(exc).__name__}: {exc}")


class MirrorPanel(QWidget):
    log = pyqtSignal(str)

    def __init__(self, handler, session, automotive=False, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.session_dict = session
        self.automotive = automotive
        self._scrcpy = None
        self._embed_timer = None
        self._fit_timer = None
        self._fit_count = 0
        self._embed_tries = 0
        self._child_hwnd = None
        self._mon = None
        self._compat = False
        self._embed_on = False
        self._retried = False
        # device-side screen recording (independent of how you're viewing —
        # works with the scrcpy mirror OR Live View, over RDP / remote)
        self._recording = False
        self._rec_path = None
        self._rec_thread = None
        self._rec_stop = None
        self._rec_mode = None        # "scrcpy" (clean, preferred) | "device" (fallback)
        self._rec_scrcpy = None      # the off-screen scrcpy --record session
        self._rec_title = None
        self._rec_wait = None
        self._shot = None
        self._live = None
        self._dev_w = self._dev_h = 0
        self._displays = []          # cached [{id,size}] from the last list
        self._multi = []             # ScrcpySessions when mirroring ALL displays

        from .flowlayout import FlowLayout
        self._rdp = is_remote_session()
        lay = QVBoxLayout(self)
        bar_w = QWidget()                         # toolbar wraps when narrow, so
        bar = FlowLayout(bar_w, hspacing=6, vspacing=4)   # the window can shrink

        # ---- two clear action buttons + a separate ⚙ options menu ----
        self.btn_mirror = QPushButton("▶ Mirror"); self.btn_mirror.setProperty("role", "ok")
        self.btn_mirror.setToolTip("Full-speed scrcpy mirror & control — best "
                                   "quality and real touch/keyboard. Needs a "
                                   "working video path (great on local/USB).")
        self.btn_mirror.clicked.connect(lambda: self.start())
        self.btn_live = QPushButton("🖥 Live View"); self.btn_live.setProperty("role", "ok")
        self.btn_live.setToolTip("Reliable screen stream over the adb connection "
                                 "— works over remote / RDP / IVI where scrcpy "
                                 "can't. Lower FPS; click/drag the image to "
                                 "tap/swipe.")
        self.btn_live.clicked.connect(self._start_live)

        self.btn_stop = QPushButton("■ Stop"); self.btn_stop.setProperty("role", "danger")
        self.btn_stop.clicked.connect(self.stop); self.btn_stop.setEnabled(False)
        self.cmb_display = QComboBox(); self.cmb_display.addItem("default display", None)
        # don't truncate display labels: size the box (and its popup) to the
        # longest entry, and give it room in the toolbar
        self.cmb_display.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.cmb_display.setMinimumWidth(160)
        self.cmb_display.setToolTip("Which display to mirror. Head units expose "
                                    "several (cluster, centre stack, passenger); "
                                    "this list fills in automatically on connect.")
        self.btn_record = QPushButton("🔴 Record…"); self.btn_record.setProperty("role", "ghost")
        self.btn_record.setToolTip("Record the device screen to a video file. "
                                   "Records on the device itself, so it works "
                                   "while mirroring OR Live View, over RDP / "
                                   "remote. Click again to stop & save.")
        self.btn_record.setEnabled(False)        # while mirror or Live View is on
        self.btn_record.clicked.connect(self._toggle_record)

        # options live in their own ⚙ menu (kept open so you can tick several)
        self.btn_opts = QToolButton(); self.btn_opts.setText("⚙ Options")
        self.btn_opts.setProperty("role", "ghost")
        self.btn_opts.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn_opts.setPopupMode(QToolButton.InstantPopup)
        self.btn_opts.setToolTip("Mirror options (audio, IVI compatibility, "
                                 "software rendering, embed, keyboard).")
        omenu = _StayOpenMenu(self.btn_opts)
        self.act_audio = omenu.addAction("Forward audio")
        self.act_audio.setCheckable(True); self.act_audio.setChecked(True)
        self.act_compat = omenu.addAction("Compatibility mode (IVI / automotive)")
        self.act_compat.setCheckable(True); self.act_compat.setChecked(automotive)
        self.act_soft = omenu.addAction("Software rendering (Remote Desktop)")
        self.act_soft.setCheckable(True); self.act_soft.setChecked(self._rdp)
        self.act_embed = omenu.addAction("Embed window in this tab (experimental)")
        self.act_embed.setCheckable(True)
        self.act_uhid = omenu.addAction("Hardware keyboard (UHID — fixes typing "
                                        "on RDP / IVI)")
        self.act_uhid.setCheckable(True); self.act_uhid.setChecked(self._rdp)
        self.btn_opts.setMenu(omenu)
        self.btn_shot = QPushButton("📸 Screenshot"); self.btn_shot.setProperty("role", "ghost")
        self.btn_shot.setToolTip("Capture a PNG of the screen (works on any "
                                 "device — phone, tablet or head unit).")
        self.btn_shot.clicked.connect(self._take_screenshot)
        self.btn_type = QPushButton("⌨ Type…"); self.btn_type.setProperty("role", "ghost")
        self.btn_type.setToolTip("Type text into the focused field on the device "
                                 "via adb — the reliable way to enter a URL etc. "
                                 "when the on-screen / physical keyboard won't "
                                 "type through the mirror.")
        self.btn_type.clicked.connect(self._type_text)
        self.btn_displays = QPushButton("↻ Displays"); self.btn_displays.setProperty("role", "ghost")
        self.btn_displays.setToolTip("Re-scan the device's displays.")
        self.btn_displays.clicked.connect(self.refresh_displays)
        self.btn_mirror_all = QPushButton("▦ Mirror all"); self.btn_mirror_all.setProperty("role", "ghost")
        self.btn_mirror_all.setToolTip("Mirror EVERY display at once, each in its "
                                       "own window — handy on an IVI with cluster + "
                                       "centre + passenger screens. Best on local / "
                                       "USB (a remote server shares one video "
                                       "tunnel, so it can only do one at a time).")
        self.btn_mirror_all.clicked.connect(self._mirror_all)
        self.btn_max = QPushButton("⛶ Max view"); self.btn_max.setProperty("role", "ghost")
        self.btn_max.setCheckable(True)
        self.btn_max.setToolTip("Hide the log + sidebar so the screen gets the "
                                "full window — much bigger for a portrait device.")
        self.btn_max.clicked.connect(self._toggle_max)
        for w in (self.btn_mirror, self.btn_live, self.btn_stop, self.btn_record,
                  self.btn_shot, self.btn_type, self.btn_opts,
                  QLabel("Display:"), self.cmb_display, self.btn_displays,
                  self.btn_mirror_all, self.btn_max):
            bar.addWidget(w)
        lay.addWidget(bar_w)

        self.status = QLabel("“▶ Mirror” = full-speed scrcpy (best on local/USB). "
                             "“🖥 Live View” = reliable over remote / RDP / IVI. "
                             "“🔴 Record” saves a video; “📸 Screenshot” / “⌨ Type” "
                             "work on any device. Options are under “⚙ Options”.")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        # native container that scrcpy gets reparented into
        self.container = QWidget()
        self.container.setAttribute(Qt.WA_NativeWindow, True)
        self.container.setStyleSheet("background:#000;")
        self.container.setMinimumHeight(200)
        lay.addWidget(self.container, 1)

        # screencap-based live view (shown instead of the container when active)
        self.live_view = _LiveView()
        self.live_view.setMinimumHeight(200)
        self.live_view.hide()
        self.live_view.tapped.connect(self._live_tap)
        self.live_view.swiped.connect(self._live_swipe)
        lay.addWidget(self.live_view, 1)

        # populate the display list automatically once the tab is up (so the
        # dropdown is ready without the user clicking "Displays" first)
        QTimer.singleShot(700, self.refresh_displays)

    # ----- live view (screencap streaming; works over remote/RDP/IVI) -----
    def _toggle_live(self):
        if self._live is not None:
            self._stop_live()
        else:
            self._start_live()

    def _start_live(self):
        if self._scrcpy is not None:             # switch away from scrcpy mirror
            self.stop()
        if self._live is not None:
            return
        self.status.hide()
        self.container.hide()
        self.live_view.show()
        self.log.emit("[OK] live view starting (screencap stream over adb)…")
        self._live = _LiveThread(self.handler, fps=2.0)
        self._live.frame.connect(self._on_live_frame)
        self._live.note.connect(self.log)
        self._live.start()
        self._refresh_buttons()

    def _stop_live(self):
        if self._live is not None:
            self._live.stop(); self._live.wait(900); self._live = None
        self.live_view.hide()
        self.container.show()
        self.status.show()
        self._refresh_buttons()
        self.log.emit("[OK] live view stopped")

    def _on_live_frame(self, data):
        from PyQt5.QtGui import QPixmap
        pm = QPixmap()
        if pm.loadFromData(data) and not pm.isNull():
            self._dev_w, self._dev_h = pm.width(), pm.height()
            self.live_view.set_frame(pm)

    def _live_tap(self, fx, fy):
        if not self._dev_w:
            return
        x, y = int(fx * self._dev_w), int(fy * self._dev_h)
        self._live_input(lambda h: h.tap(x, y, safe=False))

    def _live_swipe(self, fx, fy, tx, ty):
        if not self._dev_w:
            return
        x1, y1 = int(fx * self._dev_w), int(fy * self._dev_h)
        x2, y2 = int(tx * self._dev_w), int(ty * self._dev_h)
        self._live_input(lambda h: h.swipe(x1, y1, x2, y2, 250, safe=False))

    def _live_input(self, fn):
        """Fire a tap/swipe without blocking the UI (a quick adb round-trip)."""
        import threading
        threading.Thread(
            target=lambda: self._safe(lambda: fn(self.handler)), daemon=True).start()

    def _type_text(self):
        """Type text into the device's focused field via adb (`input text`).
        Reliable for entering a URL etc. when the mirror's keyboard won't type."""
        text, ok = QInputDialog.getText(
            self, "Type into the device",
            "Text to send to the focused field on the device\n"
            "(tap the field in the mirror FIRST so it has the cursor):")
        if not ok or not text:
            return
        self.log.emit(f"sending text to device: {text!r}")
        import threading

        def work():
            try:
                r = self.handler.input_text(text, safe=False)
                self.log.emit("[OK] text sent" if r else
                              "[WARNING] the device didn't accept injected text — "
                              "make sure a text field is focused (tap it in the "
                              "mirror first)")
            except Exception as exc:
                self.log.emit(f"[ERROR] type: {exc}")
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _safe(fn):
        try:
            fn()
        except Exception:
            pass

    def _refresh_buttons(self):
        """Single source of truth for button states: the two start buttons are
        enabled only when nothing is being viewed; Stop while viewing; Record
        while viewing OR already recording (it records device-side, so it can
        run alongside Live View)."""
        viewing = (self._scrcpy is not None) or (self._live is not None) \
            or bool(self._multi)
        self.btn_mirror.setEnabled(not viewing)
        self.btn_live.setEnabled(not viewing)
        self.btn_mirror_all.setEnabled(not viewing and len(self._displays) > 1)
        self.btn_stop.setEnabled(viewing)
        self.btn_record.setEnabled(viewing or self._recording)
        self.btn_record.setText("⏹ Stop recording" if self._recording else "🔴 Record…")

    # ----- mirror EVERY display, each in its own window -----
    def _mirror_all(self):
        if not self._displays:
            self.log.emit("[WARNING] No displays listed yet — click ↻ Displays.")
            return
        if self._scrcpy is not None or self._live is not None:
            self.stop()
        self._stop_multi()
        st = settings_mod.load()
        name = self.session_dict.get("name") or "turboadb"
        launched = 0
        for d in self._displays:
            opts = ScrcpyOptions(
                max_size=(st.get("scrcpy_max_size") or None),
                bit_rate=st.get("scrcpy_bit_rate") or None,
                video_codec=(st.get("scrcpy_video_codec") or None),
                stay_awake=st.get("scrcpy_stay_awake", True),
                no_audio=True,
                display_id=d["id"],
                keyboard_mode="uhid" if self.act_uhid.isChecked() else None,
                window_title=f"{name} — display {d['id']}")
            self._tune(opts)
            try:
                res = self.handler.mirror(opts, compat=self.act_compat.isChecked(),
                                          safe=True)
                sess = res.value if isinstance(res, OperationResult) else res
                if sess is not None:
                    self._multi.append(sess); launched += 1
            except Exception as exc:
                self.log.emit(f"[WARNING] display {d['id']}: {exc}")
        if launched:
            self.status.setText(f"Mirroring {launched} display(s), each in its own "
                                f"window. “■ Stop” closes them all.")
            self.log.emit(f"[OK] mirroring {launched} display(s) in separate windows")
        else:
            self.log.emit("[ERROR] could not mirror any display (try local/USB, or "
                          "compatibility mode)")
        self._refresh_buttons()

    def _stop_multi(self):
        for s in self._multi:
            try:
                s.stop()
            except Exception:
                pass
        self._multi = []

    def _toggle_max(self):
        """Hide/show the main window's docks so the mirror gets the whole window
        — the practical way to make a portrait device screen big and readable."""
        from PyQt5.QtWidgets import QMainWindow, QDockWidget
        win = self.window()
        if not isinstance(win, QMainWindow):
            return
        on = self.btn_max.isChecked()
        for d in win.findChildren(QDockWidget):
            d.setVisible(not on)
        self.btn_max.setText("⛶ Restore" if on else "⛶ Max view")
        QTimer.singleShot(200, self._fit)       # re-fit the embed to the new size

    # ----- display list -----
    def refresh_displays(self):
        self.log.emit("Listing displays…")
        self._dt = _DisplaysThread(self.handler)
        self._dt.done.connect(self._got_displays)
        self._dt.fail.connect(lambda m: self.log.emit("[ERROR] displays: " + m))
        self._dt.start()

    def _got_displays(self, displays):
        self._displays = list(displays or [])
        keep = self.cmb_display.currentData()       # preserve the user's choice
        self.cmb_display.clear()
        self.cmb_display.addItem("default display", None)
        for d in self._displays:
            label = f"Display {d['id']}" + (f"  ·  {d['size']}" if d.get("size") else "")
            self.cmb_display.addItem(label, d["id"])
        idx = self.cmb_display.findData(keep)
        if idx >= 0:
            self.cmb_display.setCurrentIndex(idx)
        # let several IVI displays sit side by side
        self.btn_mirror_all.setEnabled(len(self._displays) > 1)
        self.log.emit(f"[OK] {len(self._displays)} display(s) found")

    def _tune(self, opts):
        """Apply the 'software render (RDP)' choice and, in that mode, sensible
        caps so the picture stays smooth over a remote/GPU-less link."""
        if self.act_soft.isChecked():
            opts.render_driver = "software"
            opts.max_size = opts.max_size or 1024     # smaller frame = far smoother
            opts.bit_rate = opts.bit_rate or "4M"
            opts.max_fps = opts.max_fps or 30
        return opts

    # ----- screen recording (device-side; works with mirror OR Live View) -----
    def _toggle_record(self):
        if self._recording:
            self._stop_record()
        else:
            self._begin_record()

    def _begin_record(self):
        if self._scrcpy is None and self._live is None:
            QMessageBox.information(
                self, "Record",
                "Start the mirror (▶ Mirror) or Live View first, then Record.\n\n"
                "Recording captures what you're viewing on the device.")
            return
        default = os.path.join(_default_save_dir(),
                               time.strftime("recording-%Y%m%d-%H%M%S.mp4"))
        path, _sel = QFileDialog.getSaveFileName(
            self, "Record the device screen to…", default, "MP4 video (*.mp4)")
        if not path:
            return
        if not path.lower().endswith(".mp4"):
            path += ".mp4"
        self._rec_path = path
        self._recording = True
        # Prefer scrcpy --record: it muxes the H.264 stream from the first REAL
        # frame, so there's no encoder warm-up black frame, no 3-min cap, and it's
        # sharp. It needs a workable local video path (great when the device is
        # local — incl. USB on the machine you're RDP'd into). For a genuinely
        # REMOTE adb server, scrcpy's tunnel is fragile, so use the device-side
        # screenrecord (universal, auto-continues past the 3-min cap).
        cfg = getattr(self.handler, "config", None)
        remote = bool(cfg and cfg.adb_server_host) \
            and not is_local_host(cfg.adb_server_host)
        if (not remote) and scrcpy_available(cfg.scrcpy_path if cfg else None):
            self._record_scrcpy(path)
        else:
            self._record_device(path)
        self._refresh_buttons()

    def _record_scrcpy(self, path):
        """Record via an OFF-SCREEN scrcpy --record window — clean (no black start),
        sharp, no time limit. Stopped politely so the mp4 is finalized."""
        self._rec_mode = "scrcpy"
        self._rec_title = f"turboadb-rec-{os.getpid()}-{id(self)}"
        st = settings_mod.load()
        opts = ScrcpyOptions(
            max_size=(st.get("scrcpy_max_size") or None),
            bit_rate=st.get("scrcpy_bit_rate") or None,
            video_codec=(st.get("scrcpy_video_codec") or None),
            no_audio=True, no_control=True, stay_awake=True,
            display_id=self.cmb_display.currentData(),
            record=path, record_format="mp4",
            window_title=self._rec_title, window_borderless=True,
            window_x=-32000, window_y=-32000)        # off-screen: no window shows
        self._tune(opts)
        res = self.handler.mirror(opts, compat=self.act_compat.isChecked(),
                                  safe=True)
        sess = res.value if isinstance(res, OperationResult) else res
        if sess is None or (isinstance(res, OperationResult) and not res.success):
            self.log.emit("[WARNING] scrcpy recorder didn't start — using the "
                          "device-side recorder instead")
            self._record_device(path)
            return
        self._rec_scrcpy = sess
        self.status.setText(f"● Recording (scrcpy — sharp, no time limit) → {path}")
        self.log.emit(f"[OK] recording with scrcpy: no black warm-up frame, no "
                      f"3-min limit → {path}")
        QTimer.singleShot(2500, self._check_rec_scrcpy)

    def _check_rec_scrcpy(self):
        """If the off-screen scrcpy recorder died early (e.g. no video path over a
        flaky link), transparently switch to the device-side recorder so the user
        still gets a recording instead of an empty file."""
        if self._rec_mode != "scrcpy" or not self._recording:
            return
        if self._rec_scrcpy is not None and not self._rec_scrcpy.running:
            self.log.emit("[WARNING] scrcpy recorder stopped early — switching to "
                          "device-side recording")
            self._rec_scrcpy = None
            if self._recording:
                self._record_device(self._rec_path)

    def _record_device(self, path):
        """Record on the device (``screenrecord``) and pull it — works everywhere
        (Live View, remote/RDP), auto-continuing past Android's ~3-min cap."""
        import threading
        self._rec_mode = "device"
        st = settings_mod.load()
        br = st.get("scrcpy_bit_rate") or "16M"
        bits = _bitrate_to_bps(br)
        self._rec_stop = threading.Event()
        self._rec_thread = _RecordThread(self.handler, path, self._rec_stop,
                                         bit_rate=bits)
        self._rec_thread.done.connect(self._record_finished)
        self._rec_thread.fail.connect(self._record_failed)
        self._rec_thread.part.connect(self._record_part)
        self._rec_thread.start()
        self.status.setText(f"● Recording the device screen → {path}")
        self.log.emit(f"[OK] recording at {br} (device-side; auto-continues past "
                      f"the ~3-min Android cap) → {path}")

    def _stop_record(self):
        if not self._recording:
            return
        self._recording = False
        self.btn_record.setEnabled(False)
        self.log.emit("Finishing the recording (saving to your PC)…")
        self.status.setText("Finishing the recording…")
        if self._rec_mode == "scrcpy" and self._rec_scrcpy is not None:
            # WM_CLOSE -> scrcpy finalizes the mp4 cleanly; wait for it off-thread
            _post_close(self._rec_title)
            sess, self._rec_scrcpy = self._rec_scrcpy, None
            self._rec_wait = _RecWaitThread(sess, graceful=True)
            self._rec_wait.done.connect(
                lambda _clean: self._record_finished([self._rec_path]))
            self._rec_wait.start()
        elif self._rec_stop is not None:
            self._rec_stop.set()             # tell screenrecord to stop & pull

    def _record_part(self, n):
        self.status.setText(f"● Recording — part {n} (Android caps each clip at "
                            f"~3 min; the previous part was saved)…")
        self.log.emit(f"[INFO] 3-min cap reached — continuing in part {n}")

    def _record_finished(self, parts):
        self._recording = False
        self._refresh_buttons()
        self.status.show()
        parts = [p for p in (parts or [])
                 if p and os.path.exists(p) and os.path.getsize(p) > 0]
        if not parts:
            bad = self._rec_path or ""
            self.log.emit(f"[ERROR] recording file is empty → {bad}")
            QMessageBox.warning(self, "Recording",
                                f"The recording didn't save any data:\n{bad}\n\n"
                                "Some head units block screenrecord (secure "
                                "surface). Try a screenshot instead.")
            return
        if len(parts) == 1:
            self.log.emit(f"[OK] recording saved → {parts[0]}")
            self._saved_popup("Recording", parts[0])
        else:
            names = ", ".join(os.path.basename(p) for p in parts)
            self.log.emit(f"[OK] long recording saved in {len(parts)} parts "
                          f"(Android's 3-min cap): {names}")
            self._saved_popup(f"Recording — {len(parts)} parts", parts[0])

    def _record_failed(self, msg):
        self._recording = False
        self._refresh_buttons()
        self.log.emit(f"[ERROR] recording: {msg}")
        QMessageBox.warning(self, "Recording", f"Recording failed:\n{msg}")

    # ----- screenshot -----
    def _take_screenshot(self):
        default = os.path.join(_default_save_dir(),
                               time.strftime("screenshot-%Y%m%d-%H%M%S.png"))
        path, _sel = QFileDialog.getSaveFileName(
            self, "Save a screenshot to…", default, "PNG image (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        self.btn_shot.setEnabled(False)
        self.log.emit("Capturing screenshot…")
        self._shot = _ShotThread(self.handler, path)
        self._shot.done.connect(self._shot_done)
        self._shot.fail.connect(self._shot_fail)
        self._shot.start()

    def _shot_done(self, path):
        self.btn_shot.setEnabled(True)
        self.log.emit(f"[OK] screenshot saved → {path}")
        self._saved_popup("Screenshot", path)

    def _shot_fail(self, msg):
        self.btn_shot.setEnabled(True)
        self.log.emit(f"[ERROR] screenshot: {msg}")
        QMessageBox.warning(self, "Screenshot", f"Couldn't capture a screenshot.\n\n{msg}")

    def _saved_popup(self, kind, path):
        """Offer to open the just-saved file or reveal it in its folder."""
        box = QMessageBox(self)
        box.setWindowTitle(f"{kind} saved")
        box.setIcon(QMessageBox.Information)
        box.setText(f"{kind} saved to:")
        box.setInformativeText(path)
        b_open = box.addButton("Open", QMessageBox.AcceptRole)
        b_folder = box.addButton("Open folder", QMessageBox.ActionRole)
        box.addButton("Close", QMessageBox.RejectRole)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is b_open:
            _open_path(path)
        elif clicked is b_folder:
            _reveal_path(path)

    # ----- start / stop -----
    def start(self, display_id="__use_combo__", compat=None, embed=None,
              record=None, record_format=None):
        if self._live is not None:               # switch away from Live View
            self._stop_live()
        if self._scrcpy is not None:
            self.stop()
        if display_id == "__use_combo__":
            display_id = self.cmb_display.currentData()
        if compat is None:
            compat = self.act_compat.isChecked()
        if embed is None:
            embed = self.act_embed.isChecked()       # default OFF = reliable window
        elif embed:
            self.act_embed.setChecked(True)
        # Over Remote Desktop, the safest combo is compat (h264 + caps + no
        # audio) in a SEPARATE window with software rendering — embedding adds a
        # fragile window-reparent that often breaks on a GPU-less RDP session.
        if self.act_soft.isChecked():
            compat = True
            embed = False

        st = settings_mod.load()
        opts = ScrcpyOptions(
            max_size=(st.get("scrcpy_max_size") or None),
            bit_rate=st.get("scrcpy_bit_rate") or None,
            video_codec=(st.get("scrcpy_video_codec") or None),
            stay_awake=st.get("scrcpy_stay_awake", True),
            turn_screen_off=st.get("scrcpy_turn_screen_off", False),
            no_audio=not self.act_audio.isChecked(),
            record=record, record_format=record_format,
            display_id=display_id,
            keyboard_mode="uhid" if self.act_uhid.isChecked() else None,
            window_title=self.session_dict.get("name") or "turboadb")
        self._tune(opts)
        if opts.render_driver == "software":
            self.log.emit("[INFO] using software rendering (Remote Desktop / "
                          "GPU-less) — capped to keep it smooth")
        if opts.keyboard_mode == "uhid":
            self.log.emit("[INFO] keyboard = UHID (virtual hardware keyboard). "
                          "Click the scrcpy window to focus it, then type. If the "
                          "device’s on-screen keyboard stays up it doesn’t support "
                          "UHID — use the “⌨ Type…” button to enter text instead.")

        do_embed = embed and _IS_WIN
        if do_embed:
            self._embed_title = f"turboadb-embed-{os.getpid()}-{id(self)}"
            opts.window_title = self._embed_title
            opts.window_borderless = True
            opts.window_x, opts.window_y = 0, 0

        import tempfile
        self._log_path = os.path.join(tempfile.gettempdir(),
                                      f"turboadb-scrcpy-{os.getpid()}.log")
        self._compat = compat
        self._embed_on = do_embed
        res = self.handler.mirror(opts, compat=compat, log_path=self._log_path,
                                  safe=True)
        if isinstance(res, OperationResult) and not res.success:
            self.status.setText("Mirror failed.")
            self.log.emit(f"[ERROR] scrcpy: {res.error}")
            QMessageBox.warning(self, "scrcpy", f"{res.error}\n\nTip: try compat "
                                "mode, a specific display, or check that scrcpy is "
                                "installed (ribbon → Get tools).")
            return
        self._scrcpy = res.value if isinstance(res, OperationResult) else res
        self._start_t = __import__("time").time()
        self._refresh_buttons()
        # watch for the scrcpy window being closed so the buttons reset
        self._mon = QTimer(self)
        self._mon.timeout.connect(self._check_alive)
        self._mon.start(800)
        if do_embed:
            self.status.setText("Starting mirror… embedding the scrcpy window.")
            self._embed_tries = 0
            self._embed_timer = QTimer(self)
            self._embed_timer.timeout.connect(self._try_embed)
            self._embed_timer.start(200)
            self.log.emit("[OK] scrcpy launching (embedded)…")
        else:
            self.status.setText("Mirroring in a separate window "
                                "(embedding off / not supported here).")
            self.log.emit("[OK] scrcpy launched in an external window")

    def _check_alive(self):
        """If scrcpy exited (window closed / crashed), reset — and if it died
        immediately after starting, surface the error and auto-retry once in
        compatibility mode (scrcpy 'sometimes doesn't start' is usually an
        encoder/codec issue compat mode fixes)."""
        if self._scrcpy is None:
            return
        if self._scrcpy.running:
            return
        import time
        died_fast = (time.time() - getattr(self, "_start_t", 0)) < 6
        err = (self._scrcpy.read_log() or "").strip()
        self._scrcpy = None
        self._stop_embed_timer()
        self._child_hwnd = None
        if getattr(self, "_mon", None):
            self._mon.stop(); self._mon = None
        self._refresh_buttons()
        self.status.show()

        # "healthy" markers mean scrcpy actually started and rendered/recorded —
        # if it then exited it was closed on purpose (or by us), NOT a failed
        # start, so we must NOT retry (retrying used to relaunch and corrupt a
        # perfectly good recording).
        healthy = any(m in err for m in ("Renderer:", "Texture:",
                                         "Recording started", "INFO: Renderer"))
        if (died_fast and not healthy and not getattr(self, "_retried", False)
                and not self._compat):
            self._retried = True
            tail = " ".join(err.splitlines()[-2:])[:200]
            self.log.emit(f"[WARNING] scrcpy didn't start ({tail or 'no output'}); "
                          f"retrying in compatibility mode…")
            self.status.setText("Retrying mirror in compatibility mode…")
            self.start(compat=True, embed=self._embed_on)
            return
        self._retried = False
        if died_fast and err:
            self.log.emit("[ERROR] scrcpy: " + " ".join(err.splitlines()[-3:])[:300])
            self.status.setText("Mirror couldn't start — see the scrcpy log below.")
            self._show_scrcpy_log(err)
        else:
            self.status.setText("Mirror ended. Click “Start mirror” to view again.")
            self.log.emit("[OK] mirror ended")

    def _show_scrcpy_log(self, err):
        """Show scrcpy's full output so a failure (esp. over RDP / on an IVI) is
        diagnosable instead of a one-line 'failed'."""
        from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QPlainTextEdit,
                                     QDialogButtonBox, QLabel)
        from PyQt5.QtGui import QFont
        dlg = QDialog(self); dlg.setWindowTitle("scrcpy couldn't start")
        dlg.resize(720, 460)
        v = QVBoxLayout(dlg)
        low = err.lower()
        is_remote = bool(getattr(self.handler, "config", None)
                         and self.handler.config.adb_server_host)
        from ..scrcpy import TUNNEL_PORT
        if is_remote:
            hint = (
                "Mirroring a device on a REMOTE adb server is the hard case: "
                "scrcpy has to stream video over the network, through a tunnel "
                f"port (TCP {TUNNEL_PORT}) on the remote PC.\n\n"
                "To make it work, on the REMOTE PC "
                f"({self.handler.config.adb_server_host}):\n"
                f"  1. Allow TCP {TUNNEL_PORT} AND 5037 through its firewall.\n"
                "  2. Make sure its adb server was started shared "
                "(TurboADB → Remote → “Start shared server on THIS PC”, or "
                "`turboadb serve`).\n\n"
                "MOST RELIABLE: run TurboADB ON that PC (e.g. via RDP) and "
                "connect to the device locally (USB/Network) — then there's no "
                "network video tunnel at all. Mirroring a remote device's screen "
                "across the network is inherently fragile.")
        elif ("start-server" in low or "server connection" in low
              or "no host" in low or "could not start adb" in low):
            hint = ("scrcpy couldn't reach the adb server. Try the Devices ▾ menu "
                    "→ “Restart ADB server”, then mirror again. The exact command "
                    "and server address are at the top of the log below.")
        else:
            hint = ("Tips: tick “compat (IVI)” and “software render (RDP)”, use a "
                    "separate window (untick embed), and try a specific display. "
                    "The exact scrcpy command is at the top of the log below.")
        lab = QLabel(hint); lab.setWordWrap(True); v.addWidget(lab)
        view = QPlainTextEdit(); view.setReadOnly(True)
        view.setFont(QFont("Consolas", 9))
        view.setPlainText(err or "(scrcpy produced no output)")
        v.addWidget(view, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject); bb.accepted.connect(dlg.accept)
        v.addWidget(bb)
        dlg.exec_()

    def _try_embed(self):
        self._embed_tries += 1
        if self._scrcpy is None or not self._scrcpy.running:
            self._stop_embed_timer()
            self.status.setText("scrcpy exited before it could be embedded. "
                                "Try compat mode, or “Open in window”. Over Remote "
                                "Desktop, scrcpy may need a local GPU.")
            self.btn_stop.setEnabled(False); self.btn_mirror.setEnabled(True)
            self._scrcpy = None
            return
        # wait until the container has a real size before adopting the window,
        # otherwise the first resize snaps scrcpy to a tiny rectangle
        if self.container.width() < 80 or self.container.height() < 80:
            return
        try:
            hwnd = _find_window(self._embed_title)
        except Exception:
            hwnd = None
        if hwnd:
            self._child_hwnd = hwnd
            self._stop_embed_timer()
            try:
                _reparent(hwnd, int(self.container.winId()),
                          self.container.width(), self.container.height())
                self.status.setText("")
                self.status.hide()
                self.log.emit("[OK] scrcpy embedded")
                # scrcpy/SDL resizes its own window to the video frame after the
                # first frame — keep forcing it to fill the container for a few
                # seconds so it doesn't shrink back to a tiny window.
                self._fit_timer = QTimer(self)
                self._fit_timer.timeout.connect(self._fit)
                self._fit_timer.start(500)     # keeps it filling the whole session
                self._fit()
            except Exception as exc:
                self.log.emit(f"[WARNING] could not embed scrcpy: {exc} "
                              "(it stays in its own window)")
            return
        if self._embed_tries > 50:        # ~10s
            self._stop_embed_timer()
            self.status.setText("Couldn't embed the scrcpy window; it's running "
                                "in its own window instead.")

    def _fit(self):
        """Force the embedded scrcpy window to fill the container (scrcpy/SDL
        keeps resizing itself to the video frame, so we keep correcting it)."""
        if not self._child_hwnd:
            return
        try:
            _ctypes, u = _win_api()
            u.MoveWindow(self._child_hwnd, 0, 0,
                         max(1, self.container.width()),
                         max(1, self.container.height()), True)
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _stop_embed_timer(self):
        if self._embed_timer:
            self._embed_timer.stop()
            self._embed_timer = None
        if getattr(self, "_fit_timer", None):
            self._fit_timer.stop()
            self._fit_timer = None

    def stop(self):
        # the Stop button stops whatever you're VIEWING (mirror, Live View, or the
        # multi-display windows); a recording runs independently device-side.
        if self._multi:
            self._stop_multi()
            self._refresh_buttons()
            self.status.setText("Stopped all display windows.")
            return
        if self._live is not None:
            self._stop_live()
            return
        self._stop_embed_timer()
        if getattr(self, "_mon", None):
            self._mon.stop(); self._mon = None
        self._child_hwnd = None
        if self._scrcpy is not None:
            try:
                self._scrcpy.stop()
            except Exception:
                pass
            self._scrcpy = None
        self._refresh_buttons()
        self.status.show()
        self.status.setText("Stopped. Click “▶ Mirror” or “🖥 Live View” to view.")

    def _finalize_rec_sync(self):
        """If a recording is in progress, stop it and let the file finalize —
        used when the panel/tab is closing."""
        if not self._recording:
            return
        self._recording = False
        if self._rec_mode == "scrcpy" and self._rec_scrcpy is not None:
            _post_close(self._rec_title)             # finalize the mp4
            try:
                self._rec_scrcpy.wait(timeout=8)
            except Exception:
                try:
                    self._rec_scrcpy.stop()
                except Exception:
                    pass
            self._rec_scrcpy = None
        else:
            if self._rec_stop is not None:
                self._rec_stop.set()
            if self._rec_thread is not None:
                self._rec_thread.wait(8000)          # let screenrecord stop + pull

    def close_panel(self):
        if self._live is not None:
            self._live.stop(); self._live.wait(900); self._live = None
        self._finalize_rec_sync()
        if self._shot is not None:
            self._shot.wait(700)
        self.stop()
