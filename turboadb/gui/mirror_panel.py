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
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QComboBox, QCheckBox, QInputDialog,
                             QLineEdit, QMessageBox, QFileDialog, QToolButton,
                             QMenu)

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
    # declare argtypes explicitly: without them ctypes defaults the HWND arg to
    # a 32-bit int and truncates a 64-bit handle on win64
    u.GetWindowRect.restype = wintypes.BOOL
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    # style + focus plumbing for a REAL child-window embed
    long_ptr = ctypes.c_ssize_t
    get_l = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
    set_l = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
    get_l.restype = long_ptr
    get_l.argtypes = [wintypes.HWND, ctypes.c_int]
    set_l.restype = long_ptr
    set_l.argtypes = [wintypes.HWND, ctypes.c_int, long_ptr]
    u.SetWindowPos.restype = wintypes.BOOL
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               wintypes.UINT]
    u.SetFocus.restype = wintypes.HWND
    u.SetFocus.argtypes = [wintypes.HWND]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                           ctypes.POINTER(wintypes.DWORD)]
    u.AttachThreadInput.restype = wintypes.BOOL
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                    wintypes.BOOL]
    u.GetAncestor.restype = wintypes.HWND
    u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD,
                              ctypes.c_void_p]
    u.GetFocus.restype = wintypes.HWND
    u.GetFocus.argtypes = []
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetForegroundWindow.argtypes = []
    u.IsChild.restype = wintypes.BOOL
    u.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
    return ctypes, u


_GWL_STYLE = -16
_WS_CHILD = 0x40000000
_WS_POPUP = 0x80000000
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020


def _reparent(child_hwnd, parent_hwnd, w, h):
    """Adopt the scrcpy/SDL window as a real Win32 child (WS_CHILD) — the same
    approach every embedder (VLC, mpv, Chromium plugin host) uses.

    A window is only a first-class part of the parent's focus/activation chain
    when it carries the WS_CHILD style; a bare SetParent leaves it a top-level
    popup that Windows activates separately, so a real click into it fights the
    parent for activation. Converting to WS_CHILD is what lets a mouse click
    route keyboard focus to it normally.

    NOTE: this is the correct standard path for real user clicks. It cannot be
    fully verified with synthesised keystrokes (those depend on foreground
    acquisition, which is unreliable to force programmatically) — but the
    device-keyboard bar is the guaranteed-working keyboard path regardless of
    how this plays out on any given GPU/RDP session."""
    _ctypes, u = _win_api()
    getter = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
    setter = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
    u.SetParent(child_hwnd, parent_hwnd)
    style = getter(child_hwnd, _GWL_STYLE)
    style = (style | _WS_CHILD) & ~(_WS_POPUP | _WS_CAPTION | _WS_THICKFRAME)
    setter(child_hwnd, _GWL_STYLE, style)
    u.SetWindowPos(child_hwnd, None, 0, 0, max(1, w), max(1, h),
                   _SWP_NOZORDER | _SWP_FRAMECHANGED)
    u.ShowWindow(child_hwnd, 5)   # SW_SHOW


def _focus_window(hwnd) -> None:
    """Give KEYBOARD focus to the embedded (cross-process) scrcpy window.

    Brings TurboADB to the foreground first (the Alt-key nudge unlocks
    SetForegroundWindow for our own process), then — because SetFocus only acts
    within the caller thread's input queue — attaches to the scrcpy window's
    input thread before SetFocus. Standard Win32 pattern for focusing an
    embedded foreign window."""
    if not hwnd:
        return
    try:
        ctypes, u = _win_api()
        k = ctypes.windll.kernel32
        # 1) make sure OUR top-level window is the active/foreground one, or the
        #    keystrokes route nowhere useful (the Alt tap satisfies Windows'
        #    foreground-change rule for our own process)
        top = u.GetAncestor(hwnd, 2) if hasattr(u, "GetAncestor") else 0  # GA_ROOT
        if top:
            u.keybd_event(0x12, 0, 0, 0)          # VK_MENU down
            u.keybd_event(0x12, 0, 0x0002, 0)     # VK_MENU up
            u.SetForegroundWindow(top)
        # 2) attach to the scrcpy thread's input queue, then focus its window
        target_tid = u.GetWindowThreadProcessId(hwnd, None)
        my_tid = k.GetCurrentThreadId()
        attached = False
        if target_tid and target_tid != my_tid:
            attached = bool(u.AttachThreadInput(my_tid, target_tid, True))
        u.SetFocus(hwnd)
        if attached:
            u.AttachThreadInput(my_tid, target_tid, False)
    except Exception:
        pass


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
    from .fileutil import download_dir      # save to the user's Downloads folder
    return download_dir()


def _open_path(path) -> None:
    try:
        if _IS_WIN:
            os.startfile(path)                # type: ignore[attr-defined]  # Windows-only
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


