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
on the UI thread and returns a callable that does the disk work anywhere.

"Everything" has one deliberate bound: the history file of one terminal or
capture is kept to :attr:`Scrollback.HISTORY_LIMIT_MB` (``history_limit_mb`` in
settings.json changes it, 0 lifts it), and to less while the disk is nearly
full, so an unattended ``adb logcat`` or ``ping -t`` can't fill the disk over a
weekend.  The file is kept in two parts; once the newer one is full the older
one goes, and every save starts with a line saying how much was dropped."""

from __future__ import annotations

import atexit
import io
import os
import queue
import re
import shutil
import tempfile
import threading

from ..config import user_path

# CRs before a line feed: adb.exe writes its stdout in text mode, so a device
# terminal's CR LF arrives as CR CR LF, which some editors show as an extra
# blank line after every line of a saved log.
_CRS_LF_RE = re.compile(r"\r+\n")
_MB = 1024 * 1024

# The history files of this process are named turboadb-<OWNER>-xxxxxxxx.log,
# and the process holds <logs>/.turboadb-<OWNER>.lock for as long as it runs.
# The clean-up at start (app._sweep_stale_logs) removes only files whose owner
# is gone: another TurboADB's could be deleted while it still wrote them (on
# Linux nothing stops the delete, and on Windows the older part of a history
# is closed between writes).
OWNER = f"{os.getpid()}-{os.urandom(3).hex()}"
_OWNER_RE = re.compile(r"^turboadb-(\d+-[0-9a-f]{6})-[^-]+\.log$")
_LOCK_RE = re.compile(r"^\.turboadb-(\d+-[0-9a-f]{6})\.lock$")
_claimed = {}  # logs folder -> this process's lock there (None: could not be taken)
_claimed_lock = threading.Lock()


def _owner_lock_path(folder: str, owner: str) -> str:
    return os.path.join(folder, f".turboadb-{owner}.lock")


def _claim(folder: str) -> None:
    """Hold this process's owner lock in *folder*, from its first history file
    there to the end of the process."""
    with _claimed_lock:
        if folder not in _claimed:
            from .settings import _lock_acquire

            _claimed[folder] = _lock_acquire(_owner_lock_path(folder, OWNER), 0)


def _release_claims() -> None:
    """Let go of this process's owner locks on the way out: the operating
    system would a moment later, but their files then closed uncleanly."""
    from .settings import _lock_release

    with _claimed_lock:
        handles = [handle for handle in _claimed.values() if handle is not None]
        _claimed.clear()
    for handle in handles:
        _lock_release(handle)


atexit.register(_release_claims)


def history_owner(name: str):
    """The owner in a history file's *name* (see :data:`OWNER`), or None for a
    file an older TurboADB wrote, whose owner can't be told."""
    m = _OWNER_RE.match(os.path.basename(name))
    return m.group(1) if m else None


def owner_running(folder: str, owner: str) -> bool:
    """Whether the TurboADB process *owner* still holds its lock in *folder*
    (this process's own included).  A lock file that can't be opened counts as
    held: its files are left alone."""
    path = _owner_lock_path(folder, owner)
    if not os.path.exists(path):
        return False
    from .settings import _lock_acquire, _lock_release

    handle = _lock_acquire(path, 0)
    if handle is None:
        return True
    _lock_release(handle)
    return False


