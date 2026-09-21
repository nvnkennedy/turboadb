"""Embedded scrcpy mirror — the scrcpy window lives INSIDE a tab instead of a
separate floating window.

On Windows this launches scrcpy borderless, finds its window by a unique title,
and reparents it (Win32 ``SetParent``) into a Qt container that follows resizes.
If embedding can't work (non-Windows, scrcpy not found, or — common over Remote
Desktop — no GPU/decoder for scrcpy), it cleanly falls back to an external
window and says so, so you're never stuck."""

from __future__ import annotations

import atexit
import itertools
import os
import re
import threading
import time

from PyQt5.QtCore import (
    Qt, QEvent, QObject, QPointF, QRectF, QSize, QThread, pyqtSignal, QTimer
)
from PyQt5.QtGui import QColor, QKeySequence, QPainter, QPen
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QComboBox,
    QCheckBox,
    QSpinBox,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QFileDialog,
    QToolButton,
    QMenu,
    QAction,
    QDialog,
    QGridLayout,
)

from ..config import ScrcpyOptions
from ..results import OperationResult
from ..scrcpy import is_remote_session
from . import settings as settings_mod
from .device_commands import DeviceCommandDispatcher
from .fileutil import download_dir, saved_dialog
from .flowlayout import ToolbarFlowLayout
from .icons import icon as _icon
from .qtutil import FunctionThread, park_thread, thread_running

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
    u.GetParent.restype = wintypes.HWND
    u.GetParent.argtypes = [wintypes.HWND]
    u.MoveWindow.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.BOOL,
    ]
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
    u.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    u.SetFocus.restype = wintypes.HWND
    u.SetFocus.argtypes = [wintypes.HWND]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.AttachThreadInput.restype = wintypes.BOOL
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.GetAncestor.restype = wintypes.HWND
    u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
    u.GetFocus.restype = wintypes.HWND
    u.GetFocus.argtypes = []
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetForegroundWindow.argtypes = []
    u.IsChild.restype = wintypes.BOOL
    u.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
    return ctypes, u


_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_EX_NOPARENTNOTIFY = 0x00000004
_WM_PARENTNOTIFY = 0x0210
# WM_PARENTNOTIFY's LOWORD(wParam) for a mouse button pressed over a child
# window: WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN and
# WM_POINTERDOWN (pen / touch).
_PARENTNOTIFY_BUTTON_DOWN = frozenset((0x0201, 0x0204, 0x0207, 0x020B, 0x0246))
_WS_CHILD = 0x40000000
_WS_POPUP = 0x80000000
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020
_GA_PARENT = 1
_GA_ROOT = 2


def _set_restype(fn, restype) -> None:
    """Declare a pointer-sized return (ctypes defaults to a 32-bit C int)."""
    try:
        fn.restype = restype
    except (AttributeError, TypeError):  # plain Python test doubles
        pass


def _reparent(child_hwnd, parent_hwnd, w, h):
    """Adopt the scrcpy/SDL window as a real Win32 child (WS_CHILD) — the same
    approach every embedder (VLC, mpv, Chromium plugin host) uses.

    A window is only a first-class part of the parent's focus/activation chain
    when it carries the WS_CHILD style; a bare SetParent leaves it a top-level
    popup that Windows activates separately, so a real click into it fights the
    parent for activation. Converting to WS_CHILD is what lets a mouse click
    route keyboard focus to it normally.

    A click on the child sends WM_PARENTNOTIFY to the container, so the child
    must not carry WS_EX_NOPARENTNOTIFY. That notification hands Win32 keyboard
    focus to the SDL child (scrcpy then injects keys itself); only if Windows
    refuses does the container keep the keyboard and forward keys over ADB.

    Order matters. Win32 documents that WS_CHILD must be set (and WS_POPUP
    cleared) *before* ``SetParent`` when adopting a top-level window, and
    ``GetParent`` of a WS_POPUP window returns its *owner* — none — rather than
    the window it was just attached to. Checking ``GetParent`` straight after
    ``SetParent`` therefore always "failed" for scrcpy's borderless popup and
    pushed every embed out into a separate window. The attachment is verified
    with ``GetAncestor(GA_PARENT)``, which reports the real parent for any style.
    """
    import ctypes

    _ctypes, u = _win_api()
    getter = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
    setter = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
    _set_restype(u.GetAncestor, ctypes.c_void_p)
    style = getter(child_hwnd, _GWL_STYLE)
    style = (style | _WS_CHILD) & ~(_WS_POPUP | _WS_CAPTION | _WS_THICKFRAME)
    setter(child_hwnd, _GWL_STYLE, style)
    u.SetParent(child_hwnd, parent_hwnd)
    if int(u.GetAncestor(child_hwnd, _GA_PARENT) or 0) != int(parent_hwnd):
        raise OSError("Windows rejected the scrcpy child-window attachment")
    if getter(child_hwnd, _GWL_STYLE) & _WS_CHILD == 0:
        raise OSError("Windows did not apply the embedded-window style")
    # A click on the video reaches the container only as WM_PARENTNOTIFY.
    ex_style = getter(child_hwnd, _GWL_EXSTYLE)
    if ex_style & _WS_EX_NOPARENTNOTIFY:
        setter(child_hwnd, _GWL_EXSTYLE, ex_style & ~_WS_EX_NOPARENTNOTIFY)
    if not u.SetWindowPos(
        child_hwnd,
        None,
        0,
        0,
        max(1, w),
        max(1, h),
        _SWP_NOZORDER | _SWP_FRAMECHANGED,
    ):
        raise OSError("Windows could not size the embedded scrcpy window")
    u.ShowWindow(child_hwnd, 5)  # SW_SHOW


def _focus_window(hwnd) -> bool:
    """Give keyboard focus to a native (cross-process) scrcpy window.

    Brings the owner window forward, then — because SetFocus only acts within
    the caller thread's input queue — attaches to the scrcpy window's input
    thread before focusing it. Do not synthesize Alt here: Qt treats it as a
    menu activation and may route the next key to Help/Documentation."""
    if not hwnd:
        return False
    try:
        ctypes, u = _win_api()
        k = ctypes.windll.kernel32
        # 1) the toolbar/device click already makes TurboADB foreground. Restore
        #    its owner without sending Alt, which activates Qt's menu bar.
        top = u.GetAncestor(hwnd, 2) if hasattr(u, "GetAncestor") else 0  # GA_ROOT
        if top:
            u.SetForegroundWindow(top)
        # 2) attach to the scrcpy thread's input queue, then focus its window
        target_tid = u.GetWindowThreadProcessId(hwnd, None)
        my_tid = k.GetCurrentThreadId()
        attached = False
        if target_tid and target_tid != my_tid:
            attached = bool(u.AttachThreadInput(my_tid, target_tid, True))
        u.SetFocus(hwnd)
        focused = int(u.GetFocus() or 0) == int(hwnd)
        if attached:
            u.AttachThreadInput(my_tid, target_tid, False)
        return focused
    except Exception:
        return False


def _claim_native_focus(hwnd) -> bool:
    """Give Win32 keyboard focus to one of this thread's own windows.

    A click on the embedded scrcpy video can leave the keyboard with SDL's child
    window (another process, whose input queue the parent/child link attaches
    to ours).  Qt's ``setFocus`` alone does not move Win32 focus back, so do it
    explicitly for the container HWND.
    """
    if not _IS_WIN or not hwnd:
        return False
    try:
        _ctypes, u = _win_api()
        if int(u.GetFocus() or 0) != int(hwnd):
            u.SetFocus(hwnd)
        return int(u.GetFocus() or 0) == int(hwnd)
    except Exception:
        return False


def _focus_embedded_child(child_hwnd, parent_hwnd) -> bool:
    """Give Win32 keyboard focus to scrcpy's embedded SDL child window.

    With the focus there Windows delivers keys to scrcpy itself, which injects
    them over its control socket: fast, with correct key repeat and release,
    and no adb process per key.  The child belongs to another process, so this
    attaches to its input thread (as :func:`_focus_window` does, but with no
    foreground change and no Alt nudge), checks ``GetFocus`` while attached,
    and after detaching confirms this thread's keyboard input still reaches
    the child.  Returns False when Windows refuses; the caller then keeps the
    keyboard in the container and uses the ADB route.

    When the container's own HWND holds the focus, it is parked on the
    top-level window first: Qt reads focus moving from a native widget
    straight to its foreign child as the whole application losing activation,
    after which clicks no longer focus TurboADB's widgets.
    """
    if not _IS_WIN or not child_hwnd or not parent_hwnd:
        return False
    try:
        _ctypes, u = _win_api()
        child, parent = int(child_hwnd), int(parent_hwnd)
        if not u.IsChild(parent, child):
            return False  # a stale handle, or no longer embedded here
        current = int(u.GetFocus() or 0)
        if current == child:
            return True
        target_tid = int(u.GetWindowThreadProcessId(child, None) or 0)
        if not target_tid:
            return False
        if current == parent:
            root = int(u.GetAncestor(parent, _GA_ROOT) or 0)
            if root and root != parent:
                u.SetFocus(root)
        my_tid = int(threading.get_native_id())
        attached = False
        if target_tid != my_tid:
            attached = bool(u.AttachThreadInput(my_tid, target_tid, True))
        try:
            u.SetFocus(child)
            focused = int(u.GetFocus() or 0) == child
        finally:
            if attached:
                u.AttachThreadInput(my_tid, target_tid, False)
        return focused and int(u.GetFocus() or 0) == child
    except Exception:
        return False


def _release_child_focus(child_hwnd, target_hwnd, *, if_lost=False) -> bool:
    """Move Win32 keyboard focus from the embedded SDL child to *target_hwnd*.

    Acts only while the child still holds the focus, so it never takes the
    keyboard from any other window.  With *if_lost* it also acts when no
    window has the focus (the child was just destroyed): keys typed into a
    focus-less active window arrive as system keys and open Qt menus.
    Returns whether the focus was moved.
    """
    if not _IS_WIN or not child_hwnd or not target_hwnd:
        return False
    try:
        _ctypes, u = _win_api()
        current = int(u.GetFocus() or 0)
        if current != int(child_hwnd) and not (if_lost and current == 0):
            return False
        u.SetFocus(int(target_hwnd))
        return int(u.GetFocus() or 0) == int(target_hwnd)
    except Exception:
        return False


def _is_child_button_down(event_type, message) -> bool:
    """True for a WM_PARENTNOTIFY reporting a mouse button pressed over a child
    window — for the embed container, a click on the scrcpy video."""
    if not _IS_WIN:
        return False
    try:
        kind = event_type.data() if hasattr(event_type, "data") else bytes(event_type)
        if kind != b"windows_generic_MSG":
            return False
        address = int(message)
        if not address:
            return False
        from ctypes import wintypes

        msg = wintypes.MSG.from_address(address)
        return (
            msg.message == _WM_PARENTNOTIFY
            and (int(msg.wParam or 0) & 0xFFFF) in _PARENTNOTIFY_BUTTON_DOWN
        )
    except Exception:
        return False


def _find_window(title):
    """Find a scrcpy window by title, accepting harmless SDL title suffixes.

    ``FindWindowW`` is ideal for the normal exact title path.  Some SDL builds
    append a renderer/device suffix, though, which made a perfectly healthy
    separate window look "missing" to our startup watchdog and get killed.
    Fall back to an EnumWindows scan only when the exact lookup did not find
    anything, and even there take an exactly-titled window over a merely
    matching one: the titles carry a per-launch nonce, so a substring hit that
    is not exact is the least trustworthy answer of the two.
    """
    if not title:
        # FindWindowW(None, None) returns an arbitrary top-level window.
        return None
    ctypes, u = _win_api()
    hwnd = u.FindWindowW(None, title)
    if hwnd:
        return hwnd
    try:
        from ctypes import wintypes

        expected = str(title or "").casefold()
        if not expected:
            return None
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.restype = wintypes.BOOL
        u.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        exact, similar = [], []

        @callback_type
        def visit(candidate, _lparam):
            length = u.GetWindowTextLengthW(candidate)
            if length:
                text = ctypes.create_unicode_buffer(length + 1)
                u.GetWindowTextW(candidate, text, length + 1)
                title_text = text.value.casefold()
                if title_text == expected:
                    exact.append(candidate)
                    return False  # nothing can beat an exact title
                if expected in title_text:
                    similar.append(candidate)
            return True

        u.EnumWindows(visit, 0)
        found = exact or similar
        return found[0] if found else None
    except Exception:
        return None


def _window_ready(hwnd) -> bool:
    """True once SDL has shown scrcpy's window.

    SDL creates the window hidden at a small placeholder size and only shows it
    (resized to the video) once the renderer has its first frame — about half a
    second after the handle first appears on a warm start, several seconds on a
    cold adb start. Re-parenting the hidden placeholder made scrcpy exit ~2 s
    later, which showed up as "didn't start — retrying" on the first click.
    """
    if not hwnd:
        return False
    try:
        _ctypes, u = _win_api()
        return bool(u.IsWindowVisible(hwnd))
    except Exception:
        return False


def _window_aspect(hwnd):
    """Width / height of a window's client area, or None."""
    if not hwnd:
        return None
    try:
        ctypes, u = _win_api()
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not u.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        return width / height if width >= 64 and height >= 64 else None
    except Exception:
        return None


def _post_close(title, hwnd=None) -> bool:
    """Ask a scrcpy window to close politely — WM_CLOSE.

    An embedded SDL window is no longer a top-level window, so it cannot always
    be rediscovered by title.  Prefer the retained child handle in that case.
    scrcpy treats WM_CLOSE like Ctrl+C and flushes the MP4 index before exit.

    ``scrcpy._post_close_to_pid`` cannot replace this: EnumWindows only visits
    top-level windows, so it never finds an embedded (WS_CHILD) scrcpy window.
    Callers run this on a worker thread; every Win32 call here is a synchronous
    message to scrcpy's thread, and a hung scrcpy must not freeze the UI.
    """
    if not _IS_WIN:
        return False
    from ctypes import wintypes

    _ctypes, u = _win_api()
    hwnd = hwnd or _find_window(title)
    if not hwnd:
        return False
    # If the window is currently reparented as a child (WS_CHILD), detach it back to
    # the desktop so Win32 DefWindowProc and SDL2 deliver WM_CLOSE to the event pump
    try:
        getter = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
        setter = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
        style = getter(hwnd, _GWL_STYLE)
        if style & _WS_CHILD:
            # SetParent keeps the child's coordinates, so a window detached
            # at (0, 0) flashed at the desktop origin until scrcpy handled
            # WM_CLOSE.  Move it off-screen first.
            u.SetWindowPos(
                hwnd, None, -32000, -32000, 0, 0,
                _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE,
            )
            u.SetParent(hwnd, None)
            style = (style | _WS_POPUP) & ~_WS_CHILD
            setter(hwnd, _GWL_STYLE, style)
    except Exception:
        pass
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    return bool(u.PostMessageW(hwnd, 0x0010, 0, 0))  # WM_CLOSE


_window_nonce = itertools.count(1)


def _next_window_nonce() -> str:
    """A strictly increasing tag that makes each scrcpy window title unique.

    Windows' monotonic clock is coarser than a quick Stop/Start, so a timestamp
    alone can repeat; a counter cannot, and the pid keeps it unique between
    TurboADB processes.
    """
    return f"{next(_window_nonce):x}"


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


def _read_log_tail(session, limit: int = 65536) -> str:
    """The last *limit* bytes of a scrcpy session log.

    The readiness poll runs every 500 ms on the UI thread; re-reading a log
    that grows for the whole session there is wasted work.
    """
    path = getattr(session, "log_path", None)
    if not path:
        try:
            return session.read_log() or ""
        except Exception:
            return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - limit))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# Logs of ended scrcpy sessions.  Windows refuses to delete a log that a
# stopping scrcpy still holds open, so entries wait for a later purge.
_STALE_SCRCPY_LOGS = []


def _purge_scrcpy_logs() -> None:
    for path in list(_STALE_SCRCPY_LOGS):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            continue
        _STALE_SCRCPY_LOGS.remove(path)


atexit.register(_purge_scrcpy_logs)


def _command_error(result, rejected: str):
    """Failure text for a device-command result (safe-mode or raw), else None."""
    if isinstance(result, OperationResult):
        if not result.success:
            return str(result.error or rejected)
        result = result.value
    return rejected if result is False else None


def _parse_display_size(size):
    """``(width, height)`` from ``"1920x720"`` or a pair, else None."""
    if isinstance(size, (tuple, list)) and len(size) == 2:
        width, height = size
    else:
        match = re.search(r"(\d+)\s*[x×]\s*(\d+)", str(size or ""))
        if not match:
            return None
        width, height = match.groups()
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def _mp4_is_finalised(path) -> bool:
    """True when an MP4 has its ``moov`` index, i.e. the recorder closed it.

    A recorder that is killed leaves only ``ftyp``/``mdat``, which players
    reject. Only box headers are read, so this is cheap on a long recording.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            pos = 0
            while pos + 8 <= end:
                fh.seek(pos)
                header = fh.read(16)
                size = int.from_bytes(header[:4], "big")
                if header[4:8] == b"moov":
                    return True
                if size == 1 and len(header) >= 16:
                    size = int.from_bytes(header[8:16], "big")
                elif size == 0:  # the box runs to the end of the file
                    return False
                if size < 8:
                    return False
                pos += size
    except OSError:
        pass
    return False


def _record_size_for_active_view(displays, display_id, max_edge=1280):
    """Return a proportional screenrecord size capped for concurrent viewing.

    ``screenrecord`` and scrcpy each need a device video encoder. Recording a
    native 2K/4K panel while it is mirrored is a frequent source of input lag,
    so keep the recorded copy at a practical 1280-pixel long edge. Unknown
    display dimensions deliberately retain Android's default rather than
    guessing an aspect ratio.
    """
    for display in displays or []:
        if display_id is not None and str(display.get("id")) != str(display_id):
            continue
        match = re.search(r"(\d+)\s*[x×]\s*(\d+)", str(display.get("size") or ""))
        if not match:
            continue
        width, height = int(match.group(1)), int(match.group(2))
        edge = max(width, height)
        if edge <= max_edge:
            return None
        scale = max_edge / edge
        # Android video encoders generally require both dimensions to be even.
        width = max(2, int(width * scale) // 2 * 2)
        height = max(2, int(height * scale) // 2 * 2)
        return f"{width}x{height}"
    return None


# (the old _StayOpenMenu checkable-menu hack was replaced by the real options
# popover panel — see MirrorPanel._build_options_menu)


# Attributes the Options popover creates when it is first built (see
# MirrorPanel._ensure_options_menu), and the popover control of each option.
_LAZY_OPTION_ATTRS = frozenset(
    {
        "_opts_panel", "_opts_headings", "cmb_renderer", "cmb_screencap_fps",
        "act_audio", "cmb_audio_source", "act_compat", "act_soft", "act_embed",
        "act_kb_sdk", "act_kb_uhid", "_kb_group", "act_cam_back", "act_cam_front",
        "_cam_group", "btn_ivi", "act_mirror_all", "btn_toggle_max",
    }
)
_OPTION_CONTROLS = {
    "audio": "act_audio",
    "audio_source": "cmb_audio_source",
    "compat": "act_compat",
    "soft": "act_soft",
    "embed": "act_embed",
    "kb_uhid": "act_kb_uhid",
    "cam_front": "act_cam_front",
}
# Settings → Screen renderer values ("scrcpy" is the default)
SCREEN_BACKENDS = tuple(getattr(settings_mod, "SCREEN_BACKENDS", ("scrcpy", "screencap")))


def display_label(display) -> str:
    """``Display 2  ·  Cluster  ·  1920x720`` for a display from ``list_displays``."""
    display = display or {}
    bits = [f"Display {display.get('id', 0)}"]
    name = str(display.get("name") or "").strip()
    if name:
        bits.append(name)
    if display.get("size"):
        bits.append(str(display["size"]))
    return "  ·  ".join(bits)


# Edit shortcuts sent to the device as Android keycodes.
_DEVICE_EDIT_SHORTCUTS = (
    (QKeySequence.SelectAll, 29),  # KEYCODE_A
    (QKeySequence.Copy, 278),  # KEYCODE_COPY
    (QKeySequence.Cut, 277),  # KEYCODE_CUT
)


def device_paste_message(text: str) -> str:
    """The log line for a paste that went to the DEVICE.

    The text lands on the device, not in this window, so without a word
    here a paste that worked and one that did nothing look identical.
    """
    if not text:
        return "[WARNING] Nothing on the clipboard to paste"
    count = len(text)
    plural = "" if count == 1 else "s"
    return f"[OK] Pasted {count} character{plural} to the device"


def _forward_device_key(event, send, *, with_repeat=False, notify=None) -> bool:
    """Send one Qt key press to the device; False when it is not a device key.

    Paste arrives as one text batch, edit shortcuts become Android keycodes
    and everything else goes through :meth:`_DeviceKeyEdit.map_event`.  With
    *with_repeat*, ``send`` also receives ``auto_repeat=`` so the key batcher
    can drop keyboard auto-repeat the device cannot keep up with.

    *notify* is called with the pasted text (``""`` for an empty clipboard)
    so the caller can report it — the text appears on the device, where the
    user is not looking.
    """
    extra = {"auto_repeat": bool(event.isAutoRepeat())} if with_repeat else {}
    if event.matches(QKeySequence.Paste):
        from PyQt5.QtWidgets import QApplication

        text = QApplication.clipboard().text()
        if text:
            send("text", text, **extra)
        if notify is not None:
            notify(text)
        return True
    for sequence, code in _DEVICE_EDIT_SHORTCUTS:
        if event.matches(sequence):
            send("key", code, **extra)
            return True
    kind, payload = _DeviceKeyEdit.map_event(event.key(), event.text())
    if kind is None:
        return False
    send(kind, payload, **extra)
    return True


def _forward_device_key_release(event, release) -> bool:
    """Report a device key's release as ``release(keycode)``; False when the
    event is not a device key.

    Qt on Windows pairs each auto-repeat press with an auto-repeat release;
    only the final, real release is reported.
    """
    code = None
    for sequence, shortcut_code in _DEVICE_EDIT_SHORTCUTS:
        if event.matches(sequence):
            code = shortcut_code
            break
    if code is None:
        kind, payload = _DeviceKeyEdit.map_event(event.key(), "")
        if kind != "key":
            return False
        code = payload
    if not event.isAutoRepeat():
        release(code)
    return True


def _swallow_shortcut_override(event) -> bool:
    """Keep device keystrokes (notably Ctrl+W) out of application shortcuts."""
    if event.type() == QEvent.ShortcutOverride:
        event.accept()
        return True
    return False


def _hint_width(widget) -> int:
    """A widget's layout width, whether or not it is shown right now."""
    return max(widget.sizeHint().width(), widget.minimumWidth())


