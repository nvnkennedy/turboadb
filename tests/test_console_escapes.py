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


# --------------------------------------------------------------------------- #
# garbage that must cost no more than its length
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("output", [
    "\x1b]" * 8192 + "\x07",                      # strings that never end, one BEL at last
    ("\x1b]" * 1024 + "\x07\r\n") * 8,           # the same, a line at a time
    "\x1bP" * 8192 + "\x1b\\",                    # DCS strings, one ST at last
    "\x1b_" * 8192,                               # ... and none at all
], ids=["osc", "osc lines", "dcs", "apc"])
def test_a_read_full_of_string_starts_is_taken_in_at_once(qapp, output):
    """Looking for a sequence cut off at the end of a read went over the
    whole read once for every string start in it: 16 KB took ten seconds."""
    term = _console()
    try:
        started = time.monotonic()
        term.feed(output)
        term.feed("\x1b\\still here\r\n")
        assert time.monotonic() - started < 2.0
        _render(term)
        assert term.toPlainText().endswith("still here\n")
        assert term._sb.full_text().endswith("still here\n")
    finally:
        _close(term)


@pytest.mark.parametrize("output", [
    "\x1bP" * 16384,                      # DCS strings that never end
    ("\x1bP" * 4095 + "\x1b\\") * 16,     # ... one ST at the end of every slice
], ids=["dcs", "dcs ended"])
def test_a_programs_screen_takes_a_read_full_of_string_starts_in_at_once(qapp, output):
    """A program's screen looked for the end of every string start too, once
    for each of them: 64 KB held the window for seconds."""
    term = _console()
    try:
        term.feed("\x1b[?1049h\x1b[Hframe")
        _render(term)
        assert term.has_screen()
        started = time.monotonic()
        term.feed(output)
        term.feed("\x1b\\\x1b[H\x1b[2Jstill here")
        _render(term)
        assert time.monotonic() - started < 2.0
        assert term.screen().display()[0] == "still here"
    finally:
        _close(term)


def test_a_sequence_cut_off_by_a_long_read_stays_out_of_the_saved_history(qapp):
    term = _console()
    try:
        for piece in ("one\r\n" + "x" * 6000 + "\x1b]0;ti", "tle\x07two\r\n\x1b[3", "1mthree\x1b[0m\r\n"):
            term.feed(piece)
        assert term._sb.full_text() == "one\n" + "x" * 6000 + "two\nthree\n"
    finally:
        _close(term)


@pytest.mark.parametrize("text, shown", [
    ("abcdef\r\x1b[" + "9" * 5000 + "Cx", "axcdef"),   # no number int() takes: a count of 1
    ("abcdef\x1b[" + "9" * 5000 + "Gx", "xbcdef"),
    ("ab\x1b[?" + "9" * 5000 + "hcd", "abcd"),         # a private mode
    ("ab\x1b[?25;" + "9" * 5000 + "lcd", "abcd"),
], ids=["right", "column", "mode", "modes"])
def test_a_parameter_of_thousands_of_digits_neither_raises_nor_loses_output(qapp, text, shown):
    term = _console()
    try:
        term.feed(text + "\r\nnext line\r\n")
        _render(term)
        assert term.toPlainText() == shown + "\nnext line\n"
    finally:
        _close(term)
