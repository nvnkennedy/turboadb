"""TurboADB inside another program's window.

``turboadb gui --embed-into <window handle>`` (``TurboADB.exe --embed-into``
for the standalone app) puts the whole app into a window of another program:
PyQt5, PySide6, Delphi, C#, anything that has a window handle.  It fills that
window's client area, follows its size, and closes when that window goes.

``--api-port`` starts the control API (:mod:`turboadb.gui.control_api`), and
``--handshake-file`` says where TurboADB writes how to reach it, once its
window is up.  The API's token comes from ``TURBOADB_API_TOKEN`` when the
host chose one, else TurboADB makes one and the handshake file holds it.

The host side, in Python, is :mod:`turboadb.embed` (and
:mod:`turboadb.embed_qt`, a Qt widget); EMBEDDING.md describes the rest.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Optional, Tuple

TOKEN_ENV = "TURBOADB_API_TOKEN"

# Parts of the window a host may leave out (--hide, and the API's /v1/ui).
HIDE_PARTS = ("menubar", "ribbon", "sidebar", "statusbar", "log")


@dataclass
class EmbedOptions:
    """What the command line asked for."""

    parent: Optional[int] = None  # the window handle to live in
    api_port: Optional[int] = None  # None: no control API; 0: any free port
    handshake: Optional[str] = None  # where the API's address and token go
    hide: Tuple[str, ...] = ()  # parts of the window to leave out
    token: Optional[str] = None  # the API's token, when the host chose it

    @property
    def embedded(self) -> bool:
        return self.parent is not None

    @property
    def api(self) -> bool:
        return self.api_port is not None or self.handshake is not None


# The options of this process, once main() has read them.
_ACTIVE = EmbedOptions()


def active() -> EmbedOptions:
    return _ACTIVE


def is_embedded() -> bool:
    """Whether this TurboADB lives inside another program's window."""
    return _ACTIVE.embedded


def _window_handle(text: str) -> int:
    try:
        value = int(str(text).strip(), 0)  # 1234, or 0x4D2 as Spy++ shows it
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a window handle: {text!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"not a window handle: {text!r}")
    return value


def _api_port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port: {text!r}")
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError("the port must be 0 (any free one) to 65535")
    return value


