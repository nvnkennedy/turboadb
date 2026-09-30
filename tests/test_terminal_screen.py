"""Full-screen programs on a device terminal: ``top``, ``watch``, ``vi``.

The console drew output line after line, so ``top`` appended frame after
frame and keys waited in the edit line for Enter.  On a device terminal a
program that addresses the cursor now gets a screen (vtscreen, painted by
screen_view over the scrollback): frames redraw in place, every key goes
straight to the program, its questions are answered, and the screen goes
when the program leaves the alternate screen or the shell's prompt is back.
``clear`` stays a clear, and over a pipe nothing of this happens."""

import os
import shutil
import subprocess
import threading

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QColor, QKeyEvent  # noqa: E402

from turboadb.gui import theme  # noqa: E402

PROMPT = "V2318:/ $ "
# a warning sign and VS16: one cell, a character and a mark joined to it
SIGN = "\u26a0\ufe0f"


def _console(sent=None, pty=True):
    from turboadb.gui.console import AnsiConsole

    term = AnsiConsole(send_fn=lambda _data: None)
    term.set_emulate_prompt(False)
    if pty:
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


def _key(term, key, text="", mods=Qt.NoModifier):
    term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, key, mods, text))


def top_frame(n, cols=40):
    """One refresh of toybox top (see test_vt_screen.top_frame)."""
    out = "\x1b[H\x1b[J" + f"Tasks: {n} total\r\nMem: 3.6G\r\n"
    out += "\x1b[7m" + "  PID USER".ljust(cols) + "\x1b[0m\r\n"
    return out + "\n".join(f"{1000 + n + i:5d} shell top\r" for i in range(3))


def _enter_top(term):
    term.feed(PROMPT)
    term.mark_prompt()
    term.feed("top\r\n\x1b[?25l" + top_frame(1))
    _render(term)


# --------------------------------------------------------------------------- #
# top: a screen for as long as it runs
# --------------------------------------------------------------------------- #
def test_top_gets_a_screen_and_redraws_in_place(qapp):
    term = _console()
    try:
        term.feed("earlier output\r\n")
        _enter_top(term)
        assert term.has_screen()
        screen = term.screen()
        assert (screen.cols, screen.rows) == term.cell_size()
        assert screen.display()[:2] == ["Tasks: 1 total", "Mem: 3.6G"]
        assert not screen.cursor_visible
        term.feed(top_frame(2))
        _render(term)
        assert screen.display()[0] == "Tasks: 2 total"
        assert "Tasks: 1" not in "\n".join(screen.display())  # in place, not appended
        assert term._screen_view.isVisible()
    finally:
        _close(term)


def test_keys_go_straight_to_the_program(qapp):
    sent = []
    term = _console(sent)
    try:
        _enter_top(term)
        _key(term, 0, "q")
        _key(term, Qt.Key_Up)
        _key(term, Qt.Key_Return, "\r")
        _key(term, Qt.Key_Backspace, "\b")
        _key(term, Qt.Key_Tab, "\t")
        _key(term, Qt.Key_Backtab, "", Qt.ShiftModifier)
        _key(term, Qt.Key_Escape, "\x1b")
        _key(term, Qt.Key_PageDown)
        _key(term, Qt.Key_F1)
        _key(term, Qt.Key_F5)
        _key(term, Qt.Key_Home)
        _key(term, Qt.Key_Right, "", Qt.ShiftModifier)
        _key(term, Qt.Key_A, "\x01", Qt.ControlModifier)
        _key(term, Qt.Key_L, "\x0c", Qt.ControlModifier)
        _key(term, Qt.Key_X, "x", Qt.AltModifier)
        _key(term, Qt.Key_C, "\x03", Qt.ControlModifier)  # nothing selected: to the program
        assert sent == [b"q", b"\x1b[A", b"\n", b"\x7f", b"\t", b"\x1b[Z", b"\x1b",
                        b"\x1b[6~", b"\x1bOP", b"\x1b[15~", b"\x1b[H", b"\x1b[1;2C",
                        b"\x01", b"\x0c", b"\x1bx", b"\x03"]
        assert term.toPlainText().count("q") == 0  # nothing went into an edit line
    finally:
        _close(term)


