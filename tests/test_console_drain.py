"""A flood reaches the console in batches of up to a megabyte (the reader
coalesces them).  The render tick used to take 16 KB of such a batch at a
time and put the rest back as one string: one slice ran past the tick's
30 ms budget (typing and Stop waited 60-120 ms during a flood) and the rest
was copied again for every slice.  A batch is now cut into small slices
once, so the budget is checked every few milliseconds.

A slice of short lines (``yes``, ``seq``) still ran far past the budget: a
line costs about the same to draw however short it is, and 8 KB of ``y`` is
2,730 of them.  A slice is cut at a number of lines as well, and lines that
need no colour of their own go into the view together, as one insert."""

import itertools
import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtGui import QFont  # noqa: E402


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
        first = ("a" * 99 + "\n") * 200  # (one line that long is cut in the view)
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


def test_a_slice_of_short_lines_is_cut_at_a_number_of_lines(qapp, monkeypatch):
    term = _console()
    try:
        text = "y\r\n" * 20000
        term.feed(text)
        with monkeypatch.context() as patch:
            _one_item_per_tick(patch)
            term._drain_tick()
        drawn = term.toPlainText()
        assert 0 < drawn.count("\n") <= term._SLICE_LINES <= 512  # 8 KB of it is 2,730 lines
        assert drawn == "y\n" * drawn.count("\n")  # cut at a line break
        assert term._inq_len == len(text) - 3 * drawn.count("\n")
        assert all(isinstance(piece, str) and len(piece) <= term._SUB for piece in term._inq)
        while term._inq:
            term._drain_tick()
        assert term._inq_len == 0
        assert term.toPlainText() == "y\n" * 20000
    finally:
        term.close_archive()
        term.deleteLater()


def test_lines_without_a_colour_of_their_own_go_into_the_view_together(qapp):
    """An insert a line made a flood of short lines twenty times as slow as
    the same text in one piece."""
    term = _console()
    try:
        edits = []
        term.document().contentsChange.connect(lambda *change: edits.append(change))
        term.feed("".join(f"line {i}\r\n" for i in range(2000)))
        while term._inq:
            term._drain_tick()
        assert term.toPlainText() == "".join(f"line {i}\n" for i in range(2000))
        assert len(edits) <= 2000 // 50
    finally:
        term.close_archive()
        term.deleteLater()


def _look(term):
    """Every line of the view as ``(text, colour, bold)`` pieces."""
    lines = []
    block = term.document().firstBlock()
    while block.isValid():
        pieces = []
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            look = (fmt.foreground().color().name(), fmt.fontWeight() > QFont.Normal)
            if pieces and pieces[-1][1:] == look:
                pieces[-1] = (pieces[-1][0] + fragment.text(),) + look
            else:
                pieces.append((fragment.text(),) + look)
            it += 1
        lines.append(pieces)
        block = block.next()
    return lines


# Output of every kind the console draws line by line.
_MIXED = [
    "plain line one", "plain line two", "ls: /data: Permission denied", "plain again",
    "09-29 16:42:56.210  5927  5927 E Finsky  : crashed", "09-29 16:42:56.211  5927  5927 I Finsky  : fine",
    "09-29 16:42:56.212  5927  5927 W Finsky  : slow", "\x1b[32mgreen\x1b[0m and plain", "50%\r75%\r100%",
    "", "", "V2318:/ $ ls", "acct", "[ 09-29 16:42:56.210  5927: 5927 E/AndroidRuntime ]", "FATAL EXCEPTION",
    "\tat Main.run", "", "after the entry", "x" * 300 + " failed", "\x1b[1mbold on", "still bold",
    "\x1b[0mback to plain", "abc\bX", "Success", "wide \x1b[5Cgap", "last line", "0 errors", "y", "y", "y",
    "processor\t: 0", "emulator-5554\toffline", "0123456789\rab\tcd",
]


@pytest.mark.parametrize("size", [None, 1, 7, 64, 1000])
def test_lines_drawn_together_look_as_they_do_drawn_one_by_one(qapp, size):
    """*size*: characters a read; None for all of it in one read."""
    output = "\r\n".join(_MIXED * 12) + "\r\nand a line without its end"
    one_by_one = _console()
    together = _console()
    try:
        for line in output.split("\r\n")[:-1]:
            one_by_one.feed(line + "\r\n")
            while one_by_one._inq:
                one_by_one._drain_tick()
        one_by_one.feed(output.split("\r\n")[-1])
        while one_by_one._inq:
            one_by_one._drain_tick()
        if size is None:
            together.feed(output)
        else:
            for start in range(0, len(output), size):
                together.feed(output[start:start + size])
                if start % (size * 5) == 0:
                    while together._inq:
                        together._drain_tick()
        while together._inq:
            together._drain_tick()
        assert together.toPlainText() == one_by_one.toPlainText()
        assert _look(together) == _look(one_by_one)
    finally:
        for term in (one_by_one, together):
            term.close_archive()
            term.deleteLater()
