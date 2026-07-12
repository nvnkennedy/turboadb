"""Interactive terminal widget for the ``adb shell`` console.

A reader thread pumps incoming bytes into a QPlainTextEdit; key presses are
translated to bytes and sent to the shell subprocess. ANSI escape sequences are
stripped for readability — adb shell output is line-oriented, so this clean
streaming terminal is exactly what's needed (no heavyweight VT100 emulator)."""

from __future__ import annotations

from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtGui import QFont, QTextCursor
from PyQt5.QtWidgets import QPlainTextEdit

from ..results import strip_ansi
from . import settings as settings_mod
from .theme import TERM_BG


class ReaderThread(QThread):
    """Pumps bytes from a read callable and emits them. With ``decode=False`` the
    raw bytes are emitted (for a VT100 widget); otherwise they're decoded to str."""
    data = pyqtSignal(object)
    closed = pyqtSignal()

    def __init__(self, read_fn, encoding="utf-8", decode=True):
        super().__init__()
        self._read = read_fn          # callable() -> bytes (b"" idle, None/EOF stops)
        self._alive = True
        self.encoding = encoding
        self.decode = decode

    # Coalesce a burst of reads into ONE signal every ~40ms. Under a logcat
    # flood adb hands us many chunks/sec; emitting a cross-thread signal for
    # each floods the UI event queue (the "tool hangs" symptom). One batched
    # emit per tick keeps the UI thread free while losing no data.
    _FLUSH_S = 0.04
    _MAX_BATCH = 1 << 20                # 1 MB — bound a single emit

    def run(self):
        import time
        parts = []
        size = 0
        last = time.monotonic()

        def flush():
            nonlocal parts, size
            if not parts:
                return
            if self.decode:
                self.data.emit("".join(parts))
            else:
                self.data.emit(b"".join(parts))
            parts = []
            size = 0

        while self._alive:
            try:
                chunk = self._read()
            except Exception:
                break
            if chunk is None:
                break
            if chunk:
                small = len(chunk) < 8192
                if self.decode and isinstance(chunk, bytes):
                    chunk = chunk.decode(self.encoding, errors="replace")
                parts.append(chunk)
                size += len(chunk)
                now = time.monotonic()
                # coalesce only a genuine FLOOD (big back-to-back chunks); a
                # small chunk means the burst is ebbing, so flush at once — this
                # keeps interactive output instant while taming a logcat flood
                if small or size >= self._MAX_BATCH or (now - last) >= self._FLUSH_S:
                    flush()
                    last = now
            else:
                flush()
                last = time.monotonic()
                self.msleep(15)
        flush()
        self.closed.emit()

    def stop(self):
        self._alive = False


_CTRL = {Qt.Key_C: b"\x03", Qt.Key_D: b"\x04", Qt.Key_Z: b"\x1a",
         Qt.Key_A: b"\x01", Qt.Key_E: b"\x05", Qt.Key_K: b"\x0b",
         Qt.Key_L: b"\x0c", Qt.Key_U: b"\x15", Qt.Key_W: b"\x17"}

_KEYS = {Qt.Key_Return: b"\n", Qt.Key_Enter: b"\n", Qt.Key_Backspace: b"\x7f",
         Qt.Key_Tab: b"\t", Qt.Key_Escape: b"\x1b",
         Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B",
         Qt.Key_Right: b"\x1b[C", Qt.Key_Left: b"\x1b[D"}


class TerminalView(QPlainTextEdit):
    """Displays shell output and forwards keystrokes via ``send_fn(bytes)``."""

    def __init__(self, send_fn=None, parent=None):
        super().__init__(parent)
        self._send = send_fn
        fam = settings_mod.get("term_font") or "Consolas"
        size = int(settings_mod.get("term_font_size") or 10)
        self.setFont(QFont(fam, size))
        self.setMaximumBlockCount(100000)
        self.setStyleSheet(f"background:{TERM_BG}; color:#d7f5e3; border:none;")
        self.setUndoRedoEnabled(False)

    def feed(self, text: str):
        text = strip_ansi(text)
        if not text:
            return
        cur = self.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText(text)
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def keyPressEvent(self, event):
        if not self._send:
            return
        mods, key = event.modifiers(), event.key()
        try:
            if (mods & Qt.ControlModifier) and (mods & Qt.ShiftModifier) \
                    and key == Qt.Key_C:
                self.copy(); return
            if (mods & Qt.ControlModifier) and (mods & Qt.ShiftModifier) \
                    and key == Qt.Key_V:
                self.paste_clipboard(); return
            if (mods & Qt.ControlModifier) and key in _CTRL:
                self._send(_CTRL[key]); return
            if key in _KEYS:
                self._send(_KEYS[key]); return
            text = event.text()
            if text:
                self._send(text.encode("utf-8"))
        except Exception:
            pass

    def paste_clipboard(self):
        from PyQt5.QtWidgets import QApplication
        txt = QApplication.clipboard().text()
        if txt and self._send:
            self._send(txt.encode("utf-8"))