class _ElidingCombo(QComboBox):
    """The display picker: at most MAX_WIDTH px in the toolbar, its label
    elided ("Display 0 · Built-in Scr…") instead of clipped, while the open
    list and the tooltip still show every name in full."""

    MAX_WIDTH = 170
    LABEL_INSET = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.setMaximumWidth(self.MAX_WIDTH)
        self.currentIndexChanged.connect(self._sync_tooltip)

    def _sync_tooltip(self, *_args):
        hint = (
            "Which display to control. Multi-display head units expose cluster, "
            "centre console, etc."
        )
        text = self.currentText()
        self.setToolTip(f"{text}\n\n{hint}" if text else hint)

    def paintEvent(self, _event):
        from PyQt5.QtWidgets import QStyle, QStyleOptionComboBox, QStylePainter

        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        painter.drawComplexControl(QStyle.CC_ComboBox, option)
        option.currentText = self.label_text(option)
        painter.drawControl(QStyle.CE_ComboBoxLabel, option)

    def label_text(self, option=None) -> str:
        """The current name as painted: elided only when it doesn't fit."""
        from PyQt5.QtWidgets import QStyle, QStyleOptionComboBox

        if option is None:
            option = QStyleOptionComboBox()
            self.initStyleOption(option)
        field = self.style().subControlRect(
            QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxEditField, self
        )
        # the style insets the label by 1 px on each side of the edit field
        return self.fontMetrics().elidedText(
            option.currentText, Qt.ElideRight, max(0, field.width() - self.LABEL_INSET)
        )

    def showPopup(self):
        widest = max(
            (self.fontMetrics().horizontalAdvance(self.itemText(i)) for i in range(self.count())),
            default=0,
        )
        self.view().setMinimumWidth(widest + 48)
        super().showPopup()


class _IdleGlyph(QWidget):
    """The idle screen's large device icon on a soft accent disc. Colours are
    resolved at paint time, so live theme switches apply."""

    def __init__(self, parent=None, size=76):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.icon_name = "smartphone"
        self._icon = _icon(self.icon_name, "accent")

    def set_icon(self, name):
        self.icon_name = name
        self._icon = _icon(name, "accent")
        self.update()

    def paintEvent(self, _event):
        from . import theme

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.tint("accent")))
        painter.drawEllipse(QRectF(self.rect()))
        inset = self.width() // 4
        self._icon.paint(painter, self.rect().adjusted(inset, inset, -inset, -inset))
        painter.end()


class _IdleContent(QWidget):
    """Layout-only holder (a QWidget subclass never paints the page fill)."""


class _IdleScreen(QWidget):
    """What the video area shows while no screen session runs: a device-shaped
    outline holding a short explanation and the two start actions.

    The outline follows the selected display: a portrait phone for a portrait
    (or unknown) non-automotive screen, otherwise a plain rounded display bezel
    at the display's real aspect ratio — a head unit, cluster or wide screen
    never looks like a phone. Unknown automotive displays are drawn 16:9.

    Everything is painted from theme tokens at paint time. MirrorPanel wires
    ``btn_start`` / ``btn_window`` to the same actions as its toolbar and shows
    or hides this widget from ``_sync_video_surface``.
    """

    _MARGIN = 24
    PHONE_ASPECT = 9.0 / 19.5
    PHONE_ASPECT_RANGE = (9.0 / 22.0, 3.0 / 4.0)
    DISPLAY_ASPECT = 16.0 / 9.0
    DISPLAY_ASPECT_RANGE = (1.0, 32.0 / 9.0)
    _UNSET = object()

    def __init__(self, parent=None, landscape=False, automotive=None, size=None):
        super().__init__(parent)
        self.setObjectName("idleScreen")
        self._automotive = bool(landscape if automotive is None else automotive)
        self._size = _parse_display_size(size)
        self._display_shape = False
        self._aspect = self.PHONE_ASPECT
        self._embed = True
        self._content = _IdleContent(self)
        lay = QVBoxLayout(self._content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.glyph = _IdleGlyph()
        lay.addWidget(self.glyph, 0, Qt.AlignHCenter)
        lay.addSpacing(16)
        self.title = QLabel("Screen is off")
        self.title.setObjectName("pageTitle")
        self.title.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.title)
        lay.addSpacing(6)
        self.hint = QLabel()
        self.hint.setObjectName("mutedHint")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        lay.addSpacing(20)
        self.btn_start = QPushButton("Start screen")
        self.btn_start.setProperty("role", "ok")
        self.btn_start.setIcon(_icon("play", "on-accent"))
        self.btn_start.setToolTip("Show the device screen (the same as the toolbar's Start)")
        self.btn_window = QPushButton("Open in separate window")
        self.btn_window.setIcon(_icon("external", "accent"))
        self.btn_window.setToolTip("Show the device screen in a dedicated native scrcpy window")
        for index, button in enumerate((self.btn_start, self.btn_window)):
            if index:
                lay.addSpacing(8)
            button.setIconSize(QSize(16, 16))
            button.setMinimumHeight(36)
            button.setCursor(Qt.PointingHandCursor)
            lay.addWidget(button)
        self._update_shape()

    @property
    def landscape(self) -> bool:
        """True when drawn as a display bezel rather than a phone."""
        return self._display_shape

    @property
    def automotive(self) -> bool:
        return self._automotive

    @property
    def aspect(self) -> float:
        """The outline's intended width / height (content may stretch it)."""
        return self._aspect

    def set_mode(self, embed: bool) -> None:
        """Describe where the primary Start action will show the screen."""
        self._embed = bool(embed)
        self._sync_hint()
        self._place()

    def set_device(self, automotive=_UNSET, size=_UNSET) -> None:
        """Update the device kind and/or the selected display's size ("WxH",
        a (width, height) pair, or None when unknown)."""
        if automotive is not self._UNSET:
            self._automotive = bool(automotive)
        if size is not self._UNSET:
            self._size = _parse_display_size(size)
        self._update_shape()

    def set_landscape(self, landscape: bool) -> None:
        """Compatibility: a wide head-unit frame (True) or the default (False)."""
        self.set_device(automotive=bool(landscape))

    def _update_shape(self) -> None:
        size = self._size
        wide = size is not None and size[0] >= size[1]
        self._display_shape = self._automotive or wide
        if self._display_shape:
            ratio = size[0] / size[1] if size else self.DISPLAY_ASPECT
            low, high = self.DISPLAY_ASPECT_RANGE
            icon = "car" if self._automotive else "monitor"
        else:
            ratio = size[0] / size[1] if size else self.PHONE_ASPECT
            low, high = self.PHONE_ASPECT_RANGE
            icon = "smartphone"
        self._aspect = min(high, max(low, ratio))
        if self.glyph.icon_name != icon:
            self.glyph.set_icon(icon)
        self._sync_hint()
        self._place()

    def _sync_hint(self) -> None:
        if self._automotive:
            target, own = "the head unit display", "The head unit display"
        else:
            target, own = "the device", "The device screen"
        self.hint.setText(
            f"Mirror {target} here and control it with your mouse and keyboard."
            if self._embed
            else f"{own} opens in its own scrcpy window."
        )

    def _content_size(self):
        lay = self._content.layout()
        width = max(
            230, self.btn_start.sizeHint().width(), self.btn_window.sizeHint().width()
        )
        if lay.hasHeightForWidth():
            height = lay.totalHeightForWidth(width)
        else:
            height = lay.totalSizeHint().height()
        return width, height

    def frame_rect(self) -> QRectF:
        """The device outline: centred, sized to the area, never smaller than
        its content (the aspect ratio gives way first on a very short area)."""
        area_w, area_h = float(self.width()), float(self.height())
        avail_w = max(0.0, area_w - 2 * self._MARGIN)
        avail_h = max(0.0, area_h - 2 * self._MARGIN)
        content_w, content_h = self._content_size()
        aspect = self._aspect
        if self._display_shape:
            frame_w = min(avail_w, 760.0, 640.0 * aspect)
            frame_h = frame_w / aspect
            if frame_h > avail_h:
                frame_h = avail_h
                frame_w = min(avail_w, frame_h * aspect)
            frame_w = max(frame_w, content_w + 96.0)
            frame_h = max(frame_h, content_h + 64.0)
        else:
            frame_h = min(avail_h, 640.0)
            frame_w = frame_h * aspect
            if frame_w > avail_w:
                frame_w = avail_w
                frame_h = frame_w / aspect
            frame_w = max(frame_w, content_w + 44.0)
            frame_h = max(frame_h, content_h + 96.0)
        frame_w = min(frame_w, max(0.0, area_w - 8))
        frame_h = min(frame_h, max(0.0, area_h - 8))
        return QRectF((area_w - frame_w) / 2.0, (area_h - frame_h) / 2.0, frame_w, frame_h)

    def _place(self) -> None:
        frame = self.frame_rect()
        width, height = self._content_size()
        width = int(max(0, min(width, frame.width() - 16)))
        height = int(max(0, min(height, frame.height() - 16)))
        centre = frame.center()
        self._content.setGeometry(
            int(centre.x() - width / 2.0), int(centre.y() - height / 2.0), width, height
        )
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()

    def showEvent(self, event):
        super().showEvent(event)
        self._place()

    def paintEvent(self, _event):
        from . import theme

        frame = self.frame_rect()
        if frame.width() < 48 or frame.height() < 48:
            return
        c = theme.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        if self._display_shape:
            # a plain display bezel: no notch, home bar or side keys
            radius, bezel = 16.0, 10.0
        else:
            radius, bezel = 34.0, 9.0
            # volume rocker and power key peek out of the right edge
            p.setBrush(QColor(c["line"]))
            right = frame.right() - 1.0
            key_top = frame.top() + frame.height() * 0.18
            p.drawRoundedRect(QRectF(right, key_top, 3.5, 54), 1.5, 1.5)
            p.drawRoundedRect(QRectF(right, key_top + 66, 3.5, 34), 1.5, 1.5)
        p.setPen(QPen(QColor(c["line"]), 1.5))
        p.setBrush(QColor(c["chrome"]))
        p.drawRoundedRect(frame, radius, radius)
        screen = frame.adjusted(bezel, bezel, -bezel, -bezel)
        p.setPen(QPen(QColor(c["border"]), 1.0))
        p.setBrush(QColor(c["win"]))
        inner = max(4.0, radius - bezel)
        p.drawRoundedRect(screen, inner, inner)
        if not self._display_shape:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(c["border"]))
            p.drawEllipse(QPointF(frame.center().x(), screen.top() + 16), 4.0, 4.0)
            bar_w = min(96.0, screen.width() * 0.34)
            p.drawRoundedRect(
                QRectF(frame.center().x() - bar_w / 2.0, screen.bottom() - 14, bar_w, 4), 2, 2
            )
        p.end()


class _EmbedContainer(QWidget):
    """The native widget scrcpy is reparented into.

    After a click on the video the panel first gives Win32 keyboard focus to
    scrcpy's own SDL child window, so scrcpy reads the keys itself (see
    ``MirrorPanel._focus_embedded``).  When Windows refuses, the keyboard stays
    here and keys are forwarded to the device over adb through *send_key*.
    scrcpy always keeps video and mouse: Windows routes the mouse by cursor
    position, whatever holds the focus.

    Clicks on the scrcpy video go straight to that foreign window and never
    reach Qt, so the container watches for WM_PARENTNOTIFY (sent by Windows to
    the parent on a button press over a child) and reports it through
    *on_child_click*; the panel then takes the keyboard.  With
    *on_key_release*, *send_key* also receives ``auto_repeat=`` and the real
    release of each device key is reported as ``on_key_release(keycode)``."""

    def __init__(self, send_key, parent=None, on_child_click=None, on_key_release=None,
                 on_paste=None):
        super().__init__(parent)
        self._send_key = send_key
        self._on_child_click = on_child_click
        self._on_key_release = on_key_release
        self._on_paste = on_paste
        self.setFocusPolicy(Qt.StrongFocus)

    def nativeEvent(self, event_type, message):
        if self._on_child_click is not None and _is_child_button_down(event_type, message):
            try:
                self._on_child_click()  # only schedules work; never blocks here
            except Exception:
                pass
        return super().nativeEvent(event_type, message)

    def focusNextPrevChild(self, next_child):
        # Tab is a device key while the screen has the keyboard; focus moves
        # on only from the idle screen's own buttons.
        if self.hasFocus():
            return False
        return super().focusNextPrevChild(next_child)

    def mousePressEvent(self, event):
        """Use the device keyboard only after an explicit mirror click.

        The native scrcpy child normally receives its own clicks.  This covers
        the letter-box area around it and non-native/fallback cases without
        ever taking focus merely because the Device Control page was shown.
        """
        self.setFocus(Qt.MouseFocusReason)
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        repeat_aware = self._on_key_release is not None
        if not _forward_device_key(event, self._send_key, with_repeat=repeat_aware,
                                   notify=self._on_paste):
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if self._on_key_release is None or not _forward_device_key_release(
            event, self._on_key_release
        ):
            super().keyReleaseEvent(event)

    def event(self, event):
        if _swallow_shortcut_override(event):
            return True
        return super().event(event)


class _DeviceKeyEdit(QLineEdit):
    """A keyboard-capture field: every keystroke is forwarded to the device (via
    the panel's ``send`` callback) instead of edited locally, so typing here IS
    typing on the device — Enter, Backspace, arrows and all. The field itself
    stays empty (it's a wire, not a buffer)."""

    _KEYMAP = {
        Qt.Key_Return: 66,
        Qt.Key_Enter: 66,
        Qt.Key_Backspace: 67,
        Qt.Key_Tab: 61,
        Qt.Key_Escape: 111,
        Qt.Key_Delete: 112,
        Qt.Key_Up: 19,
        Qt.Key_Down: 20,
        Qt.Key_Left: 21,
        Qt.Key_Right: 22,
        Qt.Key_Home: 122,
        Qt.Key_End: 123,
        Qt.Key_PageUp: 92,
        Qt.Key_PageDown: 93,
    }

    def __init__(self, send, parent=None):
        super().__init__(parent)
        self._send = send  # send("text", str) / send("key", code)

    @classmethod
    def map_event(cls, key, text):
        """(kind, payload) for a Qt key event — pure + unit-testable."""
        if key in cls._KEYMAP:
            return "key", cls._KEYMAP[key]
        if text and text.isprintable():
            return "text", text
        return None, None

    def keyPressEvent(self, event):
        if not _forward_device_key(event, self._send):
            super().keyPressEvent(event)  # let Qt handle what we don't map

    def event(self, event):
        if _swallow_shortcut_override(event):
            return True
        return super().event(event)


_SKIPPED = object()  # a queued keyboard command discarded before it ran


class _KeyCommand:
    """One device-keyboard command: a batch of text, or one keycode ``count`` times."""

    __slots__ = ("kind", "code", "count", "text", "repeat", "started", "cancelled", "mark")

    def __init__(self, kind, payload, repeat=False):
        self.kind = kind
        self.code = payload if kind == "key" else None
        self.text = payload if kind == "text" else ""
        self.count = 1
        self.repeat = bool(repeat)  # made by keyboard auto-repeat alone
        self.started = False
        self.cancelled = False
        self.mark = None  # dispatcher.submitted just after this was queued


class _KeyBatcher(QObject):
    """Deliver device-keyboard input over ADB quickly and never stale.

    This is the fallback route, used when keys reach TurboADB rather than
    scrcpy's own window.  Every ``adb shell input`` call starts a JVM on the
    device and takes a few hundred milliseconds, so:

    * printable characters typed within a short window become one
      ``input text`` call, and more text joins it while it waits;
    * consecutive presses of one key become one ``input keyevent K K K``;
    * keyboard auto-repeat is dropped while an earlier command for that key
      (or, for characters, any text) is still queued or running, so a held
      Backspace deletes at the speed the device answers;
    * releasing a key discards its queued repeats that have not started;
    * at most ``MAX_PENDING`` commands wait to start.

    Any key flushes pending text first, and work is merged only into a command
    that nothing else has been queued behind (``dispatcher.submitted``), so
    text, keys, taps and control actions keep their order in the dispatcher's
    single FIFO queue.  Called on the UI thread; commands run on the
    dispatcher's thread.

    *display_id* (a number, or a callable returning one) sends the input to
    that display with ``input -d``: without it Android delivers keys to
    whichever display it considers focused, which on a multi-display head
    unit is often not the screen the user is typing on.  None or 0 is the
    default display.
    """

    note = pyqtSignal(str)
    _TEXT_BATCH_MS = 60
    MAX_PENDING = 32
    MAX_KEYS_PER_CALL = 16

    def __init__(self, handler, dispatcher, parent=None, display_id=None):
        super().__init__(parent)
        self.handler = handler
        self.dispatcher = dispatcher
        self._display_id = display_id
        self._text = ""
        self._closed = False
        self._lock = threading.Lock()
        self._commands = []  # queued or running, oldest first
        self._warned_full = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.flush)

    def push(self, kind, payload, auto_repeat=False):
        """Queue one press: ``("text", str)`` or ``("key", android_keycode)``."""
        if self._closed:
            return
        if kind == "text":
            if auto_repeat and (self._text or self._busy("text")):
                return
            if not self._text:
                # A fixed deadline from the first character keeps it
                # responsive even during continuous typing.
                self._timer.start(self._TEXT_BATCH_MS)
            self._text += payload
            return
        self.flush()
        if auto_repeat:
            if self._busy("key", payload):
                return
        elif self._merge_key(payload):
            return
        self._enqueue(_KeyCommand("key", payload, repeat=auto_repeat))

    def release(self, code):
        """A key was released: discard its queued repeats that have not started."""
        with self._lock:
            for command in self._commands:
                if (
                    command.kind == "key"
                    and command.code == code
                    and command.repeat
                    and not command.started
                ):
                    command.cancelled = True

    def flush(self):
        self._timer.stop()
        text, self._text = self._text, ""
        if not text or self._closed:
            return
        with self._lock:
            last = self._mergeable()
            if last is not None and last.kind == "text":
                last.text += text
                return
        self._enqueue(_KeyCommand("text", text))

    def stop(self):
        """Drop pending text and queued commands; later results are ignored."""
        self._closed = True
        self._timer.stop()
        self._text = ""
        with self._lock:
            for command in self._commands:
                if not command.started:
                    command.cancelled = True

    @property
    def pending(self) -> int:
        """Commands queued or running."""
        with self._lock:
            return len(self._commands)

    def display_id(self):
        """The display keys go to now (None = the default display)."""
        value = self._display_id() if callable(self._display_id) else self._display_id
        try:
            return int(value) or None
        except (TypeError, ValueError):
            return None

    def _busy(self, kind, code=None) -> bool:
        with self._lock:
            return any(
                c.kind == kind and (code is None or c.code == code) and not c.cancelled
                for c in self._commands
            )

    def _mergeable(self):
        """The newest command if it has not started and nothing was queued
        behind it; the caller holds the lock."""
        if not self._commands:
            return None
        last = self._commands[-1]
        if last.started or last.cancelled or last.mark is None:
            return None
        if last.mark != getattr(self.dispatcher, "submitted", None):
            return None
        return last

    def _merge_key(self, code) -> bool:
        with self._lock:
            last = self._mergeable()
            if (
                last is None
                or last.kind != "key"
                or last.code != code
                or last.count >= self.MAX_KEYS_PER_CALL
            ):
                return False
            last.count += 1
            last.repeat = False  # it now carries a real press
            return True

    def _enqueue(self, command):
        with self._lock:
            waiting = sum(1 for c in self._commands if not (c.started or c.cancelled))
            accepted = waiting < self.MAX_PENDING
            if accepted:
                self._commands.append(command)
        if not accepted:
            if not self._warned_full:
                self._warned_full = True
                self.note.emit(
                    "[WARNING] device keyboard: the device is not keeping up, "
                    "so some keys were dropped"
                )
            return
        handler = self.handler
        display = self.display_id()
        # only a non-default display adds -d: older Android has no such option
        extra = {"display_id": display} if display else {}
        if command.kind == "text":
            rejected = "text was rejected by the device"
        else:
            rejected = f"keyevent {command.code} was rejected by the device"

        def run():
            with self._lock:
                if command.cancelled or self._closed:
                    return _SKIPPED
                command.started = True
                kind, code, count, text = command.kind, command.code, command.count, command.text
            if kind == "text":
                return handler.input_text(text, safe=True, **extra)
            if count == 1:
                return handler.keyevent(code, safe=True, **extra)
            return handler.keyevents([code] * count, safe=True, **extra)

        def finish():
            with self._lock:
                if command in self._commands:
                    self._commands.remove(command)
                idle = not self._commands
            if idle:
                self._warned_full = False

        def on_done(result):
            finish()
            if result is _SKIPPED or self._closed:
                return
            error = _command_error(result, rejected)
            if error:
                self.note.emit(f"[WARNING] device keyboard: {error}")

        def on_fail(message):
            finish()
            if not self._closed:
                self.note.emit(f"[WARNING] device keyboard: {message}")

        self.dispatcher.submit(run, on_done=on_done, on_fail=on_fail, priority=0)
        command.mark = getattr(self.dispatcher, "submitted", None)


class _ScrcpyCloseThread(QThread):
    """Close a scrcpy session off the UI thread: WM_CLOSE first, then stop().

    WM_CLOSE gives scrcpy a real close path, far more reliable than Ctrl+Break
    from a GUI process, and lets it flush a recording's MP4 index before it
    exits.  ``done`` reports whether the process ended cleanly.
    """

    done = pyqtSignal(bool)
    CLOSE_TIMEOUT = 5.0

    def __init__(self, session, window_title: str | None = None, window_handle=None):
        super().__init__()
        self.session = session
        self.window_title = window_title
        self.window_handle = window_handle

    def run(self):
        try:
            if self.session is not None:
                if _post_close(self.window_title, self.window_handle):
                    try:
                        self.session.wait(timeout=self.CLOSE_TIMEOUT)
                        self.done.emit(True)
                        return
                    except Exception:
                        pass
                self.session.stop()
                self.done.emit(not self.session.running)
                return
        except Exception:
            pass
        self.done.emit(False)


class _RecWaitThread(_ScrcpyCloseThread):
    """Finish a recording session; scrcpy needs time to write the MP4 index."""

    CLOSE_TIMEOUT = 15.0


