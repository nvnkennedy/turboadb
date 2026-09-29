"""The terminals' reader thread and scrollback archive.

The reader hands output to the GUI thread as soon as it is read (a blocking
read must not hold a large burst back until the shell prints again) and
coalesces a flood into a few signals (Windows pipes deliver ~4 KB per read).
The archive keeps one CR per line when adb.exe doubles it (CR CR LF)."""

from __future__ import annotations

import queue
import threading
import time

import pytest

pytest.importorskip("PyQt5")


def _pump(qapp, done, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if done():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return done()


def _reader(read_fn):
    from turboadb.gui.terminal import ReaderThread

    reader = ReaderThread(read_fn, decode=False)
    events = []
    reader.data.connect(lambda data: events.append(data))
    reader.closed.connect(lambda: events.append(None))
    return reader, events


def _stop(reader, release=None):
    reader.stop()
    if release is not None:
        release.set()
    assert reader.wait(3000)


def test_a_large_read_is_delivered_while_the_next_read_blocks(qapp):
    """On Linux/macOS the read blocks until the shell prints again."""
    release = threading.Event()
    chunks = [b"x" * 20000, b"y" * 20000]

    def read():
        if chunks:
            return chunks.pop(0)
        release.wait(10)  # a blocking pipe read with nothing to read
        return None

    reader, events = _reader(read)
    reader.start()
    try:
        assert _pump(qapp, lambda: sum(len(e) for e in events if e) == 40000, 3.0)
    finally:
        _stop(reader, release)
    assert b"".join(e for e in events if e) == b"x" * 20000 + b"y" * 20000


def test_a_flood_of_small_reads_becomes_a_few_signals(qapp):
    """One signal per 4 KB read flooded the GUI thread's event queue."""
    chunks = [bytes([65 + i % 26]) * 4096 for i in range(400)]
    feed = iter(chunks)
    reader, events = _reader(lambda: next(feed, None))
    reader.start()
    try:
        assert _pump(qapp, lambda: events and events[-1] is None, 10.0)
    finally:
        _stop(reader)
    data = [e for e in events if e is not None]
    assert b"".join(data) == b"".join(chunks)
    assert len(data) < 40


def test_a_steady_stream_is_batched_by_time(qapp):
    def read():
        if time.monotonic() - started > 0.4:
            return None
        pause = time.perf_counter() + 0.001  # spin: sleep() may take 15 ms on Windows
        while time.perf_counter() < pause:
            pass
        return b"z" * 4096

    reader, events = _reader(read)
    started = time.monotonic()
    reader.start()
    try:
        assert _pump(qapp, lambda: events and events[-1] is None, 10.0)
    finally:
        _stop(reader)
    data = [e for e in events if e is not None]
    reads = sum(len(e) for e in data) // 4096
    assert reads > 20
    assert len(data) <= reads // 2  # the old reader emitted one signal per read


def test_interactive_output_is_not_delayed_and_closed_comes_last(qapp):
    pending = queue.Queue()

    def read():
        try:
            return pending.get(timeout=0.02)
        except queue.Empty:
            return b""

    reader, events = _reader(read)
    reader.start()
    try:
        for expected in (b"C:\\w>", b"dir\r\n", b" Volume in drive C\r\n"):
            sent_at = time.monotonic()
            pending.put(expected)
            assert _pump(qapp, lambda: expected in events, 2.0)
            assert time.monotonic() - sent_at < 0.5
        assert events == [b"C:\\w>", b"dir\r\n", b" Volume in drive C\r\n"]
        pending.put(b"last\r\n")
        pending.put(None)
        assert _pump(qapp, lambda: events and events[-1] is None, 3.0)
    finally:
        _stop(reader)
    assert events[-2:] == [b"last\r\n", None]


def test_archive_keeps_one_carriage_return_per_line(qapp):
    from PyQt5.QtWidgets import QPlainTextEdit
    from turboadb.gui.scrollback import Scrollback

    sb = Scrollback(QPlainTextEdit())
    try:
        for chunk in ("ls\r\r\n", "acct\r", "\r\n", "bin\r\r", "\n", "50%\r", "60%\n", "\r\r\r\n"):
            sb.archive(chunk)
        assert sb.full_text() == "ls\r\nacct\r\nbin\r\n50%\r60%\n\r\n"
        sb.archive("tail\r")
        sb.reset()
        sb.archive("\nfresh\n")  # the CR before the reset is gone with it
        assert sb.full_text() == "\nfresh\n"
    finally:
        sb.close()


def test_archive_on_disk_keeps_one_carriage_return_per_line(qapp, tmp_path):
    from PyQt5.QtWidgets import QPlainTextEdit
    from turboadb.gui.scrollback import Scrollback

    sb = Scrollback(QPlainTextEdit())
    sb._MEMORY_LIMIT = 8  # spill to the history file almost at once
    try:
        for index in range(50):
            sb.archive(f"line {index}\r")
            sb.archive("\r\n")
        out = tmp_path / "saved.log"
        sb.save_to(str(out))
        saved = out.read_bytes()
        assert b"\r\r" not in saved
        assert saved.count(b"\r\n") == 50
    finally:
        sb.close()
