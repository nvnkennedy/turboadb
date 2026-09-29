"""A flood of output while a program has the screen.

The console never capped what waited for a program's screen (forty reads of
a megabyte queued 42 MB, drawn for minutes after the program was done),
Ctrl+C and Stop could not drop any of it, and the up to 80,000 rows that
had scrolled off the screen were written back in one go when it went (six
seconds with the window frozen).  Now the screen's backlog is capped like
the output lines' and keeps the newest output (the program redraws), never
with half a sequence and never past a switch of screens; Ctrl+C and Stop
drop it too, the key still going to the program; and a few thousand
scrolled rows go back at once."""

import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QKeyEvent  # noqa: E402

PROMPT = "V2318:/ $ "


def _console(sent=None):
    from turboadb.gui.console import AnsiConsole

    term = AnsiConsole(send_fn=lambda _data: None)
    term.set_emulate_prompt(False)
    term.set_screen_input(lambda data: sent.append(data) is None if sent is not None else True)
    term.resize(800, 400)
    term.show()
    return term


def _render(term):
    while term._inq:
        term._drain_tick()


def _close(term):
    term.close_archive()
    term.deleteLater()


def top_frame(n, cols=40):
    """One refresh of toybox top (see test_terminal_screen.top_frame)."""
    out = "\x1b[H\x1b[J" + f"Tasks: {n} total\r\nMem: 3.6G\r\n"
    out += "\x1b[7m" + "  PID USER".ljust(cols) + "\x1b[0m\r\n"
    return out + "\n".join(f"{1000 + n + i:5d} shell top\r" for i in range(3))


def _enter_top(term):
    term.feed(PROMPT)
    term.mark_prompt()
    term.feed("top\r\n\x1b[?25l" + top_frame(1))
    _render(term)
    assert term.has_screen()


# --------------------------------------------------------------------------- #
# the cap
# --------------------------------------------------------------------------- #
def test_a_flood_on_the_screen_is_capped_and_the_newest_frame_shows(qapp):
    term = _console()
    try:
        _enter_top(term)
        term._render_rate = 1  # the smallest cap
        cap = term._backlog_cap()
        number = 1
        for _read in range(4):  # reads of a megabyte, as the reader hands a flood over
            frames, size = [], 0
            while size < 1 << 20:
                number += 1
                frames.append(top_frame(number))
                size += len(frames[-1])
            term.feed("".join(frames))
            assert term._inq_len <= cap
        _render(term)
        rows = term.screen().display()
        assert rows[0] == f"Tasks: {number} total" and rows[1] == "Mem: 3.6G"
        assert rows[2].startswith("  PID USER") and rows[3].startswith(f"{1000 + number:5d}")
        term.feed("\x1b[?25h\x1b[999H\x1b[K" + PROMPT)
        term.mark_prompt()
        _render(term)
        assert not term.has_screen()
        assert "skipped on screen" not in term.toPlainText()  # it redraws: nothing to say
        saved = term._sb.full_text()
        assert "Tasks: 2 total" in saved and f"Tasks: {number} total" in saved
    finally:
        _close(term)


def test_a_sequence_cut_by_the_drop_leaves_no_garbage(qapp):
    """The screen held the start of a sequence whose rest was in what went:
    joined to the output kept, it printed its parameters as text."""
    term = _console()
    try:
        term.feed("\x1b[?1049h\x1b[H\x1b[2J\x1b[3;1H\x1b[5;")  # a read ends in a sequence
        _render(term)
        assert term.screen().alt_active
        term._render_rate = 1
        cap = term._backlog_cap()
        term.feed("1H" + "".join(f"\x1b[1;1Hline {n:06d}" for n in range(30000)))
        assert term._inq_len <= cap
        _render(term)
        assert term.screen().display() == ["line 029999"] + [""] * (term.screen().rows - 1)
    finally:
        _close(term)


