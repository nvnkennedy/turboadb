"""Reader worker shared by TurboADB's ANSI terminal widgets."""

from __future__ import annotations

from PyQt5.QtCore import QThread, pyqtSignal


class ReaderThread(QThread):
    """Pumps bytes from a read callable and emits them. With ``decode=False`` the
    raw bytes are emitted (for a VT100 widget); otherwise they're decoded to str."""

    data = pyqtSignal(object)
    closed = pyqtSignal()

    def __init__(self, read_fn, encoding="utf-8", decode=True):
        super().__init__()
        self._read = read_fn  # callable() -> bytes (b"" idle, None/EOF stops)
        self._alive = True
        self.encoding = encoding
        self.decode = decode

    # Coalesce a burst of reads into ONE signal every ~40ms. Under a logcat
    # flood adb hands us many chunks/sec; emitting a cross-thread signal for
    # each floods the UI event queue (the "tool hangs" symptom). One batched
    # emit per tick keeps the UI thread free while losing no data.
    _FLUSH_S = 0.04
    _MAX_BATCH = 1 << 20  # 1 MB — bound a single emit

    def run(self):
        import codecs
        import time

        parts = []
        size = 0
        last = time.monotonic()
        # An incremental decoder keeps a multi-byte UTF-8 character that is
        # split across two reads intact (per-chunk decode turned it into U+FFFD).
        decoder = codecs.getincrementaldecoder(self.encoding)("replace") if self.decode else None

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
                if decoder is not None and isinstance(chunk, bytes):
                    chunk = decoder.decode(chunk)
                    if not chunk:
                        continue
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
        if decoder is not None:
            tail = decoder.decode(b"", final=True)
            if tail:
                parts.append(tail)
        flush()
        self.closed.emit()

    def stop(self):
        self._alive = False