def test_application_cursor_keys(qapp):
    sent = []
    term = _console(sent)
    try:
        _enter_top(term)
        term.feed("\x1b[?1h")
        _render(term)
        _key(term, Qt.Key_Up)
        _key(term, Qt.Key_End)
        assert sent == [b"\x1bOA", b"\x1bOF"]
    finally:
        _close(term)


def test_ctrl_z_never_reaches_adb(qapp):
    sent = []
    term = _console(sent)
    try:
        _enter_top(term)
        _key(term, Qt.Key_Z, "\x1a", Qt.ControlModifier)
        term._paste_text("a\x1ab")
        assert sent == [b"a" + b"b"] or sent == [b"ab"]
        assert all(b"\x1a" not in data for data in sent)
    finally:
        _close(term)


def test_ctrl_m_is_a_line_feed_never_a_bare_cr(qapp):
    """Ctrl+M is a CR, and adb.exe holds a write that ends in one until more
    comes: it goes as the Enter key does, a line feed."""
    sent = []
    term = _console(sent)
    try:
        _enter_top(term)
        _key(term, Qt.Key_M, "\r", Qt.ControlModifier)
        _key(term, Qt.Key_M, "\r", Qt.ControlModifier | Qt.AltModifier)
        term.send_key(b"\r")
        assert sent == [b"\n", b"\x1b\n", b"\n"]
    finally:
        _close(term)


def test_a_paste_is_typed_text_with_line_feeds(qapp):
    sent = []
    term = _console(sent)
    try:
        _enter_top(term)
        term._paste_text("one\r\ntwo\rthree")
        term.feed("\x1b[?2004h")
        _render(term)
        term._paste_text("x\ny\n")
        assert sent == [b"one\ntwo\nthree", b"\x1b[200~x\ny\n\x1b[201~"]
    finally:
        _close(term)


def test_ctrl_c_copies_a_selection_on_the_screen(qapp, monkeypatch):
    from turboadb.gui import fileutil

    copied, sent = [], []
    monkeypatch.setattr(fileutil, "copy_to_clipboard", lambda _widget, text: copied.append(text))
    term = _console(sent)
    try:
        _enter_top(term)
        view = term._screen_view
        view._anchor, view._focus = (0, 0), (1, 3)
        _key(term, Qt.Key_C, "\x03", Qt.ControlModifier)
        assert copied == ["Tasks: 1 total\nMem:"] and sent == []
    finally:
        _close(term)


def test_the_prompt_ends_the_screen_and_the_last_frame_stays(qapp):
    term = _console()
    try:
        term.feed("\x1b[32mgreen before\x1b[0m\r\n")
        _enter_top(term)
        # q: toybox's tty_reset(), then mksh's prompt on the last row
        term.feed("\x1b[?25h\x1b[0m\x1b[999H\x1b[K" + PROMPT)
        term.mark_prompt()
        _render(term)
        assert not term.has_screen() and not term._screen_view.isVisible()
        text = term.toPlainText()
        assert text.startswith("Tasks: 1 total\nMem: 3.6G\n  PID USER")
        assert text.endswith("\n" + PROMPT)
        assert PROMPT + "top" not in text  # the rows top wiped are gone, as in a terminal window
        # the output goes on after the prompt, which is coloured
        last = term.document().lastBlock()
        assert last.text() == PROMPT
        assert last.begin().fragment().charFormat().foreground().color().name() == \
            QColor(theme.TERM_PROMPT_COLORS["host"][0]).name()
        term.feed("ls\r\nacct\r\n")
        _render(term)
        assert term.toPlainText().endswith(PROMPT + "ls\nacct\n")
    finally:
        _close(term)


