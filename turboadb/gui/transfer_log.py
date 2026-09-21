"""The Files page's transfer history: what was copied, how big, how fast.

Pure Python with no Qt import, so it is testable anywhere and the panel only
renders it.  Every mutation happens on the UI thread (transfer and measuring
workers report back through queued signals), so nothing here needs a lock.

A *batch* is everything queued while at least one transfer was still queued
or running; the header of the panel describes the current (or last) batch,
while the history keeps every batch until it is cleared.

Progress is weighted by bytes once every item's size is known — copying one
4 GB film and ninety-nine photos must not read "99 %" after the photos — and
falls back to counting items while sizes are still being measured.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
FINISHED = frozenset((DONE, FAILED, CANCELLED))
ACTIVE = frozenset((QUEUED, RUNNING))


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def transfer_name(path) -> str:
    """Display name of a transfer source (``folder/.`` merge sources included)."""
    text = str(path)
    if text.endswith(("/.", "\\.")):
        text = text[:-2]
    return re.split(r"[/\\]", text.rstrip("/\\"))[-1] or text


def human_bytes(num) -> str:
    """``1536`` -> ``"1.5 KB"``; ``None`` -> ``"—"``."""
    if num is None:
        return "—"
    num = float(num)
    if num < 1024:
        return f"{int(num)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num /= 1024.0
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}"
    return f"{num:.1f} TB"  # pragma: no cover - the loop always returns


def human_speed(bytes_per_second) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return ""
    return f"{human_bytes(bytes_per_second)}/s"


def human_duration(seconds) -> str:
    """``7`` -> ``"0:07"``, ``92`` -> ``"1:32"``, ``3723`` -> ``"1:02:03"``."""
    if seconds is None:
        return ""
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


DIRECTION_LABELS = {"push": "PC → Device", "pull": "Device → PC"}
_PAST = {"push": "Pushed", "pull": "Pulled"}
_PROGRESSIVE = {"push": "Pushing", "pull": "Pulling"}


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #
@dataclass
class TransferItem:
    """One queued copy, from the moment it is queued until it is cleared."""

    id: int
    src: str
    dst: str
    direction: str  # "push" (PC -> device) or "pull" (device -> PC)
    name: str
    batch: int
    added_at: float
    status: str = QUEUED
    size: Optional[int] = None  # bytes; None until measured
    size_exact: bool = True  # False while the size is a `du` estimate
    files: Optional[int] = None  # files inside a folder, when known
    percent: int = 0
    transferred: int = 0  # bytes counted as moved so far
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    duration: Optional[float] = None  # the worker's own measurement, when known
    error: str = ""
    rev: int = 0  # bumped on every change; lets a view repaint only what moved

    @property
    def job(self) -> Tuple[str, str, str]:
        return (self.src, self.dst, self.direction)

    @property
    def finished(self) -> bool:
        return self.status in FINISHED

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    def elapsed(self, now: float) -> Optional[float]:
        """Seconds spent transferring (so far, while running)."""
        if self.started_at is None:
            return None
        if self.finished:
            if self.duration is not None:
                return self.duration
            return max(0.0, (self.finished_at or now) - self.started_at)
        return max(0.0, now - self.started_at)

    def speed(self, now: float) -> float:
        """Average bytes per second while running, the final rate once done."""
        elapsed = self.elapsed(now)
        if not elapsed or elapsed <= 0:
            return 0.0
        if self.status == DONE:
            return (self.size or 0) / elapsed
        if self.status == RUNNING:
            return self.transferred / elapsed
        return 0.0


@dataclass
class TransferStats:
    """Aggregate view of one batch, for the panel header and the summary toast."""

    total: int = 0
    queued: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0
    cancelled: int = 0
    measuring: int = 0  # items whose size is not known yet
    bytes_total: Optional[int] = None  # None while any size is unknown
    bytes_estimated: bool = False  # part of bytes_total is a `du` estimate
    bytes_done: int = 0  # bytes of the items that completed or are moving
    fraction: float = 0.0  # 0..1, processed share of the batch
    by_size: bool = False  # fraction weighted by bytes (else by item count)
    speed: float = 0.0  # bytes per second
    eta: Optional[float] = None  # seconds left, when it can be estimated
    elapsed: float = 0.0
    directions: Tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.queued or self.running)

    @property
    def processed(self) -> int:
        return self.done + self.failed + self.cancelled

    @property
    def verb(self) -> str:
        """"Pushing"/"Pulling" while active (one direction), else "Transferring"."""
        if len(self.directions) == 1:
            return _PROGRESSIVE.get(self.directions[0], "Transferring")
        return "Transferring"

    @property
    def past(self) -> str:
        if len(self.directions) == 1:
            return _PAST.get(self.directions[0], "Transferred")
        return "Transferred"


class TransferLog:
    """Every transfer this Files page queued, newest last."""

    # History kept beyond the current batch; the current batch is never trimmed.
    MAX_ITEMS = 5000
    # Speed is the byte rate across this many recent seconds, so one slow file
    # doesn't define the whole batch and a stall shows up quickly.
    SPEED_WINDOW_S = 5.0

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._items: Dict[int, TransferItem] = {}  # insertion ordered
        self._next_id = 1
        self._batch = 0
        self._batch_started: Optional[float] = None
        self._batch_ended: Optional[float] = None
        self._batch_bytes = 0  # running total of the current batch's transferred
        self._batch_count = 0  # items in the current batch (O(1) for hot paths)
        self._samples: deque = deque()  # (time, batch bytes) for the speed window
        self.revision = 0  # any change
        self.structure = 0  # items added or removed (a view must re-sync rows)

    # ---- reading -----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(list(self._items.values()))

    @property
    def items(self) -> List[TransferItem]:
        return list(self._items.values())

    def get(self, item_id: int) -> Optional[TransferItem]:
        return self._items.get(item_id)

    @property
    def active(self) -> bool:
        return any(item.active for item in self._items.values())

    @property
    def batch(self) -> int:
        return self._batch

    def batch_items(self) -> List[TransferItem]:
        return [item for item in self._items.values() if item.batch == self._batch]

    def now(self) -> float:
        return self._clock()

    def batch_size(self) -> int:
        """Items in the current batch, without scanning the history."""
        return self._batch_count

    # ---- bookkeeping -------------------------------------------------------
    def _touch(self, item: Optional[TransferItem] = None, *, structural: bool = False) -> None:
        self.revision += 1
        if structural:
            self.structure += 1
        if item is not None:
            item.rev = self.revision

    def _set_transferred(self, item: TransferItem, value: int) -> None:
        value = max(0, int(value))
        if item.batch == self._batch:
            self._batch_bytes += value - item.transferred
        item.transferred = value

    def _sample(self) -> None:
        now = self._clock()
        self._samples.append((now, self._batch_bytes))
        while len(self._samples) > 2 and now - self._samples[0][0] > self.SPEED_WINDOW_S:
            self._samples.popleft()

    def _close_batch_if_idle(self) -> None:
        if self._batch_ended is None and not self.active:
            self._batch_ended = self._clock()

    # ---- mutation ----------------------------------------------------------
    def add(self, jobs: Iterable[Sequence[str]]) -> List[int]:
        """Queue *jobs* (``(src, dst, direction)``); returns their ids in order.

        Starts a new batch unless a transfer is still queued or running."""
        jobs = [tuple(job) for job in jobs]
        if not jobs:
            return []
        now = self._clock()
        if not self.active:
            self._batch += 1
            self._batch_started = now
            self._batch_ended = None
            self._batch_bytes = 0
            self._batch_count = 0
            self._samples.clear()
        ids = []
        for src, dst, direction in jobs:
            item = TransferItem(
                id=self._next_id, src=src, dst=dst, direction=direction,
                name=transfer_name(src), batch=self._batch, added_at=now,
            )
            self._next_id += 1
            self._items[item.id] = item
            self._batch_count += 1
            ids.append(item.id)
            self._touch(item)
        self._touch(structural=True)
        self._trim()
        return ids

    def start(self, item_id: int) -> None:
        item = self._items.get(item_id)
        if item is None or item.finished:
            return
        item.status = RUNNING
        item.started_at = self._clock()
        item.percent = 0
        self._set_transferred(item, 0)
        self._sample()
        self._touch(item)

    def progress(self, item_id: int, percent) -> None:
        item = self._items.get(item_id)
        if item is None or item.status != RUNNING:
            return
        try:
            percent = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return
        if percent == item.percent:
            return
        item.percent = percent
        if item.size is not None:
            self._set_transferred(item, item.size * percent // 100)
        self._sample()
        self._touch(item)

    def set_size(self, item_id: int, size, *, exact: bool = True, files=None) -> None:
        """Record *item_id*'s size (``None`` leaves it unknown)."""
        item = self._items.get(item_id)
        if item is None or size is None:
            return
        size = max(0, int(size))
        was_unknown = item.size is None
        # A worker's exact figure is never replaced by a later estimate.
        if not exact and item.size is not None and item.size_exact:
            return
        item.size, item.size_exact = size, bool(exact)
        if files is not None:
            item.files = int(files)
        if item.status == RUNNING:
            if was_unknown:
                # its bytes appear all at once: that isn't throughput
                self._samples.clear()
            self._set_transferred(item, size * item.percent // 100)
        elif item.status == DONE:
            self._set_transferred(item, size)
        self._touch(item)

    def finish(self, item_id: int, status: str, *, error: str = "", size=None,
               duration=None) -> None:
        item = self._items.get(item_id)
        if item is None or item.finished:
            return
        if status not in FINISHED:
            raise ValueError(f"not a finished status: {status!r}")
        now = self._clock()
        if item.started_at is None:
            item.started_at = now
        item.status = status
        item.finished_at = now
        item.error = str(error or "")
        if duration is not None:
            try:
                item.duration = max(0.0, float(duration))
            except (TypeError, ValueError):
                pass
        if size is not None:
            item.size, item.size_exact = max(0, int(size)), True
        if status == DONE:
            item.percent = 100
            self._set_transferred(item, item.size or 0)
        else:
            # only what arrived intact counts as transferred
            self._set_transferred(item, 0)
        self._sample()
        self._touch(item)
        self._close_batch_if_idle()

    def cancel_queued(self) -> int:
        """Mark every queued (not yet started) item cancelled; returns the count."""
        now = self._clock()
        count = 0
        for item in self._items.values():
            if item.status == QUEUED:
                item.status = CANCELLED
                item.finished_at = now
                self._touch(item)
                count += 1
        self._close_batch_if_idle()
        return count

    def remove(self, ids: Iterable[int]) -> int:
        """Drop finished items from the history (active ones are kept)."""
        removed = 0
        for item_id in list(ids):
            item = self._items.get(item_id)
            if item is None or item.active:
                continue
            if item.batch == self._batch:
                self._batch_bytes -= item.transferred
                self._batch_count -= 1
            del self._items[item_id]
            removed += 1
        if removed:
            self._touch(structural=True)
        return removed

    def clear_finished(self) -> int:
        return self.remove([item.id for item in self._items.values() if item.finished])

    def _trim(self) -> None:
        excess = len(self._items) - self.MAX_ITEMS
        if excess <= 0:
            return
        old = [item.id for item in self._items.values()
               if item.finished and item.batch != self._batch][:excess]
        if old:
            self.remove(old)

    # ---- aggregates --------------------------------------------------------
    def _speed(self, now: float, active: bool) -> float:
        if not active:
            start, end = self._batch_started, self._batch_ended
            if start is None or end is None or end <= start:
                return 0.0
            return self._batch_bytes / (end - start)
        if len(self._samples) >= 2:
            t0, b0 = self._samples[0]
            t1, b1 = self._samples[-1]
            if t1 - t0 >= 0.5:
                return max(0.0, (b1 - b0) / (t1 - t0))
        if self._batch_started is not None and now > self._batch_started:
            return self._batch_bytes / (now - self._batch_started)
        return 0.0

    def stats(self) -> TransferStats:
        """The current (or most recent) batch, summed up."""
        items = self.batch_items()
        now = self._clock()
        stats = TransferStats(total=len(items))
        if not items:
            return stats
        counts = Counter(item.status for item in items)
        stats.queued, stats.running = counts[QUEUED], counts[RUNNING]
        stats.done, stats.failed = counts[DONE], counts[FAILED]
        stats.cancelled = counts[CANCELLED]
        stats.directions = tuple(sorted({item.direction for item in items}))
        stats.bytes_done = self._batch_bytes
        # a cancelled-before-start item needs no size to finish the batch
        sized = [item for item in items if not (item.status == CANCELLED and item.size is None)]
        stats.measuring = sum(1 for item in sized if item.size is None)
        if stats.measuring == 0:
            stats.bytes_total = sum(item.size for item in sized)
            stats.bytes_estimated = any(not item.size_exact for item in sized)
        stats.by_size = bool(stats.bytes_total)
        if stats.by_size:
            processed = sum(
                (item.size or 0) if item.finished else item.transferred for item in sized
            )
            stats.fraction = processed / stats.bytes_total
        else:
            processed_items = sum(1 for item in items if item.finished) + sum(
                item.percent / 100.0 for item in items if item.status == RUNNING
            )
            stats.fraction = processed_items / len(items)
        stats.fraction = min(1.0, max(0.0, stats.fraction))
        if self._batch_started is not None:
            end = self._batch_ended if (self._batch_ended and not stats.active) else now
            stats.elapsed = max(0.0, end - self._batch_started)
        stats.speed = self._speed(now, stats.active)
        if stats.active and stats.by_size and stats.speed > 0:
            left = stats.bytes_total * (1.0 - stats.fraction)
            stats.eta = max(0.0, left / stats.speed)
        return stats

    def retryable(self, ids: Optional[Iterable[int]] = None) -> List[int]:
        """Ids of failed or cancelled items (of *ids*, when given)."""
        pool = self._items.values() if ids is None else (
            self._items[i] for i in ids if i in self._items)
        return [item.id for item in pool if item.status in (FAILED, CANCELLED)]

    def report(self) -> str:
        """A plain-text record of the history, for Copy / Save report."""
        now = self._clock()
        stats = self.stats()
        lines = ["TurboADB transfer report", "=" * 24]
        if stats.total:
            lines.append(
                f"Last batch: {stats.total} item(s) - {stats.done} done, "
                f"{stats.failed} failed, {stats.cancelled} cancelled, "
                f"{stats.queued + stats.running} still active"
            )
            if stats.bytes_total is not None:
                lines.append(
                    f"Transferred {human_bytes(stats.bytes_done)} of "
                    f"{'~' if stats.bytes_estimated else ''}{human_bytes(stats.bytes_total)}"
                    f" in {human_duration(stats.elapsed)}"
                    + (f" ({human_speed(stats.speed)})" if stats.speed else "")
                )
        lines.append("")
        for item in self._items.values():
            size = human_bytes(item.size) if item.size is not None else "size unknown"
            if item.size is not None and not item.size_exact:
                size = "~" + size
            took = item.elapsed(now)
            rate = human_speed(item.speed(now))
            detail = " ".join(bit for bit in (
                f"[{item.status}]", DIRECTION_LABELS.get(item.direction, item.direction),
                size, human_duration(took) if took is not None else "", rate) if bit)
            lines.append(f"{detail}: {item.src} -> {item.dst}")
            if item.error:
                lines.append(f"    error: {item.error}")
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# measuring (run on a worker thread)
# --------------------------------------------------------------------------- #
def _strip_merge(path: str) -> str:
    """``folder/.`` (a merge source) -> ``folder``."""
    text = str(path)
    while text.endswith(("/.", "\\.")):
        text = text[:-2]
    return text or str(path)


def measure_local(paths: Iterable[str]) -> Dict[str, Tuple[int, int]]:
    """``{path: (bytes, files)}`` for PC paths; unreadable ones are left out.

    Folders are walked without following links, like ``adb push`` copies them.
    """
    out: Dict[str, Tuple[int, int]] = {}
    for path in paths:
        real = os.path.normpath(_strip_merge(path))
        try:
            if os.path.isfile(real):
                out[path] = (os.path.getsize(real), 1)
                continue
            if not os.path.isdir(real):
                continue
            total = files = 0
            stack = [real]
            while stack:
                current = stack.pop()
                try:
                    with os.scandir(current) as entries:
                        for entry in entries:
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    stack.append(entry.path)
                                elif entry.is_file(follow_symlinks=False):
                                    total += entry.stat(follow_symlinks=False).st_size
                                    files += 1
                            except OSError:
                                continue
                except OSError:
                    continue
            out[path] = (total, files)
        except OSError:
            continue
    return out


_MEASURE_CHUNK = 100
_MEASURE_LINE_RE = re.compile(r"^@@(\d+) ([DFN])(?: (\d+))?\s*$")


def measure_remote(handler, paths: Sequence[str], *, timeout: float = 120.0
                   ) -> Dict[str, Tuple[int, bool]]:
    """``{path: (bytes, exact)}`` for device paths; unknown ones are left out.

    One shell call per chunk of paths: a file's size comes from ``stat`` and
    is exact, a folder's from ``du -sk`` and is an estimate (blocks, not bytes)
    until the finished pull measures the real thing on this PC."""
    out: Dict[str, Tuple[int, bool]] = {}
    if handler is None or not paths:
        return out
    paths = list(paths)
    for start in range(0, len(paths), _MEASURE_CHUNK):
        chunk = paths[start:start + _MEASURE_CHUNK]
        parts = []
        for index, path in enumerate(chunk):
            quoted = shlex.quote(_strip_merge(path).rstrip("/") or "/")
            parts.append(
                f"if [ -d {quoted} ]; then echo \"@@{index} D $(du -sk {quoted} 2>/dev/null"
                f" | cut -f1)\"; elif [ -e {quoted} ]; then echo \"@@{index} F $(stat -c %s "
                f"{quoted} 2>/dev/null)\"; else echo \"@@{index} N\"; fi"
            )
        try:
            res = handler.shell("; ".join(parts), timeout=timeout, safe=False)
        except Exception:
            continue
        text = getattr(res, "stdout", "") or ""
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        for line in text.splitlines():
            match = _MEASURE_LINE_RE.match(line.strip())
            if not match or match.group(3) is None:
                continue
            index = int(match.group(1))
            if index >= len(chunk):
                continue
            number = int(match.group(3))
            if match.group(2) == "D":
                out[chunk[index]] = (number * 1024, False)
            elif match.group(2) == "F":
                out[chunk[index]] = (number, True)
    return out