def test_the_cap_never_drops_a_switch_of_screens(qapp):
    """vi leaves the alternate screen and a flood of lines follows, before
    any of it is drawn: without its switch the screen would stay up and the
    lines would be drawn on it."""
    term = _console()
    try:
        term.feed("vi x\r\n\x1b[?1049h\x1b[H\x1b[2J~")
        _render(term)
        assert term.screen().alt_active
        term._render_rate = 1
        lines = "".join(f"line {n:06d}\r\n" for n in range(25000))
        assert len(lines) > term._backlog_cap()
        term.feed("\x1b[H~\x1b[?1049l" + lines)
        _render(term)
        assert not term.has_screen()
        text = term.toPlainText()
        assert text.startswith("vi x\nline 000000\n") and text.endswith("line 024999\n")
        assert "~" not in text
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# Ctrl+C and Stop
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("how", ["key", "menu", "stop"])
def test_ctrl_c_and_stop_drop_what_waits_for_the_screen(qapp, how):
    sent = []
    term = _console(sent)
    try:
        _enter_top(term)
        term.feed("".join(top_frame(n) for n in range(2, 1500)))  # under the cap, not drawn yet
        assert term._inq_len > term._little_output()
        if how == "key":
            term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_C, Qt.ControlModifier, "\x03"))
            assert sent == [b"\x03"]  # still a key for the program
        elif how == "menu":
            assert term.send_key(b"\x03") and sent == [b"\x03"]
        else:
            assert term.interrupt_output() is True
        assert term._inq_len == 0 and not term._held_until  # no echo is waited for
        term.feed(top_frame(9999))  # the program redraws
        _render(term)
        assert term.has_screen() and term.screen().display()[0] == "Tasks: 9999 total"
    finally:
        _close(term)


def test_stop_in_an_adb_shell_drops_a_flood_on_the_screen(qapp, monkeypatch, tmp_path):
    from test_local_adb_shell import FakeSession, out, submit
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", FakeSession)
    widget = _LocalShellWidget("cmd", serial="V2318")
    try:
        widget._shell_cwd = str(tmp_path)
        widget.ensure_started()
        widget._strip_startup_banner = False
        submit(widget, "adb shell top")
        out(widget, "adb shell -t -t top\r\n\x1b[?25l" + top_frame(1))
        assert widget.term.has_screen()
        flood = "".join(top_frame(n) for n in range(2, 1500))
        widget._feed_from(widget.reader, flood.encode())  # not drawn yet
        widget.interrupt()
        assert widget.session.sent[-1] == b"\x03" and widget.term._inq_len == 0
        assert widget.term.has_screen()
    finally:
        widget.close_panel()
        widget.deleteLater()


# --------------------------------------------------------------------------- #
# rows that scrolled off the screen
# --------------------------------------------------------------------------- #
def test_rows_scrolled_off_a_screen_are_capped_and_go_back_at_once(qapp):
    from turboadb.gui import vtscreen

    term = _console()
    try:
        term.feed("progress 0%\r\n\x1b[1A\rprogress 1%\r\n")  # up a line: a screen
        _render(term)
        assert term.has_screen()
        term.feed("".join(f"line {n:05d}\r\n" for n in range(3000)))  # scrolls through it
        _render(term)
        assert term._screen_scrolled and term._screen_scrolled[-1].text().startswith("line ")
        # a long flood's worth of rows (80,000 of them were kept)
        rows = []
        for n in range(90000):
            row = vtscreen.Line(40)
            row.chars[:10] = list(f"row {n:06d}")
            rows.append(row)
        term._keep_scrolled(rows)
        assert len(term._screen_scrolled) == term._SCREEN_SCROLLBACK <= 10000
        started = time.perf_counter()
        term.feed(PROMPT)
        term.mark_prompt()
        _render(term)
        assert time.perf_counter() - started < 2.0  # six seconds, with the window frozen
        assert not term.has_screen()
        text = term.toPlainText()
        assert text.count("row 0") == term._SCREEN_SCROLLBACK
        assert "row 089999" in text and text.endswith(PROMPT)
    finally:
        _close(term)