def test_rows_above_the_screen_stay_in_the_scrollback(qapp):
    term = _console()
    try:
        rows = term.cell_size()[1]
        for n in range(rows + 5):
            term.feed(f"old line {n}\r\n")
        _enter_top(term)
        term.feed("\x1b[999H" + PROMPT)
        term.mark_prompt()
        _render(term)
        text = term.toPlainText()
        # the lines that were on the screen when top began went with it; the
        # ones above it are still there
        assert text.startswith("old line 0\n")
        assert f"old line {rows + 4}" not in text
    finally:
        _close(term)


def test_untouched_rows_keep_their_colours(qapp):
    """A progress display that moves up two lines leaves the others as they were."""
    term = _console()
    try:
        term.feed("\x1b[31mred line\x1b[0m\r\nfile 1: 0%\r\nfile 2: 0%\r\n")
        term.feed("\x1b[2A\rfile 1: 100%\x1b[2B\r")
        _render(term)
        assert term.has_screen()
        term.feed(PROMPT)
        term.mark_prompt()
        _render(term)
        assert not term.has_screen()
        assert term.toPlainText() == f"red line\nfile 1: 100%\nfile 2: 0%\n{PROMPT}"
        first = term.document().firstBlock().begin().fragment().charFormat()
        assert first.foreground().color().name() == QColor(theme.ANSI_FG[31]).name()
    finally:
        _close(term)


def test_notices_while_a_screen_shows_come_after_it(qapp):
    term = _console()
    try:
        _enter_top(term)
        term.notice("a note", new_stream=False)
        _render(term)
        assert "a note" not in term.toPlainText()
        term.feed("\x1b[999H" + PROMPT)
        term.mark_prompt()
        _render(term)
        assert term.toPlainText().endswith(PROMPT + "\na note\n")
    finally:
        _close(term)


def test_a_new_stream_ends_the_screen(qapp):
    term = _console()
    try:
        _enter_top(term)
        term.notice("Stopped — Android shell reopened")
        _render(term)
        assert not term.has_screen()
        assert term.toPlainText().endswith("Stopped — Android shell reopened\n")
    finally:
        _close(term)


def test_the_screen_follows_the_views_size(qapp):
    term = _console()
    try:
        _enter_top(term)
        seen = []
        term.screen_resized.connect(lambda cols, rows: seen.append((cols, rows)))
        term.resize(1000, 500)
        qapp.processEvents()
        assert seen and seen[-1] == term.cell_size()
        assert (term.screen().cols, term.screen().rows) == seen[-1]
        assert term._screen_view.geometry() == term.viewport().geometry()
    finally:
        _close(term)


def test_cells_with_marks_at_the_edge_resize_and_paint(qapp):
    """Cells with marks (``SIGN``) at the new edge of a narrower view: the
    screen's resize raised TypeError half-way, and every repaint then read
    past the end of its rows."""
    term = _console()
    try:
        _enter_top(term)
        cols = term.screen().cols
        term.feed("\x1b[2;1H" + SIGN * cols + "\x1b[2;5H")  # the cursor on one
        _render(term)
        term.resize(500, 300)
        qapp.processEvents()
        screen = term.screen()
        assert 2 <= screen.cols < cols
        assert all(len(line.chars) == screen.cols for line in screen.lines)
        assert screen.lines[1].chars[screen.cols - 1] == SIGN
        term._screen_view.grab()  # paints every cell and the cursor
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# clear stays a clear
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("clear", [
    "\x1b[2J\x1b[H",            # toybox clear
    "\x1b[H\x1b[2J\x1b[3J",     # ncurses clear
    "\x1b[H\x1b[2J",
    "\x1b[H\x1b[J",             # printf '\e[H\e[J'
])
def test_clear_clears_the_view_and_leaves_no_screen(qapp, clear):
    term = _console()
    try:
        term.feed("lots\r\nof\r\noutput\r\n" + PROMPT + "clear\r\n" + clear + PROMPT)
        term.mark_prompt()
        _render(term)
        assert not term.has_screen()
        assert term.toPlainText() == PROMPT
        term.feed("ls\r\nacct\r\n" + PROMPT)
        _render(term)
        assert term.toPlainText() == PROMPT + "ls\nacct\n" + PROMPT
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# the alternate screen: vi, less
# --------------------------------------------------------------------------- #
def test_leaving_the_alternate_screen_restores_the_view(qapp):
    sent = []
    term = _console(sent)
    try:
        term.feed("\x1b[35mcoloured\x1b[0m\r\n" + PROMPT)
        term.mark_prompt()
        term.feed("vi f\r\n")
        _render(term)
        before = term.toPlainText()
        term.feed("\x1b[?1049h\x1b[2J\x1b[Hhello\x1b[2;1H\x1b[2m~\x1b[m")
        _render(term)
        assert term.has_screen() and term.screen().alt_active
        # a line on it that looks like the prompt does not end it
        term.feed("\x1b[3;1H" + PROMPT)
        term.mark_prompt()
        _render(term)
        assert term.has_screen()
        _key(term, 0, "i")
        assert sent[-1] == b"i"
        term.feed("\x1b[?1049l")
        _render(term)
        assert not term.has_screen()
        assert term.toPlainText() == before
        first = term.document().firstBlock().begin().fragment().charFormat()
        assert first.foreground().color().name() == QColor(theme.ANSI_FG[35]).name()
        term.feed(PROMPT)
        term.mark_prompt()
        _render(term)
        assert term.toPlainText() == before + PROMPT
    finally:
        _close(term)


