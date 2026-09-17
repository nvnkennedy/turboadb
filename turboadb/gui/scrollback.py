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
the on-screen text for a short session that never spilled to disk.  For a save
that must not block the UI thread use ``save_job()``: it snapshots widget state
on the UI thread and returns a callable that does the disk work anywhere."""

from __future__ import annotations

import os
import queue
import shutil
import tempfile
import threading

from ..config import user_path


class ScrollbackSaveError(OSError):
    """The complete history could not be written (e.g. the writer is stuck)."""


class Scrollback:
    # Do not touch the filesystem for a prompt, banner, or short command.  Apart
    # from being wasteful, creating a temp file synchronously on the Qt thread
    # can stall a terminal while an antivirus/indexer has the logs directory
    # open.  Larger sessions spill asynchronously and retain the same complete
    # history guarantee.
    _MEMORY_LIMIT = 64 * 1024
    # Cap for the fallback buffer used when the writer FAILED (no log file to
    # spill to).  It is a RAM budget, not the "don't touch the disk yet"
    # threshold above, so it is far larger: a whole unbounded capture in memory
    # is what this stops.  The oldest text goes first and the loss is recorded.
    _MEMORY_FALLBACK_LIMIT = 8 * 1024 * 1024
    # How long a save waits for the background writer to drain.  Saves run on a
    # worker thread (see ``save_job``), so this can be generous; on timeout the
    # save FAILS loudly instead of silently writing a truncated history.
    _FLUSH_TIMEOUT_S = 120.0

    def __init__(self, edit, display_cap: int = 100000):
        self._edit = edit
        # Qt drops the oldest blocks for us — efficient and hitch-free.
        edit.setMaximumBlockCount(max(2000, display_cap))
        self._path = None
        self._fh = None
        self._memory = []
        self._memory_size = 0
        self._truncated = 0  # characters the failed-writer fallback had to drop
        self._queue = None
        self._writer = None
        self._writer_error = None
        self._closed = False
        self._delete_when_finished = False
        self._generation = 0
        self._lock = threading.RLock()

    # ---- complete capture (call at the SOURCE, with every incoming chunk) ----
    def _start_writer(self) -> None:
        """Start the spill writer exactly once, without blocking Qt's thread."""
        if self._writer is not None:
            return
        self._queue = queue.Queue()
        generation = self._generation
        self._writer = threading.Thread(
            target=self._writer_main,
            args=(self._queue, generation),
            name="turboadb-scrollback",
            daemon=True,
        )
        self._writer.start()

    def _keep_in_memory(self, generation: int, text: str) -> None:
        """Writer fallback: make *text* visible to saves immediately.

        Bounded, unlike before: with a failed writer there is no file to spill
        to, so an unattended capture buffered the whole session in RAM.  Past
        ``_MEMORY_FALLBACK_LIMIT`` the oldest text is dropped and counted, and
        :meth:`_memory_note` tells a save the history is no longer complete.
        """
        with self._lock:
            if generation != self._generation:
                return
            self._memory.append(text)
            self._memory_size += len(text)
            while self._memory_size > self._MEMORY_FALLBACK_LIMIT and len(self._memory) > 1:
                oldest = self._memory.pop(0)
                self._memory_size -= len(oldest)
                self._truncated += len(oldest)

    def _memory_note(self) -> str:
        """A line for a save when the fallback buffer had to drop history."""
        with self._lock:
            dropped = self._truncated
        if not dropped:
            return ""
        return (f"[TurboADB: the history file could not be written, so the oldest {dropped} "
                "characters of this capture were dropped]\n")

    def _writer_main(self, work_queue, generation: int) -> None:
        """Write queued terminal output and remove the temporary file on close."""
        fh = None
        path = None
        current_item = None
        try:
            d = user_path("logs")
            os.makedirs(d, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix="turboadb-", suffix=".log", dir=d)
            fh = os.fdopen(fd, "w", encoding="utf-8", newline="")
            with self._lock:
                if generation == self._generation:
                    self._path = path
                    self._fh = fh
            while True:
                item = work_queue.get()
                current_item = item
                if item is None:
                    break
                if isinstance(item, tuple):
                    _, done = item
                    fh.flush()
                    done.set()
                    current_item = None
                    continue
                fh.write(item)
                current_item = None
        except Exception as exc:
            # Preserve output in memory if the profile/log directory is not
            # writable.  The terminal must remain usable even in a restricted
            # profile or while another program interferes with temp creation.
            # Chunks go straight to ``_memory`` (not a thread-local list that
            # only surfaced at close), so a save made now is complete.
            with self._lock:
                self._writer_error = exc
            if isinstance(current_item, str):
                self._keep_in_memory(generation, current_item)
            elif isinstance(current_item, tuple):
                current_item[1].set()
            while True:
                item = work_queue.get()
                if item is None:
                    break
                if isinstance(item, tuple):
                    _, done = item
                    done.set()
                else:
                    self._keep_in_memory(generation, item)
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            with self._lock:
                current = generation == self._generation
                remove = self._delete_when_finished or not current
                if current and self._path is None:
                    self._path = path
                if current:
                    self._fh = None
            if remove and path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def archive(self, text: str):
        """Append *text* to the full-history file. Cheap: buffered, not flushed
        per call (flushed on save/close), so even 1M lines costs almost nothing."""
        if not text:
            return
        with self._lock:
            if self._closed:
                return
            if self._writer is None and self._memory_size + len(text) <= self._MEMORY_LIMIT:
                self._memory.append(text)
                self._memory_size += len(text)
                return
            if self._writer is None:
                self._start_writer()
                for part in self._memory:
                    self._queue.put(part)
                self._memory.clear()
                self._memory_size = 0
            self._queue.put(text)

    # ---- read-out (the COMPLETE history, not just the visible tail) ----
    def _flush(self, timeout: float = None) -> bool:
        """Wait until the writer has written everything queued so far.

        Returns False when it did not finish within *timeout*."""
        with self._lock:
            current_queue = self._queue if self._writer is not None else None
        if current_queue is None:
            return True
        done = threading.Event()
        current_queue.put(("flush", done))
        return done.wait(timeout=self._FLUSH_TIMEOUT_S if timeout is None else timeout)

    def _snapshot(self):
        """(spilled, path, memory_text) under the lock."""
        with self._lock:
            return self._writer is not None, self._path, "".join(self._memory)

    def full_text(self) -> str:
        spilled, _, memory = self._snapshot()
        if not spilled:
            return memory if memory else self._edit.toPlainText()
        if not self._flush():
            raise ScrollbackSaveError("the terminal history writer did not finish in time")
        _, path, fallback = self._snapshot()
        note = self._memory_note()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as a:
                return a.read() + note + fallback
        return (note + fallback) if (note or fallback) else self._edit.toPlainText()

    def save_job(self):
        """Return ``write(path)`` producing the complete history.

        Call this on the UI thread (it may read the widget once, for a short
        session that never archived anything); run the returned callable on a
        worker — it only touches the disk.  It raises ``OSError`` on failure.
        """
        spilled, _, memory = self._snapshot()
        visible = None
        if not spilled and not memory:
            visible = self._edit.toPlainText()

        def write(path: str) -> None:
            self._write_complete(path, visible)

        return write

    def _write_complete(self, path: str, visible=None) -> None:
        spilled, _, memory = self._snapshot()
        if not spilled:
            with open(path, "w", encoding="utf-8", newline="") as out:
                out.write(memory if memory else (visible or ""))
            return
        if not self._flush():
            raise ScrollbackSaveError(
                "the terminal history writer did not finish in time; nothing was saved"
            )
        _, archive_path, fallback = self._snapshot()
        note = self._memory_note()  # says so when the fallback dropped history
        if archive_path and os.path.exists(archive_path):
            shutil.copyfile(archive_path, path)
            if note or fallback:
                with open(path, "a", encoding="utf-8", newline="") as out:
                    out.write(note + fallback)
        else:  # the writer failed before creating its file: everything is in memory
            with open(path, "w", encoding="utf-8", newline="") as out:
                out.write(note + fallback)

    def save_to(self, path: str) -> None:
        """Synchronous save (tests / scripts).  GUI code should prefer
        ``save_job()`` + ``fileutil.write_file_async`` to keep Qt responsive."""
        self.save_job()(path)

    # ---- lifecycle ----
    def _discard_current(self, *, close: bool) -> None:
        with self._lock:
            old_queue = self._queue
            old_path = self._path
            self._generation += 1
            self._writer = None
            self._queue = None
            self._fh = None
            self._path = None
            self._writer_error = None
            self._memory.clear()
            self._memory_size = 0
            self._truncated = 0
            self._closed = close
            self._delete_when_finished = close
        if old_queue is not None:
            old_queue.put(None)
        elif old_path and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

    def reset(self):
        """Forget archived history while leaving the panel ready for new output."""
        self._discard_current(close=False)

    def close(self):
        """Discard history when the owning terminal/log panel is closing."""
        self._discard_current(close=True)
