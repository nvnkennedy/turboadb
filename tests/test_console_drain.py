"""A flood reaches the console in batches of up to a megabyte (the reader
coalesces them).  The render tick used to take 16 KB of such a batch at a
time and put the rest back as one string: one slice ran past the tick's
30 ms budget (typing and Stop waited 60-120 ms during a flood) and the rest
was copied again for every slice.  A batch is now cut into small slices
once, so the budget is checked every few milliseconds."""

import itertools
import time

import pytest

pytest.importorskip("PyQt5")


def _one_item_per_tick(monkeypatch):
    """A clock for the console on which every render tick has time for
    exactly one queued item."""
    from turboadb.gui import console as console_mod

    ticks = itertools.cycle([0.0, 0.0, 1.0])  # deadline, one check passes, the next fails

    class Clock:
        monotonic = staticmethod(time.monotonic)
        strftime = staticmethod(time.strftime)

        @staticmethod
        def perf_counter():
            return next(ticks)

    monkeypatch.setattr(console_mod, "time", Clock)


def _console():
    from turboadb.gui.console import AnsiConsole

    term = AnsiConsole(send_fn=lambda _data: None)
    term.set_emulate_prompt(False)
    return term


def test_a_big_batch_is_cut_into_small_slices_once(qapp, monkeypatch):
    term = _console()
    try:
        lines = [f"line {i:06d} with some padding text" for i in range(30000)]
        text = "\r\n".join(lines) + "\r\n"          # ~1 MB, as one reader batch
        term.feed(text)
        assert list(term._inq) == [text]
        with monkeypatch.context() as patch:
            _one_item_per_tick(patch)
            term._drain_tick()
        pieces = list(term._inq)
        assert pieces and all(isinstance(p, str) and len(p) <= term._SUB for p in pieces)
        assert term._SUB <= 8 * 1024  # ~20 ms of rendering at most
        assert term._inq_len == sum(len(p) for p in pieces)
        drawn = len(text) - term._inq_len
        assert 0 < drawn <= term._SUB
        while term._inq:
            term._drain_tick()
        assert term._inq_len == 0
        assert term.toPlainText() == "\n".join(lines) + "\n"
    finally:
        term.close_archive()
        term.deleteLater()


def test_markers_between_batches_keep_their_place(qapp, monkeypatch):
    term = _console()
    try:
        first = "a" * 20000 + "\n"
        term.feed(first)
        term.notice("between")
        term.feed("tail\n")
        with monkeypatch.context() as patch:
            _one_item_per_tick(patch)
            term._drain_tick()
        while term._inq:
            term._drain_tick()
        assert term.toPlainText() == first + "between\ntail\n"
    finally:
        term.close_archive()
        term.deleteLater()