def test_the_wheel_scrolls_a_program_on_the_alternate_screen(qapp):
    from PyQt5.QtCore import QPoint, QPointF
    from PyQt5.QtGui import QWheelEvent

    sent = []
    term = _console(sent)
    try:
        term.feed("\x1b[?1049hless")
        _render(term)
        event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        term.screen_wheel(event)
        assert sent == [b"\x1b[B" * 3]
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# questions a program asks
# --------------------------------------------------------------------------- #
def test_cursor_position_and_status_are_answered(qapp):
    sent = []
    term = _console(sent)
    try:
        cols, rows = term.cell_size()
        # toybox's terminal_probesize() when the terminal has no size
        term.feed("\x1b[s\x1b[999C\x1b[999B\x1b[6n\x1b[u\x1b[5n")
        _render(term)
        assert term.has_screen()
        assert sent == [f"\x1b[{rows};{cols}R".encode(), b"\x1b[0n"]
    finally:
        _close(term)


def test_a_position_question_in_plain_output_is_answered_too(qapp):
    sent = []
    term = _console(sent)
    try:
        term.feed("ab\x1b[6n\x1b[c")
        _render(term)
        assert not term.has_screen()
        assert sent == [b"\x1b[1;3R", b"\x1b[?1;2c"]
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# over a pipe: none of it
# --------------------------------------------------------------------------- #
def test_over_a_pipe_there_is_no_screen_and_no_answer(qapp):
    term = _console(pty=False)
    try:
        term.feed("a\x1b[H\x1b[Jb\x1b[5;5Hc\x1b[?1049hd\x1b[6n\x1b[2Ae\r\n")
        _render(term)
        assert not term.has_screen()
        assert term.toPlainText() == "abcde\n"
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# the Android shell
# --------------------------------------------------------------------------- #
def test_the_android_shells_first_line_names_the_terminal_and_leaves_colours_alone(qapp):
    """ls and grep are not made to colour their output: the console colours
    the words that say what happened, and nothing else."""
    from test_android_shell_pty import _Handler, _Mksh, _close as close_widget, _ready, _widget

    class Named(_Mksh):
        def _line(self, line):
            if line.startswith(" ") and "7718" in line:
                self.init_line = line
                self.editor = False
                self.emit("\x1b]7718;ready;/dev/pts/3\x07" + self.prompt())
                return
            super()._line(line)

    widget, handler = _widget(qapp, _Handler(lambda tty: Named(tty)))
    try:
        _ready(qapp, widget)
        line = handler.sessions[0].init_line
        assert "--color" not in line and "alias " not in line and "ls()" not in line
        assert line.endswith("printf '\\033]7718;ready;%s\\007' \"$(tty 2>/dev/null)\"")
        assert widget._tty_name == "/dev/pts/3"
        assert "7718" not in widget.term.toPlainText()
    finally:
        close_widget(widget)


@pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="needs a POSIX sh")
def test_the_android_shells_first_line_is_valid_shell(qapp):
    from test_android_shell_pty import _Handler, _Mksh, _close as close_widget, _ready, _widget

    widget, handler = _widget(qapp, _Handler(lambda tty: _Mksh(tty)))
    try:
        _ready(qapp, widget)
        line = handler.sessions[0].init_line
    finally:
        close_widget(widget)
    done = subprocess.run(["sh", "-n", "-c", line], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


def test_a_screen_resized_in_the_android_shell_resizes_the_device_terminal(qapp, monkeypatch):
    from test_android_shell_pty import _Handler, _close as close_widget, _pump, _ready, _widget
    from turboadb.gui.device_tab import _AndroidShellWidget
    from turboadb.results import OperationResult

    monkeypatch.setattr(_AndroidShellWidget, "RESIZE_DELAY_MS", 10)
    calls = []

    class Handler(_Handler):
        def resize_terminal(self, tty, columns, rows, safe=None):
            calls.append((tty, columns, rows))
            return OperationResult(True, "resize_terminal")

    widget, handler = _widget(qapp, Handler())
    try:
        _ready(qapp, widget)
        widget.show()
        widget._tty_name = "/dev/pts/7"
        session = handler.sessions[0]
        session.emit("\x1b[?1049hvi")
        assert _pump(qapp, lambda: widget.term.has_screen())
        widget.resize(1200, 700)
        assert _pump(qapp, lambda: calls and calls[-1][1:] == widget.term.cell_size())
        assert calls[-1][0] == "/dev/pts/7"
        assert _pump(qapp, lambda: widget._tty_size == calls[-1][1:])
        # Ctrl+C on the screen is a key for the program, not a Stop
        _key(widget.term, Qt.Key_C, "\x03", Qt.ControlModifier)
        assert session.sent[-1] == b"\x03" and not widget._interrupt_pending
    finally:
        close_widget(widget)


def test_the_device_terminal_is_resized_one_size_at_a_time(qapp, monkeypatch):
    """Each resize ran a job of its own, and the tab's adb gate lets two run
    at once: they could reach the device in either order, and the size
    recorded was then not the terminal's.  One runs at a time; once it is
    done, the view's size goes next if it changed meanwhile."""
    from test_android_shell_pty import _Handler, _close as close_widget, _pump, _ready, _widget
    from turboadb.gui.device_tab import _AndroidShellWidget
    from turboadb.results import OperationResult

    monkeypatch.setattr(_AndroidShellWidget, "RESIZE_DELAY_MS", 10)
    release = threading.Event()
    calls, running, most = [], [], [0]

    class Handler(_Handler):
        def resize_terminal(self, tty, columns, rows, safe=None):
            running.append(tty)
            most[0] = max(most[0], len(running))
            calls.append((columns, rows))
            release.wait(10)
            running.pop()
            return OperationResult(True, "resize_terminal")

    widget, handler = _widget(qapp, Handler())
    try:
        _ready(qapp, widget)
        widget.show()
        widget._tty_name = "/dev/pts/7"
        release.set()
        handler.sessions[0].emit("\x1b[?1049hvi")
        assert _pump(qapp, lambda: widget.term.has_screen())
        _pump(qapp, lambda: False, timeout=0.3)  # a first size, if any, is told
        release.clear()
        del calls[:]
        widget.resize(1200, 700)
        assert _pump(qapp, lambda: len(calls) == 1)  # on its way, and held there
        widget.resize(1000, 600)
        widget.resize(1100, 650)
        _pump(qapp, lambda: False, timeout=0.3)
        assert len(calls) == 1  # nothing else while it runs
        release.set()
        size = widget.term.cell_size()
        assert _pump(qapp, lambda: widget._tty_size == size)
        assert calls[-1] == size and len(calls) == 2 and most[0] == 1
    finally:
        release.set()
        close_widget(widget)


# --------------------------------------------------------------------------- #
# PowerShell / CMD: adb shell with a full-screen program
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("typed, sent", [
    ("adb shell top", "adb shell -t -t top"),
    ("adb -s V2318 shell watch -n 1 date", "adb -s V2318 shell -t -t watch -n 1 date"),
    ("adb shell /system/bin/top -d 2", "adb shell -t -t /system/bin/top -d 2"),
    ("adb shell vi /sdcard/a.txt", "adb shell -t -t vi /sdcard/a.txt"),
    ("adb shell less x", "adb shell -t -t less x"),
])
def test_a_full_screen_program_in_an_adb_shell_gets_a_device_terminal(qapp, monkeypatch, tmp_path,
                                                                      typed, sent):
    from test_local_adb_shell import FakeSession, submit
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", FakeSession)
    widget = _LocalShellWidget("cmd", serial="V2318")
    try:
        widget._shell_cwd = str(tmp_path)
        widget.ensure_started()
        submit(widget, typed)
        assert widget.session.sent[-1] == sent.encode() + b"\r\n"
        assert widget._in_adb_shell and widget._adb_shell_command == ""  # never reopened
        assert widget.term._screen_input is not None
    finally:
        widget.close_panel()
        widget.deleteLater()


@pytest.mark.parametrize("typed", [
    "adb shell -T top", "adb shell -t top", "adb shell top -n 1 | findstr x",
    "adb shell top > top.txt", "adb shell toptool",
])
def test_explicit_forms_and_host_pipes_stay_as_typed(qapp, monkeypatch, tmp_path, typed):
    from test_local_adb_shell import FakeSession, submit
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", FakeSession)
    widget = _LocalShellWidget("cmd", serial="V2318")
    try:
        widget._shell_cwd = str(tmp_path)
        widget.ensure_started()
        submit(widget, typed)
        assert b"-t -t" not in widget.session.sent[-1]  # (CMD may still stream the findstr)
        assert not widget._in_adb_shell and widget.term._screen_input is None
    finally:
        widget.close_panel()
        widget.deleteLater()


def test_top_in_an_adb_shell_ends_with_the_local_prompt(qapp, monkeypatch, tmp_path):
    from test_local_adb_shell import FakeSession, out, submit
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", FakeSession)
    widget = _LocalShellWidget("cmd", serial="V2318")
    try:
        widget._shell_cwd = str(tmp_path)
        widget.ensure_started()
        widget._strip_startup_banner = False
        cwd = "C:\\w"
        local = f"{cwd}>\x1b]7717;{cwd}\x07"
        out(widget, local)
        submit(widget, "adb shell top")
        out(widget, "adb shell -t -t top\r\n\x1b[?25l" + top_frame(1))
        assert widget.term.has_screen()
        _key(widget.term, 0, "q")
        assert widget.session.sent[-1] == b"q"  # straight to top, no Enter needed
        out(widget, "\x1b[?25h\x1b[0m\x1b[999H\x1b[K\r\n" + local)
        assert not widget.term.has_screen() and not widget._in_adb_shell
        assert widget.term._screen_input is None
        assert widget.term.toPlainText().endswith(f"{cwd}>")
    finally:
        widget.close_panel()
        widget.deleteLater()


@pytest.mark.parametrize("shell", ["cmd", "powershell"])
@pytest.mark.parametrize("typed", ["adb shell", "adb shell vi notes.txt"])
@pytest.mark.parametrize("error", ["", "error: closed\r\n"])
def test_a_screen_the_adb_shell_left_behind_goes_with_it(qapp, monkeypatch, tmp_path, shell,
                                                         typed, error):
    """vi on the alternate screen, and then the device goes away (or adb is
    restarted): the adb shell ends and the local prompt comes back.  The
    screen went only when vi left it itself, so its frame stayed up with the
    prompt drawn on it, and every key typed went nowhere.  What adb says as
    it ends is shown, not drawn on vi's frame and gone with it."""
    from test_local_adb_shell import FakeSession, out, submit
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", FakeSession)
    widget = _LocalShellWidget(shell, serial="V2318")
    try:
        widget._shell_cwd = str(tmp_path)
        widget.ensure_started()
        widget._strip_startup_banner = False
        prompt = "PS C:\\w> " if shell == "powershell" else "C:\\w>"
        local = prompt + "\x1b]7717;C:\\w\x07"
        out(widget, local)
        submit(widget, typed)
        if typed == "adb shell":
            out(widget, "V2318:/ $ ")
            submit(widget, "vi notes.txt")
            echo = "vi notes.txt\r\r\n"
        else:
            echo = "adb shell -t -t vi notes.txt\r\n"
        out(widget, echo + "\x1b[?1049h\x1b[H\x1b[2Jhello\x1b[2;1H~\x1b[1;1H")
        term = widget.term
        assert term.has_screen() and term.screen().alt_active
        out(widget, "\r\n" + error + "\r\n" + local)  # adb ended: the local shell's prompt
        assert not widget._in_adb_shell and term._screen_input is None
        assert not term.has_screen() and not term._screen_view.isVisible()
        text = term.toPlainText()
        assert text.endswith("\n" + prompt) and "hello" not in text
        assert ("error: closed\n" in text) == bool(error)
        submit(widget, "dir")  # typing reaches the local shell again
        assert widget.session.sent[-1].endswith(b"dir\r\n")
        assert term.toPlainText().endswith("\n" + prompt + "dir\n")
    finally:
        widget.close_panel()
        widget.deleteLater()


def test_keys_go_to_the_edit_line_once_the_screens_terminal_is_gone(qapp):
    sent = []
    term = _console(sent)
    try:
        term.feed(PROMPT)
        term.mark_prompt()
        term.feed("vi x\r\n\x1b[?1049h\x1b[H\x1b[2Jhello")
        _render(term)
        assert term.screen().alt_active
        term.set_screen_input(None)  # the adb shell it ran in ended
        _key(term, 0, "l")
        _key(term, 0, "s")
        assert sent == [] and term._line == "ls"
        term.end_screen()
        assert not term.has_screen()
        assert term.toPlainText() == PROMPT + "vi x\nls"  # the view as before vi, the line typed
    finally:
        _close(term)


def test_the_end_of_a_screen_comes_in_order_with_the_output(qapp):
    term = _console()
    try:
        term.feed("\x1b[?1049h\x1b[Hframe")
        term.end_screen()  # queued after the frame, before what follows
        term.feed("after\r\n")
        _render(term)
        assert not term.has_screen()
        assert term.toPlainText() == "after\n"
    finally:
        _close(term)


def test_the_device_prompt_in_an_adb_shell_is_coloured(qapp, monkeypatch, tmp_path):
    from test_local_adb_shell import FakeSession, out, submit
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", FakeSession)
    widget = _LocalShellWidget("powershell", serial="V2318")
    try:
        widget._shell_cwd = str(tmp_path)
        widget.ensure_started()
        widget._strip_startup_banner = False
        submit(widget, "adb shell")
        out(widget, "1|V2318:/sdcard $ ")
        block = widget.term.document().lastBlock()
        fragment = block.begin().fragment()
        assert fragment.text() == "1|"
        assert fragment.charFormat().foreground().color().name() == \
            QColor(theme.TERM_PROMPT_COLORS["status"][0]).name()
    finally:
        widget.close_panel()
        widget.deleteLater()


def test_the_engine_resizes_a_device_terminal_with_stty(fake_adb):
    from turboadb.config import ADBConfig
    from turboadb.core import ADBHandler

    handler = ADBHandler(ADBConfig(serial="V2318"), safe=True)
    result = handler.resize_terminal("/dev/pts/3", 132, 43)
    assert result.success
    assert fake_adb.argv_after_adb()[-2:] == ["shell", "stty -F /dev/pts/3 cols 132 rows 43"]
    refused = handler.resize_terminal("/dev/pts/3; reboot", 80, 24)
    assert not refused.success
    assert all("reboot" not in " ".join(call) for call in fake_adb.calls)
