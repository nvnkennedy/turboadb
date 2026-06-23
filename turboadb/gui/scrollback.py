"""Keep the COMPLETE log without ever freezing the UI.

Two independent concerns, deliberately separated:

* **Completeness** — every incoming byte is streamed to a per-session temp file
  the instant it arrives (``archive``), *before* any on-screen dropping. So a
  capture of 1,000,000+ lines is saved in full even if the view only ever shows
  a window of it.

* **Responsiveness** — the on-screen widget uses Qt's native, O(1) block-count
  cap (``setMaximumBlockCount``) to drop its oldest lines efficiently. The widget
  never holds more than a bounded number of lines, so painting/scrolling stay
  fast no matter how much data flows (the full history lives on disk, not in the
  widget).

``save_to`` / ``full_text`` return the disk archive (everything), falling back to
the on-screen text for a short session that never spilled to disk."""

from __future__ import annotations

import os
import shutil
import tempfile


class Scrollback:
    def __init__(self, edit, display_cap: int = 100000):
        self._edit = edit
        # Qt drops the oldest blocks for us — efficient and hitch-free.
        edit.setMaximumBlockCount(max(2000, display_cap))
        self._path = None
        self._fh = None
        self._dirty = False

    # ---- complete capture (call at the SOURCE, with every incoming chunk) ----
    def _ensure_fh(self):
        if self._fh is None:
            d = os.path.join(os.path.expanduser("~"), ".turboadb", "logs")
            os.makedirs(d, exist_ok=True)
            fd, self._path = tempfile.mkstemp(prefix="turboadb-", suffix=".log", dir=d)
            self._fh = os.fdopen(fd, "w", encoding="utf-8", newline="")

    def archive(self, text: str):
        """Append *text* to the full-history file. Cheap: buffered, not flushed
        per call (flushed on save/close), so even 1M lines costs almost nothing."""
        if not text:
            return
        self._ensure_fh()
        self._fh.write(text)
        self._dirty = True

    # ---- read-out (the COMPLETE history, not just the visible tail) ----
    def _flush(self):
        if self._fh and self._dirty:
            self._fh.flush()
            self._dirty = False

    def full_text(self) -> str:
        if self._path:
            self._flush()
            with open(self._path, "r", encoding="utf-8", errors="replace") as a:
                return a.read()
        return self._edit.toPlainText()

    def save_to(self, path: str) -> None:
        self._flush()
        if self._path and os.path.exists(self._path):
            shutil.copyfile(self._path, path)
        else:                                   # nothing spilled to disk yet
            with open(path, "w", encoding="utf-8", newline="") as out:
                out.write(self._edit.toPlainText())

    def has_archive(self) -> bool:
        return bool(self._path and os.path.exists(self._path))

    # ---- lifecycle ----
    def reset(self):
        """Forget the archived history (used when the user clears the view)."""
        try:
            if self._fh:
                self._fh.close()
        finally:
            self._fh = None
        if self._path and os.path.exists(self._path):
            try:
                os.remove(self._path)
            except Exception:
                pass
        self._path = None
        self._dirty = False

    close = reset