# (the old _StayOpenMenu checkable-menu hack was replaced by the real options
# popover panel — see MirrorPanel._build_options_menu)


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
    over a remote adb server / RDP / head units where scrcpy can't.

    Tuned for smoothness: the rate is ADAPTIVE (captures back-to-back, capped at
    *max_fps*, instead of the old hard 2 fps), and the expensive work — PNG
    decode of a full-resolution screenshot plus the smooth down-scale to the
    view size — happens HERE, off the UI thread. The GUI only receives an
    already-scaled QImage, so painting is cheap and the app never stutters
    under the stream (which it did when every frame was decoded + smoothly
    rescaled on the UI thread)."""
    frame = pyqtSignal(object, int, int)   # pre-scaled QImage, device W, H
    note = pyqtSignal(str)

    def __init__(self, handler, max_fps: float = 10.0):
        super().__init__()
        self.handler = handler
        self._stop = False
        self.min_interval = 1.0 / max(1.0, max_fps)
        # the panel keeps these current with the view size; plain-int reads are
        # safe across threads
        self.target_w = 0
        self.target_h = 0

    def run(self):
        from PyQt5.QtGui import QImage
        first = True
        while not self._stop:
            t0 = time.time()
            try:
                data = self.handler.screenshot(None, safe=False)   # PNG bytes
                if isinstance(data, (bytes, bytearray)) and data[:4] == b"\x89PNG":
                    img = QImage.fromData(bytes(data), "PNG")
                    if not img.isNull():
                        dev_w, dev_h = img.width(), img.height()
                        tw, th = self.target_w, self.target_h
                        if tw > 0 and th > 0 and (dev_w > tw or dev_h > th):
                            # smooth-downscale ONCE here, from the native frame
                            img = img.scaled(tw, th, Qt.KeepAspectRatio,
                                             Qt.SmoothTransformation)
                        self.frame.emit(img, dev_w, dev_h)
                        if first:
                            self.note.emit("[OK] live view streaming (adaptive "
                                           "rate — as fast as this link allows)")
                            first = False
                elif first:
                    self.note.emit("[WARNING] live view: screencap returned no "
                                   "image on this device")
                    first = False
            except Exception as exc:
                if first:
                    self.note.emit(f"[ERROR] live view: {exc}")
                    first = False
            # adaptive pacing: on a fast link this caps at max_fps; on a slow
            # (remote/RDP) link the capture time dominates and we simply run
            # back-to-back — no artificial 0.5 s wait on top
            rem = self.min_interval - (time.time() - t0)
            while rem > 0 and not self._stop:
                time.sleep(min(0.05, rem)); rem -= 0.05

    def stop(self):
        self._stop = True


class _LiveView(QLabel):
    """Shows live frames scaled to fit, and turns clicks/drags on the image into
    device taps/swipes — and, once clicked, forwards KEYSTROKES to the device too
    (so you type straight onto the Live View, no separate field needed)."""
    tapped = pyqtSignal(float, float)
    swiped = pyqtSignal(float, float, float, float)
    key_typed = pyqtSignal(str, object)          # (kind, payload) → adb forward

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#000;")
        self.setMouseTracking(False)
        # accept keyboard focus so typing on the image goes to the device
        self.setFocusPolicy(Qt.StrongFocus)
        self._pm = None
        self._press = None

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Paste):
            from PyQt5.QtWidgets import QApplication
            txt = QApplication.clipboard().text()
            if txt:
                self.key_typed.emit("text", txt)
            return
        kind, payload = _DeviceKeyEdit.map_event(event.key(), event.text())
        if kind is None:
            super().keyPressEvent(event)
            return
        self.key_typed.emit(kind, payload)

    def set_frame(self, pm):
        self._pm = pm
        self._draw()

    def _draw(self):
        if self._pm is None or self._pm.isNull():
            return
        # frames arrive pre-scaled to this view — when they already fit, show
        # them as-is instead of smooth-rescaling a full pixmap every frame
        if (self._pm.width() <= self.width() and
                self._pm.height() <= self.height()):
            self.setPixmap(self._pm)
            return
        self.setPixmap(self._pm.scaled(self.size(), Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e); self._draw()

    def _frac(self, pos):
        # measure the pixmap that is ACTUALLY displayed (centred, unscaled by
        # the label), so tap coordinates stay exact for both draw paths
        disp = self.pixmap()
        if disp is None or disp.isNull():
            return None
        s = disp.size()
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
        self.setFocus(Qt.MouseFocusReason)       # clicking the image = ready to type
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


class _DeviceKeyEdit(QLineEdit):
    """A keyboard-capture field: every keystroke is forwarded to the device (via
    the panel's ``send`` callback) instead of edited locally, so typing here IS
    typing on the device — Enter, Backspace, arrows and all. The field itself
    stays empty (it's a wire, not a buffer)."""

    _KEYMAP = {
        Qt.Key_Return: 66, Qt.Key_Enter: 66, Qt.Key_Backspace: 67,
        Qt.Key_Tab: 61, Qt.Key_Escape: 111, Qt.Key_Delete: 112,
        Qt.Key_Up: 19, Qt.Key_Down: 20, Qt.Key_Left: 21, Qt.Key_Right: 22,
        Qt.Key_Home: 122, Qt.Key_End: 123,
        Qt.Key_PageUp: 92, Qt.Key_PageDown: 93,
    }

    def __init__(self, send, parent=None):
        super().__init__(parent)
        self._send = send                # send("text", str) / send("key", code)

    @classmethod
    def map_event(cls, key, text):
        """(kind, payload) for a Qt key event — pure + unit-testable."""
        if key in cls._KEYMAP:
            return "key", cls._KEYMAP[key]
        if text and text.isprintable():
            return "text", text
        return None, None

    def keyPressEvent(self, event):
        # paste lands as one text batch
        if event.matches(QKeySequence.Paste):
            from PyQt5.QtWidgets import QApplication
            txt = QApplication.clipboard().text()
            if txt:
                self._send("text", txt)
            return
        kind, payload = self.map_event(event.key(), event.text())
        if kind is None:
            super().keyPressEvent(event)     # let Qt handle what we don't map
            return
        self._send(kind, payload)


class _KeyPumpThread(QThread):
    """Serialises the keyboard-bar events into adb calls off the UI thread,
    coalescing bursts of printable characters into single ``input text`` calls
    so fast typing stays fluid."""
    note = pyqtSignal(str)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        import queue
        self._q = queue.Queue()
        self._stop = False

    def push(self, kind, payload):
        self._q.put((kind, payload))

    def stop(self):
        self._stop = True
        self._q.put(None)

    def run(self):
        import queue
        while not self._stop:
            item = self._q.get()
            if item is None or self._stop:
                break
            kind, payload = item
            try:
                if kind == "text":
                    buf = payload
                    # coalesce whatever printable text queued up meanwhile
                    while True:
                        try:
                            nxt = self._q.get_nowait()
                        except queue.Empty:
                            break
                        if nxt is None:
                            self._stop = True
                            break
                        if nxt[0] == "text":
                            buf += nxt[1]
                        else:
                            self._flush_text(buf); buf = ""
                            self._do_key(nxt[1])
                    if buf:
                        self._flush_text(buf)
                else:
                    self._do_key(payload)
            except Exception as exc:
                self.note.emit(f"[WARNING] device keyboard: {exc}")

    def _flush_text(self, s):
        if s:
            self.handler.input_text(s, safe=False)

    def _do_key(self, code):
        self.handler.keyevent(code, safe=False)


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

    def __init__(self, handler, session, automotive=False, prefer_embed=False,
                 parent=None):
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
        self._retry_count = 0
        self._became_ready = False
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
        self._closing = False        # guards deferred callbacks (retry timers)
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

        # options live in a proper POPOVER panel (see _build_options_menu) —
        # the old checkable-menu tick list read poorly and its stay-open hack
        # felt broken; a real panel with checkboxes/radios + hints reads right
        self.btn_opts = QToolButton(); self.btn_opts.setText("⚙ Options")
        self.btn_opts.setProperty("role", "ghost")
        self.btn_opts.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn_opts.setPopupMode(QToolButton.InstantPopup)
        self.btn_opts.setToolTip("Mirror options (audio, IVI compatibility, "
                                 "software rendering, embed, keyboard, camera).")
        self.btn_opts.setMenu(self._build_options_menu(automotive, prefer_embed))
        self.btn_shot = QPushButton("📸 Screenshot"); self.btn_shot.setProperty("role", "ghost")
        self.btn_shot.setToolTip("Capture a PNG of the screen (works on any "
                                 "device — phone, tablet or head unit).")
        self.btn_shot.clicked.connect(self._take_screenshot)
        # secondary actions live in ONE ⋯ More menu — thirteen buttons in a
        # wrapping row was the "cluttered" look; the toolbar is now 8 items
        self.btn_more = QToolButton(); self.btn_more.setText("⋯ More")
        self.btn_more.setProperty("role", "ghost")
        self.btn_more.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn_more.setPopupMode(QToolButton.InstantPopup)
        mmore = QMenu(self.btn_more)
        mmore.addAction("📷  Mirror the device camera", self._mirror_camera)
        self.act_mirror_all = mmore.addAction(
            "▦  Mirror ALL displays (each in its own window)", self._mirror_all)
        self.act_mirror_all.setEnabled(False)
        mmore.addAction("↻  Re-scan displays", self.refresh_displays)
        mmore.addSeparator()
        mmore.addAction("⌨  Type / paste a block of text…", self._type_text)
        mmore.addSeparator()
        self.act_max = mmore.addAction("⛶  Max view (hide side panels)")
        self.act_max.setCheckable(True)
        self.act_max.toggled.connect(self._toggle_max)
        self.btn_more.setMenu(mmore)

        for w in (self.btn_mirror, self.btn_live, self.btn_stop,
                  self.btn_record, self.btn_shot,
                  QLabel("Display:"), self.cmb_display,
                  self.btn_opts, self.btn_more):
            bar.addWidget(w)
        lay.addWidget(bar_w)

        self.status = QLabel("▶ Mirror = full scrcpy (best local/USB) · "
                             "🖥 Live View = works anywhere (RDP / IVI) · "
                             "settings under ⚙ Options")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        # ---- the DEVICE KEYBOARD bar (shown while mirroring / Live View) ----
        # A capture field that forwards every keystroke to the device over adb —
        # guaranteed typing on ANY device (phone or IVI, embedded or separate
        # window, local or RDP), independent of Win32 focus games. The 🎯 button
        # appears when the mirror is embedded and hands it the real keyboard.
        self.kb_bar = QWidget()
        eb = QHBoxLayout(self.kb_bar)
        eb.setContentsMargins(4, 2, 4, 2); eb.setSpacing(8)
        self.kb_input = _DeviceKeyEdit(self._send_key)
        self.kb_input.setPlaceholderText(
            "⌨ click here, then type — every key goes live to the device "
            "(Enter, Backspace, arrows too)")
        eb.addWidget(self.kb_input, 1)
        self.btn_kb_focus = QPushButton("🎯 Mirror keys")
        self.btn_kb_focus.setProperty("role", "ghost")
        self.btn_kb_focus.setToolTip(
            "Hand the REAL keyboard back to the embedded mirror window (scrcpy "
            "then injects keys itself). The field on the left always works "
            "regardless.")
        self.btn_kb_focus.clicked.connect(self._focus_embedded)
        self.btn_kb_focus.hide()
        eb.addWidget(self.btn_kb_focus)
        self.kb_bar.hide()
        lay.addWidget(self.kb_bar)
        self._key_pump = None

        # native container that scrcpy gets reparented into
        self.container = QWidget()
        self.container.setAttribute(Qt.WA_NativeWindow, True)
        self.container.setStyleSheet("background:#000;")
        self.container.setMinimumHeight(200)
        self.container.setFocusPolicy(Qt.NoFocus)   # never steal from the child
        self.container.installEventFilter(self)     # click margins -> focus mirror
        lay.addWidget(self.container, 1)

        # screencap-based live view (shown instead of the container when active)
        self.live_view = _LiveView()
        self.live_view.setMinimumHeight(200)
        self.live_view.hide()
        self.live_view.tapped.connect(self._live_tap)
        self.live_view.swiped.connect(self._live_swipe)
        self.live_view.key_typed.connect(self._send_key)   # type ON the image
        lay.addWidget(self.live_view, 1)

        # Load the display list LAZILY — only the first time this Mirror tab is
        # actually shown, not on connect. (Auto-loading on connect launched scrcpy
        # immediately — twice, with the Control+Mirror view — which looked like the
        # app "auto-starting" something. Now nothing scrcpy-related runs until you
        # open the Mirror tab.)
        self._displays_loaded = False

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_displays_loaded", True):
            self._displays_loaded = True
            QTimer.singleShot(150, self.refresh_displays)
        # returning to a tab with an embedded mirror: hand it the keyboard again
        if self._child_hwnd:
            QTimer.singleShot(120, self._focus_embedded)

    def eventFilter(self, obj, event):
        # a click on the container (its margins around the embedded child)
        # forwards keyboard focus to the mirror — matches what users expect
        from PyQt5.QtCore import QEvent
        if obj is self.container and event.type() == QEvent.MouseButtonPress \
                and self._child_hwnd:
            self._focus_embedded()
        return super().eventFilter(obj, event)

    def _focus_embedded(self):
        _focus_window(self._child_hwnd)

    def _keep_embed_focus(self):
        """Keep keyboard focus on the embedded scrcpy window while the pointer is
        over the mirror and TurboADB is the active app — so you type straight
        into the screen. Cheap and non-intrusive: only acts over the mirror area,
        and only calls SetFocus when the child isn't already focused (no Alt
        nudge here, unlike the one-shot 🎯 button)."""
        if not self._child_hwnd or not _IS_WIN:
            return
        try:
            from PyQt5.QtGui import QCursor
            from PyQt5.QtWidgets import QApplication
            # if the user is deliberately typing in the tool's keyboard field,
            # don't steal focus back to the mirror
            if QApplication.focusWidget() is self.kb_input:
                return
            ctypes, u = _win_api()
            top = int(self.window().winId())
            fg = u.GetForegroundWindow()
            # only when OUR window (or a child of it) is the foreground one
            if fg != top and not u.IsChild(top, fg):
                return
            # only when the cursor is over the mirror container
            gp = QCursor.pos()
            tl = self.container.mapToGlobal(self.container.rect().topLeft())
            br = self.container.mapToGlobal(self.container.rect().bottomRight())
            if not (tl.x() <= gp.x() <= br.x() and tl.y() <= gp.y() <= br.y()):
                return
            k = ctypes.windll.kernel32
            tid = u.GetWindowThreadProcessId(self._child_hwnd, None)
            mytid = k.GetCurrentThreadId()
            attached = False
            if tid and tid != mytid:
                attached = bool(u.AttachThreadInput(mytid, tid, True))
            if u.GetFocus() != self._child_hwnd:      # avoid needless churn
                u.SetFocus(self._child_hwnd)
            if attached:
                u.AttachThreadInput(mytid, tid, False)
        except Exception:
            pass

    def _send_key(self, kind, payload):
        """Forward one keyboard-bar event to the device via the pump thread
        (started lazily on first use)."""
        if self._key_pump is None or not self._key_pump.isRunning():
            self._key_pump = _KeyPumpThread(self.handler)
            self._key_pump.note.connect(self.log)
            self._key_pump.start()
        self._key_pump.push(kind, payload)

    def _kb_mode(self):
        """The scrcpy --keyboard mode from the options popover: None = scrcpy's
        own default (SDK — identical to running scrcpy by hand), 'uhid' only
        when explicitly chosen."""
        return "uhid" if self.act_kb_uhid.isChecked() else None

    def _build_options_menu(self, automotive, prefer_embed):
        """The ⚙ Options POPOVER: a real, themed panel (via QWidgetAction) with
        grouped checkboxes, radio groups and per-option hints — replacing the
        old checkable menu whose tiny tick marks and stay-open hack read as
        broken. Toggling anything keeps the popover open; click outside (or the
        Done button) to dismiss."""
        from PyQt5.QtWidgets import (QWidgetAction, QRadioButton, QButtonGroup)
        from . import theme, settings as _s
        _name = _s.get("theme")
        atext = theme.accent_text(_name)
        raised = theme.THEMES.get(_name, theme.THEMES["dark"])["raised"]

        menu = QMenu(self.btn_opts)
        panel = QWidget(menu)
        # match the menu's raised surface — the global QWidget rule would paint
        # the window colour here, leaving a mismatched slab inside the border
        panel.setStyleSheet(f"background: {raised};")
        panel.setMinimumWidth(330)
        v = QVBoxLayout(panel)
        v.setContentsMargins(14, 12, 14, 10)
        v.setSpacing(5)

        def section(text):
            lab = QLabel(text.upper())
            lab.setStyleSheet(f"color:{atext}; font-weight:800; "
                              f"font-size:8.5pt; letter-spacing:1px; "
                              f"padding-top:8px;")
            v.addWidget(lab)

        def hint(text):
            lab = QLabel(text)
            lab.setWordWrap(True)
            lab.setStyleSheet("font-size:8.5pt; padding-left:30px;")
            v.addWidget(lab)

        title = QLabel("Mirror options")
        title.setStyleSheet(f"color:{atext}; font-weight:800; font-size:11.5pt;")
        v.addWidget(title)

        section("Output")
        self.act_audio = QCheckBox("Forward audio")
        self.act_audio.setChecked(True)
        v.addWidget(self.act_audio)
        hint("play the device's sound on this PC (Android 11+)")
        self.act_compat = QCheckBox("Compatibility mode (IVI / automotive)")
        self.act_compat.setChecked(automotive)
        v.addWidget(self.act_compat)
        hint("H.264 + safe caps — for head-unit encoders that choke on defaults")
        self.act_soft = QCheckBox("Software rendering")
        self.act_soft.setChecked(self._rdp)
        v.addWidget(self.act_soft)
        hint("needed over Remote Desktop / GPU-less sessions")
        self.act_embed = QCheckBox("Embed mirror in this tab")
        self.act_embed.setChecked(prefer_embed)
        v.addWidget(self.act_embed)
        hint("the mirror lives inside TurboADB instead of a separate window")

        section("Keyboard")
        self.act_kb_sdk = QRadioButton("Standard (SDK) — like plain scrcpy")
        self.act_kb_sdk.setChecked(True)
        self.act_kb_uhid = QRadioButton("UHID hardware keyboard")
        self._kb_group = QButtonGroup(panel)
        self._kb_group.setExclusive(True)
        self._kb_group.addButton(self.act_kb_sdk)
        self._kb_group.addButton(self.act_kb_uhid)
        v.addWidget(self.act_kb_sdk)
        v.addWidget(self.act_kb_uhid)
        hint("UHID only if standard typing is ignored — most IVI kernels "
             "don't support it")

        section("Camera source")
        cam_row = QHBoxLayout()
        cam_row.setSpacing(14)
        self.act_cam_back = QRadioButton("Back")
        self.act_cam_back.setChecked(True)
        self.act_cam_front = QRadioButton("Front")
        self._cam_group = QButtonGroup(panel)
        self._cam_group.setExclusive(True)
        self._cam_group.addButton(self.act_cam_back)
        self._cam_group.addButton(self.act_cam_front)
        cam_row.addWidget(self.act_cam_back)
        cam_row.addWidget(self.act_cam_front)
        cam_row.addStretch(1)
        v.addLayout(cam_row)
        hint("used by the 📷 Camera button (scrcpy 2.2+, Android 12+)")

        done_row = QHBoxLayout()
        done_row.addStretch(1)
        done = QPushButton("Done")
        done.setProperty("role", "ghost")
        done.clicked.connect(menu.close)
        done_row.addWidget(done)
        v.addLayout(done_row)

        wa = QWidgetAction(menu)
        wa.setDefaultWidget(panel)
        menu.addAction(wa)
        return menu

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
        self.live_view.setFocus(Qt.OtherFocusReason)   # ready to type immediately
        self.log.emit("[OK] live view — click the image to tap/swipe, and just "
                      "TYPE to send keys to the device (works on any device)")
        self._live = _LiveThread(self.handler, max_fps=10.0)
        self._live.target_w = max(0, self.live_view.width())
        self._live.target_h = max(0, self.live_view.height())
        self._live.frame.connect(self._on_live_frame)
        self._live.note.connect(self.log)
        self._live.start()
        self._refresh_buttons()

    def _stop_live(self):
        if self._live is not None:
            live, self._live = self._live, None
            live.stop()
            try:                       # ignore late frames from a mid-flight
                live.frame.disconnect()          # screencap after we've stopped
            except Exception:
                pass
            if not live.wait(900):
                # a slow remote screencap is still running — DON'T drop the ref
                # (a GC of a live QThread hard-crashes); park it until it exits
                from .qtutil import park_thread
                park_thread(live)
        self.live_view.hide()
        self.container.show()
        self.status.show()
        self._refresh_buttons()
        self.log.emit("[OK] live view stopped")

    def _on_live_frame(self, img, dev_w, dev_h):
        # img arrives DECODED and pre-scaled from the worker — turning it into a
        # pixmap is the only work left on the UI thread, so no more stutter
        from PyQt5.QtGui import QPixmap
        self._dev_w, self._dev_h = dev_w, dev_h    # native size for tap mapping
        pm = QPixmap.fromImage(img)
        if not pm.isNull():
            self.live_view.set_frame(pm)
        if self._live is not None:                 # track view resizes for the
            self._live.target_w = self.live_view.width()      # next frame
            self._live.target_h = self.live_view.height()

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
        embedded = self._child_hwnd is not None
        self.btn_mirror.setEnabled(not viewing)
        self.btn_live.setEnabled(not viewing)
        self.act_mirror_all.setEnabled(not viewing and len(self._displays) > 1)
        self.btn_stop.setEnabled(viewing)
        self.btn_record.setEnabled(viewing or self._recording)
        self.btn_record.setText("⏹ Stop recording" if self._recording else "🔴 Record…")
        # The device-keyboard bar is only for cases where scrcpy's OWN native
        # keyboard isn't available: the EMBEDDED window (foreign-window focus is
        # unreliable) and Live View (screencap has no input at all). A separate
        # scrcpy window types natively — just click it — so no bar is pushed on
        # you there.
        live = self._live is not None
        self.kb_bar.setVisible(embedded or live)
        self.btn_kb_focus.setVisible(embedded)

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
                keyboard_mode=self._kb_mode(),
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

    # ----- mirror the device CAMERA (scrcpy 2.2+, Android 12+) -----
    def _mirror_camera(self):
        """Open the device's CAMERA as the video source instead of the screen —
        a live webcam-style view (front or back, chosen under ⚙ Options). Needs
        scrcpy 2.2+ on the host and Android 12+ on the device."""
        facing = "front" if self.act_cam_front.isChecked() else "back"
        self.log.emit(f"Opening the device {facing} camera (scrcpy)…")
        self.start(camera=facing)

    def _toggle_max(self, on=None):
        """Hide/show the main window's docks so the mirror gets the whole window
        — the practical way to make a portrait device screen big and readable."""
        from PyQt5.QtWidgets import QMainWindow, QDockWidget
        win = self.window()
        if not isinstance(win, QMainWindow):
            return
        if on is None:
            on = self.act_max.isChecked()
        if on:
            # only hide docks that are showing, and restore exactly those —
            # blanket-showing everything un-hid docks the user had closed
            self._max_hidden = [d for d in win.findChildren(QDockWidget)
                                if d.isVisible()]
            for d in self._max_hidden:
                d.setVisible(False)
        else:
            for d in getattr(self, "_max_hidden", []):
                try:
                    d.setVisible(True)
                except RuntimeError:
                    pass
            self._max_hidden = []
        self.act_max.setText("⛶  Restore side panels" if on
                             else "⛶  Max view (hide side panels)")
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
        self.act_mirror_all.setEnabled(len(self._displays) > 1)
        self.log.emit(f"[OK] {len(self._displays)} display(s) found")

    def _tune(self, opts):
        """Apply the 'software render (RDP)' choice and, in that mode, sensible
        caps so the picture stays smooth over a remote/GPU-less link. The old
        1024 px / 4M caps looked visibly soft and blocky — 1280 px / 8M is still
        smooth in software rendering (with linear SDL scaling set at launch) but
        markedly sharper. Settings → scrcpy overrides these when set."""
        if self.act_soft.isChecked():
            opts.render_driver = "software"
            opts.max_size = opts.max_size or 1280
            opts.bit_rate = opts.bit_rate or "8M"
            opts.max_fps = opts.max_fps or 30
            if not opts.no_audio:
                self.log.emit("[INFO] tip: untick ⚙ Options → Forward audio for "
                              "an even smoother mirror over Remote Desktop "
                              "(audio costs extra CPU + bandwidth there)")
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
            self._rec_wait.done.connect(self._on_rec_wait_done)
            self._rec_wait.start()
        elif self._rec_stop is not None:
            self._rec_stop.set()             # tell screenrecord to stop & pull

    def _on_rec_wait_done(self, clean):
        if not clean:
            self.log.emit("[WARNING] the recorder didn't close cleanly and had "
                          "to be force-stopped — the video file may be "
                          "truncated; check it plays before relying on it")
        self._record_finished([self._rec_path])

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
              record=None, record_format=None, camera=None, _retry=False):
        if self._closing:                        # tab closed during a retry delay
            return
        if not _retry:
            self._retry_count = 0                # a fresh manual start
        if self._live is not None:               # switch away from Live View
            self._stop_live()
        if self._scrcpy is not None:
            self.stop()
        if display_id == "__use_combo__":
            display_id = self.cmb_display.currentData()
        # The camera is its own video source — it has no display id and scrcpy
        # disables control for it, so a plain separate window is the safe choice.
        if camera:
            display_id = None
            embed = False
        if compat is None:
            compat = self.act_compat.isChecked()
        if embed is None:
            embed = self.act_embed.isChecked()
        elif embed:
            self.act_embed.setChecked(True)
        # Over Remote Desktop keep the compat profile, but HONOUR the embed
        # choice — it used to be silently disabled here, which is why "embed"
        # appeared to do nothing at all on RDP setups.
        if self.act_soft.isChecked():
            compat = True
            if embed:
                self.log.emit("[INFO] embedding over Remote Desktop — if the "
                              "picture stays black, untick “Embed window in "
                              "this tab” and mirror in a separate window")

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
            video_source=("camera" if camera else None),
            camera_facing=(camera or None),
            keyboard_mode=self._kb_mode(),
            window_title=self.session_dict.get("name") or "turboadb")
        self._tune(opts)
        if camera:
            self.status.setText(f"Camera ({camera}) — live view")
        if opts.render_driver == "software":
            self.log.emit("[INFO] using software rendering (Remote Desktop / "
                          "GPU-less) — capped to keep it smooth")
        if opts.keyboard_mode == "uhid":
            self.log.emit("[WARNING] keyboard = UHID (explicitly selected). Most "
                          "IVI/head-unit kernels have NO uhid support — if typing "
                          "does nothing, switch ⚙ Options → Keyboard mode back to "
                          "Standard (SDK), which is what plain scrcpy uses.")

        do_embed = embed and _IS_WIN
        if do_embed:
            opts.window_title = f"turboadb-embed-{os.getpid()}-{id(self)}"
            opts.window_borderless = True
            opts.window_x, opts.window_y = 0, 0
        # the EXACT window title we can later find to confirm scrcpy really put a
        # window up (readiness) and, when embedding, to reparent it
        self._win_title = opts.window_title

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
        self._became_ready = False           # set once it renders / embeds
        self._refresh_buttons()
        # watch for the scrcpy window being closed so the buttons reset
        self._mon = QTimer(self)
        self._mon.timeout.connect(self._check_alive)
        self._mon.start(500)
        if do_embed:
            self.status.setText("Starting mirror… embedding the scrcpy window.")
            self._embed_tries = 0
            self._embed_timer = QTimer(self)
            self._embed_timer.timeout.connect(self._try_embed)
            self._embed_timer.start(200)
            self.log.emit("[OK] scrcpy launching (embedded)…")
        else:
            self.status.setText("Mirroring in a separate window — click it and "
                                "type / use the mouse directly (native scrcpy).")
            self.log.emit("[OK] scrcpy launched in a separate window — keyboard "
                          "and mouse work DIRECTLY in that window, exactly like "
                          "plain scrcpy (no need for the tool's keyboard bar)")

    # markers that prove scrcpy actually brought up VIDEO (so a later exit is a
    # normal close, NOT a failed start). Deliberately NOT "Device:" / "New
    # display" — scrcpy prints those the instant it CONNECTS, before it opens a
    # decoder, so a connect-then-die failure used to look "started" and was left
    # alone (the "fake started" bug). Only a renderer/texture/recording line
    # means a frame actually flowed.
    _HEALTHY = ("Renderer:", "Texture:", "Recording started", "INFO: Renderer",
                "Frame: ", "v4l2", "audio player")
    _STARTUP_TIMEOUT = 6.0        # no scrcpy window within this = hung/failed

    def _scrcpy_window_up(self) -> bool:
        """True once scrcpy has actually put its window up — the reliable
        readiness signal for BOTH embed and separate-window modes. (The log is
        block-buffered to a file, so its 'Renderer:' markers don't appear until
        scrcpy exits; the WINDOW existing is what proves a live start.)"""
        if self._child_hwnd is not None:
            return True
        if not _IS_WIN:
            # no cheap window probe off Windows — fall back to a time grace
            import time
            return (time.time() - getattr(self, "_start_t", 0)) > self._STARTUP_TIMEOUT
        title = getattr(self, "_win_title", None)
        if not title:
            return False
        try:
            return bool(_find_window(title))
        except Exception:
            return False

    def _check_alive(self):
        """Poll the scrcpy process. Readiness = its window is up (works for embed
        AND separate window). A launch that never shows a window within
        _STARTUP_TIMEOUT is hung/failed → killed and auto-retried. On exit, a
        never-ready start is a failure (retry, then show the real log); a
        ready→exit is a normal close."""
        if self._scrcpy is None:
            return
        import time
        err_live = self._scrcpy.read_log() or ""
        # readiness = the window is up (fast, while running) OR a video marker is
        # in the log (a fallback that lands at exit, when the buffered log flushes
        # — covers scrcpy builds whose window title we can't match)
        if not self._became_ready and (self._scrcpy_window_up()
                or any(m in err_live for m in self._HEALTHY)):
            self._became_ready = True

        running = self._scrcpy.running
        ran_s = time.time() - getattr(self, "_start_t", 0)
        # startup watchdog: alive but no window yet = it hung (the first-launch
        # server-push race can leave scrcpy waiting forever) — kill it so the
        # retry path runs instead of the user staring at nothing
        if running and not self._became_ready and ran_s > self._STARTUP_TIMEOUT:
            self.log.emit(f"[WARNING] scrcpy didn't show a window within "
                          f"{int(self._STARTUP_TIMEOUT)}s — treating as a failed "
                          f"start and retrying.")
            try:
                self._scrcpy.stop()
            except Exception:
                pass
            running = False
        if running:
            return

        err = (self._scrcpy.read_log() or "").strip()
        ready = self._became_ready
        self._scrcpy = None
        self._stop_embed_timer()
        self._child_hwnd = None
        if getattr(self, "_mon", None):
            self._mon.stop(); self._mon = None
        self._refresh_buttons()
        self.status.show()

        if ready:
            self._retry_count = 0
            self.status.setText("Mirror ended. Click “▶ Mirror” to view again.")
            self.log.emit("[OK] mirror ended")
            return

        # ---- failed to start: auto-retry with safer options, then explain ----
        tries = getattr(self, "_retry_count", 0)
        tail = " ".join(err.splitlines()[-2:])[:200] or "no output from scrcpy"
        if tries == 0:
            # 1st retry: SAME settings. Most first-launch failures — on ordinary
            # phones too — are transient: scrcpy pushing/starting its server on
            # the device races the fresh adb connection, so a plain second
            # attempt (server jar now in place) simply works. This IS the "starts
            # on the 2nd try" behaviour, now done automatically.
            self._retry_count = 1
            self.log.emit(f"[WARNING] scrcpy didn't start ({tail}); retrying…")
            self.status.setText("scrcpy didn't start — retrying…")
            QTimer.singleShot(600, lambda: self.start(_retry=True))
            return
        if tries == 1:
            # 2nd retry: compatibility profile in a SEPARATE window (H.264 + caps
            # + forward tunnel + no embed) — the safe fallback that fixes
            # automotive/IVI encoder, reverse-tunnel and embed failures
            self._retry_count = 2
            self.log.emit("[WARNING] still didn't start; one more try in "
                          "compatibility mode, separate window…")
            self.status.setText("Retrying in compatibility mode…")
            QTimer.singleShot(600, lambda: self.start(compat=True, embed=False,
                                                      _retry=True))
            return
        # gave up after 3 real attempts — show scrcpy's ACTUAL output + reason
        self._retry_count = 0
        self.log.emit("[ERROR] scrcpy could not start after 3 attempts. "
                      "Last output: " + tail)
        self.status.setText("Mirror couldn't start — see the log below.")
        self._show_scrcpy_log(err or "(scrcpy exited immediately with no output. "
                              "It likely couldn't reach the device, or couldn't "
                              "open a video encoder for this display.)")

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
            self._scrcpy = None
            self._refresh_buttons()
            return
        # wait until the container has a real size before adopting the window,
        # otherwise the first resize snaps scrcpy to a tiny rectangle
        if self.container.width() < 80 or self.container.height() < 80:
            return
        try:
            hwnd = _find_window(self._win_title)
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
                self._refresh_buttons()          # shows the 🎯 Mirror keys button
                # hand the keyboard straight to the mirror, so typing works
                # immediately — exactly like clicking into native scrcpy
                QTimer.singleShot(200, self._focus_embedded)
                self.log.emit("[OK] scrcpy embedded — move the mouse over the "
                              "screen and type: keys go straight to the device. "
                              "(The ⌨ bar and 🎯 button remain as fallbacks.)")
                # scrcpy/SDL resizes its own window to the video frame after the
                # first frame — keep forcing it to fill the container for a few
                # seconds so it doesn't shrink back to a tiny window.
                self._fit_timer = QTimer(self)
                self._fit_timer.timeout.connect(self._fit)
                self._fit_timer.start(500)     # keeps it filling the whole session
                self._fit()
                # KEYBOARD focus keeper: while the mouse is over the embedded
                # mirror and TurboADB is the active app, keep the scrcpy child
                # focused so you can type DIRECTLY into the screen (a reparented
                # window doesn't hold keyboard focus on its own — that's why
                # typing needed the bar). Runs only over the mirror, so clicking
                # the controls / the ⌨ bar still works normally.
                self._kbfocus_timer = QTimer(self)
                self._kbfocus_timer.timeout.connect(self._keep_embed_focus)
                self._kbfocus_timer.start(300)
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
        keeps resizing itself to the video frame, so we keep correcting it).
        Skips the call when the window ALREADY fills the container — the old
        unconditional MoveWindow every 500 ms made SDL re-handle a resize twice
        a second forever, a steady source of embedded-mirror stutter."""
        if not self._child_hwnd:
            return
        w = max(1, self.container.width())
        h = max(1, self.container.height())
        try:
            import ctypes
            from ctypes import wintypes
            _c, u = _win_api()
            rect = wintypes.RECT()
            if u.GetWindowRect(self._child_hwnd, ctypes.byref(rect)):
                if (rect.right - rect.left) == w and (rect.bottom - rect.top) == h:
                    return                       # already the right size
            u.MoveWindow(self._child_hwnd, 0, 0, w, h, True)
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
        if getattr(self, "_kbfocus_timer", None):
            self._kbfocus_timer.stop()
            self._kbfocus_timer = None

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
        self._closing = True            # neutralise any pending retry timer
        self._stop_live()               # parks the thread if it's still running
        if self._key_pump is not None:
            self._key_pump.stop()
            if not self._key_pump.wait(700):
                from .qtutil import park_thread
                park_thread(self._key_pump)
            self._key_pump = None
        self._finalize_rec_sync()
        if self._shot is not None:
            self._shot.wait(700)
        # keep still-running worker threads alive past this widget's destruction
        from .qtutil import park_thread
        park_thread(getattr(self, "_dt", None))
        park_thread(getattr(self, "_rec_wait", None))
        park_thread(getattr(self, "_rec_thread", None))
        self.stop()