def forget_owners(folder: str) -> None:
    """Remove the lock files of owners that are gone and left no history in
    *folder* (every clean exit leaves its own behind)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        m = _LOCK_RE.match(name)
        if m is None:
            continue
        owner = m.group(1)
        if any(history_owner(other) == owner for other in names):
            continue
        if not owner_running(folder, owner):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


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
    # The most one history file takes on disk (see the module docstring).
    HISTORY_LIMIT_MB = 1024
    _HISTORY_MIN_MB = 16  # a smaller setting (other than 0) is taken as this
    # While the disk has less free space than this, the older part of the
    # history goes at once; the free space is checked every _SPACE_CHECK
    # bytes written.
    _FREE_SPACE_FLOOR = 1024 * _MB
    _SPACE_CHECK = 64 * _MB
    # Output the writer has not written yet (a slow disk under a flood).  A
    # producer that may wait (logcat's reader) waits for room; the terminal
    # (the UI thread) drops what does not fit and the history says so there.
    _QUEUE_LIMIT = 64 * _MB

    def __init__(self, edit, display_cap: int = 100000):
        self._edit = edit
        # Qt drops the oldest blocks for us — efficient and hitch-free.
        edit.setMaximumBlockCount(max(2000, display_cap))
        self._path = None       # the history file being written
        self._old_path = None   # its older part, once the limit split it
        self._fh = None
        self._memory = []
        self._memory_size = 0
        self._truncated = 0  # characters the failed-writer fallback had to drop
        self._queue = None
        self._queued = 0     # characters queued for the writer, not written yet
        self._hole = 0       # characters dropped from a full queue, not noted yet
        self._at_line_start = True  # the last text archived ended a line
        self._limit = 0          # the writer's limit in bytes (0: none), see HISTORY_LIMIT_MB
        self._dropped_bytes = 0  # dropped with older parts
        self._dropped_lines = 0
        self._rotations = 0      # older parts dropped so far (readers retry on a change)
        self._writer = None
        self._writer_error = None
        self._closed = False
        self._delete_when_finished = False
        self._generation = 0
        self._cr_carry = ""  # CRs that ended the last chunk; a LF may follow
        self._lock = threading.RLock()
        self._room = threading.Condition(self._lock)  # the writer made room

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

    @staticmethod
    def _hole_note(hole: int, at_line_start: bool) -> str:
        """The line that marks output dropped from a full queue (see archive)."""
        note = (f"[TurboADB: {hole:,} characters of output are missing here: "
                "the disk did not keep up]\n")
        return note if at_line_start else "\n" + note

    def _end_note(self) -> str:
        """The hole note for output dropped last, with nothing archived after it."""
        with self._lock:
            hole, at_line_start = self._hole, self._at_line_start
        return self._hole_note(hole, at_line_start) if hole else ""

    def _history_note(self, dropped_bytes: int, dropped_lines: int) -> str:
        """A line for the start of a save when older parts of the history went."""
        if not dropped_bytes:
            return ""
        with self._lock:
            limit = self._limit
        why = (f"to keep its file under {limit // _MB} MB and the disk from filling up"
               if limit else "to keep the disk from filling up")
        return (f"[TurboADB: the oldest {dropped_lines:,} lines ({dropped_bytes / _MB:.1f} MB) "
                f"of this history were dropped {why}]\n")

    def _history_limit(self) -> int:
        """Bytes one history file may take (0: no limit), from settings.json's
        ``history_limit_mb`` when it holds a number, else the default."""
        megabytes = self.HISTORY_LIMIT_MB
        try:
            from . import settings as settings_mod

            value = settings_mod.get("history_limit_mb")
            if value is not None and not isinstance(value, bool):
                megabytes = int(value)
        except Exception:
            pass
        if megabytes <= 0:
            return 0
        return max(self._HISTORY_MIN_MB, megabytes) * _MB

    def _low_on_space(self, folder: str) -> bool:
        try:
            return shutil.disk_usage(folder).free < self._FREE_SPACE_FLOOR
        except OSError:
            return False

    def _written(self, generation: int, count: int) -> None:
        """The writer is done with *count* queued characters: room for more."""
        with self._room:
            if generation == self._generation:
                self._queued = max(0, self._queued - count)
                self._room.notify_all()

    @staticmethod
    def _remove(path: str) -> bool:
        """Remove *path*; False while it can't be (a save still reading it)."""
        try:
            os.remove(path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _writer_main(self, work_queue, generation: int) -> None:
        """Write queued terminal output and remove the temporary files on close.

        The history is written to a file of its own until that reaches half
        the limit (or the disk runs low); then the part before it is removed
        and a new file starts.  Readers open the parts under the lock's
        snapshot and retry when a part went meanwhile (``_open_archive``)."""
        fh = None
        path = None
        made = []  # every file this writer made that may still exist
        current_item = None
        try:
            limit = self._history_limit()
            part_limit = limit // 2 if limit else 0
            d = user_path("logs")
            os.makedirs(d, exist_ok=True)
            _claim(d)  # before the first file: a file of ours is never unowned
            fd, path = tempfile.mkstemp(prefix=f"turboadb-{OWNER}-", suffix=".log", dir=d)
            made.append(path)
            fh = os.fdopen(fd, "wb")
            with self._lock:
                if generation == self._generation:
                    self._path = path
                    self._fh = fh
                    self._limit = limit
            part_bytes = part_lines = 0
            older = None  # (path, bytes, lines) of the part before this one
            since_check = 0
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
                data = item.encode("utf-8", "replace")
                low = False
                if since_check >= self._SPACE_CHECK:
                    since_check = 0
                    low = self._low_on_space(d)
                if part_bytes and (low or (part_limit and part_bytes + len(data) > part_limit)):
                    fh.close()
                    fd, new_path = tempfile.mkstemp(prefix=f"turboadb-{OWNER}-", suffix=".log",
                                                    dir=d)
                    made.append(new_path)
                    fh = os.fdopen(fd, "wb")
                    with self._lock:
                        if generation == self._generation:
                            self._old_path, self._path, self._fh = path, new_path, fh
                            if older is not None:
                                self._dropped_bytes += older[1]
                                self._dropped_lines += older[2]
                            self._rotations += 1
                    # A part a save is still copying can't go yet on Windows:
                    # it is removed with the next part, or when the writer ends.
                    for stale in [p for p in made if p not in (path, new_path)]:
                        if self._remove(stale):
                            made.remove(stale)
                    older = (path, part_bytes, part_lines)
                    path = new_path
                    part_bytes = part_lines = 0
                fh.write(data)
                part_bytes += len(data)
                part_lines += item.count("\n")
                since_check += len(data)
                current_item = None
                self._written(generation, len(item))
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
                self._written(generation, len(current_item))
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
                    self._written(generation, len(item))
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
            if remove:
                for stale in made:
                    self._remove(stale)

    def archive(self, text: str, *, lossless: bool = False):
        """Append *text* to the full-history file. Cheap: buffered, not flushed
        per call (flushed on save/close), so even 1M lines costs almost nothing.

        A line ends in at most one CR (see ``_CRS_LF_RE``), also when the CRs
        and the LF arrive in separate chunks.  While the writer is behind by
        ``_QUEUE_LIMIT`` the text is dropped instead, and a note marks the
        spot; *lossless* keeps it anyway (logcat counts its archived lines, so
        its reader waits for room first: :meth:`wait_for_room`)."""
        if not text:
            return
        with self._lock:
            if self._closed:
                return
            if self._cr_carry or "\r" in text:
                text = self._cr_carry + text
                body = text.rstrip("\r")
                self._cr_carry = text[len(body):]
                text = _CRS_LF_RE.sub("\r\n", body)
                if not text:
                    return
            if self._writer is None and self._memory_size + len(text) <= self._MEMORY_LIMIT:
                self._memory.append(text)
                self._memory_size += len(text)
                self._at_line_start = text.endswith("\n")
                return
            if self._writer is None:
                self._start_writer()
                for part in self._memory:
                    self._queue.put(part)
                    self._queued += len(part)
                self._memory.clear()
                self._memory_size = 0
            if not lossless and self._queued + len(text) > self._QUEUE_LIMIT:
                self._hole += len(text)
                return
            if self._hole:
                note = self._hole_note(self._hole, self._at_line_start)
                self._hole = 0
                self._queue.put(note)
                self._queued += len(note)
            self._queue.put(text)
            self._queued += len(text)
            self._at_line_start = text.endswith("\n")

    def wait_for_room(self, size: int, cancelled=None) -> None:
        """Wait until the writer has room for *size* more characters, or
        *cancelled()* says to stop waiting.  For a producer off the UI thread,
        so a disk slower than its output slows it down instead of filling
        memory; never while holding a lock the UI thread takes."""
        with self._room:
            while (
                not self._closed and self._writer is not None and self._queued
                and self._queued + size > self._QUEUE_LIMIT
            ):
                if cancelled is not None and cancelled():
                    return
                self._room.wait(0.1)

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

    def _open_archive(self):
        """``(files, dropped_bytes, dropped_lines)``: the history files opened
        for reading, oldest first, and what older parts took with them.

        Opened as one consistent set: a part dropped between the look and the
        opening makes it look again.  An open part stays readable even when
        it is dropped meanwhile."""
        while True:
            with self._lock:
                paths = [p for p in (self._old_path, self._path) if p]
                rotations = self._rotations
                dropped = (self._dropped_bytes, self._dropped_lines)
            files = []
            for path in paths:
                try:
                    files.append(open(path, "rb"))
                except FileNotFoundError:
                    pass
                except OSError:
                    for fh in files:
                        fh.close()
                    raise
            with self._lock:
                if self._rotations == rotations:
                    return files, dropped[0], dropped[1]
            for fh in files:
                fh.close()

    def full_text(self) -> str:
        spilled, _, memory = self._snapshot()
        if not spilled:
            return memory if memory else self._edit.toPlainText()
        if not self._flush():
            raise ScrollbackSaveError("the terminal history writer did not finish in time")
        files, dropped_bytes, dropped_lines = self._open_archive()
        try:
            text = "".join(fh.read().decode("utf-8", "replace") for fh in files)
        finally:
            for fh in files:
                fh.close()
        _, _, fallback = self._snapshot()
        note = self._memory_note()
        end = self._end_note()
        if files:
            return self._history_note(dropped_bytes, dropped_lines) + text + note + fallback + end
        if note or fallback or end:
            return note + fallback + end
        return self._edit.toPlainText()

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
        files, dropped_bytes, dropped_lines = self._open_archive()
        try:
            _, _, fallback = self._snapshot()
            note = self._memory_note()  # says so when the fallback dropped history
            with open(path, "wb") as out:
                if files:
                    out.write(self._history_note(dropped_bytes, dropped_lines).encode("utf-8"))
                    for fh in files:
                        shutil.copyfileobj(fh, out)
                # else the writer failed before creating its file: everything is in memory
                out.write((note + fallback + self._end_note()).encode("utf-8", "replace"))
        finally:
            for fh in files:
                fh.close()

    def save_to(self, path: str) -> None:
        """Synchronous save (tests / scripts).  GUI code should prefer
        ``save_job()`` + ``fileutil.write_file_async`` to keep Qt responsive."""
        self.save_job()(path)

    # ---- searching the archive (the logcat filter reads everything) ----
    def memory_text(self):
        """The archived text while it is still held in memory, else None.

        Never touches the disk and never waits, so the UI thread may call it;
        once the history has spilled, read it with :meth:`iter_lines`."""
        spilled, _, memory = self._snapshot()
        return None if spilled else memory

    def archived_lines(self):
        """``(skipped, lines)``: how many lines older parts of the history took
        with them (see :attr:`HISTORY_LIMIT_MB`), and an iterator over every
        line still archived, oldest first, without its newline.

        It waits for the background writer first, so call it off the UI thread
        (it raises ScrollbackSaveError when the writer does not finish).  Only
        ``"\\n"`` ends a line, as in the archive itself."""
        spilled, _, memory = self._snapshot()
        if not spilled:
            return 0, iter(self._split_lines(memory))
        if not self._flush():
            raise ScrollbackSaveError("the history writer did not finish in time")
        files, _dropped_bytes, dropped_lines = self._open_archive()
        _, _, fallback = self._snapshot()

        def lines():
            tail = ""
            try:
                for fh in files:
                    reader = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="\n")
                    for line in reader:
                        if line.endswith("\n"):
                            yield tail + line[:-1]
                            tail = ""
                        else:
                            tail += line  # the line goes on in the next part
            finally:
                for fh in files:
                    fh.close()
            if tail:
                yield tail
            yield from self._split_lines(fallback)

        return dropped_lines, lines()

    def iter_lines(self):
        """Yield every archived line, oldest first, without its newline (see
        :meth:`archived_lines`, which also says how many older lines went)."""
        _skipped, lines = self.archived_lines()
        yield from lines

    @staticmethod
    def _split_lines(text: str) -> list:
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines

    # ---- lifecycle ----
    def _discard_current(self, *, close: bool) -> None:
        with self._room:
            old_queue = self._queue
            old_path = self._path
            self._generation += 1
            self._writer = None
            self._queue = None
            self._fh = None
            self._path = None
            self._old_path = None
            self._writer_error = None
            self._memory.clear()
            self._memory_size = 0
            self._truncated = 0
            self._queued = 0
            self._hole = 0
            self._at_line_start = True
            self._dropped_bytes = 0
            self._dropped_lines = 0
            self._cr_carry = ""
            self._closed = close
            self._delete_when_finished = close
            self._room.notify_all()
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
