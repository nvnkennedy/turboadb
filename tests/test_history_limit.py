"""A terminal's or a logcat capture's history file has a size limit.

An unattended ``adb logcat`` or ``ping -t`` used to write gigabytes into
~/.turboadb/logs until the tab closed.  The history is now kept in two parts:
once the newer one is full the older one goes, and so it does early while the
disk is nearly full.  A save says what went, logcat's filter keeps its line
numbering across the drop, and a disk too slow for the output never fills
memory: the terminal marks what it could not keep, logcat's reader waits."""

import glob
import os
import re
import threading
import time

import pytest

pytest.importorskip("PyQt5")

_MB = 1024 * 1024


def _pump(qapp, until, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return until()


@pytest.fixture(autouse=True)
def private_logs(monkeypatch, tmp_path):
    """This test's own logs folder: other tests' history files never count."""
    from turboadb.gui import scrollback as sb_mod

    folder = tmp_path / "logs"
    monkeypatch.setattr(sb_mod, "user_path", lambda *parts: str(tmp_path.joinpath(*parts)))
    return folder


def _scrollback(limit=None):
    from PyQt5.QtWidgets import QPlainTextEdit
    from turboadb.gui.scrollback import Scrollback

    sb = Scrollback(QPlainTextEdit())
    sb._MEMORY_LIMIT = 8  # spill to the history file at once
    if limit is not None:
        sb._history_limit = lambda: limit
    return sb


def _history_files():
    from turboadb.gui import scrollback as sb_mod

    return glob.glob(os.path.join(sb_mod.user_path("logs"), "turboadb-*.log"))


def _lines(count, width=40):
    return [f"line {i:05d} " + "x" * width for i in range(count)]


def test_the_history_file_keeps_to_its_limit_and_a_save_says_what_went(qapp, tmp_path):
    sb = _scrollback(limit=4000)  # two parts of 2000 bytes
    lines = _lines(1000)
    try:
        for line in lines:
            sb.archive(line + "\n")
        assert sb._flush(10)
        files = _history_files()
        assert len(files) <= 2
        assert sum(os.path.getsize(f) for f in files) <= 4000
        skipped, kept = sb.archived_lines()
        kept = list(kept)
        assert skipped > 0 and kept == lines[skipped:]  # nothing lost between the parts
        out = tmp_path / "saved.log"
        sb.save_to(str(out))
        first, rest = out.read_text(encoding="utf-8").split("\n", 1)
        assert first.startswith(f"[TurboADB: the oldest {skipped:,} lines (")
        assert "were dropped to keep its file under" in first
        assert rest.splitlines() == lines[skipped:]
        assert sb.full_text() == out.read_text(encoding="utf-8")
    finally:
        sb.close()
    assert _pump(qapp, lambda: not _history_files())  # every part goes with the tab


def test_a_nearly_full_disk_drops_the_older_part_early(qapp, tmp_path, monkeypatch):
    sb = _scrollback(limit=0)  # no size limit: only the free space counts
    sb._SPACE_CHECK = 1000
    monkeypatch.setattr(sb, "_low_on_space", lambda folder: True)
    lines = _lines(1000)
    try:
        for line in lines:
            sb.archive(line + "\n")
        assert sb._flush(10)
        assert sum(os.path.getsize(f) for f in _history_files()) < 4000
        out = tmp_path / "saved.log"
        sb.save_to(str(out))
        first, rest = out.read_text(encoding="utf-8").split("\n", 1)
        assert first.endswith("were dropped to keep the disk from filling up]")
        assert rest.splitlines() == lines[-len(rest.splitlines()):]
    finally:
        sb.close()


def test_without_a_limit_or_a_full_disk_the_history_is_complete(qapp, tmp_path):
    sb = _scrollback(limit=0)
    lines = _lines(2000)
    try:
        for line in lines:
            sb.archive(line + "\n")
        out = tmp_path / "saved.log"
        sb.save_to(str(out))
        assert out.read_text(encoding="utf-8").splitlines() == lines
        assert len(_history_files()) == 1
    finally:
        sb.close()


def test_the_limit_is_a_setting(qapp):
    from turboadb.gui import settings as settings_mod
    from turboadb.gui.scrollback import Scrollback

    sb = _scrollback()
    try:
        assert sb._history_limit() == Scrollback.HISTORY_LIMIT_MB * _MB == 1024 * _MB
        settings_mod.set("history_limit_mb", 256)
        assert sb._history_limit() == 256 * _MB
        settings_mod.set("history_limit_mb", 0)
        assert sb._history_limit() == 0  # no limit
        settings_mod.set("history_limit_mb", 1)
        assert sb._history_limit() == 16 * _MB  # never uselessly small
        settings_mod.set("history_limit_mb", "lots")
        assert sb._history_limit() == 1024 * _MB
    finally:
        sb.close()


@pytest.fixture
def slow_disk(monkeypatch):
    """The history writer stays stuck creating its file until released."""
    from turboadb.gui import scrollback as sb_mod

    gate = threading.Event()
    real = sb_mod.tempfile.mkstemp

    def mkstemp(*args, **kwargs):
        gate.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(sb_mod.tempfile, "mkstemp", mkstemp)
    yield gate
    gate.set()


def test_terminal_output_the_disk_could_not_keep_up_with_is_marked_where_it_went(
        qapp, tmp_path, slow_disk):
    sb = _scrollback()
    sb._QUEUE_LIMIT = 100
    try:
        sb.archive("start\n")
        sb.archive("a" * 50 + "\n")
        sb.archive("b" * 50 + "\n")  # the writer is 100 characters behind: dropped
        sb.archive("c" * 50 + "\n")
        slow_disk.set()
        assert sb._flush(10)
        sb.archive("after\n")
        out = tmp_path / "saved.log"
        sb.save_to(str(out))
        assert out.read_text(encoding="utf-8") == (
            "start\n" + "a" * 50 + "\n"
            + "[TurboADB: 102 characters of output are missing here: the disk did not keep up]\n"
            + "after\n"
        )
    finally:
        sb.close()


def test_output_dropped_last_is_still_noted(qapp, tmp_path, slow_disk):
    sb = _scrollback()
    sb._QUEUE_LIMIT = 70
    try:
        sb.archive("start ")
        sb.archive("a" * 60)
        sb.archive("b" * 60)  # dropped, and nothing comes after it
        slow_disk.set()
        text = sb.full_text()
        assert text == ("start " + "a" * 60
                        + "\n[TurboADB: 60 characters of output are missing here: "
                          "the disk did not keep up]\n")
    finally:
        sb.close()


def test_a_logcat_capture_waits_for_a_slow_disk_and_keeps_every_line(qapp, slow_disk):
    from turboadb.gui.logcat_view import _Capture

    sb = _scrollback()
    sb._QUEUE_LIMIT = 100
    capture = _Capture(sb)
    lines = _lines(20)
    done = threading.Event()

    def reader():
        for line in lines:
            capture.add([line], cancelled=lambda: False)
        done.set()

    thread = threading.Thread(target=reader, daemon=True)
    try:
        thread.start()
        assert not done.wait(0.5)  # held back by the writer, not dropping lines
        assert capture.position()[0] < len(lines)
        slow_disk.set()
        assert done.wait(10)
        skipped, kept = sb.archived_lines()
        assert skipped == 0 and list(kept) == lines
        assert capture.position()[0] == len(lines)
    finally:
        slow_disk.set()
        thread.join(10)
        sb.close()


def test_a_stopped_capture_stops_waiting(qapp, slow_disk):
    from turboadb.gui.logcat_view import _Capture

    sb = _scrollback()
    sb._QUEUE_LIMIT = 130  # two lines fit, the third waits
    capture = _Capture(sb)
    stop = threading.Event()
    try:
        capture.add(["x" * 60], cancelled=stop.is_set)  # spills: the writer is stuck
        capture.add(["y" * 60], cancelled=stop.is_set)
        waiting = threading.Thread(
            target=lambda: capture.add(["z" * 60], cancelled=stop.is_set), daemon=True)
        waiting.start()
        waiting.join(0.3)
        assert waiting.is_alive()
        stop.set()
        waiting.join(5)
        assert not waiting.is_alive()
        assert capture.position()[0] == 3  # archived all the same: nothing is dropped
    finally:
        slow_disk.set()
        sb.close()


def test_the_logcat_filter_keeps_its_split_after_older_lines_went(qapp):
    from turboadb.gui.logcat_view import LogcatPanel

    p = LogcatPanel(None)
    p._sb._MEMORY_LIMIT = 8
    p._sb._history_limit = lambda: 40000  # parts of 20 KB
    lines = [f"09-27 10:00:00.{i % 1000:03d}  1234  5678 I Tag: "
             f"{'NEEDLE' if i % 50 == 0 else 'noise'} #{i:05d}" for i in range(4000)]
    try:
        for start in range(0, len(lines), 100):
            p._on_batch(lines[start:start + 100])
        assert p._sb._flush(10)
        p.filt.setText("needle")
        p._refilter_view()
        assert _pump(qapp, lambda: not p._searching)
        shown = p.view.toPlainText().splitlines()
        match = re.match(r"… \(the oldest (\d+) lines of this capture were dropped", shown[0])
        assert match, shown[:2]
        skipped = int(match.group(1))
        assert 0 < skipped < len(lines)
        assert shown[1:] == [ln for ln in lines[skipped:] if "NEEDLE" in ln]
        late = "09-27 10:00:01.000  1234  5678 I Tag: NEEDLE late"
        p._on_batch(["09-27 10:00:01.000  1234  5678 I Tag: noise late", late])
        p._render_pending()
        shown = p.view.toPlainText().splitlines()
        assert shown[-1] == late and shown.count(late) == 1  # the live stream, once
        assert len(set(shown)) == len(shown)  # nothing drawn twice across the split
    finally:
        p.close_panel()
        p.deleteLater()
