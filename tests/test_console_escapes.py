"""Escape sequences the terminal's output lines got wrong.

An OSC or DCS string without its terminator (a window title sent without
its BEL) swallowed every prompt and line after it; a parameter in digits
other than ASCII ones (``ESC[²C``) raised and lost the rest of the output; a
huge insert count made a gigabyte of blanks; and ED 1 (erase to the cursor)
deleted the whole scrollback.  A string now ends at the next escape too, or
after :data:`console._STRING_MAX` characters; parameters are ASCII numbers;
an insert is never wider than the view; and ED 1 blanks the start of the
cursor's line only, as EL 1 does."""

import time

import pytest

pytest.importorskip("PyQt5")

PROMPT = "V2318:/ $ "


def _console(pty=True):
    from turboadb.gui.console import AnsiConsole

    term = AnsiConsole(send_fn=lambda _data: None)
    term.set_emulate_prompt(False)
    if pty:
        term.set_screen_input(lambda _data: True)
    term.resize(900, 500)
    return term


def _render(term):
    while term._inq:
        term._drain_tick()


def _close(term):
    term.close_archive()
    term.deleteLater()


def _show(term, pieces):
    """Feed *pieces*, each as a read of its own, and draw them."""
    for piece in pieces:
        term.feed(piece)
        _render(term)
    return term.toPlainText()


# --------------------------------------------------------------------------- #
# strings: OSC, DCS, SOS, PM, APC
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pty", [True, False])
@pytest.mark.parametrize("pieces", [
    # one read: complete sequences are read in one go
    ["\x1b]0;title\r\n\x1b[1m" + PROMPT + "\x1b[0mls\r\nacct\r\n"],
    # the string cut off by the end of a read: the parser's states
    ["\x1b]0;title", "\r\n\x1b[1m" + PROMPT + "\x1b[0m", "ls\r\nacct\r\n"],
    ["\x1b]0;title\x1b", "[1m" + PROMPT + "\x1b[0mls\r\nacct\r\n"],
    ["\x1bP1$r", "\x1b[1m" + PROMPT + "\x1b[0mls\r\nacct\r\n"],  # a DCS
])
def test_a_string_without_its_end_ends_at_the_next_escape(qapp, pty, pieces):
    term = _console(pty)
    try:
        text = _show(term, pieces)
        assert text.endswith(PROMPT + "ls\nacct\n")
        assert "title" not in text and "1$r" not in text
        assert not term.has_screen()
    finally:
        _close(term)


@pytest.mark.parametrize("pieces", [
    ["\x1b]0;title\x07ok"],
    ["\x1b]0;title\x1b\\ok"],
    ["\x1b]0;ti", "tle\x07ok"],
    ["\x1b]0;title\x1b", "\\ok"],
    ["\x1b]8;;http://example.com\x1b\\ok\x1b]8;;\x1b\\"],  # a hyperlink around its text
])
def test_a_string_still_ends_at_bel_or_st(qapp, pieces):
    term = _console()
    try:
        assert _show(term, pieces) == "ok"
    finally:
        _close(term)


@pytest.mark.parametrize("split", [False, True])
def test_a_string_that_never_ends_stops_swallowing(qapp, split):
    from turboadb.gui import console

    body = "0;" + "x" * 5000
    output = "\x1b]" + body + "\r\nstill here\r\n"
    pieces = [output[i:i + 700] for i in range(0, len(output), 700)] if split else [output]
    term = _console()
    try:
        text = _show(term, pieces)
        assert text.endswith("\nstill here\n")
        # the string's first characters went, the rest is output
        assert text.count("x") == len(body) - console._STRING_MAX
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# parameters
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, shown", [
    ("abcdef\r\x1b[\u00b2Cx", "axcdef"),     # "²" is no number: a count of 1
    ("abcdef\r\x1b[\u0663Cx", "axcdef"),     # nor an Arabic-Indic three
    ("abcdef\x1b[\u00b2Gx", "xbcdef"),       # a column
    ("ab\x1b[?\u00b2hcd", "abcd"),           # a private mode
    ("ab\x1b[?25;\u00b2lcd", "abcd"),
])
def test_parameters_in_other_digits_neither_raise_nor_lose_output(qapp, text, shown):
    term = _console()
    try:
        term.feed(text + "\r\nnext line\r\n")
        _render(term)
        assert term.toPlainText() == shown + "\nnext line\n"
    finally:
        _close(term)


def test_a_huge_insert_count_is_no_wider_than_the_view(qapp):
    term = _console()
    try:
        started = time.monotonic()
        term.feed("abcdef\x1b[2G\x1b[1000000000@X")
        _render(term)
        assert time.monotonic() - started < 2.0
        cols = term.cell_size()[0]
        assert cols > 10
        assert term.document().firstBlock().text() == "aX" + " " * (cols - 1) + "bcdef"
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# erasing up to the cursor
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pty", [True, False])
def test_erasing_to_the_cursor_keeps_the_lines_above(qapp, pty):
    term = _console(pty)
    try:
        term.feed("line 1\r\nline 2\r\nline 3\r\nabcdefgh\x1b[5G\x1b[1J")
        _render(term)
        assert term.toPlainText() == "line 1\nline 2\nline 3\n     fgh"
        term.feed("X")  # the cursor stayed where it was
        _render(term)
        assert term.toPlainText() == "line 1\nline 2\nline 3\n    Xfgh"
        assert not term.has_screen()
    finally:
        _close(term)