def _hide_parts(text: str) -> Tuple[str, ...]:
    parts = tuple(p.strip().lower() for p in str(text).split(",") if p.strip())
    unknown = [p for p in parts if p not in HIDE_PARTS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"cannot hide {', '.join(unknown)}: the parts are {', '.join(HIDE_PARTS)}")
    return parts


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The embedding options, on *parser* (``turboadb gui`` has them too)."""
    parser.add_argument(
        "--embed-into", dest="parent", type=_window_handle, metavar="HWND",
        help="live inside this window of another program (its handle, decimal or 0x hex)")
    parser.add_argument(
        "--api-port", type=_api_port, metavar="PORT",
        help="start the control API on 127.0.0.1 at this port (0: any free one)")
    parser.add_argument(
        "--handshake-file", dest="handshake", metavar="FILE",
        help="write the control API's address and token to FILE once the window is up")
    parser.add_argument(
        "--hide", type=_hide_parts, default=(), metavar="PARTS",
        help="leave out parts of the window, comma separated: " + ", ".join(HIDE_PARTS))


def parse(argv) -> EmbedOptions:
    """The embedding options in *argv*; anything else in it is left alone.
    Raises SystemExit (argparse's usage error) for a bad value."""
    parser = argparse.ArgumentParser(prog="turboadb gui", add_help=False)
    add_arguments(parser)
    known, _rest = parser.parse_known_args([str(a) for a in (argv or [])])
    return EmbedOptions(parent=known.parent, api_port=known.api_port,
                        handshake=known.handshake, hide=tuple(known.hide or ()))


def take_token(environ=None) -> Optional[str]:
    """The token the host chose, taken out of the environment so nothing
    TurboADB starts (a terminal, scrcpy) inherits it."""
    environ = os.environ if environ is None else environ
    token = (environ.pop(TOKEN_ENV, "") or "").strip()
    return token or None


def activate(options: EmbedOptions) -> None:
    global _ACTIVE
    _ACTIVE = options


# ---- Windows: the host's window -------------------------------------------------
def _user32():
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    u.IsWindow.argtypes = (wintypes.HWND,)
    u.IsWindow.restype = wintypes.BOOL
    u.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    u.GetClientRect.restype = wintypes.BOOL
    return ctypes, wintypes, u


def host_alive(hwnd: int) -> bool:
    """Whether the window *hwnd* still exists (always True off Windows)."""
    if os.name != "nt":
        return True
    try:
        _c, _w, u = _user32()
        return bool(u.IsWindow(hwnd))
    except Exception:
        return True


def host_client_size(hwnd: int) -> Optional[Tuple[int, int]]:
    """``(width, height)`` of *hwnd*'s client area in device pixels, or None."""
    if os.name != "nt":
        return None
    try:
        ctypes, wintypes, u = _user32()
        rect = wintypes.RECT()
        if not u.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        return max(0, rect.right - rect.left), max(0, rect.bottom - rect.top)
    except Exception:
        return None


def match_host_dpi(hwnd: int, environ=None) -> Optional[int]:
    """Make this process as DPI aware as the host's window, before Qt starts.

    Windows does not let a window of one DPI awareness hold a child of
    another one well: a Delphi or WinForms program that is not per-monitor
    aware showed TurboADB scaled wrongly or not at all.  Returns the awareness
    set (0 unaware, 1 system, 2 per monitor), or None when it was left alone:
    off Windows, on a Windows before 10 1607, or when QT_QPA_PLATFORM was set
    on purpose."""
    environ = os.environ if environ is None else environ
    if os.name != "nt" or environ.get("QT_QPA_PLATFORM"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        u.GetWindowDpiAwarenessContext.argtypes = (wintypes.HWND,)
        u.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        u.GetAwarenessFromDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
        u.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        context = u.GetWindowDpiAwarenessContext(hwnd)
        if not context:
            return None
        awareness = int(u.GetAwarenessFromDpiAwarenessContext(context))
    except Exception:
        return None
    if awareness not in (0, 1, 2):
        return None
    environ["QT_QPA_PLATFORM"] = f"windows:dpiawareness={awareness}"
    return awareness


# ---- the handshake -----------------------------------------------------------------
def write_handshake(path: str, info: dict) -> None:
    """Write *info* to *path* as JSON, whole or not at all: a host polling for
    the file never reads half of it."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    temp = f"{path}.{os.getpid()}.tmp"
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    os.replace(temp, path)


# ---- the window inside the host ----------------------------------------------------
try:
    from PyQt5.QtCore import QObject, QRect, Qt, QTimer, pyqtSignal
except ImportError:  # the host side imports this module's helpers without Qt
    QObject = object
    pyqtSignal = None


if pyqtSignal is not None:

    class Embedder(QObject):
        """Keeps *window* inside the host window *parent*: as its child, the
        size of its client area, until it goes (then :attr:`host_closed`)."""

        host_closed = pyqtSignal()
        POLL_MS = 120

        def __init__(self, window, parent: int):
            super().__init__(window)
            self._window = window
            self._parent = int(parent)
            self._host = None
            self._timer = QTimer(self)
            self._timer.setInterval(self.POLL_MS)
            self._timer.timeout.connect(self._follow)

        @property
        def parent_handle(self) -> int:
            return self._parent

        def attach(self) -> bool:
            """Put the window inside the host's; False when there is no such
            window (the host gave a wrong handle, or has closed already)."""
            from PyQt5.QtGui import QWindow

            if not host_alive(self._parent):
                return False
            host = QWindow.fromWinId(self._parent)
            if host is None:
                return False
            self._host = host
            window = self._window
            # no frame or title bar: the host's window is the frame
            window.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
            window.winId()  # the native window, to be given its parent
            window.windowHandle().setParent(host)
            self._follow()
            window.show()
            self._timer.start()
            return True

        def detach(self) -> None:
            self._timer.stop()

        def _follow(self) -> None:
            if not host_alive(self._parent):
                self._timer.stop()
                self.host_closed.emit()
                return
            size = host_client_size(self._parent)
            if size is None:
                return
            ratio = float(self._window.devicePixelRatioF() or 1.0)
            wanted = QRect(0, 0, max(1, round(size[0] / ratio)), max(1, round(size[1] / ratio)))
            if self._window.geometry() != wanted:
                self._window.setGeometry(wanted)