class _MirrorStopThread(_ScrcpyCloseThread):
    """Stop an interactive viewer; closing an SDL window is normally immediate."""

    CLOSE_TIMEOUT = 5.0


class _RecorderStopThread(_ScrcpyCloseThread):
    """Finish a windowless (parallel) recorder.

    It has no window to close, so ``ScrcpySession.stop`` delivers Ctrl+Break,
    which scrcpy treats like a closed window: it writes the MP4 index, then
    exits. The generous timeout covers the index of a long recording.
    """

    CLOSE_TIMEOUT = 20.0

    def run(self):
        try:
            if self.session is not None:
                self.session.stop(timeout=self.CLOSE_TIMEOUT)
                self.done.emit(not self.session.running)
                return
        except Exception:
            pass
        self.done.emit(False)


class _RecorderDiscardThread(_RecorderStopThread):
    """Stop a parallel recorder that never worked and remove its stub file.

    Only an unfinished file of a few bytes is removed: the device-side fallback
    later pulls a complete recording to the same path, and that is kept.
    """

    CLOSE_TIMEOUT = 5.0
    STUB_MAX_BYTES = 4096

    def __init__(self, session, path=None):
        super().__init__(session)
        self.path = path

    def run(self):
        try:
            if self.session is not None:
                self.session.stop(timeout=self.CLOSE_TIMEOUT)
        except Exception:
            pass
        try:
            path = self.path
            if (
                path
                and os.path.isfile(path)
                and os.path.getsize(path) <= self.STUB_MAX_BYTES
                and not _mp4_is_finalised(path)
            ):
                os.remove(path)
        except OSError:
            pass
        self.done.emit(True)


class _MirrorLaunchThread(QThread):
    done = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, handler, opts, compat, log_path):
        super().__init__()
        self.handler = handler
        self.opts = opts
        self.compat = compat
        self.log_path = log_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            # Launch scrcpy straight away.  It already owns the startup
            # handshake with ADB, and a separate ``adb get-state`` round-trip
            # delays every launch (and is particularly slow while the server is
            # starting).  Early failures still go through the retry ladder.
            res = self.handler.mirror(
                self.opts, compat=self.compat, log_path=self.log_path, safe=True
            )
            if isinstance(res, OperationResult) and not res.success:
                self.fail.emit(str(res.error))
            else:
                sess = res.value if isinstance(res, OperationResult) else res
                if self._cancelled and sess is not None:
                    # The owning tab may already be gone. Clean up here instead
                    # of relying on a Qt slot on a deleted widget.
                    sess.stop()
                elif not self._cancelled:
                    self.done.emit(sess)
        except Exception as exc:
            if not self._cancelled:
                self.fail.emit(str(exc))


class _RecordThread(QThread):
    """Record the screen on the DEVICE itself (``adb shell screenrecord``) and
    pull the file. No video tunnel and no GPU needed, so it works while using
    Live View, over a remote adb server / RDP / IVI — anywhere the adb
    connection works. (screenrecord caps at ~3 min and has no audio.)"""

    done = pyqtSignal(list)  # the saved part file(s)
    fail = pyqtSignal(str)
    part = pyqtSignal(int)  # a new part started (Android's 3-min cap)

    def __init__(self, handler, path, stop_event, bit_rate=None, size=None, display_id=None):
        super().__init__()
        self.handler = handler
        self.path = path
        self.stop_event = stop_event
        self.bit_rate = bit_rate
        self.size = size
        self.display_id = display_id

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
                self.handler.screen_record(
                    seg,
                    time_limit=180,
                    size=self.size,
                    bit_rate=self.bit_rate,
                    display_id=self.display_id,
                    stop_event=self.stop_event,
                    safe=False,
                )
                if os.path.exists(seg) and os.path.getsize(seg) > 0:
                    parts.append(seg)
                n += 1
                # if the user didn't press stop, the 3-min cap ended this segment —
                # loop straight into the next part instead of stopping
            self.done.emit(parts)
        except Exception as exc:
            if parts:
                self.done.emit(parts)  # keep whatever was captured
            else:
                self.fail.emit(f"{type(exc).__name__}: {exc}")


