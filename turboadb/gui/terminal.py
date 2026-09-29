"""Reader worker shared by TurboADB's ANSI terminal widgets."""

from __future__ import annotations

import codecs
import threading
import time

from PyQt5.QtCore import QThread, QTimer, pyqtSignal, pyqtSlot


class ReaderThread(QThread):
    """Pumps bytes from a read callable and emits them. With ``decode=False`` the
    raw bytes are emitted (for a VT100 widget); otherwise they're decoded to str.

    The worker thread only reads and collects; ``data`` is emitted on the
    thread that created the reader (the GUI thread).  What was read goes out
    at once, never held until the next read returns (a blocking read may wait
    until the shell prints again), and at most one cross-thread wake-up is in
    flight however fast the shell writes."""

    data = pyqtSignal(object)
    closed = pyqtSignal()
    _ready = pyqtSignal()   # worker -> owner thread: there is data to deliver
    _ended = pyqtSignal()   # worker -> owner thread: nothing more will come

    # Coalesce a flood into ONE signal every ~40ms. Under a logcat flood adb
    # hands us many chunks/sec (on Windows ~4 KB each, the size of a pipe);
    # one signal each floods the UI event queue (the "tool hangs" symptom).
    # The first _BURST_BYTES of every _FLUSH_S window go out at once, so
    # interactive output is never delayed; the rest of the window is batched.
    _FLUSH_S = 0.04
    _BURST_BYTES = 16 * 1024
    _MAX_BATCH = 1 << 20  # 1 MB — bound a single emit

    def __init__(self, read_fn, encoding="utf-8", decode=True):
        super().__init__()
        self._read = read_fn  # callable() -> bytes (b"" idle, None/EOF stops)
        self._alive = True
        self.encoding = encoding
        self.decode = decode
        self._lock = threading.Lock()
        self._parts = []            # read but not emitted yet (under _lock)
        self._wake_pending = False  # a delivery is on its way (under _lock)
        self._window_start = 0.0
        self._window_bytes = 0
        self._later = QTimer(self)
        self._later.setSingleShot(True)
        self._later.timeout.connect(self._flush_window)
        # Auto connections: queued from the worker, direct when run() is
        # called on the owner's thread.
        self._ready.connect(self._on_ready)
        self._ended.connect(self._on_ended)

    def run(self):
        # An incremental decoder keeps a multi-byte UTF-8 character that is
        # split across two reads intact (per-chunk decode turned it into U+FFFD).
        decoder = codecs.getincrementaldecoder(self.encoding)("replace") if self.decode else None
        while self._alive:
            try:
                chunk = self._read()
            except Exception:
                break
            if chunk is None:
                break
            if not chunk:
                self.msleep(15)
                continue
            if decoder is not None and isinstance(chunk, bytes):
                chunk = decoder.decode(chunk)
                if not chunk:
                    continue
            self._collect(chunk)
        if decoder is not None:
            tail = decoder.decode(b"", final=True)
            if tail:
                self._collect(tail)
        self._ended.emit()

    def _collect(self, chunk):
        """Worker side: keep *chunk* for the owner thread and wake it once."""
        with self._lock:
            self._parts.append(chunk)
            wake = not self._wake_pending
            self._wake_pending = True
        if wake:
            self._ready.emit()

    @pyqtSlot()
    def _on_ready(self):
        now = time.monotonic()
        if now - self._window_start >= self._FLUSH_S:
            self._window_start = now
            self._window_bytes = 0
        if self._window_bytes < self._BURST_BYTES:
            self._deliver()
        elif not self._later.isActive():
            # A flood: what arrives until this window ends goes out as one batch.
            wait = self._window_start + self._FLUSH_S - now
            self._later.start(max(1, int(wait * 1000) + 1))

    @pyqtSlot()
    def _flush_window(self):
        self._window_start = time.monotonic()
        self._window_bytes = 0
        self._deliver()

    def _deliver(self):
        with self._lock:
            parts, self._parts = self._parts, []
            self._wake_pending = False
        if not parts:
            return
        joined = parts[0][:0].join(parts)
        self._window_bytes += len(joined)
        for start in range(0, len(joined), self._MAX_BATCH):
            self.data.emit(joined[start:start + self._MAX_BATCH])

    @pyqtSlot()
    def _on_ended(self):
        self._later.stop()
        self._deliver()
        self.closed.emit()

    def stop(self):
        self._alive = False