class MirrorPanel(QWidget):
    log = pyqtSignal(str)
    # Display discovery results, for views that show the list (display manager).
    displays_changed = pyqtSignal(object)
    # True while a screen session shows video here, False when it goes idle.
    video_active_changed = pyqtSignal(bool)
    displays_failed = pyqtSignal(str)
    # The shape of the shown screen changed (another display, or the real video).
    display_shape_changed = pyqtSignal()
    # The current screen session shows video (its window is up).
    screen_ready = pyqtSignal()
    # A screen start failed for good (after scrcpy's retries, or a screencap
    # stream that ended with an error): nothing more will come of it.
    screen_failed = pyqtSignal(str)
    # A screen session finished stopping: nothing holds its window any more, so
    # the embed container may safely be destroyed (see DisplayWall._remove_tile).
    screen_stopped = pyqtSignal()
    # Maximize view was turned on (True) or off: the owner gives the screen the
    # whole tab (Device Control hides its controls panel) or restores it.
    max_view_changed = pyqtSignal(bool)
    # The toolbar's one-row width changed (Start/Stop swapped, the frame-rate
    # chip appeared…): an owner that sizes the screen card to it re-plans.
    toolbar_changed = pyqtSignal()
    # "All displays" was chosen: the device tab shows every display at once.
    all_displays_requested = pyqtSignal()
    # Android must encode once for scrcpy and once for screenrecord. Keep the
    # latter modest whenever a display is already active so control stays
    # responsive and the final pull is reasonably small.
    _RECORDER_VIEWING_MAX_BPS = 8_000_000
    # How often the running session is re-fitted to the container and its
    # process polled, and the slower rate used while the panel is off screen
    # (nothing is visible to correct there, but the session stays live).
    POLL_MS = 500
    IDLE_POLL_MS = 2000
    # The screencap renderer's default frame-rate cap (it rarely reaches more
    # than 3 fps on a 1080x2400 phone; the cap keeps a small display modest).
    SCREENCAP_FPS = 5

    def __init__(
        self,
        handler,
        session,
        automotive=False,
        prefer_embed=True,
        parent=None,
        dispatcher=None,
    ):
        super().__init__(parent)
        self.handler = handler
        self.session_dict = session
        self.automotive = automotive
        self._compat_user_changed = False
        self._setting_compat_default = False
        self._scrcpy = None
        self._stopping_mirror = False
        self._mirror_stop_thread = None
        self._embed_timer = None
        self._fit_timer = None
        self._embed_tries = 0
        self._win_first_seen = 0.0
        self._child_hwnd = None
        self._embed_parent_hwnd = None  # the container HWND scrcpy was adopted into
        self._separate_hwnd = None
        self._win_title = None
        self._mon = None
        self._compat = False
        self._embed_on = False
        self._retry_count = 0
        self._became_ready = False
        # screen recording (independent of how you're viewing)
        self._recording = False
        self._record_finalizing = False
        self._rec_path = None
        self._rec_thread = None
        self._rec_stop = None
        self._rec_mode = None  # "integrated" (scrcpy's recorder) | "device" (screenrecord)
        self._rec_integrated_starting = False
        self._rec_wait = None
        self._shot = None
        self._shot_path = None
        self._closing = False  # guards deferred callbacks (retry timers)
        self._displays = []  # cached [{id,size,name}] from the last list
        self._displays_loaded = False
        self._quiet_scan = False
        self._fixed_display = False  # a tile locked to one display (all-displays tab)
        self._video_aspect = None  # real width / height of the embedded video, once seen
        self._dt = None
        self._launch_thread = None
        self._launch_pending = False
        self._cancel_launch = False
        self._queued_start = None
        self._launch_spec = {}
        self._log_path = None
        self._scrcpy_error = ""  # the last failed start's output, shown on demand
        self._stale_logs = []  # scrcpy logs of ended sessions, removed when unlocked
        self._max_hidden = []
        self._rec_status_text = None  # the recording's own status line, while shown
        self._retry_pending = False
        self._retry_generation = 0
        self._owns_dispatcher = dispatcher is None
        self._dispatcher = dispatcher or DeviceCommandDispatcher()
        self._rec_restore_spec = None
        self._rec_restore_after_finish = False
        # a "parallel" recording: a second, windowless scrcpy beside the live one
        self._rec_parallel_starting = False
        self._rec_launch_thread = None
        self._rec_session = None
        self._rec_watch = None
        self._rec_confirmed = False
        self._rec_started_t = 0.0
        self._rec_title = None
        self._rec_log_path = None
        self._launch_opts = None  # the options of the current screen session
        self._default_display_size = None
        self._poll_interval = self.POLL_MS  # slowed while the panel is off screen

        self._rdp = is_remote_session()
        # Screen options. The popover that edits them is built on first use
        # (see _ensure_options_menu): a wall of display tiles never opens one,
        # and each popover is a few hundred KB of widgets. Until then these
        # values are the options; afterwards the popover's controls keep them.
        self._opt = {
            "audio": bool(settings_mod.get("scrcpy_audio", True)),
            "audio_source": str(settings_mod.get("scrcpy_audio_source", "output") or "output"),
            "compat": bool(automotive),
            "soft": bool(self._rdp),
            "embed": bool(prefer_embed),
            "kb_uhid": False,
            "cam_front": False,
        }
        self._options_built = False
        # "scrcpy" | "screencap" for this panel only; None follows Settings
        self._backend_override = None
        self._screencap = None  # the ScreencapView while that renderer shows the screen
        self._screencap_threads = []  # capture workers of stopped views, still finishing
        self._screencap_fps = self.SCREENCAP_FPS
        self._video_budget = {}  # per-panel scrcpy caps (DisplayWall tiles)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # One toolbar row: the display picker, Start/Stop and the renderer's
        # frame rate on the left; icon-only screen actions and view toggles on
        # the right. It wraps only when the panel is too narrow for one row
        # (Device Control sizes the screen card so it rarely is: every row
        # here costs the device picture height).
        bar_w = QWidget()
        bar_w.setObjectName("pageToolbar")
        bar_w.setAttribute(Qt.WA_StyledBackground, True)
        bar = ToolbarFlowLayout(bar_w, hspacing=6, vspacing=4)
        bar.setContentsMargins(10, 6, 10, 6)
        self._toolbar_widget = bar_w

        # ---- Primary screen controls ----
        self.cmb_display = _ElidingCombo()
        self.cmb_display.addItem("default display", None)
        self.cmb_display.setMinimumWidth(120)
        self.cmb_display._sync_tooltip()
        self.cmb_display.activated.connect(self._on_display_combo_changed)

        self.btn_mirror = QPushButton()
        self.btn_mirror.setProperty("role", "ok")
        self.btn_mirror.setIcon(_icon("play", "on-accent"))
        self.btn_mirror.setIconSize(QSize(16, 16))
        # This primary action deliberately follows the view mode selected in
        # Options.  The nearby pop-out control is the unambiguous way to force
        # a native window, so the two paths can never be mistaken for each
        # other.
        self.btn_mirror.clicked.connect(lambda _checked=False: self.start())

        self.btn_popout = self._icon_button(
            "external",
            "Separate window",
            "Separate window: show the device screen in a dedicated native scrcpy window",
            tone="accent",
        )
        self.btn_popout.clicked.connect(self._popout_mirror)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setProperty("role", "danger")
        self.btn_stop.setIcon(_icon("stop", "on-danger"))
        self.btn_stop.setIconSize(QSize(16, 16))
        self.btn_stop.setToolTip("Stop showing the device screen")
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setVisible(False)

        # The renderer's measured frame rate ("screencap · 4.5 fps"), shown in
        # the toolbar while screencap frames run — never over the picture.
        self.lbl_renderer = QLabel("")
        self.lbl_renderer.setObjectName("deviceChip")
        self.lbl_renderer.setToolTip(
            "ADB screencap renderer: device frames shown per second. Choose scrcpy "
            "in Options or Settings for smooth video."
        )
        self.lbl_renderer.setVisible(False)

        self.btn_shot = self._icon_button(
            "camera", "Screenshot", "Capture a PNG screenshot of the device screen"
        )
        # QPushButton.clicked carries a ``checked`` boolean. Passing it to
        # _take_screenshot made the default display become ``False`` (hence
        # filenames like screenshot-dispFalse-...), instead of no display ID.
        self.btn_shot.clicked.connect(lambda _checked=False: self._take_screenshot())

        self.btn_record = self._icon_button(
            "record",
            "Record…",
            "Record the device screen to a video file. Click again to stop & save.",
            tone="red",
        )
        self.btn_record.setEnabled(True)
        self.btn_record.clicked.connect(self._toggle_record)

        self.btn_opts = self._icon_button("settings", "Options", None)
        self.btn_opts.setPopupMode(QToolButton.InstantPopup)
        self.btn_opts.setToolTip(
            "Screen options: renderer, audio, IVI compatibility, rendering, embedding, "
            "keyboard and camera."
        )
        # An empty menu until the button is first used: the press (or the
        # menu's aboutToShow, for a programmatic popup) fills it.
        self.btn_opts.setMenu(QMenu(self.btn_opts))
        self.btn_opts.menu().aboutToShow.connect(self._ensure_options_menu)
        self.btn_opts.installEventFilter(self)
        self._sync_start_mode(self._opt["embed"])

        # Audio needs to be as discoverable as recording, not buried inside a
        # large settings popover.  It takes effect on the next scrcpy start,
        # since scrcpy configures capture streams at launch.
        self.btn_audio = self._icon_button("volume-up", "Audio on", None, tone="accent")
        self.btn_audio.setCheckable(True)
        self.btn_audio.toggled.connect(self._on_audio_enabled_changed)
        self._sync_audio_button()

        self.act_max = QAction("Maximize view", self)
        self.act_max.setCheckable(True)
        self.act_max.toggled.connect(self._toggle_max)

        # Max view is used frequently with a portrait/IVI screen, so keep it
        # beside the primary controls as well as in Screen options. The owner
        # (Device Control) hides its controls panel so the screen fills the
        # tab; the main window's docks are hidden too (_toggle_max).
        self.btn_max = self._icon_button("maximize", "Maximize view", None)
        self.btn_max.setCheckable(True)
        self.btn_max.toggled.connect(self.act_max.setChecked)
        self.act_max.toggled.connect(self._sync_max_buttons)
        # Esc restores the view. Enabled only while maximized, and only while the
        # keyboard focus is inside this panel: in split view another pane (the
        # Terminal clearing its line) keeps its own Esc. The screen itself keeps
        # Esc as a device key while it has the keyboard (it accepts
        # ShortcutOverride). Maximizing moves focus here when it was elsewhere
        # (_focus_for_esc), so Esc works straight after the click.
        from PyQt5.QtWidgets import QShortcut

        self._esc_restore = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._esc_restore.setContext(Qt.WidgetWithChildrenShortcut)
        self._esc_restore.setEnabled(False)
        self._esc_restore.activated.connect(lambda: self.act_max.setChecked(False))

        for w in (self.cmb_display, self.btn_mirror, self.btn_stop, self.lbl_renderer):
            bar.addWidget(w)
        bar.addStretch()  # the rest (and add_toolbar_widget) sits on the right
        for w in (
            self.btn_popout,
            self.btn_shot,
            self.btn_record,
            self.btn_audio,
            self.btn_opts,
            self.btn_max,
        ):
            bar.addWidget(w)
        self._toolbar = bar
        self._sync_max_buttons(False)
        # any change of the toolbar's items (a label, a shown/hidden button)
        # re-checks the width it needs on one row (_note_toolbar_width)
        bar_w.installEventFilter(self)
        lay.addWidget(bar_w)

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setContentsMargins(12, 6, 12, 6)
        self.status.setVisible(False)
        # A locked tile reports a failed start here instead of in a dialog, with
        # a link that opens scrcpy's log (see _show_scrcpy_log).
        self.status.linkActivated.connect(self._on_status_link)
        lay.addWidget(self.status)

        self._key_batcher = None
        # A click on the embedded video arrives as WM_PARENTNOTIFY while SDL is
        # still handling it; take the keyboard a moment later (see
        # _on_embedded_child_click).
        self._click_focus_timer = QTimer(self)
        self._click_focus_timer.setSingleShot(True)
        self._click_focus_timer.timeout.connect(self._focus_after_child_click)
        self._click_focus_recheck = False
        # True while scrcpy's own SDL window holds the keyboard (the native
        # route, see _focus_embedded); False while keys reach the container.
        self._native_keyboard = False
        self._keyboard_route = None  # the route last reported in the log
        self._native_kb_timer = QTimer(self)
        self._native_kb_timer.setSingleShot(True)
        self._native_kb_timer.timeout.connect(self._reassert_native_keyboard)
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_app_focus_changed)
            app.applicationStateChanged.connect(self._on_app_state_changed)

        # native container that scrcpy gets reparented into — it also captures
        # keyboard and forwards it to the device (see _EmbedContainer)
        self.container = _EmbedContainer(
            self._send_key,
            on_child_click=self._on_embedded_child_click,
            on_key_release=self._release_key,
            on_paste=lambda text: self.log.emit(device_paste_message(text)),
        )
        self.container.setAttribute(Qt.WA_NativeWindow, True)
        self.container.setMinimumHeight(200)
        self.container.installEventFilter(self)  # click margins -> focus mirror
        # Idle state shown over the video area: a device-shaped frame with the
        # same Start / Separate window actions as the toolbar. The area turns
        # into the black video surface only while a screen session is active.
        self.empty_state = _IdleScreen(self.container, automotive=automotive)
        # the idle frame follows the selected display's real shape
        self.cmb_display.currentIndexChanged.connect(self._sync_idle_shape)
        self.empty_state.btn_start.clicked.connect(lambda _checked=False: self.start())
        self.empty_state.btn_window.clicked.connect(
            lambda _checked=False: self._popout_mirror()
        )
        self._video_active = None
        lay.addWidget(self.container, 1)
        self._sync_start_mode(self._opt["embed"])
        self._sync_renderer_controls()
        self._sync_video_surface(False)

    def showEvent(self, event):
        super().showEvent(event)
        # Settings → Screen renderer may have changed while this was hidden
        self._sync_start_mode()
        self._refresh_buttons()

    def __getattr__(self, name):
        # The options popover's controls exist once it is built; asking for
        # one (tests, older callers) builds it.
        if name in _LAZY_OPTION_ATTRS and not self.__dict__.get("_options_built", True):
            self._ensure_options_menu()
            if name in self.__dict__:
                return self.__dict__[name]
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def eventFilter(self, obj, event):
        if obj is self.__dict__.get("_toolbar_widget"):
            if event.type() == QEvent.LayoutRequest:
                self._note_toolbar_width()
            return super().eventFilter(obj, event)
        if obj is self.__dict__.get("btn_opts"):
            if event.type() in (QEvent.MouseButtonPress, QEvent.KeyPress):
                self._ensure_options_menu()  # before QToolButton sizes the popup
            return super().eventFilter(obj, event)
        if obj is self.__dict__.get("container") and event.type() == QEvent.Resize:
            self.empty_state.setGeometry(self.container.rect())
            if self._screencap is not None:
                self._screencap.setGeometry(self.container.rect())
        # A click on the container's margins targets the mirror.  Importantly,
        # changing tabs or interacting with the Device Controls never does:
        # that previously put keystrokes into an unrelated TurboADB field.
        if obj is self.container and event.type() == QEvent.MouseButtonPress and self._child_hwnd:
            self._focus_embedded(Qt.MouseFocusReason)
        return super().eventFilter(obj, event)

    # A click on the video notifies us before SDL has processed it; SDL may
    # then focus its own window, so the keyboard is claimed after this delay
    # and confirmed once more a little later.
    _CLICK_FOCUS_DELAY_MS = 30
    _CLICK_FOCUS_RECHECK_MS = 250

    def _on_embedded_child_click(self):
        """WM_PARENTNOTIFY: the user pressed a mouse button on the scrcpy video.

        Runs inside native message handling, so it only schedules the focus
        change. Mouse input keeps going to scrcpy (Windows routes it by cursor
        position); only the keyboard is redirected to the container.
        """
        if self._closing or not self._child_hwnd:
            return
        self._click_focus_recheck = True
        self._click_focus_timer.start(self._CLICK_FOCUS_DELAY_MS)

    def _focus_after_child_click(self):
        if self._closing or not self._child_hwnd:
            return
        if not self._click_focus_recheck and not self.container.hasFocus():
            return  # the user moved on to another control meanwhile
        self._focus_embedded(Qt.MouseFocusReason)
        if self._click_focus_recheck:
            self._click_focus_recheck = False
            self._click_focus_timer.start(self._CLICK_FOCUS_RECHECK_MS)

    def _focus_embedded(self, reason=Qt.OtherFocusReason):
        """Give the embedded screen the keyboard after an explicit user action.

        Native first: Win32 focus goes to scrcpy's SDL child window, so scrcpy
        reads the keys itself (fast, correct key repeat, no adb per key).  Qt
        focus still moves to the container, so choosing any other TurboADB
        widget is noticed (_on_app_focus_changed) and takes the keyboard back.
        When Windows refuses, Win32 focus goes to the container's own HWND and
        the container forwards keys over ADB (_KeyBatcher).
        Returns whether the container took focus.
        """
        if not self.container.isVisible():
            return False
        if self._screencap is not None:
            self._screencap.setFocus(reason)  # it reads the keys itself
            return True
        self.container.setFocus(reason)
        if self._child_hwnd:
            native = False
            try:
                native = _focus_embedded_child(self._child_hwnd, int(self.container.winId()))
            except Exception:
                native = False
            self._native_keyboard = native
            if not native:
                try:
                    _claim_native_focus(int(self.container.winId()))
                except Exception:
                    pass
            self._report_keyboard_route(native)
        return True

    def _report_keyboard_route(self, native):
        """Log which keyboard route is in use, once per change."""
        route = "native" if native else "adb"
        if route == self._keyboard_route:
            return
        self._keyboard_route = route
        if native:
            self.log.emit(
                "[INFO] keyboard: keys go straight to scrcpy's window "
                "(fast, with normal key repeat)."
            )
        else:
            self.log.emit(
                "[INFO] keyboard: Windows did not let scrcpy's window take the "
                "keyboard; keys are sent over adb instead (slower)."
            )

    def _on_app_focus_changed(self, old, new):
        """Keep the native keyboard route in step with Qt's focus widget."""
        if self._closing or not self._native_keyboard:
            return
        try:
            container = self.container
        except RuntimeError:
            return
        if new is container:
            if old is None:
                # TurboADB is active again; Windows focuses the top-level
                # window on activation, so hand the keyboard back to scrcpy.
                self._native_kb_timer.start(self._CLICK_FOCUS_DELAY_MS)
            return
        if new is None:
            return  # deactivated, or focus cleared: no widget wants the keys
        self._release_native_keyboard(new)

    def _on_app_state_changed(self, state):
        if state == Qt.ApplicationActive and self._native_keyboard and not self._closing:
            self._native_kb_timer.start(self._CLICK_FOCUS_DELAY_MS)

    def _reassert_native_keyboard(self):
        """Return the keyboard to scrcpy's window after TurboADB is activated
        again, if the user had given it there and no other widget took it."""
        if self._closing or not self._native_keyboard or not self._child_hwnd:
            return
        try:
            if not self.container.hasFocus() or not self.isActiveWindow():
                return
            container_hwnd = int(self.container.winId())
            native = _focus_embedded_child(self._child_hwnd, container_hwnd)
            if not native:
                self._native_keyboard = False
                _claim_native_focus(container_hwnd)
            self._report_keyboard_route(native)
        except Exception:
            pass

    def _release_native_keyboard(self, target=None, *, if_lost=False):
        """Take the keyboard back from scrcpy's SDL window.

        Win32 focus moves to *target*'s native window (by default this panel's
        top-level window), but only while the SDL child still holds it.
        """
        was, self._native_keyboard = self._native_keyboard, False
        self._native_kb_timer.stop()
        if not was or not self._child_hwnd:
            return
        try:
            widget = target if target is not None else self.window()
            # effectiveWinId() is empty until the widget itself is created;
            # its top-level window then owns Qt's keyboard input.
            hwnd = widget.effectiveWinId() or widget.window().winId()
            _release_child_focus(
                self._child_hwnd,
                int(hwnd),
                if_lost=bool(if_lost and self.isActiveWindow()),
            )
        except Exception:
            pass

    def _disconnect_focus_tracking(self):
        from PyQt5.QtWidgets import QApplication

        self._native_kb_timer.stop()
        app = QApplication.instance()
        if app is None:
            return
        for signal, slot in (
            (app.focusChanged, self._on_app_focus_changed),
            (app.applicationStateChanged, self._on_app_state_changed),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _restore_mirror_focus(self, *, direct: bool = False):
        """Return focus after a toolbar action without activating Qt's menus.

        Embedded typing is delivered by the screen container through ADB. A
        native scrcpy window uses a Win32 handoff. Either route is changed only
        by a direct device, Record, or Stop click.  ``direct`` is reserved for a
        live toolbar click (such as Record): on Windows, that is the only time
        we can reliably transfer keyboard focus across processes.  Do not use
        it from a timer.
        """
        if self._screencap is not None:
            if direct and self._screencap.isVisible():
                self._screencap.setFocus(Qt.OtherFocusReason)
            return
        if self._scrcpy is None or not self._scrcpy.running:
            return
        if self._embed_on:
            if direct:
                self._focus_embedded()
            return
        if direct:
            self._focus_separate_window(focus_keyboard=True)
        else:
            self._focus_separate_window()

    def yield_keyboard(self):
        """Release embedded-screen keyboard capture when another page is chosen."""
        try:
            # A click-focus still pending must not pull the keyboard back.
            self._click_focus_timer.stop()
            self._click_focus_recheck = False
            # scrcpy's own window may hold the keyboard: give it back to Qt.
            self._release_native_keyboard()
            # Calling clearFocus is harmless when another widget already owns
            # focus, and it prevents the native embedded container retaining a
            # stale Qt focus owner in a split layout.
            self.container.clearFocus()
            if self._screencap is not None:
                self._screencap.clearFocus()
        except Exception:
            pass

    def _icon_button(self, name, text, tip, tone="text"):
        """A compact toolbar button showing a theme-following vector icon. The
        label stays its text (tooltip + accessibility), so the toolbar fits on
        one row."""
        button = QToolButton()
        button.setObjectName("iconButton")
        button.setIcon(_icon(name, tone))
        button.setIconSize(QSize(18, 18))
        button.setText(text)
        button.setAccessibleName(text)
        button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        button.setToolTip(tip or text)
        return button

    def _sync_video_surface(self, active: bool) -> None:
        """Black video surface while a session runs; empty state when idle."""
        empty = getattr(self, "empty_state", None)
        if empty is None:
            return
        empty.setVisible(not active)
        if self._video_active is active:
            return
        self._video_active = active
        # the idle area shows the card surface; only live video sits on black
        self.container.setStyleSheet("background:#000;" if active else "")
        self.video_active_changed.emit(bool(active))

    def add_toolbar_widget(self, widget) -> None:
        """Append a caller-owned control to the screen toolbar."""
        self._toolbar.addWidget(widget)

    def ensure_embedded(self) -> None:
        """Keep the embedded scrcpy window attached after Device Control moves
        between parents (split view).

        Qt normally keeps a native child's HWND across a reparent, so this is
        usually just a re-fit; re-adopting on a changed container handle is a
        cheap safety net.
        """
        if self._closing or not self._child_hwnd or not _IS_WIN:
            return
        if self._scrcpy is None or not self._scrcpy.running:
            return
        try:
            parent = int(self.container.winId())
            _ctypes, u = _win_api()
            attached = int(u.GetParent(self._child_hwnd) or 0)
            if parent != self._embed_parent_hwnd or attached != parent:
                _reparent(
                    self._child_hwnd, parent, self.container.width(), self.container.height()
                )
                self._embed_parent_hwnd = parent
        except Exception as exc:
            self.log.emit(f"[WARNING] could not re-attach the embedded screen: {exc}")
            return
        self._fit()

    def _integrated_recording_blocks(
        self,
        message="Stop the current recording before changing displays.",
        *,
        restore_selection=False,
    ) -> bool:
        """True (with a status hint) while scrcpy itself records: changing the
        view would restart that one process and end the recording."""
        if not (self._recording and self._rec_mode == "integrated"):
            return False
        if restore_selection:
            self._sync_display_selection((self._launch_spec or {}).get("display_id"))
        self.status.show()
        self.status.setText(message)
        return True

    def _send_key(self, kind, payload, auto_repeat=False):
        """Forward one key press to the device over ADB, in order, through the
        shared device command dispatcher (the fallback keyboard route)."""
        if self._closing:
            return
        if self._key_batcher is None:
            self._key_batcher = _KeyBatcher(
                self.handler, self._dispatcher, self, display_id=self.input_display_id
            )
            self._key_batcher.note.connect(self.log)
        self._key_batcher.push(kind, payload, auto_repeat)

    def input_display_id(self):
        """The logical display this panel's input goes to (None = default):
        the display of the running screen, else the picker's choice."""
        spec = self._launch_spec or {}
        if self.is_active() and "display_id" in spec:
            value = spec.get("display_id")
        else:
            value = self.cmb_display.currentData()
        try:
            return int(value) or None
        except (TypeError, ValueError):
            return None

    def _release_key(self, code):
        """A device key was released: drop its queued, unstarted repeats."""
        if self._key_batcher is not None:
            self._key_batcher.release(code)

    def _kb_mode(self):
        """The scrcpy --keyboard mode from the options popover: None = scrcpy's
        own default (SDK — identical to running scrcpy by hand), 'uhid' only
        when explicitly chosen."""
        return "uhid" if self._opt["kb_uhid"] else None

    def _set_option(self, key, value) -> None:
        """Change one screen option, and its popover control when that exists."""
        self._opt[key] = value
        widget = self.__dict__.get(_OPTION_CONTROLS.get(key, ""))
        if widget is None:
            return
        if isinstance(widget, QComboBox):
            index = widget.findData(value)
            if index >= 0 and index != widget.currentIndex():
                widget.setCurrentIndex(index)
        elif widget.isChecked() != bool(value):
            widget.setChecked(bool(value))

    def _ensure_options_menu(self) -> None:
        """Fill the Options button's popover the first time it is needed."""
        if self.__dict__.get("_options_built", True):
            return
        self._options_built = True
        self._build_options_menu(self.btn_opts.menu())
        self._sync_all_displays_button()
        self._sync_max_buttons(self.act_max.isChecked())
        self._sync_renderer_controls()

    def _track_option(self, key, widget) -> None:
        """Keep ``self._opt[key]`` in step with a popover control."""
        if isinstance(widget, QComboBox):
            widget.currentIndexChanged.connect(
                lambda _i, k=key, w=widget: self._opt.__setitem__(k, w.currentData())
            )
        else:
            widget.toggled.connect(lambda on, k=key: self._opt.__setitem__(k, bool(on)))

    def _build_options_menu(self, menu=None, automotive=None, prefer_embed=None):
        """The Options POPOVER: a real, themed panel (via QWidgetAction) with
        grouped checkboxes, radio groups and per-option hints — replacing the
        old checkable menu whose tiny tick marks and stay-open hack read as
        broken. Toggling anything keeps the popover open; click outside (or the
        Done button) to dismiss. Controls start from (and keep) ``self._opt``."""
        from PyQt5.QtWidgets import QWidgetAction, QRadioButton, QButtonGroup

        if not isinstance(menu, QMenu):
            menu = QMenu(self.btn_opts)
        opt = self._opt
        panel = QWidget(menu)
        panel.setMinimumWidth(330)
        # The raised surface, headings and indented hints are #screenOptions
        # rules in theme.stylesheet(), so they follow live theme switches.
        panel.setObjectName("screenOptions")
        self._opts_panel = panel
        self._opts_headings = []  # [(QLabel, is_title)]
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(6)

        def section(text):
            lab = QLabel(text.upper())
            lab.setObjectName("screenOptionsSection")
            self._opts_headings.append((lab, False))
            v.addWidget(lab)

        def hint(text):
            # indented to line up with the text of the checkbox/radio above
            lab = QLabel(text)
            lab.setWordWrap(True)
            lab.setObjectName("mutedHint")
            v.addWidget(lab)

        title = QLabel("Screen settings")
        title.setObjectName("screenOptionsTitle")
        self._opts_headings.append((title, True))
        v.addWidget(title)

        section("Renderer")
        self.cmb_renderer = QComboBox()
        self.cmb_renderer.addItem("Settings default", None)
        self.cmb_renderer.addItem("scrcpy video (fast)", "scrcpy")
        self.cmb_renderer.addItem("ADB screencap (slower fallback)", "screencap")
        self.cmb_renderer.setCurrentIndex(max(0, self.cmb_renderer.findData(self._backend_override)))
        self.cmb_renderer.currentIndexChanged.connect(
            lambda _i: self.set_screen_backend(self.cmb_renderer.currentData())
        )
        v.addWidget(self.cmb_renderer)
        hint("screencap works where scrcpy can't start; it shows a few frames a second")
        self.cmb_screencap_fps = QComboBox()
        for fps in (1, 2, 5, 10):
            self.cmb_screencap_fps.addItem(f"Screencap: up to {fps} fps", fps)
        self.cmb_screencap_fps.setCurrentIndex(
            max(0, self.cmb_screencap_fps.findData(self._screencap_fps))
        )
        self.cmb_screencap_fps.currentIndexChanged.connect(
            lambda _i: self._set_screencap_fps(self.cmb_screencap_fps.currentData())
        )
        v.addWidget(self.cmb_screencap_fps)

        section("Audio")
        self.act_audio = QCheckBox("Forward device audio to this PC")
        self.act_audio.setChecked(opt["audio"])
        self._track_option("audio", self.act_audio)
        self.act_audio.toggled.connect(self._on_audio_enabled_changed)
        v.addWidget(self.act_audio)
        hint("play sound from the Android device through this PC (Android 11+)")
        self.cmb_audio_source = QComboBox()
        self.cmb_audio_source.addItem("Whole device output (mutes device)", "output")
        self.cmb_audio_source.addItem("App playback (keeps device sound)", "playback")
        self.cmb_audio_source.addItem("Microphone", "mic")
        self.cmb_audio_source.addItem("Voice-call downlink", "voice-call-downlink")
        self.cmb_audio_source.addItem("Voice performance / karaoke", "voice-performance")
        source_index = self.cmb_audio_source.findData(opt["audio_source"])
        self.cmb_audio_source.setCurrentIndex(max(0, source_index))
        self._track_option("audio_source", self.cmb_audio_source)
        self.cmb_audio_source.currentIndexChanged.connect(self._save_audio_options)
        v.addWidget(QLabel("Audio source"))
        v.addWidget(self.cmb_audio_source)
        hint("More codecs, bit rates and latency controls are in Settings → scrcpy.")

        section("Video")
        self.act_compat = QCheckBox("Compatibility mode (IVI / automotive)")
        self.act_compat.setChecked(opt["compat"])
        self._track_option("compat", self.act_compat)
        self.act_compat.toggled.connect(self._on_compat_toggled)
        v.addWidget(self.act_compat)
        hint("H.264 + safe caps — for head-unit encoders that choke on defaults")
        self.act_soft = QCheckBox("Software rendering")
        self.act_soft.setChecked(opt["soft"])
        self._track_option("soft", self.act_soft)
        v.addWidget(self.act_soft)
        hint("needed over Remote Desktop / GPU-less sessions")
        self.act_embed = QCheckBox("Show screen inside this tab")
        self.act_embed.setChecked(opt["embed"])
        self._track_option("embed", self.act_embed)
        self.act_embed.toggled.connect(self._sync_start_mode)
        v.addWidget(self.act_embed)
        hint("the mirror lives inside TurboADB instead of a separate window")

        section("Keyboard")
        self.act_kb_sdk = QRadioButton("Standard (SDK) — like plain scrcpy")
        self.act_kb_uhid = QRadioButton("UHID hardware keyboard")
        self._kb_group = QButtonGroup(panel)
        self._kb_group.setExclusive(True)
        self._kb_group.addButton(self.act_kb_sdk)
        self._kb_group.addButton(self.act_kb_uhid)
        (self.act_kb_uhid if opt["kb_uhid"] else self.act_kb_sdk).setChecked(True)
        self._track_option("kb_uhid", self.act_kb_uhid)
        v.addWidget(self.act_kb_sdk)
        v.addWidget(self.act_kb_uhid)
        hint("UHID only if standard typing is ignored — most IVI kernels don't support it")

        section("Camera source")
        cam_row = QHBoxLayout()
        cam_row.setSpacing(14)
        self.act_cam_back = QRadioButton("Back")
        self.act_cam_front = QRadioButton("Front")
        self._cam_group = QButtonGroup(panel)
        self._cam_group.setExclusive(True)
        self._cam_group.addButton(self.act_cam_back)
        self._cam_group.addButton(self.act_cam_front)
        (self.act_cam_front if opt["cam_front"] else self.act_cam_back).setChecked(True)
        self._track_option("cam_front", self.act_cam_front)
        cam_row.addWidget(self.act_cam_back)
        cam_row.addWidget(self.act_cam_front)
        cam_row.addStretch(1)
        v.addLayout(cam_row)
        hint("used by Device camera below (scrcpy 2.2+, Android 12+)")

        section("Screen tools")
        extra_grid = QGridLayout()
        extra_grid.setSpacing(8)

        btn_cam = QPushButton("Device camera")
        btn_cam.setProperty("role", "ghost")
        btn_cam.setToolTip("Mirror front/back device camera via scrcpy")
        btn_cam.clicked.connect(lambda: (menu.close(), self._mirror_camera()))
        extra_grid.addWidget(btn_cam, 0, 0)

        self.btn_ivi = QPushButton("All displays")
        self.btn_ivi.setProperty("role", "ghost")
        self.btn_ivi.setToolTip("Show every display of this device live, side by side")
        self.btn_ivi.clicked.connect(lambda: (menu.close(), self.open_ivi_view()))
        self.btn_ivi.setVisible(self._all_displays_offered())
        extra_grid.addWidget(self.btn_ivi, 2, 1)  # last: hidden unless IVI

        self.act_mirror_all = QPushButton("Manage displays…")
        self.act_mirror_all.setProperty("role", "ghost")
        self.act_mirror_all.setEnabled(True)
        self.act_mirror_all.setToolTip("View device displays with per-display screen and recording controls")
        self.act_mirror_all.clicked.connect(lambda: (menu.close(), self._show_display_manager()))
        extra_grid.addWidget(self.act_mirror_all, 0, 1)

        btn_scan = QPushButton("⟳ Rescan displays")
        btn_scan.setProperty("role", "ghost")
        btn_scan.setToolTip("Query connected device displays")
        btn_scan.clicked.connect(lambda: (menu.close(), self.refresh_displays()))
        extra_grid.addWidget(btn_scan, 1, 0)

        btn_text = QPushButton("Type or paste text…")
        btn_text.setProperty("role", "ghost")
        btn_text.setToolTip("Send multiline text block directly to device")
        btn_text.clicked.connect(lambda: (menu.close(), self._type_text()))
        extra_grid.addWidget(btn_text, 1, 1)

        self.btn_toggle_max = QPushButton("⛶ Maximize view")
        self.btn_toggle_max.setProperty("role", "ghost")
        self.btn_toggle_max.setToolTip(self._MAX_TIP)  # kept in step by _sync_max_buttons
        self.btn_toggle_max.clicked.connect(lambda: (menu.close(), self.act_max.toggle()))
        extra_grid.addWidget(self.btn_toggle_max, 2, 0)

        v.addLayout(extra_grid)

        done_row = QHBoxLayout()
        done_row.setContentsMargins(0, 8, 0, 0)
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

    def refresh_theme(self) -> None:
        """Hook for MainWindow._apply_theme after a live theme switch.

        The options popover is styled by the application stylesheet
        (#screenOptions), which the switch already replaced; the buttons are
        re-synced (their state may follow a changed renderer setting).
        """
        if not self._closing:
            self._refresh_buttons()

    def _audio_enabled(self) -> bool:
        return bool(self._opt["audio"])

    def _audio_value(self, name: str, default):
        if name == "cmb_audio_source" and "cmb_audio_source" not in self.__dict__:
            return self._opt.get("audio_source") or default
        control = self.__dict__.get(name)
        if control is None:
            return default
        if isinstance(control, QComboBox):
            value = control.currentData()
            return value if value is not None else control.currentText().strip()
        if isinstance(control, QCheckBox):
            return control.isChecked()
        if isinstance(control, QSpinBox):
            return control.value()
        return default

    def _sync_audio_button(self) -> None:
        enabled = self._audio_enabled()
        button = getattr(self, "btn_audio", None)
        if button is None:
            return
        button.blockSignals(True)
        button.setChecked(enabled)
        button.blockSignals(False)
        button.setText("Audio on" if enabled else "Audio off")
        button.setIcon(_icon("volume-up", "accent") if enabled else _icon("mute", "dim"))
        button.setToolTip(
            "Forward device audio to this PC on the next mirror start"
            if enabled else "Audio forwarding is off; click to enable it for the next mirror start"
        )

    def _sync_start_mode(self, embed=None) -> None:
        """Describe the selected launch target directly on the primary action."""
        button = getattr(self, "btn_mirror", None)
        if button is None:
            return
        if embed is None:
            embed = self._opt["embed"]
        screencap = self.screen_backend() == "screencap"
        if screencap:
            # screencap frames are always drawn inside the tab
            button.setText("Start in this tab")
            button.setToolTip(
                "Start the selected display with ADB screencap frames (the slower "
                "fallback renderer; choose the renderer in Options or Settings)"
            )
        elif embed:
            button.setText("Start in this tab")
            button.setToolTip("Start the selected display embedded in Device Control")
        else:
            button.setText("Start separate window")
            button.setToolTip("Start the selected display in a native scrcpy window")
        empty = getattr(self, "empty_state", None)
        if empty is not None:
            empty.set_mode(bool(embed) or screencap)
        # "Start separate window" is wider (and a hidden button's new label
        # posts no layout request): let Device Control re-plan
        self._note_toolbar_width()

    # ----- screen renderer: scrcpy video or ADB screencap frames -----
    def screen_backend(self) -> str:
        """``"scrcpy"`` or ``"screencap"``: this panel's own choice (Options →
        Renderer) or else Settings → Screen renderer."""
        choice = self._backend_override
        if choice not in SCREEN_BACKENDS:
            choice = settings_mod.get("screen_backend", "scrcpy")
        return choice if choice in SCREEN_BACKENDS else "scrcpy"

    def set_screen_backend(self, backend) -> None:
        """Use *backend* for this panel (None follows Settings). A running
        screen switches over at once."""
        backend = backend if backend in SCREEN_BACKENDS else None
        self._backend_override = backend
        combo = self.__dict__.get("cmb_renderer")
        if combo is not None and combo.currentData() != backend:
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(backend)))
            combo.blockSignals(False)
        self._sync_start_mode()
        self._refresh_buttons()
        self._restart_for_backend()

    def default_backend_changed(self, old_backend) -> None:
        """Settings → Screen renderer was changed and saved (it was
        *old_backend*). Refresh this panel's buttons and tooltips; a panel
        that follows the Settings default also moves a running screen over to
        the new renderer (one with its own Options → Renderer choice keeps it).
        """
        if self._closing:
            return
        self._sync_start_mode()
        self._refresh_buttons()
        if self._backend_override in SCREEN_BACKENDS:
            return
        old = old_backend if old_backend in SCREEN_BACKENDS else "scrcpy"
        if old != self.screen_backend():
            self._restart_for_backend()

    @classmethod
    def apply_default_backend(cls, root, old_backend) -> None:
        """:meth:`default_backend_changed` for every screen panel under *root*
        (the main window after its Settings dialog was accepted)."""
        for panel in root.findChildren(cls):
            try:
                panel.default_backend_changed(old_backend)
            except RuntimeError:  # a panel being destroyed
                pass

    def _running_backend(self):
        """The renderer of the screen running now, or None."""
        if self._screencap is not None:
            return "screencap"
        if self._scrcpy is not None or self._launch_pending or self._retry_pending:
            return "scrcpy"
        return None

    def _restart_for_backend(self) -> None:
        """Restart a running screen whose renderer is no longer the one a start
        would pick. A camera view, a scrcpy recording or an explicit separate
        window stay with scrcpy, so they are left alone."""
        running = self._running_backend()
        if self._closing or running is None or self._stopping_mirror:
            return
        spec = dict(self._launch_spec or {})
        if self._recording and self._rec_mode == "integrated":
            return
        wanted = (
            "screencap"
            if self._uses_screencap(spec.get("embed"), spec.get("camera"), spec.get("record"))
            else "scrcpy"
        )
        if wanted == running:
            return
        self.log.emit(f"[INFO] screen renderer: switching this screen to {wanted}")
        self.start(display_id=spec.get("display_id", "__use_combo__"), embed=spec.get("embed"))

    def _set_screencap_fps(self, fps) -> None:
        try:
            fps = max(1, int(fps))
        except (TypeError, ValueError):
            return
        if fps == self._screencap_fps:
            return
        self._screencap_fps = fps
        view = self._screencap
        if view is not None and not self._closing:
            view.max_fps = fps
            view.start()  # the device-side loop takes the new pace

    def _sync_renderer_controls(self) -> None:
        """Describe what Record does with the current renderer. With screencap
        frames it records on the device (screenrecord), which needs no scrcpy."""
        screencap = self.screen_backend() == "screencap"
        button = self.__dict__.get("btn_record")
        if button is not None:
            rec_starting = self._rec_integrated_starting or self._rec_parallel_starting
            button.setEnabled(not self._record_finalizing and not rec_starting)
            if self._recording or self._record_finalizing:
                button.setToolTip("Stop the recording and save it to your PC.")
            elif screencap:
                button.setToolTip(
                    "Record this display on the device (Android's screenrecord) and "
                    "save the MP4 to your PC. Click again to stop & save. Works without "
                    "scrcpy; virtual displays and secure screens can't be recorded."
                )
            else:
                button.setToolTip(
                    "Record the device screen to a video file. Click again to stop & save."
                )
        fps = self.__dict__.get("cmb_screencap_fps")
        if fps is not None:
            fps.setEnabled(screencap)

    def _uses_screencap(self, embed, camera, record) -> bool:
        """A start draws screencap frames, unless it needs scrcpy itself: the
        camera, a recording, or an explicit separate window."""
        if camera or record or embed is False:
            return False
        return self.screen_backend() == "screencap"

    def _selected_display_entry(self, display_id) -> dict:
        wanted = 0 if display_id is None else int(display_id)
        for display in self._displays or []:
            try:
                if int(display.get("id", -1)) == wanted:
                    return dict(display)
            except (TypeError, ValueError):
                continue
        entry = {"id": wanted}
        if wanted == 0 and self._default_display_size:
            entry["size"] = "{}x{}".format(*self._default_display_size)
        return entry

    def _start_screencap(self, display_id, requested) -> None:
        """Show *display_id* with ADB screencap frames inside the container."""
        from .screencap_view import ScreencapView

        self._stop_screencap()
        view = ScreencapView(
            self.handler,
            self._selected_display_entry(display_id),
            dispatcher=self._dispatcher,
            max_fps=self._screencap_fps,
            key_sink=self._send_key,
            key_release=self._release_key,
            parent=self.container,
        )
        view.setGeometry(self.container.rect())
        view.log.connect(self.log)
        view.first_frame.connect(self._on_screencap_ready)
        view.frame_shape_changed.connect(self._on_screencap_shape)
        view.ended.connect(self._on_screencap_ended)
        view.stats_changed.connect(self._sync_renderer_chip)
        self._screencap = view
        self._launch_spec = dict(requested)
        self._retry_count = 0
        self._became_ready = False
        self.status.show()
        self.status.setText("Starting ADB screencap…")
        self._refresh_buttons()
        view.show()
        view.start()
        if self.container.hasFocus():
            view.setFocus(Qt.OtherFocusReason)  # keep typing where the user was
        self.log.emit(
            f"[INFO] screen renderer: ADB screencap for display {view.display_id} "
            f"(up to {self._screencap_fps} fps; slower than scrcpy, needs only adb)"
        )

    def _on_screencap_ready(self) -> None:
        if self._closing or self._screencap is None:
            return
        self._became_ready = True
        self.status.setText("")
        self.status.hide()
        self.screen_ready.emit()

    def _on_screencap_shape(self) -> None:
        view = self._screencap
        aspect = view.aspect() if view is not None else None
        if aspect and abs(aspect - (self._video_aspect or 0.0)) > 0.01:
            self._video_aspect = aspect
            self.display_shape_changed.emit()

    def _on_screencap_ended(self, message, failed) -> None:
        if self._closing:
            return
        self._stop_screencap()
        self._refresh_buttons()
        self.status.show()
        if failed:
            self.log.emit(f"[ERROR] screencap: {message}")
            self.status.setText(
                f"Screen capture stopped: {message}. <a href='#screencap-retry'>Retry</a>"
            )
            self.screen_failed.emit(str(message))
        else:
            self.status.setText("Screen capture ended. <a href='#screencap-retry'>Retry</a>")

    def _sync_renderer_chip(self) -> None:
        """Show the screencap frame rate in the toolbar (hidden without one)."""
        chip = self.__dict__.get("lbl_renderer")
        if chip is None or self._closing:
            return
        view = self._screencap
        text = view.renderer_text() if view is not None else ""
        chip.setText(text)
        # one width for every rate, so "9.8 fps" -> "10.2 fps" moves nothing
        chip.setMinimumWidth(self._renderer_chip_width())
        chip.setVisible(bool(text))
        self._note_toolbar_width()

    # the widest frame-rate text the chip is sized for (the cap is 10 fps)
    RENDERER_CHIP_SAMPLE = "screencap · 99.9 fps"

    def _renderer_chip_width(self) -> int:
        """The chip's width with its widest text, measured on a hidden twin
        styled like it (so neither the chip's own text nor a layout changes)."""
        probe = self.__dict__.get("_chip_probe")
        if probe is None:
            probe = QLabel(self.RENDERER_CHIP_SAMPLE, self)
            probe.setObjectName(self.lbl_renderer.objectName())
            probe.hide()
            self._chip_probe = probe
        probe.ensurePolished()  # the chip style's own (smaller) font and padding
        return probe.sizeHint().width()

    def _note_toolbar_width(self) -> None:
        """Emit toolbar_changed when the one-row toolbar width changed (only
        then: an owner re-plans its layout on it)."""
        if self.__dict__.get("_toolbar") is None or self._closing:
            return
        width = self.toolbar_width()
        if width != self.__dict__.get("_toolbar_width_seen"):
            self._toolbar_width_seen = width
            self.toolbar_changed.emit()

    def _stop_screencap(self) -> bool:
        """Stop and remove the screencap view; True when one was running."""
        view, self._screencap = self._screencap, None
        if view is None:
            return False
        self._sync_renderer_chip()
        for signal in (view.first_frame, view.frame_shape_changed, view.ended, view.stats_changed):
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
        view.close_view()
        # its capture worker may still be finishing a display lookup: keep it
        # listed for shutdown_threads until it has
        self._screencap_threads = [
            thread for thread in self._screencap_threads if thread_running(thread)
        ] + view.running_threads()
        view.hide()
        view.setParent(None)
        view.deleteLater()
        return True

    def screencap_view(self):
        """The ScreencapView showing the screen now, or None."""
        return self._screencap

    def _apply_video_budget(self, opts) -> None:
        """Lower *opts* to this panel's video budget; never raise anything."""
        budget = self._video_budget
        cap = budget.get("bit_rate")
        if cap:
            current = int(_bitrate_to_bps(opts.bit_rate or "16M"))
            if int(_bitrate_to_bps(cap)) < current:
                opts.bit_rate = str(cap)
        fps = budget.get("max_fps")
        if fps and (not opts.max_fps or int(opts.max_fps) > int(fps)):
            opts.max_fps = int(fps)

    def set_video_budget(self, *, bit_rate=None, max_fps=None) -> None:
        """Caps for this panel's scrcpy video (a display tile shares the device's
        encoder and the adb link with the other tiles). None = no cap. Applies
        from the next start."""
        self._video_budget = {
            key: value
            for key, value in (("bit_rate", bit_rate), ("max_fps", max_fps))
            if value
        }

    def _on_audio_enabled_changed(self, enabled: bool) -> None:
        self._opt["audio"] = bool(enabled)
        for control in (self.__dict__.get("act_audio"), self.__dict__.get("btn_audio")):
            if control is not None and control.isChecked() != bool(enabled):
                control.blockSignals(True)
                control.setChecked(bool(enabled))
                control.blockSignals(False)
        self._sync_audio_button()
        self._save_audio_options()
        if self._scrcpy is not None or self._launch_pending:
            self.log.emit("[INFO] audio setting saved — restart the display to apply it")

    def _save_audio_options(self, *_args) -> None:
        """Persist sound controls without touching unrelated workspace settings."""
        try:
            settings_mod.update(
                {
                    "scrcpy_audio": self._audio_enabled(),
                    "scrcpy_audio_source": self._audio_value("cmb_audio_source", "output"),
                }
            )
        except Exception:
            pass

    def _apply_audio_options(self, opts: ScrcpyOptions, *, allow_audio=True) -> ScrcpyOptions:
        """Attach the persisted audio profile to one scrcpy session."""
        compat = bool(self._opt["compat"])
        enabled = (
            allow_audio
            and self._audio_enabled()
            and not compat
            and not getattr(opts, "no_audio", False)
        )
        opts.no_audio = not enabled
        if not enabled:
            return opts
        saved = settings_mod.load()
        source = str(self._audio_value("cmb_audio_source", "output"))
        opts.audio_source = source
        opts.audio_codec = str(saved.get("scrcpy_audio_codec", "opus"))
        opts.audio_bit_rate = str(saved.get("scrcpy_audio_bit_rate", "128K"))
        opts.audio_buffer = int(saved.get("scrcpy_audio_buffer", 50))
        opts.audio_output_buffer = int(saved.get("scrcpy_audio_output_buffer", 10))
        opts.audio_dup = bool(saved.get("scrcpy_audio_dup", False)) and source == "playback"
        return opts

    _MAX_TIP = "Maximize view: give the device screen the whole tab"
    _RESTORE_TIP = "Restore view: bring back the controls and side panels (Esc)"

    def _sync_max_buttons(self, on: bool) -> None:
        """Keep the toolbar button, the options-panel entry, the action and the
        Esc shortcut in step with Maximize view."""
        on = bool(on)
        text = "Restore view" if on else "Maximize view"
        tip = self._RESTORE_TIP if on else self._MAX_TIP
        self.act_max.setText(text)
        self.act_max.setToolTip(tip)
        button = self.__dict__.get("btn_max")
        if button is not None:
            button.blockSignals(True)
            button.setChecked(on)
            button.blockSignals(False)
            button.setText(text)
            button.setAccessibleName(text)
            button.setToolTip(tip)
        entry = self.__dict__.get("btn_toggle_max")
        if entry is not None:
            entry.setText(f"⛶ {text}")
            entry.setToolTip(tip)
        shortcut = self.__dict__.get("_esc_restore")
        if shortcut is not None:
            shortcut.setEnabled(on)

    def _on_compat_toggled(self, _checked: bool) -> None:
        if not self._setting_compat_default:
            self._compat_user_changed = True

    def set_automotive_default(self, automotive: bool) -> None:
        """Apply delayed automotive detection without overriding user options."""
        self.automotive = bool(automotive)
        self._sync_idle_shape()
        self._sync_all_displays_button()
        if self._compat_user_changed or self._opt["compat"] == self.automotive:
            return
        self._setting_compat_default = True
        try:
            self._set_option("compat", self.automotive)
        finally:
            self._setting_compat_default = False

    def _type_text(self):
        """Type text into the device's focused field via adb (`input text`).
        Reliable for entering a URL etc. when the mirror's keyboard won't type."""
        text, ok = QInputDialog.getText(
            self,
            "Type into the device",
            "Text to send to the focused field on the device\n"
            "(tap the field in the mirror FIRST so it has the cursor):",
        )
        if not ok or not text:
            return
        count = len(text)
        self.log.emit(f"[INFO] Sending {count} character{'' if count == 1 else 's'} to the device…")

        def done(result):
            if self._closing:
                return
            if _command_error(result, "rejected") is None:
                self.log.emit("[OK] text sent")
            else:
                self.log.emit(
                    "[WARNING] the device didn't accept injected text — "
                    "make sure a text field is focused (tap it in the mirror first)"
                )

        def failed(message):
            if not self._closing:
                self.log.emit(f"[ERROR] type: {message}")

        handler = self.handler
        self._dispatcher.submit(
            lambda: handler.input_text(text, safe=True),
            on_done=done,
            on_fail=failed,
            priority=0,
        )

    def _refresh_buttons(self):
        """Keep the screen start/stop controls mutually exclusive and clear."""
        viewing = self._scrcpy is not None
        active = (
            viewing
            or self._screencap is not None
            or self._launch_pending
            or self._stopping_mirror
            or self._retry_pending
        )
        self.btn_mirror.setVisible(not active)
        self.btn_mirror.setEnabled(not active)
        if hasattr(self, "btn_popout"):
            # Keep this affordance visible throughout the session.  It either
            # starts a real native window, switches from an embedded mirror, or
            # brings the existing native window back to the foreground.
            self.btn_popout.setVisible(True)
            self.btn_popout.setEnabled(not self._launch_pending)
        if self.__dict__.get("act_mirror_all") is not None:
            self.act_mirror_all.setEnabled(True)
        self.btn_stop.setVisible(active)
        self.btn_stop.setEnabled(active and not self._stopping_mirror)
        # Screenshot remains available while a mirror is running; it is disabled
        # only during its own short capture worker to prevent duplicate writes.
        self.btn_shot.setEnabled(self._shot is None)
        rec_starting = self._rec_integrated_starting or self._rec_parallel_starting
        self.btn_record.setEnabled(not self._record_finalizing and not rec_starting)
        if rec_starting:
            self.btn_record.setText("Starting recording…")
        elif self._record_finalizing:
            self.btn_record.setText("Saving recording…")
        else:
            self.btn_record.setText("Stop recording" if self._recording else "Record…")
        # Idle, Record is a glyph; while recording its state is spelled out.
        recording_state = self._recording or self._record_finalizing or rec_starting
        self.btn_record.setToolButtonStyle(
            Qt.ToolButtonTextBesideIcon if recording_state else Qt.ToolButtonIconOnly
        )
        self._sync_renderer_controls()
        self._sync_video_surface(active)
        self._note_toolbar_width()

    def _popout_mirror(self):
        """Launch scrcpy in a dedicated, native full-size floating window."""
        if self._integrated_recording_blocks(
            "Stop the current recording before moving the screen window."
        ):
            return
        # A button click while the initial launch is still underway should not
        # be lost.  Cancel that launch and start the requested native window as
        # soon as its worker returns, rather than racing two scrcpy instances.
        if self._launch_pending:
            self._queued_start = {"embed": False}
            self._cancel_launch = True
            self.status.show()
            self.status.setText("Switching to a separate scrcpy window…")
            self._refresh_buttons()
            return
        # When a native window is already open this is a useful, stable
        # "bring it back" command — never restart a healthy external scrcpy.
        if self._scrcpy is not None and self._scrcpy.running and not self._embed_on:
            self._focus_separate_window()
            return
        # ScrcpySession.stop() waits for its process to exit.  Explicitly pass
        # False here as well: a separate window must never inherit the saved
        # “show inside this tab” preference.
        if self._scrcpy is not None:
            self.stop()
        # a separate window is always scrcpy: the screencap view gives way
        self._stop_screencap()
        self.start(embed=False)

    def _focus_separate_window(self, *, focus_keyboard: bool = False):
        """Restore and foreground the existing native scrcpy window."""
        if not _IS_WIN:
            self.status.setText("The separate scrcpy window is already running.")
            return
        try:
            hwnd = self._separate_hwnd or _find_window(self._win_title)
            if not hwnd:
                self.status.setText("The separate scrcpy window is still starting…")
                return
            self._separate_hwnd = hwnd
            _ctypes, user32 = _win_api()
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE, if minimized
            if focus_keyboard:
                # Called directly from a user click, while Windows still lets
                # this process hand keyboard input to the native scrcpy window.
                # The helper uses AttachThreadInput for the cross-process focus
                # transfer; it must never be scheduled through QTimer because
                # its Alt nudge would activate a Qt menu after the event ends.
                _focus_window(hwnd)
            else:
                user32.SetForegroundWindow(hwnd)
            self.status.setText("Separate scrcpy window is active.")
        except Exception:
            self.status.setText("The separate scrcpy window is already running.")

    # ----- mirror the device CAMERA (scrcpy 2.2+, Android 12+) -----
    def _mirror_camera(self):
        """Open the device's CAMERA as the video source instead of the screen —
        a live webcam-style view (front or back, chosen under Options). Needs
        scrcpy 2.2+ on the host and Android 12+ on the device."""
        facing = "front" if self._opt["cam_front"] else "back"
        self.log.emit(f"[INFO] Opening the device {facing} camera (scrcpy)…")
        self.start(camera=facing)

    def is_maximized(self) -> bool:
        """True while Maximize view is on."""
        return bool(self.act_max.isChecked())

    def _toggle_max(self, on=None):
        """Maximize view: the device screen gets the whole tab.

        The owner does the in-tab part when told through max_view_changed
        (Device Control hides its controls panel); the main window's docks are
        hidden here. Hiding only the docks did nothing visible most of the time:
        the device list already hides itself while a screen shows, and the log
        is closed by default.
        """
        if on is None:
            on = self.act_max.isChecked()
        on = bool(on)
        if on:
            self._hide_docks()
        else:
            self._show_docks()
            self._max_hidden = []
        self._sync_max_buttons(on)
        self.max_view_changed.emit(on)
        if on:
            self._focus_for_esc()
        QTimer.singleShot(200, self._fit)  # re-fit the embed to the new size

    def _focus_for_esc(self) -> None:
        """Maximize was chosen with the keyboard focus outside this panel (a
        toolbar click leaves it where it was): move it to the Maximize button,
        so Esc restores the view right away. Focus already inside the panel —
        the screen typing to the device — is left alone."""
        from PyQt5.QtWidgets import QApplication

        if self._closing or not self.isVisible():
            return
        focus = QApplication.focusWidget()
        if focus is not None and (focus is self or self.isAncestorOf(focus)):
            return
        self.btn_max.setFocus(Qt.OtherFocusReason)

    def _hide_docks(self):
        from PyQt5.QtWidgets import QDockWidget, QMainWindow

        win = self.window()
        if not isinstance(win, QMainWindow):
            return
        # only hide docks that are showing, and restore exactly those —
        # blanket-showing everything un-hid docks the user had closed.
        # Merge with the docks already recorded: resuming max view twice
        # (tab switch + split view) must not forget what the first call hid.
        for d in win.findChildren(QDockWidget):
            if d.isVisible():
                if d not in self._max_hidden:
                    self._max_hidden.append(d)
                d.setVisible(False)

    def _show_docks(self):
        for d in getattr(self, "_max_hidden", []):
            try:
                d.setVisible(True)
            except RuntimeError:
                pass

    def _suspend_max(self):
        """Give the docks back while another tab shows. The tab's own maximized
        layout is kept, so coming back finds it unchanged."""
        self._show_docks()

    def _resume_max(self):
        """Re-hide docks when switching back to the mirror tab if max view is active."""
        if hasattr(self, "act_max") and self.act_max.isChecked():
            self._hide_docks()

    # ----- display list -----
    def open_ivi_view(self) -> None:
        """Ask the device tab to show every display at once (the displays tab)."""
        self.all_displays_requested.emit()

    def _all_displays_offered(self) -> bool:
        return not self._fixed_display and (self.automotive or len(self._displays) > 1)

    def _sync_all_displays_button(self) -> None:
        button = self.__dict__.get("btn_ivi")
        if button is not None:
            button.setVisible(self._all_displays_offered())

    def refresh_displays(self, quiet: bool = False):
        """List the device's displays in the background.

        A *quiet* scan (the automatic one on connect) asks Android over adb only
        — it never starts scrcpy, so it can't delay the first screen — and stays
        silent unless it finds several displays.
        """
        if thread_running(self._dt):
            if not quiet:
                self.log.emit("[INFO] Display scan is already running.")
            return
        if self.handler is None:
            return
        self._quiet_scan = bool(quiet)
        if not quiet:
            self.log.emit("[INFO] Listing displays…")
        handler = self.handler
        method = "adb" if quiet else "auto"

        def scan():
            try:
                return handler.list_displays(method=method, safe=True)
            except TypeError:  # a handler without the *method* option
                return handler.list_displays(safe=True)

        self._dt = FunctionThread(scan)
        self._dt.done.connect(self._on_displays_result)
        self._dt.fail.connect(self._on_displays_failed)
        self._dt.start()

    def _on_displays_result(self, result):
        if self._closing:
            return
        if isinstance(result, OperationResult):
            if not result.success:
                self._on_displays_failed(str(result.error or "display discovery failed"))
                return
            result = result.value
        self._got_displays(result, quiet=self._quiet_scan)

    def _on_displays_failed(self, message):
        if self._closing:
            return
        if not self._quiet_scan:
            self.log.emit("[ERROR] displays: " + message)
        self.displays_failed.emit(message)

    def set_displays(self, displays, quiet=True):
        """Use a display list another probe already fetched, instead of
        scanning again (the device tab's connect probe lists them)."""
        self._got_displays(displays, quiet=quiet)

    def _got_displays(self, displays, quiet=False):
        if self._fixed_display:
            return
        self._displays = sorted(
            (dict(d) for d in (displays or []) if isinstance(d, dict) and "id" in d),
            key=lambda d: int(d.get("id", 0) or 0),
        )
        self._displays_loaded = True
        keep = self.cmb_display.currentData()  # preserve the user's choice
        self.cmb_display.blockSignals(True)
        try:
            self.cmb_display.clear()
            # Display 0 is the default display: listed once, started without an id.
            if not any(int(d["id"]) == 0 for d in self._displays):
                self.cmb_display.addItem("default display", None)
            for d in self._displays:
                display_id = int(d["id"])
                self.cmb_display.addItem(display_label(d), None if display_id == 0 else display_id)
            idx = self.cmb_display.findData(keep)
            self.cmb_display.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self.cmb_display.blockSignals(False)
        if self.__dict__.get("act_mirror_all") is not None:
            self.act_mirror_all.setEnabled(True)
        self._sync_all_displays_button()
        self._sync_idle_shape()
        if not quiet or len(self._displays) > 1:
            self.log.emit(f"[OK] {len(self._displays)} display(s) found")
        self.displays_changed.emit(list(self._displays))

    def _on_display_combo_changed(self, idx: int):
        if idx >= 0:
            self._switch_display(self.cmb_display.itemData(idx))

    def _switch_display(self, disp_id):
        """Apply a display chosen in the picker to the selection and the mirror."""
        if self._integrated_recording_blocks(restore_selection=True):
            return
        self._sync_display_selection(disp_id)
        if self._scrcpy is not None or self._screencap is not None:
            # start() stops the running viewer and launches the new display.
            self.start(display_id=disp_id)

    def _show_display_manager(self):
        """Open an interactive dialog showing all detected displays, allowing the user
        to select, mirror, record, or screenshot each display individually."""
        from PyQt5.QtWidgets import QDialog, QTableWidget, QTableWidgetItem, QHeaderView

        dlg = QDialog(self)
        dlg.setWindowTitle("Device displays")
        dlg.setMinimumWidth(620)
        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(18, 16, 18, 16)
        vl.setSpacing(10)

        title = QLabel("Device displays")
        title.setObjectName("iviPreviewTitle")
        vl.addWidget(title)

        info = QLabel(
            "Select any display to mirror, record, or screenshot independently. "
            "Multi-display IVI units expose center console, cluster, passenger screen, etc."
        )
        info.setWordWrap(True)
        info.setObjectName("mutedHint")
        vl.addWidget(info)

        lbl_status = QLabel("")
        lbl_status.setObjectName("mutedHint")
        vl.addWidget(lbl_status)

        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["Display ID", "Resolution", "Status", "Actions"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        vl.addWidget(table)

        def populate():
            current_disp = self.cmb_display.currentData()
            displays_list = self._displays if self._displays else [{"id": 0, "size": "primary"}]
            table.setRowCount(len(displays_list))

            for row, d in enumerate(displays_list):
                disp_id = d.get("id", row)
                size = d.get("size", "") or "default"
                name = f"Display {disp_id}" + (" (Primary)" if disp_id == 0 else "")
                table.setItem(row, 0, QTableWidgetItem(name))
                table.setItem(row, 1, QTableWidgetItem(str(size)))

                is_active = (current_disp == disp_id) or (current_disp is None and disp_id == 0)
                status_text = "Active" if is_active else "Available"
                table.setItem(row, 2, QTableWidgetItem(status_text))

                act_w = QWidget()
                hl = QHBoxLayout(act_w)
                hl.setContentsMargins(4, 2, 4, 2)
                hl.setSpacing(6)

                btn_m = QPushButton("▶ Start")
                btn_m.setProperty("role", "ghost")
                btn_m.setToolTip(f"Mirror Display {disp_id}")
                btn_m.clicked.connect(lambda _=False, did=disp_id: (dlg.accept(), self._select_and_mirror(did)))
                hl.addWidget(btn_m)

                btn_r = QPushButton("Record")
                btn_r.setProperty("role", "ghost")
                btn_r.setToolTip(f"Record Display {disp_id}")
                btn_r.clicked.connect(lambda _=False, did=disp_id: (dlg.accept(), self._select_and_record(did)))
                hl.addWidget(btn_r)

                btn_s = QPushButton("Screenshot")
                btn_s.setProperty("role", "ghost")
                btn_s.setToolTip(f"Screenshot Display {disp_id}")
                btn_s.clicked.connect(lambda _=False, did=disp_id: (dlg.accept(), self._take_screenshot(display_id=did)))
                hl.addWidget(btn_s)

                table.setCellWidget(row, 3, act_w)

        populate()

        btn_row = QHBoxLayout()
        btn_refresh = QPushButton("⟳ Refresh displays")

        def on_displays(_displays=None):
            populate()
            lbl_status.setText(f"Found {len(self._displays)} display(s)")
            btn_refresh.setEnabled(True)

        def on_failed(err):
            lbl_status.setText(f"Display query error: {err}")
            btn_refresh.setEnabled(True)

        # Listen on the panel, not on one scan worker, and only while the dialog
        # is open: repeated refreshes can never stack up duplicate handlers.
        self.displays_changed.connect(on_displays)
        self.displays_failed.connect(on_failed)

        def on_refresh_clicked():
            btn_refresh.setEnabled(False)
            lbl_status.setText("Querying device displays…")
            self.refresh_displays()

        btn_refresh.clicked.connect(on_refresh_clicked)
        btn_row.addWidget(btn_refresh)
        btn_row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_close)
        vl.addLayout(btn_row)

        if not self._displays:
            on_refresh_clicked()

        try:
            dlg.exec_()
        finally:
            for signal, slot in (
                (self.displays_changed, on_displays),
                (self.displays_failed, on_failed),
            ):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            dlg.deleteLater()

    def _sync_display_selection(self, disp_id):
        c_idx = self.cmb_display.findData(disp_id)
        if c_idx < 0 and disp_id in (0, None):
            # Display 0 is listed as the default display (no id), and vice versa.
            c_idx = self.cmb_display.findData(None if disp_id == 0 else 0)
        if c_idx >= 0 and c_idx != self.cmb_display.currentIndex():
            self._video_aspect = None  # another display: its real shape is unknown yet
        if c_idx >= 0:
            self.cmb_display.blockSignals(True)
            self.cmb_display.setCurrentIndex(c_idx)
            self.cmb_display.blockSignals(False)
        self._sync_idle_shape()  # the combo's signals were blocked above

    def set_default_display_size(self, size) -> None:
        """Size ("WxH") of the default display when known without a display
        scan (e.g. from ``wm size``); it shapes the idle screen."""
        self._default_display_size = _parse_display_size(size)
        self._sync_idle_shape()

    def _selected_display_size(self):
        """The (width, height) of the display chosen in the combo, if known."""
        display_id = self.cmb_display.currentData()
        chosen = None
        for display in self._displays or []:
            wanted = "0" if display_id is None else str(display_id)
            if str(display.get("id")) == wanted:
                chosen = display
                break
        size = _parse_display_size(chosen.get("size")) if chosen else None
        if size is None and display_id in (None, 0):
            size = self._default_display_size
        return size

    def _sync_idle_shape(self, *_args) -> None:
        """Shape the idle "Screen is off" frame like the selected display."""
        empty = getattr(self, "empty_state", None)
        if empty is None:
            return
        empty.set_device(automotive=self.automotive, size=self._selected_display_size())
        self.display_shape_changed.emit()

    def display_aspect(self) -> float:
        """Width / height of the screen shown here: the real video once it has
        been embedded, else the selected display's known (or typical) shape."""
        if self._video_aspect:
            return float(self._video_aspect)
        empty = getattr(self, "empty_state", None)
        return float(empty.aspect) if empty is not None else 16.0 / 9.0

    def chrome_height(self, width=None) -> int:
        """Height taken above the video area (toolbar, status line): as laid out
        now, or — given a panel *width* — what it would be at that width (the
        toolbar wraps onto more rows when the panel is narrower than
        :meth:`toolbar_width`)."""
        container = getattr(self, "container", None)
        if width is None:
            if container is None or self.height() <= 0:
                return 0
            return max(0, self.height() - container.height())
        bar = self._toolbar_widget.heightForWidth(max(1, int(width)))
        if bar <= 0:
            bar = self._toolbar_widget.sizeHint().height()
        status = 0
        if not self.status.isHidden():
            status = self.status.heightForWidth(max(1, int(width)))
            if status <= 0:
                status = self.status.sizeHint().height()
        return bar + status

    def toolbar_width(self) -> int:
        """Width the screen toolbar needs to fit on ONE row (its shown items,
        the gaps between them and the margins).

        Record counts at its idle, icon-only width: its "Stop recording" label
        is transient, and re-planning Device Control around it would reflow
        the controls panel whenever a recording starts or ends (at worst the
        toolbar wraps for the duration instead)."""
        bar = self._toolbar
        self._toolbar_widget.ensurePolished()  # hidden buttons measure styled too
        gap = bar._hspace
        # Idle, Start shows; while a screen runs, Stop and (screencap) the frame
        # rate. One slot takes the wider of the two states, so starting or
        # stopping a screen never re-plans (and reflows) Device Control. The
        # frame rate is reserved while that renderer is selected, at its widest
        # text, so neither its appearance nor its changing rate does either.
        slotted = (self.btn_mirror, self.btn_stop, self.lbl_renderer, self.btn_record)
        widths = []
        for index in range(bar.count()):
            item = bar.itemAt(index)
            if item is None or item.widget() in slotted:
                continue
            if item.isEmpty() and item.spacerItem() is None:
                continue
            widths.append(item.sizeHint().width())
        running = _hint_width(self.btn_stop)
        if not self.lbl_renderer.isHidden() or self.screen_backend() == "screencap":
            running += gap + self._renderer_chip_width()
        if not (self.btn_mirror.isHidden() and self.btn_stop.isHidden()):
            widths.append(max(_hint_width(self.btn_mirror), running))
        if not self.btn_record.isHidden():
            widths.append(_hint_width(self.btn_shot))
        if not widths:
            return 0
        margins = bar.contentsMargins()
        return sum(widths) + gap * (len(widths) - 1) + margins.left() + margins.right()

    def set_polling_paused(self, paused: bool) -> None:
        """Slow this panel's upkeep timers while it is off screen.

        A screen the user cannot see still needs no re-fitting twice a second,
        and a wall of them polls the process list that often each. The scrcpy
        session itself is untouched, so showing the tab again is immediate.
        """
        self._poll_interval = self.IDLE_POLL_MS if paused else self.POLL_MS
        # (a screencap view pauses itself when it is hidden: see ScreencapView)
        for name in ("_fit_timer", "_mon"):
            timer = getattr(self, name, None)
            if timer is not None and timer.isActive():
                timer.setInterval(self._poll_interval)

    def is_active(self) -> bool:
        """True while a screen session is starting, running, stopping or retrying."""
        return bool(
            self._scrcpy is not None
            or self._screencap is not None
            or self._launch_pending
            or self._stopping_mirror
            or self._retry_pending
        )

    def set_fixed_display(self, display) -> None:
        """Lock this panel to one display — a tile in the all-displays tab.

        The display picker, options, audio and max-view controls are hidden:
        several sessions can't all play sound, and the tab owns the layout.

        Re-applying a display that changed (a head unit that resized a screen)
        drops the remembered video shape, so the idle frame and the tab's tile
        layout follow the new size instead of the old one.
        """
        display = dict(display or {})
        display_id = int(display.get("id", 0) or 0)
        if self._displays[:1] != [display]:
            self._video_aspect = None
        self._fixed_display = True
        self._displays = [display]
        self._displays_loaded = True
        self.cmb_display.blockSignals(True)
        try:
            self.cmb_display.clear()
            self.cmb_display.addItem(display_label(display), None if display_id == 0 else display_id)
            self.cmb_display.setCurrentIndex(0)
        finally:
            self.cmb_display.blockSignals(False)
        for widget in (self.cmb_display, self.btn_audio, self.btn_opts, self.btn_max):
            widget.hide()
        if self._screencap is not None:
            self._screencap.display.update(display)  # a new size maps touches anew
        self._sync_all_displays_button()
        self._sync_idle_shape()

    def _select_and_mirror(self, disp_id):
        if self._integrated_recording_blocks():
            return
        self._sync_display_selection(disp_id)
        if self._scrcpy is not None:
            self.stop()
        # The display chooser must honour the selected view mode just like the
        # main Start button.  Explicit top-level menu actions still pass an
        # embed value when they need to override it.
        self.start(display_id=disp_id)

    def _select_and_record(self, disp_id):
        self._sync_display_selection(disp_id)
        self._begin_record(display_id=disp_id)


    def _tune(self, opts):
        """Apply performance and quality tuning. Over GPU-accelerated local sessions,
        preserves native resolution and high 16M bitrate identical to native scrcpy.
        Over Remote Desktop, applies software rendering and sensible caps."""
        if self._opt["soft"]:
            opts.render_driver = "software"
            opts.max_size = opts.max_size or 1280
            opts.bit_rate = opts.bit_rate or "8M"
            opts.max_fps = opts.max_fps or 30
            if not opts.no_audio:
                self.log.emit(
                    "[INFO] tip: untick Options → Forward audio for "
                    "an even smoother mirror over Remote Desktop "
                    "(audio costs extra CPU + bandwidth there)"
                )
        else:
            if not opts.bit_rate:
                opts.bit_rate = "16M"
            # The embedded screen is usually drawn 3-4x smaller than the phone's
            # resolution. SDL's default Direct3D renderer scales without mipmaps,
            # which made small text grainy; OpenGL enables trilinear filtering.
            if not opts.render_driver and not getattr(self, "_avoid_opengl", False):
                opts.render_driver = "opengl"
        return opts

    # ----- screen recording (device-side; works with mirror OR Live View) -----
    def _set_record_status(self, text, *, show=False) -> None:
        """Put a recording progress line on the status row. It is remembered,
        so the finished (or failed) recording can take exactly that line away
        again instead of leaving "Saving the MP4…" under a live screen."""
        self._rec_status_text = text
        self.status.setText(text)
        if show:
            self.status.show()

    def _clear_record_status(self) -> None:
        """Remove the recording's own progress line, if it is still the one shown."""
        text = self.__dict__.get("_rec_status_text")
        self._rec_status_text = None
        if text and self.status.text() == text:
            self.status.setText("")
            self.status.hide()

    def _toggle_record(self):
        if self._record_finalizing:
            self._set_record_status("Saving the previous recording to your PC…")
            return
        if self._recording:
            self._stop_record()
        else:
            self._begin_record()

    def _begin_record(self, display_id="__use_combo__"):
        if self._record_finalizing:
            return
        if self._recording:
            self.status.show()
            self.status.setText("A recording is already running. Stop it before starting another.")
            self.log.emit("[WARNING] ignored duplicate Record request; one recording is already active")
            return
        if display_id == "__use_combo__":
            disp_id = self.cmb_display.currentData()
        else:
            disp_id = display_id
        disp_lbl = f"Display {disp_id}" if disp_id is not None else "Device Screen"
        tag = f"disp{disp_id}-" if disp_id is not None else ""
        default = os.path.join(download_dir(), time.strftime(f"recording-{tag}%Y%m%d-%H%M%S.mp4"))
        path, _sel = QFileDialog.getSaveFileName(
            self, f"Record {disp_lbl} to…", default, "MP4 video (*.mp4)"
        )
        if not path:
            return
        if not path.lower().endswith(".mp4"):
            path += ".mp4"
        self._rec_path = path
        self._rec_display_id = disp_id
        self._recording = True
        self._record_finalizing = False
        # If a mirror is still launching/stopping, replace that pending view
        # with a single integrated recording launch. Never start a competing
        # device-side encoder while scrcpy is in transition.
        if self._launch_pending or self._stopping_mirror:
            spec = dict(self._launch_spec or {})
            spec.update(
                {
                    "display_id": disp_id,
                    "record": path,
                    "record_format": "mp4",
                }
            )
            self._rec_mode = "integrated"
            self._rec_integrated_starting = True
            self._rec_restore_spec = {
                key: value for key, value in spec.items() if key not in ("record", "record_format")
            }
            self._queued_start = spec
            if self._launch_pending:
                self._cancel_launch = True
            self._set_record_status("Starting one integrated scrcpy recording…")
            self._refresh_buttons()
            return
        # Recording must never stop, restart or re-embed a live screen (that
        # blanked the screen for seconds, sometimes for good, exactly when the
        # user wanted to capture something). A second, windowless scrcpy
        # records alongside it; scrcpy runs concurrent sessions per device.
        if (
            self._scrcpy is not None
            and self._scrcpy.running
            and not self._launch_pending
        ):
            self._start_parallel_record(path, disp_id)
            return
        self._record_device(path, display_id=disp_id)
        self._refresh_buttons()
        # Clicking Record moves Windows focus to the toolbar button. Return it
        # synchronously, while this user event may still transfer focus to the
        # separate scrcpy process. A deferred QTimer callback is too late for
        # Windows' foreground-input policy and leaves typing in TurboADB.
        self._restore_mirror_focus(direct=True)

    # A parallel recorder that exits this soon failed to start; one that has
    # written no MP4 data by the timeout never will.  Either falls back to
    # device-side screenrecord.
    _PARALLEL_REC_GRACE = 5.0
    _PARALLEL_REC_DATA_TIMEOUT = 20.0

    def _parallel_record_options(self, path, display_id):
        """Options for a windowless recorder matching the live session's video."""
        live = self._launch_opts
        if live is None:
            st = settings_mod.load()
            live = self._tune(
                ScrcpyOptions(
                    max_size=(st.get("scrcpy_max_size") or None),
                    bit_rate=st.get("scrcpy_bit_rate") or None,
                    video_codec=(st.get("scrcpy_video_codec") or None),
                    no_audio=True,
                )
            )
        camera = (self._launch_spec or {}).get("camera")
        self._rec_title = f"turboadb-record-{os.getpid()}-{id(self):x}-{_next_window_nonce()}"
        opts = ScrcpyOptions(
            max_size=live.max_size,
            bit_rate=live.bit_rate,
            max_fps=live.max_fps,
            video_codec=live.video_codec,
            display_id=None if camera else display_id,
            video_source=("camera" if camera else None),
            camera_facing=(camera or None),
            record=path,
            record_format="mp4",
            no_playback=True,
            no_control=True,
            stay_awake=False,
            # Its own title and log keep window lookups from ever confusing the
            # two sessions. --no-window matters: with only --no-playback,
            # scrcpy 4 still opens a window and takes the foreground.
            window_title=self._rec_title,
            extra_args=["--no-window"],
        )
        return self._apply_audio_options(opts, allow_audio=not bool(camera))

    def _start_parallel_record(self, path, display_id):
        """Record with a second, windowless scrcpy; the live session is untouched."""
        import tempfile

        opts = self._parallel_record_options(path, display_id)
        self._retire_rec_log()
        self._rec_log_path = os.path.join(
            tempfile.gettempdir(),
            f"turboadb-scrcpy-rec-{os.getpid()}-{id(self):x}-{time.monotonic_ns()}.log",
        )
        self._rec_mode = "parallel"
        self._rec_parallel_starting = True
        self._rec_session = None
        self._rec_confirmed = False
        disp_str = f" Display {display_id}" if display_id is not None else ""
        self._set_record_status(f"Starting recording{disp_str} alongside the live screen…")
        self.log.emit(
            f"[INFO] recording{disp_str} with a second scrcpy session; "
            f"the live screen keeps running → {path}"
        )
        # The live session's launch path: same serial, adb, remote adb server
        # and compatibility profile.
        worker = _MirrorLaunchThread(self.handler, opts, self._compat, self._rec_log_path)
        self._rec_launch_thread = worker
        park_thread(worker)
        worker.done.connect(self._on_parallel_record_launched)
        worker.fail.connect(self._on_parallel_record_launch_failed)
        worker.start()
        self._refresh_buttons()
        self._restore_mirror_focus(direct=True)

    def _on_parallel_record_launched(self, session):
        self._rec_launch_thread = None
        self._rec_parallel_starting = False
        if self._rec_mode != "parallel":
            if session is not None:
                self._discard_recorder(session)
            return
        if session is None:
            self._on_parallel_record_launch_failed("no scrcpy session was created")
            return
        self._rec_session = session
        self._rec_started_t = time.time()
        if not self._recording:
            # Stop (or a closing tab) arrived while the recorder launched.
            self._finish_parallel_record()
            return
        disp = getattr(self, "_rec_display_id", None)
        disp_str = f" Display {disp}" if disp is not None else ""
        self._set_record_status(f"● Recording{disp_str} → {self._rec_path}")
        self._stop_rec_watch()
        self._rec_watch = QTimer(self)
        self._rec_watch.timeout.connect(self._poll_parallel_record)
        self._rec_watch.start(500)
        self._refresh_buttons()

    def _on_parallel_record_launch_failed(self, error):
        self._rec_launch_thread = None
        self._rec_parallel_starting = False
        if self._rec_mode == "parallel":
            self._fallback_parallel_record(f"scrcpy could not start: {error}")

    def _poll_parallel_record(self):
        """Confirm the recorder produces data; fall back if it never does."""
        session = self._rec_session
        if session is None or self._rec_mode != "parallel":
            self._stop_rec_watch()
            return
        ran = time.time() - self._rec_started_t
        if not self._rec_confirmed:
            try:
                self._rec_confirmed = os.path.getsize(self._rec_path) > 0
            except (OSError, TypeError):
                self._rec_confirmed = False
            if self._rec_confirmed:
                self.log.emit("[OK] recording started; the live screen was not restarted")
        if not session.running:
            if not self._rec_confirmed or ran < self._PARALLEL_REC_GRACE:
                tail = _read_log_tail(session, 4096).strip().splitlines()[-1:]
                detail = f": {tail[0][:160]}" if tail else ""
                self._fallback_parallel_record(f"the recorder exited after {ran:.1f} s{detail}")
                return
            # It ran, then ended by itself (e.g. the device disconnected);
            # scrcpy closed the MP4 on the way out.
            self._stop_rec_watch()
            self._rec_session = None
            self._recording = False
            self._record_finalizing = True
            self.log.emit("[WARNING] the scrcpy recorder ended on its own; saving what it captured")
            self._on_parallel_record_done(True)
            return
        if not self._rec_confirmed and ran > self._PARALLEL_REC_DATA_TIMEOUT:
            self._fallback_parallel_record(f"no video data after {int(ran)} s")

    def _fallback_parallel_record(self, reason):
        """Switch a failed parallel recording to device-side screenrecord."""
        self._stop_rec_watch()
        session, self._rec_session = self._rec_session, None
        self._rec_parallel_starting = False
        path = self._rec_path
        if session is not None:
            self._discard_recorder(session, path)
        self._retire_rec_log()
        if not self._recording or self._closing:
            self._record_failed(f"the recording could not start ({reason})")
            return
        self.log.emit(
            f"[WARNING] parallel scrcpy recording failed ({reason}) — falling back to "
            "device-side recording (screenrecord); the live screen keeps running"
        )
        self._record_device(path, display_id=getattr(self, "_rec_display_id", None))
        self._refresh_buttons()

    def _discard_recorder(self, session, path=None):
        worker = _RecorderDiscardThread(session, path)
        park_thread(worker)
        worker.start()

    def _finish_parallel_record(self):
        """Stop the parallel recorder so it writes the MP4 index (off the UI thread)."""
        self._stop_rec_watch()
        session, self._rec_session = self._rec_session, None
        if session is None:
            self._record_failed("the recording session was no longer running")
            return
        self._rec_wait = _RecorderStopThread(session)
        park_thread(self._rec_wait)  # a closing tab must not cut the MP4 short
        self._rec_wait.done.connect(self._on_parallel_record_done)
        self._rec_wait.start()

    def _on_parallel_record_done(self, clean):
        self._rec_wait = None
        path = self._rec_path
        self._retire_rec_log()
        if path and os.path.isfile(path) and not _mp4_is_finalised(path):
            self.log.emit(
                "[WARNING] scrcpy did not finish the MP4 index; the recording "
                f"may not play → {path}"
            )
        elif not clean:
            self.log.emit("[WARNING] the scrcpy recorder did not exit cleanly")
        _purge_scrcpy_logs()
        self._record_finished([path])

    def _stop_rec_watch(self):
        timer, self._rec_watch = self._rec_watch, None
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _retire_rec_log(self):
        """Queue the parallel recorder's scrcpy log for deletion."""
        if self._rec_log_path:
            _STALE_SCRCPY_LOGS.append(self._rec_log_path)
            self._rec_log_path = None
        _purge_scrcpy_logs()

    def _record_device(self, path, display_id=None):
        """Record on the device (``screenrecord``) and pull it — works everywhere
        (remote adb server / RDP), auto-continuing past Android's ~3-min cap."""
        import threading

        self._rec_mode = "device"
        st = settings_mod.load()
        disp_id = display_id if display_id is not None else getattr(self, "_rec_display_id", None)
        if disp_id is None:
            disp_id = self.cmb_display.currentData()
        requested_bps = int(_bitrate_to_bps(st.get("scrcpy_bit_rate") or "16M"))
        viewing = self._scrcpy is not None
        if viewing and requested_bps > self._RECORDER_VIEWING_MAX_BPS:
            bits = str(self._RECORDER_VIEWING_MAX_BPS)
            br = "8M (performance while viewing)"
            self.log.emit(
                "[INFO] recording bitrate limited to 8M while the display is open "
                "to keep mirroring and typing responsive"
            )
        else:
            bits = str(requested_bps)
            br = st.get("scrcpy_bit_rate") or "16M"
        size = _record_size_for_active_view(self._displays, disp_id) if viewing else None
        if size:
            self.log.emit(
                f"[INFO] recording size limited to {size} while the display is open "
                "to reduce encoder contention"
            )
        disp_str = f" Display {disp_id}" if disp_id is not None else ""
        unique = str(self._selected_display_entry(disp_id).get("unique_id") or "")
        if unique.startswith("virtual:"):
            # screenrecord --display-id takes a physical display id only
            self._record_failed(
                f"Display {disp_id} is a virtual display, and Android's screenrecord "
                "records only physical displays. Show it with the scrcpy renderer "
                "and record while that screen runs."
            )
            return
        self._rec_stop = threading.Event()
        self._rec_thread = _RecordThread(
            self.handler,
            path,
            self._rec_stop,
            bit_rate=bits,
            size=size,
            display_id=disp_id,
        )
        park_thread(self._rec_thread)  # outlives the panel while the final pull runs
        self._rec_thread.done.connect(self._record_finished)
        self._rec_thread.fail.connect(self._record_failed)
        self._rec_thread.part.connect(self._record_part)
        self._rec_thread.start()
        self._set_record_status(f"● Recording{disp_str} the device screen → {path}")
        self.log.emit(
            f"[OK] recording{disp_str} at {br} (device-side; auto-continues past "
            f"the ~3-min Android cap) → {path}"
        )

    def _stop_record(self, *, restore_view=True):
        if not self._recording:
            return
        self._recording = False
        self._record_finalizing = True
        self._rec_restore_after_finish = bool(restore_view)
        self._refresh_buttons()
        self.log.emit("[INFO] Recording stopped — saving the finished MP4 to your PC in the background…")
        self._set_record_status("Recording stopped. Saving the MP4 to your PC…")
        if self._rec_mode == "parallel":
            # A recorder still launching is finished once the worker reports.
            if not self._rec_parallel_starting:
                self._finish_parallel_record()
            if not self._closing:
                # Stop is a direct toolbar click: keep typing on the live screen.
                self._restore_mirror_focus(direct=True)
            return
        if self._rec_mode == "integrated":
            self._rec_integrated_starting = False
            self._queued_start = None  # never open a second, normal mirror
            window_handle = self._release_viewer()
            session, self._scrcpy = self._scrcpy, None
            if session is None:
                if self._launch_pending:
                    self._cancel_launch = True
                    self._queued_start = None
                    self._record_finalizing = False
                    self._rec_integrated_starting = False
                    self._rec_mode = None
                    self._set_record_status("Recording start cancelled.")
                    self._refresh_buttons()
                    return
                self._record_failed("the recording session was no longer running")
                return
            # _RecWaitThread posts WM_CLOSE and waits for scrcpy to write the
            # MP4 index; unlike process termination, this preserves the file.
            self._rec_wait = _RecWaitThread(
                session,
                window_title=self._win_title,
                window_handle=window_handle,
            )
            park_thread(self._rec_wait)  # a closing tab must not cut the MP4 short
            self._rec_wait.done.connect(self._on_integrated_record_done)
            self._rec_wait.start()
            return
        if self._rec_stop is not None:
            self._rec_stop.set()  # tell screenrecord to stop & pull
        # Stop is another direct toolbar action, so preserve native scrcpy
        # keyboard input before the asynchronous final pull completes.
        self._restore_mirror_focus(direct=True)

    def _on_integrated_record_done(self, clean):
        restore = self._rec_restore_spec if self._rec_restore_after_finish else None
        self._rec_restore_spec = None
        self._rec_wait = None
        self._record_finalizing = False
        self._rec_mode = None
        if not clean:
            self.log.emit(
                "[WARNING] scrcpy reported an unclean close; verifying the saved MP4"
            )
        self._record_finished([self._rec_path])
        self._rec_restore_after_finish = False
        _purge_scrcpy_logs()
        if restore and not self._closing:
            QTimer.singleShot(0, lambda spec=dict(restore): self.start(**spec))

    def _record_part(self, n):
        self._set_record_status(
            f"● Recording — part {n} (Android caps each clip at "
            f"~3 min; the previous part was saved)…"
        )
        self.log.emit(f"[INFO] 3-min cap reached — continuing in part {n}")

    def _reset_record_state(self):
        self._recording = False
        self._record_finalizing = False
        self._rec_integrated_starting = False
        self._rec_parallel_starting = False
        self._rec_confirmed = False
        self._stop_rec_watch()
        self._rec_mode = None
        self._rec_stop = None
        thread, self._rec_thread = self._rec_thread, None
        park_thread(thread)
        self._refresh_buttons()

    def _tile_notice(self, text) -> bool:
        """Report *text* on this panel's own status line instead of in a dialog.

        A locked tile is one of several screens on the displays tab; a modal
        dialog each would stack up in front of the running start chain. Returns
        whether the notice was handled here.
        """
        if not self._fixed_display:
            return False
        self.status.show()
        self.status.setText(text)
        return True

    def _record_finished(self, parts):
        self._reset_record_state()
        # The save is done: "Recording stopped. Saving the MP4…" must not stay
        # under a live screen. The result is reported by the saved dialog (or,
        # on a display tile, as its own status line below).
        self._clear_record_status()
        parts = [p for p in (parts or []) if p and os.path.exists(p) and os.path.getsize(p) > 0]
        if not parts:
            bad = self._rec_path or ""
            self.log.emit(f"[ERROR] recording file is empty → {bad}")
            if self._closing:
                return
            if self._tile_notice("The recording saved no data — this display may block it."):
                return
            QMessageBox.warning(
                self,
                "Recording",
                f"The recording didn't save any data:\n{bad}\n\n"
                "Some head units block screenrecord (secure "
                "surface). Try a screenshot instead.",
            )
            return
        if len(parts) == 1:
            self.log.emit(f"[OK] recording saved → {parts[0]}")
            if not self._closing and not self._tile_notice(
                f"Recording saved → {os.path.basename(parts[0])}"
            ):
                saved_dialog(self, parts[0], "recording")
        else:
            names = ", ".join(os.path.basename(p) for p in parts)
            self.log.emit(
                f"[OK] long recording saved in {len(parts)} parts (Android's 3-min cap): {names}"
            )
            if not self._closing and not self._tile_notice(
                f"Recording saved in {len(parts)} parts → {names}"
            ):
                saved_dialog(self, parts[0], f"recording ({len(parts)} parts)")
        if not self._closing:
            QTimer.singleShot(0, self._restore_mirror_focus)

    def _record_failed(self, msg):
        self._reset_record_state()
        self._clear_record_status()
        self.log.emit(f"[ERROR] recording: {msg}")
        if self._closing:
            return
        if not self._tile_notice(f"Recording failed: {msg}"):
            QMessageBox.warning(self, "Recording", f"Recording failed:\n{msg}")
        QTimer.singleShot(0, self._restore_mirror_focus)

    # ----- screenshot -----
    def _take_screenshot(self, display_id=None):
        # The IVI wall and display manager call this directly, bypassing the
        # disabled toolbar button; one capture at a time.
        if thread_running(self._shot):
            self.log.emit("[INFO] A screenshot is already being captured.")
            return
        if display_id is None:
            display_id = self.cmb_display.currentData()
        disp_tag = f"-disp{display_id}" if display_id is not None else ""
        default = os.path.join(download_dir(), time.strftime(f"screenshot{disp_tag}-%Y%m%d-%H%M%S.png"))
        path, _sel = QFileDialog.getSaveFileName(
            self, "Save a screenshot to…", default, "PNG image (*.png)"
        )
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        self.btn_shot.setEnabled(False)
        disp_lbl = f" Display {display_id}" if display_id is not None else ""
        self.log.emit(f"[INFO] Capturing screenshot{disp_lbl}…")
        handler = self.handler
        self._shot_path = path
        self._shot = FunctionThread(
            lambda: handler.screenshot(path, display_id=display_id, safe=True)
        )
        park_thread(self._shot)  # ``done`` arrives before run() returns
        self._shot.done.connect(self._on_shot_result)
        self._shot.fail.connect(self._shot_fail)
        self._shot.start()

    def _on_shot_result(self, result):
        error = _command_error(result, "the device returned no screenshot")
        if error:
            self._shot_fail(error)
        else:
            self._shot_done(self._shot_path)

    def _shot_done(self, path):
        self._shot = None
        self.btn_shot.setEnabled(True)
        self.log.emit(f"[OK] screenshot saved → {path}")
        if not self._closing:
            saved_dialog(self, path, "screenshot")

    def _shot_fail(self, msg):
        self._shot = None
        self.btn_shot.setEnabled(True)
        self.log.emit(f"[ERROR] screenshot: {msg}")
        if self._closing or self._tile_notice(f"Couldn't capture a screenshot: {msg}"):
            return
        QMessageBox.warning(self, "Screenshot", f"Couldn't capture a screenshot.\n\n{msg}")

    # ----- start / stop -----
    def start(
        self,
        display_id="__use_combo__",
        compat=None,
        embed=None,
        record=None,
        record_format=None,
        camera=None,
        _retry=False,
    ):
        if self._closing:  # tab closed during a retry delay
            return
        if not _retry:
            self._cancel_retry()
            self._retry_count = 0  # a fresh manual start
        if display_id == "__use_combo__":
            display_id = self.cmb_display.currentData()
        # The camera is its own video source — it has no display id and scrcpy
        # disables control for it, so a plain separate window is the safe choice.
        if camera:
            display_id = None
            embed = False
        # The caller's own choice (None = "use the Options setting"). It is what
        # a queued or retried start repeats: the resolved option must not come
        # back as an explicit "separate window" request.
        embed_request = None if embed is None else bool(embed)
        # Settings → Screen renderer (or this panel's Options) may pick ADB
        # screencap frames instead of scrcpy for an in-tab screen.
        use_screencap = self._uses_screencap(embed_request, camera, record)
        if compat is None:
            # Automotive/IVI devices need the conservative H.264/forward-tunnel
            # profile by default.  Respect an explicit user change, but do not
            # make an early click race the slower identity probe that discovers
            # the device is automotive.
            compat = self._opt["compat"] or (
                self.automotive and not self._compat_user_changed
            )
        if embed is None:
            embed = self._opt["embed"]
        elif embed:
            self._set_option("embed", True)
        # Over Remote Desktop keep the compat profile, but HONOUR the embed
        # choice — it used to be silently disabled here, which is why "embed"
        # appeared to do nothing at all on RDP setups.
        if self._opt["soft"]:
            compat = True
            if embed and not use_screencap:
                self.log.emit(
                    "[INFO] embedding over Remote Desktop — if the "
                    "picture stays black, untick “Embed window in "
                    "this tab” and mirror in a separate window"
                )

        requested = {
            "display_id": display_id,
            "compat": compat,
            "embed": embed_request,
            "record": record,
            "record_format": record_format,
            "camera": camera,
        }
        # A parallel recorder finishes on its own; it never blocks the screen.
        if self._record_finalizing and self._rec_mode != "parallel":
            self.status.show()
            self.status.setText("Wait for the recording to finish saving before restarting the screen.")
            return
        # Starting is a replace operation. Queue exactly one requested launch,
        # cancel an in-flight launch, and wait for the previous process to close
        # before creating another. This removes the overlapping scrcpy processes
        # behind duplicate windows and duplicate recordings.
        if self._launch_pending:
            if not _retry:
                self._queued_start = requested
                self._cancel_launch = True
                self.status.show()
                self.status.setText("Switching screen mode…")
                self._refresh_buttons()
            return
        if self._stopping_mirror:
            self._queued_start = requested
            self.status.show()
            self.status.setText("Stopping the current display before restarting…")
            self._refresh_buttons()
            return
        if record is None and self._integrated_recording_blocks(
            "Stop the current recording before changing the screen mode."
        ):
            return
        if self._scrcpy is not None:
            self._queued_start = requested
            self.stop()
            return
        if use_screencap:
            self._start_screencap(display_id, requested)
            return
        self._stop_screencap()  # a scrcpy start replaces the screencap view

        st = settings_mod.load()
        opts = ScrcpyOptions(
            max_size=(st.get("scrcpy_max_size") or None),
            bit_rate=st.get("scrcpy_bit_rate") or None,
            video_codec=(st.get("scrcpy_video_codec") or None),
            stay_awake=st.get("scrcpy_stay_awake", True),
            turn_screen_off=st.get("scrcpy_turn_screen_off", False),
            record=record,
            record_format=record_format,
            display_id=display_id,
            video_source=("camera" if camera else None),
            camera_facing=(camera or None),
            keyboard_mode=self._kb_mode(),
            window_title=(self.session_dict or {}).get("name") or "turboadb",
        )
        # Camera mirroring is a video-source operation; screen mirroring and
        # recordings use the complete user-selected audio profile.
        self._apply_audio_options(opts, allow_audio=not bool(camera) and not self._fixed_display)
        self._tune(opts)
        self._apply_video_budget(opts)
        self._launch_opts = opts  # a parallel recorder copies its video settings
        if camera:
            self.status.setText(f"Camera ({camera}) — live view")
        if opts.render_driver == "software":
            self.log.emit(
                "[INFO] using software rendering (Remote Desktop / "
                "GPU-less) — capped to keep it smooth"
            )
        if opts.keyboard_mode == "uhid":
            self.log.emit(
                "[WARNING] keyboard = UHID (explicitly selected). Most "
                "IVI/head-unit kernels have NO uhid support — if typing "
                "does nothing, switch Options → Keyboard mode back to "
                "Standard (SDK), which is what plain scrcpy uses."
            )

        do_embed = embed and _IS_WIN
        if do_embed:
            # A per-launch nonce (as the recorder's title carries) makes the
            # title unique: a quick Stop/Start — or a new tile that reuses a
            # freed panel address — can never adopt the previous, dying window.
            opts.window_title = (
                f"turboadb-embed-{os.getpid()}-{id(self):x}-{_next_window_nonce()}"
            )
            opts.window_borderless = True
            # Launch it OFF-SCREEN, not at (0,0). scrcpy renders its first frame
            # into this hidden window; we then adopt it into the container once it
            # has settled. That's what stops the mirror from flashing up as a
            # separate top-level window for a moment before it embeds.
            opts.window_x, opts.window_y = -32000, -32000
            # Embedded, scrcpy's SDL window never gets SDL keyboard focus (SDL
            # derives it from GetForegroundWindow, which is always our
            # top-level window), and SDL drops text events without it. The
            # default SDK keyboard sends digits and punctuation as text, so
            # send every key as a key event. UHID/AOA only use key events, and
            # scrcpy refuses the flag without an SDK keyboard (--keyboard=uhid,
            # or --no-control, which disables the keyboard).
            if opts.keyboard_mode in (None, "sdk") and not camera and not opts.no_control:
                extra = list(opts.extra_args or [])
                if not {"--raw-key-events", "--prefer-text"} & set(extra):
                    extra.append("--raw-key-events")
                opts.extra_args = extra
        else:
            # A readable, app-owned title gives a native window an independent
            # identity and lets the watchdog find it reliably.  It is distinct
            # from the embedded off-screen title above.
            name = (self.session_dict or {}).get("name") or "Android device"
            serial = getattr(self.handler, "serial", None) or "device"
            opts.window_title = f"TurboADB — {name} — {serial} — Screen [{id(self):x}]"
        # the EXACT window title we can later find to confirm scrcpy really put a
        # window up (readiness) and, when embedding, to reparent it
        self._win_title = opts.window_title

        import tempfile

        self._retire_log()
        self._log_path = os.path.join(
            tempfile.gettempdir(),
            f"turboadb-scrcpy-{os.getpid()}-{id(self):x}-{time.monotonic_ns()}.log",
        )
        self._compat = compat
        self._embed_on = do_embed
        self._separate_hwnd = None
        self._launch_spec = requested
        self._launch_pending = True
        self._cancel_launch = False
        self.status.setText("Launching scrcpy…")
        self._launch_thread = _MirrorLaunchThread(self.handler, opts, compat, self._log_path)
        # Parked: the previous worker may still be returning when a queued start
        # replaces this reference.  Bound methods (not lambdas) are disconnected
        # by Qt if the panel is deleted before the result arrives.
        park_thread(self._launch_thread)
        self._launch_thread.done.connect(self._on_mirror_launched)
        self._launch_thread.fail.connect(self._on_mirror_launch_failed)
        self._launch_thread.start()
        self._refresh_buttons()

    def _retire_log(self):
        """Queue the previous session's scrcpy log for deletion."""
        if self._log_path:
            _STALE_SCRCPY_LOGS.append(self._log_path)
            self._log_path = None
        _purge_scrcpy_logs()

    def _clear_cancelled_record_start(self):
        """Reset an integrated recording whose scrcpy launch was cancelled.

        A queued start still carries the recording (Record pressed while a
        mirror was launching), so only a cancel with nothing queued ends it.
        Without this the Record button stayed disabled on "Starting recording…".
        """
        if not self._rec_integrated_starting or self._queued_start is not None:
            return
        self._recording = False
        self._record_finalizing = False
        self._rec_integrated_starting = False
        self._rec_mode = None
        self._rec_restore_spec = None
        self.log.emit("[INFO] Recording start cancelled.")

    def _on_mirror_launched(self, sess, do_embed=None):
        if do_embed is None:
            do_embed = self._embed_on  # set by start() for this launch
        self._launch_pending = False
        if sess is None:
            self._on_mirror_launch_failed("No scrcpy session created")
            return
        if self._cancel_launch:
            self._cancel_launch = False
            self._clear_cancelled_record_start()
            self.status.setText("Closing the cancelled screen start…")
            self._stop_scrcpy_async(sess, self._win_title)
            self._refresh_buttons()
            return
        self._scrcpy = sess
        if self._rec_mode == "integrated" and self._recording:
            self._rec_integrated_starting = False
        self._start_t = time.time()
        self._became_ready = False  # set once it renders / embeds
        self._refresh_buttons()
        # watch for the scrcpy window being closed so the buttons reset
        self._mon = QTimer(self)
        self._mon.timeout.connect(self._check_alive)
        self._mon.start(self._poll_interval)
        if do_embed:
            self.status.setText("Starting mirror… embedding the scrcpy window.")
            self._embed_tries = 0
            self._win_first_seen = 0.0
            self._embed_timer = QTimer(self)
            self._embed_timer.timeout.connect(self._try_embed)
            self._embed_timer.start(200)
            self.log.emit("[OK] scrcpy launching (embedded)…")
        else:
            self.status.setText(
                "Mirroring in a separate window — click it and "
                "type / use the mouse directly (native scrcpy)."
            )
            self.log.emit(
                "[OK] scrcpy launched in a separate window — keyboard "
                "and mouse work DIRECTLY in that window, exactly like "
                "plain scrcpy"
            )

    def _on_mirror_launch_failed(self, error):
        queued = getattr(self, "_queued_start", None)
        cancelled = self._cancel_launch
        was_record_launch = bool((self._launch_spec or {}).get("record"))
        self._launch_pending = False
        self._cancel_launch = False
        if cancelled:
            self._clear_cancelled_record_start()
            self.status.setText("Screen start cancelled.")
            self._refresh_buttons()
            if queued:
                self._start_queued_after_cancel()
            return
        if was_record_launch and self._rec_mode == "integrated":
            self._record_failed(f"scrcpy recording could not start: {error}")
            return
        self._refresh_buttons()
        self._retry_or_show_scrcpy_failure(error)

    # markers that prove scrcpy actually brought up VIDEO (so a later exit is a
    # normal close, NOT a failed start). Deliberately NOT "Device:" / "New
    # display" — scrcpy prints those the instant it CONNECTS, before it opens a
    # decoder, so a connect-then-die failure used to look "started" and was left
    # alone (the "fake started" bug). Only a renderer/texture/recording line
    # means a frame actually flowed.
    _HEALTHY = (
        "Renderer:",
        "Texture:",
        "Recording started",
        "INFO: Renderer",
        "Frame: ",
        "v4l2",
        "audio player",
    )
    # Scrcpy itself is spawned immediately.  A first device-side server push
    # (especially after Android reboot) can legitimately take more than six
    # seconds, so do not kill a healthy launch before it can create its window.
    _STARTUP_TIMEOUT = 25.0
    _STARTUP_GRACE = 5.0  # a mirror that dies this fast = failed start
    _EMBED_SETTLE = 0.6  # let the window render its 1st frame before we adopt it

    def _scrcpy_window_up(self) -> bool:
        """True once scrcpy has actually put its window up — the reliable
        readiness signal for BOTH embed and separate-window modes. (The log is
        block-buffered to a file, so its 'Renderer:' markers don't appear until
        scrcpy exits; the WINDOW existing is what proves a live start.)"""
        if self._child_hwnd is not None:
            return True
        if not _IS_WIN:
            # no cheap window probe off Windows — fall back to a time grace
            return (time.time() - getattr(self, "_start_t", 0)) > self._STARTUP_TIMEOUT
        title = getattr(self, "_win_title", None)
        if not title:
            return False
        try:
            hwnd = _find_window(title)
            if hwnd and not self._embed_on:
                self._separate_hwnd = hwnd
            # SDL's hidden placeholder window is not a started mirror yet.
            return _window_ready(hwnd)
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
        # readiness = the window is up (fast, while running) OR a video marker is
        # in the log (a fallback that lands at exit, when the buffered log flushes
        # — covers scrcpy builds whose window title we can't match).  Neither
        # probe runs once the session is ready, and only the log tail is read.
        # Focus is deliberately left alone here: moving it from a timer would
        # steal typing from wherever the user is working meanwhile.
        if not self._became_ready and (
            self._scrcpy_window_up()
            or any(m in _read_log_tail(self._scrcpy) for m in self._HEALTHY)
        ):
            self._became_ready = True
            self.screen_ready.emit()

        running = self._scrcpy.running
        ran_s = time.time() - getattr(self, "_start_t", 0)
        # startup watchdog: alive but no window yet = it hung (the first-launch
        # server-push race can leave scrcpy waiting forever) — kill it so the
        # retry path runs instead of the user staring at nothing
        if running and not self._became_ready and ran_s > self._STARTUP_TIMEOUT:
            action = (
                "failing the recording start"
                if self._rec_mode == "integrated"
                else "retrying"
            )
            self.log.emit(
                f"[WARNING] scrcpy didn't show a window within "
                f"{int(self._STARTUP_TIMEOUT)}s — {action}."
            )
            session, self._scrcpy = self._scrcpy, None
            err = (session.read_log() or "").strip()
            window_handle = self._release_viewer()
            self._stop_scrcpy_async(session, self._win_title, window_handle)
            if self._rec_mode == "integrated" and self._recording:
                self._record_failed(err or "scrcpy timed out before recording became ready")
            else:
                self._retry_or_show_scrcpy_failure(err or "scrcpy startup timed out")
            self._refresh_buttons()
            return
        if running:
            return

        err = (self._scrcpy.read_log() or "").strip()
        # A mirror that DIED within a few seconds of starting is a failed start,
        # even if it briefly showed a window — this is the "embedded, then ended
        # 2s later, then works on the 2nd try" case. An EMBEDDED borderless
        # window can only be closed via our Stop button (which sets _scrcpy=None
        # and never reaches here), so any quick unexpected exit is a crash to
        # retry. (A separate window the user can close whenever, so we respect
        # its ready state.)
        early_death = ran_s < self._STARTUP_GRACE
        ready = self._became_ready and not (self._embed_on and early_death)
        was_integrated_recording = self._rec_mode == "integrated" and self._recording
        self._scrcpy = None
        self._release_viewer()
        self._refresh_buttons()
        self.status.show()

        if was_integrated_recording:
            if ready and self._rec_path and os.path.isfile(self._rec_path):
                self._record_finished([self._rec_path])
            else:
                detail = err or "scrcpy ended before the recording became ready"
                self._record_failed(detail)
            return

        if ready:
            self._retry_count = 0
            self.status.setText("Screen ended. Click “▶ Start” to view it again.")
            self.log.emit("[OK] mirror ended")
            return

        self._retry_or_show_scrcpy_failure(err)

    def _retry_or_show_scrcpy_failure(self, error):
        """Recover immediate launch errors and early process exits uniformly.

        Previously only a process that had already spawned used the retry
        ladder; an ADB/server race reported directly by the launch worker made
        the user press Start again manually.  Both cases now get the same
        automatic warm-up and IVI-compatible fallback.
        """
        tail = " ".join(str(error or "").splitlines()[-2:])[:200] or "no output from scrcpy"
        lowered = str(error or "").lower()
        if "opengl" in lowered or "render" in lowered:
            # A PC without a working OpenGL driver: retry with scrcpy's default.
            self._avoid_opengl = True
        tries = getattr(self, "_retry_count", 0)
        if tries == 0:
            self._retry_count = 1
            self.log.emit(f"[WARNING] scrcpy didn't start ({tail}); retrying…")
            self.status.setText("scrcpy is waiting for ADB — retrying…")
            self._schedule_retry(900, self._retry_start)
            return
        if tries == 1:
            self._retry_count = 2
            # The last rung also drops embedding — but never for a locked tile:
            # the displays tab would put one free-floating scrcpy window per
            # failing display outside TurboADB.  Keep the caller's choice there.
            overrides = {"compat": True}
            if not self._fixed_display:
                overrides["embed"] = False
            self.log.emit(
                "[WARNING] scrcpy still didn't start; retrying with IVI-compatible video…"
            )
            self.status.setText("Retrying with IVI-compatible video…")
            self._schedule_retry(900, lambda spec=overrides: self._retry_start(**spec))
            return
        self._retry_count = 0
        self.status.setText("Mirror couldn't start — see the log below.")
        self.log.emit("[ERROR] scrcpy could not start after 3 attempts. Last output: " + tail)
        self._refresh_buttons()
        self.screen_failed.emit(tail)
        self._show_scrcpy_log(
            str(error)
            or "(scrcpy exited immediately with no output. "
            "It likely couldn't reach the device, or couldn't "
            "open a video encoder for this display.)"
        )

    def _show_scrcpy_log(self, err):
        """Offer scrcpy's full output so a failure (esp. over RDP / on an IVI)
        is diagnosable instead of a one-line 'failed'.

        A locked tile only says so on its status line, with a link to the log:
        several failing displays would otherwise stack modal dialogs in front
        of the tab's still-running start chain.  Everywhere else the dialog is
        opened from the event loop rather than here, because this is reached
        from the ``_check_alive`` timer slot and a modal ``exec_()`` inside a
        timer slot re-enters it.
        """
        if self._closing:
            return
        self._scrcpy_error = str(err or "")
        if self._fixed_display:
            self._tile_notice(
                "This display's screen couldn't start. "
                "<a href='#scrcpy-log'>Show log</a>"
            )
            return
        QTimer.singleShot(0, lambda text=self._scrcpy_error: self._open_scrcpy_log(text))

    def _on_status_link(self, href):
        """The status line's links: "Show log" (a locked tile's failure notice)
        and "Retry" (a screencap stream that stopped)."""
        if href == "#screencap-retry" and not self._closing:
            spec = dict(self._launch_spec or {})
            QTimer.singleShot(
                0, lambda: self.start(display_id=spec.get("display_id", "__use_combo__"))
            )
            return
        if href == "#scrcpy-log" and not self._closing:
            QTimer.singleShot(0, lambda: self._open_scrcpy_log(self._scrcpy_error))

    def _open_scrcpy_log(self, err):
        """The modal log window itself. Never call this from a timer slot that
        Qt is still dispatching — go through :meth:`_show_scrcpy_log`."""
        from PyQt5.QtWidgets import QPlainTextEdit, QDialogButtonBox
        from PyQt5.QtGui import QFont

        if self._closing:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("scrcpy couldn't start")
        dlg.resize(720, 460)
        v = QVBoxLayout(dlg)
        low = err.lower()
        is_remote = bool(
            getattr(self.handler, "config", None) and self.handler.config.adb_server_host
        )
        from ..scrcpy import TUNNEL_PORT, TUNNEL_PORT_LAST

        if is_remote:
            hint = (
                "Mirroring a device on a REMOTE adb server is the hard case: "
                "scrcpy has to stream video over the network, through a tunnel "
                f"ports (TCP {TUNNEL_PORT}-{TUNNEL_PORT_LAST}) on the remote PC.\n\n"
                "To make it work, on the REMOTE PC "
                f"({self.handler.config.adb_server_host}):\n"
                f"  1. Allow TCP {TUNNEL_PORT}-{TUNNEL_PORT_LAST} AND 5037 "
                "through its firewall.\n"
                "  2. Make sure its adb server was started shared "
                "(TurboADB → Remote → “Start shared server on THIS PC”, or "
                "`turboadb serve`).\n\n"
                "MOST RELIABLE: run TurboADB ON that PC (e.g. via RDP) and "
                "connect to the device locally (USB/Network) — then there's no "
                "network video tunnel at all. Mirroring a remote device's screen "
                "across the network is inherently fragile."
            )
        elif (
            "start-server" in low
            or "server connection" in low
            or "no host" in low
            or "could not start adb" in low
        ):
            hint = (
                "scrcpy couldn't reach the adb server. Try the Devices ▾ menu "
                "→ “Restart ADB server”, then mirror again. The exact command "
                "and server address are at the top of the log below."
            )
        else:
            hint = (
                "Tips: tick “compat (IVI)” and “software render (RDP)”, use a "
                "separate window (untick embed), and try a specific display. "
                "The exact scrcpy command is at the top of the log below."
            )
        lab = QLabel(hint)
        lab.setWordWrap(True)
        v.addWidget(lab)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 9))
        view.setPlainText(err or "(scrcpy produced no output)")
        v.addWidget(view, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept)
        v.addWidget(bb)
        dlg.exec_()
        dlg.deleteLater()

    def _try_embed(self):
        self._embed_tries += 1
        if self._scrcpy is None or not self._scrcpy.running:
            self._stop_embed_timer()
            self.status.setText(
                "scrcpy exited before it could be embedded. "
                "Try compat mode, or “Open in window”. Over Remote "
                "Desktop, scrcpy may need a local GPU."
            )
            # The monitor owns process-exit classification and retry. Clearing
            # the session here used to strand a failed off-screen launch and
            # bypass the recovery path entirely.
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
            # Never adopt SDL's hidden placeholder: scrcpy only shows its window
            # once the renderer has a first frame, and re-parenting before that
            # made it exit ~2 s later (the "embedded then ended, works on the
            # 2nd try" bug — every cold adb start). After it is shown, wait a
            # short beat so the first frame is on screen, then adopt it.
            if not _window_ready(hwnd):
                return
            now = time.time()
            if self._win_first_seen == 0.0:
                self._win_first_seen = now
                return
            if now - self._win_first_seen < self._EMBED_SETTLE:
                return
            self._stop_embed_timer()
            aspect = _window_aspect(hwnd)
            if aspect and abs(aspect - (self._video_aspect or 0.0)) > 0.01:
                # scrcpy sized its window to the video: the real (rotated) shape
                self._video_aspect = aspect
                self.display_shape_changed.emit()
            parent_hwnd = int(self.container.winId())
            try:
                _reparent(hwnd, parent_hwnd, self.container.width(), self.container.height())
            except Exception as exc:
                # Embedding failed after an off-screen launch.  Pass the handle
                # explicitly (a half-attached child is no longer found by title)
                # and treat the session as a separate window from now on, so
                # _fit never moves a top-level window around.
                rescued = self._rescue_offscreen(hwnd)
                self._embed_on = False
                self._separate_hwnd = hwnd
                self._refresh_buttons()
                # never leave "embedding the scrcpy window…" on screen
                self.status.show()
                self.status.setText(
                    "Couldn't embed scrcpy — it's running in its own window instead."
                    if rescued
                    else "Couldn't embed scrcpy; use ■ Stop display, then ↗ Open separate window."
                )
                self.log.emit(
                    f"[WARNING] could not embed scrcpy: {exc} — "
                    + (
                        "showing it in its own window instead."
                        if rescued
                        else "it could not be moved on screen; use ■ Stop display, "
                        "then ↗ Open separate window."
                    )
                )
                return
            self._child_hwnd = hwnd
            self._embed_parent_hwnd = parent_hwnd
            self.status.setText("")
            self.status.hide()
            self._refresh_buttons()
            self.log.emit(
                "[OK] scrcpy embedded — click the device screen, then type: "
                "your keys are sent to the device."
            )
            # scrcpy/SDL resizes its own window to the video frame after the
            # first frame — keep forcing it to fill the container for a few
            # seconds so it doesn't shrink back to a tiny window.
            self._fit_timer = QTimer(self)
            self._fit_timer.timeout.connect(self._fit)
            self._fit_timer.start(self._poll_interval)  # keeps it filling the whole session
            self._fit()
            return
        # Keep looking for at least as long as the process startup watchdog.
        # The old 10-second cutoff preceded the 25-second launch timeout and
        # could leave a legitimate slow start permanently at (-32000,-32000).
        if self._embed_tries > int((self._STARTUP_TIMEOUT + 2.0) / 0.2):
            # we launched scrcpy off-screen for a seamless embed but never managed
            # to adopt its window — move it on-screen so it isn't stuck invisible.
            # Stop polling either way: FindWindow + EnumWindows every 200 ms for
            # the rest of the session cannot succeed any more.
            self._stop_embed_timer()
            if self._rescue_offscreen():
                self.status.setText(
                    "Couldn't embed the scrcpy window; it's running in its own window instead."
                )
            else:
                self.status.setText("Couldn't find the scrcpy window to embed.")

    def _rescue_offscreen(self, hwnd=None):
        """We launch scrcpy off-screen so it can render its first frame before we
        embed it (no flash). If embedding then fails, the window would be stranded
        at (-32000,-32000) — invisible. Detach it, give it a normal frame and move
        it on-screen so it's usable as a plain scrcpy window."""
        if not _IS_WIN:
            return False
        try:
            hwnd = hwnd or self._child_hwnd or _find_window(self._win_title)
        except Exception:
            hwnd = None
        if not hwnd:
            return False
        try:
            _ctypes, u = _win_api()
            u.SetParent(hwnd, None)  # back to a real top-level window
            if u.GetParent(hwnd):
                raise OSError("Windows kept scrcpy attached to the embed container")
            getter = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
            setter = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
            style = getter(hwnd, _GWL_STYLE)
            # give it a title bar + resize frame (it was launched borderless) so
            # the user can move/resize/close it like any window
            style = (style | _WS_POPUP | _WS_CAPTION | _WS_THICKFRAME) & ~_WS_CHILD
            setter(hwnd, _GWL_STYLE, style)
            w = max(720, self.container.width() or 0)
            h = max(520, self.container.height() or 0)
            if not u.SetWindowPos(
                hwnd, None, 120, 90, w, h, _SWP_NOZORDER | _SWP_FRAMECHANGED
            ):
                raise OSError("Windows could not move scrcpy back on screen")
            u.ShowWindow(hwnd, 5)  # SW_SHOW
            try:
                u.SetForegroundWindow(hwnd)
            except Exception:
                pass
            rescued = True
        except Exception:
            rescued = False
        if rescued:
            # it's a normal separate window now: forget the embed handle and clear
            # the embed flag so a later close is a plain close (not a failed-embed
            # retry that would relaunch off-screen and loop)
            self._child_hwnd = None
            self._embed_parent_hwnd = None
            self._separate_hwnd = hwnd
            self._embed_on = False
            self._refresh_buttons()
        return rescued

    def _fit(self):
        """Force the embedded scrcpy window to fill the container (scrcpy/SDL
        keeps resizing itself to the video frame, so we keep correcting it).
        Skips the call when the window ALREADY fills the container — the old
        unconditional MoveWindow every 500 ms made SDL re-handle a resize twice
        a second forever, a steady source of embedded-mirror stutter."""
        if not self._child_hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes

            _c, u = _win_api()
            target_rect = wintypes.RECT()
            c_hwnd = int(self.container.winId()) if hasattr(self.container, "winId") else None
            if c_hwnd and u.GetClientRect(c_hwnd, ctypes.byref(target_rect)):
                w = max(1, target_rect.right - target_rect.left)
                h = max(1, target_rect.bottom - target_rect.top)
            else:
                dpr = float(getattr(self, "devicePixelRatioF", lambda: 1.0)() or 1.0)
                w = max(1, int(round(self.container.width() * dpr)))
                h = max(1, int(round(self.container.height() * dpr)))

            rect = wintypes.RECT()
            if u.GetWindowRect(self._child_hwnd, ctypes.byref(rect)):
                curr_w = rect.right - rect.left
                curr_h = rect.bottom - rect.top
                if abs(curr_w - w) <= 1 and abs(curr_h - h) <= 1:
                    return  # already the right size
            u.MoveWindow(self._child_hwnd, 0, 0, w, h, True)
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _stop_embed_timer(self):
        # Each launch creates fresh timers parented to the panel; delete them
        # so they don't accumulate for the life of the tab.
        for name in ("_embed_timer", "_fit_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()
                timer.deleteLater()
                setattr(self, name, None)

    def _release_viewer(self):
        """Stop the embed/fit/monitor timers and forget the scrcpy window handles.

        Returns the handle to close: the embedded child or the separate window.
        """
        self._stop_embed_timer()
        if self._mon is not None:
            self._mon.stop()
            self._mon.deleteLater()
            self._mon = None
        # scrcpy's window is closing (or already gone): give the keyboard back
        # to Qt while the child handle is still known.
        self._release_native_keyboard(if_lost=True)
        self._keyboard_route = None
        handle = self._child_hwnd or self._separate_hwnd
        self._child_hwnd = None
        self._embed_parent_hwnd = None
        self._separate_hwnd = None
        return handle

    def _retry_start(self, **overrides):
        """Retry the same requested screen mode, with optional safe overrides."""
        spec = dict(getattr(self, "_launch_spec", {}) or {})
        spec.update(overrides)
        spec["_retry"] = True
        self.start(**spec)

    def _schedule_retry(self, delay_ms, callback):
        """Schedule one cancellable retry without leaving a ghost start behind."""
        self._retry_generation += 1
        generation = self._retry_generation
        self._retry_pending = True
        self._refresh_buttons()

        def fire():
            if self._closing or generation != self._retry_generation:
                return
            self._retry_pending = False
            self._refresh_buttons()
            callback()

        QTimer.singleShot(delay_ms, fire)

    def _cancel_retry(self):
        self._retry_generation += 1
        self._retry_pending = False

    def _start_queued_after_cancel(self):
        spec = getattr(self, "_queued_start", None)
        self._queued_start = None
        if spec and not self._closing:
            QTimer.singleShot(0, lambda start_spec=spec: self.start(**start_spec))

    def _display_stopped_notice(self):
        """Report that stopping the viewer never asks the recorder to finish."""
        self.status.show()
        if self._recording:
            self._set_record_status(
                "Display stopped. Recording continues — click “■ Stop recording” to save."
            )
            self.log.emit("[INFO] display stopped; the recording continues")
        elif self._record_finalizing:
            self._set_record_status("Display stopped. Saving the finished recording to your PC…")
        else:
            self.status.setText(
                "Stopped. Choose Start screen or a viewing mode to continue."
            )

    def _stop_scrcpy_async(self, session, window_title=None, window_handle=None):
        """Release the viewer immediately; let its process exit in the background."""
        self._stopping_mirror = True
        worker = _MirrorStopThread(session, window_title, window_handle)
        self._mirror_stop_thread = worker
        park_thread(worker)  # also deletes it once finished
        worker.done.connect(self._on_scrcpy_stop_done)
        worker.start()

    def _on_scrcpy_stop_done(self, clean):
        self._stopping_mirror = False
        self._mirror_stop_thread = None
        _purge_scrcpy_logs()
        if not clean:
            self.log.emit("[WARNING] scrcpy did not stop cleanly; its process was ended.")
        next_start = self._queued_start
        self._queued_start = None
        self._refresh_buttons()
        # The window is gone: an owner that was waiting to destroy this panel's
        # container (a removed display tile) can do so now.
        self.screen_stopped.emit()
        if next_start and not self._closing:
            QTimer.singleShot(0, lambda spec=next_start: self.start(**spec))

    def stop(self):
        # the Stop button stops the scrcpy viewer; a device-side recording runs
        # independently.
        if self._retry_pending:
            self._cancel_retry()
            self._refresh_buttons()
            self._display_stopped_notice()
            return
        if self._launch_pending:
            self._queued_start = None
            self._cancel_launch = True
            self.status.show()
            self.status.setText("Cancelling screen start…")
            return
        if (
            self._rec_mode == "integrated"
            and self._recording
            and not self._rec_integrated_starting
        ):
            self._stop_record(restore_view=False)
            return
        window_handle = self._release_viewer()
        stopped_screencap = self._stop_screencap()
        if self._scrcpy is not None:
            session, self._scrcpy = self._scrcpy, None
            try:
                self._stop_scrcpy_async(session, self._win_title, window_handle)
            except Exception:
                pass
        self._refresh_buttons()
        self._display_stopped_notice()
        if stopped_screencap:
            self.screen_stopped.emit()  # nothing is left to wait for

    def _finalize_rec_async(self):
        """Request recording finalization without freezing a closing tab."""
        if not self._recording:
            return
        if self._rec_mode in ("integrated", "parallel"):
            # scrcpy itself is recording: close it through the parked, clean
            # stop path (WM_CLOSE / Ctrl+Break, then a long wait).  The plain
            # viewer stop (5 s, then kill) truncated the MP4 when a tab was
            # closed mid-recording.
            self._stop_record(restore_view=False)
            return
        self._recording = False
        if self._rec_stop is not None:
            self._rec_stop.set()

    def close_panel(self):
        if self._closing:
            return
        self._closing = True
        self._cancel_retry()
        self._queued_start = None
        # The launch thread deletes itself when it finishes, so its wrapper may
        # be stale here; an unguarded isRunning() would abort the whole close.
        launch_thread = getattr(self, "_launch_thread", None)
        if thread_running(launch_thread):
            launch_thread.cancel()
            self._cancel_launch = True
        # A recorder still launching stops itself once it has started (its
        # result would otherwise reach a deleted panel and never be closed).
        rec_launch = self._rec_launch_thread
        if thread_running(rec_launch):
            rec_launch.cancel()
        if self._key_batcher is not None:
            self._key_batcher.stop()
        self._disconnect_focus_tracking()
        # Max view hid the main window's docks; a closed tab must give them back.
        self._suspend_max()
        self._max_hidden = []
        self._esc_restore.setEnabled(False)
        self._finalize_rec_async()
        # keep still-running worker threads alive past this widget's destruction
        park_thread(self._shot)

        # the screencap stream's adb process ends here, whatever stop() decides
        self._stop_screencap()
        self.stop()
        if self._owns_dispatcher:
            self._dispatcher.stop()
        for thread in self.shutdown_threads():
            park_thread(thread)
        self._retire_log()

    def shutdown_threads(self):
        """Return workers that must finish before the ADB handler is disconnected."""
        names = (
            "_dt",
            "_rec_wait",
            "_rec_thread",
            "_rec_launch_thread",
            "_launch_thread",
            "_mirror_stop_thread",
        )
        threads = []
        for name in names:
            thread = getattr(self, name, None)
            if thread_running(thread) and thread not in threads:
                threads.append(thread)
        view = self._screencap
        for thread in (view.running_threads() if view is not None else []) + list(
            self._screencap_threads
        ):
            if thread_running(thread) and thread not in threads:
                threads.append(thread)
        if self._owns_dispatcher and self._dispatcher.isRunning():
            threads.append(self._dispatcher)
        return threads
