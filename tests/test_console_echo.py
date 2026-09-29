"""Hiding a shell's echo of the submitted line, and what Enter draws.

adb.exe writes its stdout in text mode, so a device terminal's echo ends in
CR CR LF, often one byte per read.  A line typed while a command runs is input
for that command: an empty one must not put a blank line into its output."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt5")

DEVICE = "console:/ $ "
CMD = "C:\\w>"


def _console(sent=None):
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole(send_fn=(sent.append if sent is not None else lambda _data: None))
    con.set_emulate_prompt(False)
    return con


def _close(con):
    con.close_archive()
    con.close()


def _render(con):
    while con._inq:
        con._drain_tick()


def _feed(con, *chunks):
    for chunk in chunks:
        con.feed(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
    _render(con)


def _key(con, key, text=""):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, key, Qt.NoModifier, text))


def _submit(con, line):
    from PyQt5.QtCore import Qt

    for ch in line:
        _key(con, 0, ch)
    _key(con, Qt.Key_Return, "\r")


@pytest.mark.parametrize(
    "chunks",
    [
        ["ls\r\r\r\n", "acct\r\r\n", DEVICE],
        ["l", "s", "\r\r\r\n", "acct\r\r\n", DEVICE],
        ["ls", "\r\r", "\n", "acct\r\r\n", DEVICE],
        ["ls", "\r", "\r", "\n", "acct\r\r\n", DEVICE],
        ["ls\r", "\r\nacct\r\r\n" + DEVICE],
        ["ls\r\n", "acct\r\n", DEVICE],
        ["ls\n", "acct\n", DEVICE],
    ],
)
def test_the_echo_and_any_line_break_after_it_are_hidden(qapp, chunks):
    con = _console()
    try:
        _feed(con, DEVICE)
        _submit(con, "ls")
        for chunk in chunks:
            _feed(con, chunk)
        assert con.toPlainText() == f"{DEVICE}ls\nacct\n{DEVICE}"
        assert con._pending_echo is None
    finally:
        _close(con)


def test_an_empty_enter_at_the_prompt_leaves_no_blank_line(qapp):
    con = _console()
    try:
        _feed(con, DEVICE)
        _submit(con, "")
        _feed(con, "\r\r\r\n" + DEVICE)
        assert con.toPlainText() == f"{DEVICE}\n{DEVICE}"
    finally:
        _close(con)


def test_only_one_line_break_is_hidden(qapp):
    con = _console()
    try:
        _feed(con, DEVICE)
        _submit(con, "blank")
        _feed(con, "blank\r\r\r\n", "\r\r\nx\r\r\n" + DEVICE)
        assert con.toPlainText() == f"{DEVICE}blank\n\nx\n{DEVICE}"
    finally:
        _close(con)


def test_output_that_merely_starts_like_the_line_is_shown_in_full(qapp):
    con = _console()
    try:
        _feed(con, CMD)
        _submit(con, "hello")
        _feed(con, "hel", "lo world\r\n")
        assert con.toPlainText() == f"{CMD}hello\nhello world\n"
        _submit(con, "abc")
        _feed(con, "abc", "xyz\r\n")  # the echo ended the chunk: wait, then show it
        assert con.toPlainText().endswith("abc\nabcxyz\n")
    finally:
        _close(con)


def test_a_held_back_echo_is_shown_when_nothing_else_comes(qapp):
    con = _console()
    try:
        _feed(con, CMD)
        _submit(con, "ls")
        _feed(con, "l")
        assert con.toPlainText() == f"{CMD}ls\n"
        con.clear_pending_echo()
        _render(con)
        assert con.toPlainText() == f"{CMD}ls\nl"
        _submit(con, "dir")
        _feed(con, "di")
        con._pending_echo_at -= 10  # the rest never came
        _feed(con, "Volume\r\n")
        assert con.toPlainText().endswith("dir\ndiVolume\n")
    finally:
        _close(con)


def test_a_complete_echo_is_never_shown_twice(qapp):
    con = _console()
    try:
        _feed(con, CMD)
        _submit(con, "echo one")
        _feed(con, "echo one")  # PowerShell writes the line break separately
        con._pending_echo_at -= 10  # even when it comes late
        _feed(con, "\r\none\r\n")
        assert con.toPlainText() == f"{CMD}echo one\none\n"
        _submit(con, "echo two")
        _feed(con, "echo two\r")
        con.reset_stream()  # Stop: the rest of that echo never comes
        _render(con)
        assert con.toPlainText() == f"{CMD}echo one\none\necho two\n"
    finally:
        _close(con)


def test_empty_enter_while_a_command_runs_adds_no_blank_line(qapp):
    sent = []
    con = _console(sent)
    try:
        _feed(con, CMD)
        _submit(con, "ping -n 4 host")
        _feed(con, "ping -n 4 host\r\n\r\nPinging host\r\nReply 1\r\n")
        for _ in range(3):
            _submit(con, "")
        assert con.toPlainText() == f"{CMD}ping -n 4 host\n\nPinging host\nReply 1\n"
        assert sent[-3:] == [b"\r\n"] * 3
        _feed(con, "Reply 2\r\n\r\nPing statistics\r\n\r\n" + CMD)
        assert con.toPlainText().endswith("Reply 1\nReply 2\n\nPing statistics\n\n" + CMD)
        # its prompt is back: Enter draws the line break at once again
        _submit(con, "")
        assert con.toPlainText().endswith(CMD + "\n")
    finally:
        _close(con)


def test_an_empty_answer_ends_the_programs_prompt_line(qapp):
    """Enter for a program's own prompt moves on to a new line (a console
    echoes that Enter), but still adds no blank line and expects no echo."""
    for verdict in (None, False):
        con = _console()
        try:
            _feed(con, CMD)
            _submit(con, "pause-script")
            _feed(con, "pause-script\r\nPress Enter to continue...")
            con.set_shell_at_prompt(verdict)
            _submit(con, "")
            assert con.toPlainText() == f"{CMD}pause-script\nPress Enter to continue...\n"
            assert con._pending_echo is None
            _feed(con, "\r\ndone\r\n")
            assert con.toPlainText().endswith("continue...\n\ndone\n")
        finally:
            _close(con)
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole(send_fn=lambda _data: None)  # the Android shell over a pipe
    try:
        con.show_prompt()
        con._submit_line("read -p 'Name: ' x")
        _feed(con, "Name: ")
        _submit(con, "")
        assert con.toPlainText().endswith("Name: \n")
    finally:
        _close(con)


def test_a_fast_second_enter_keeps_the_first_echo_hidden(qapp):
    con = _console()
    try:
        _feed(con, CMD)
        _submit(con, "dir")
        _submit(con, "")  # before cmd echoed "dir"
        assert con._pending_echo == "dir"
        _feed(con, "dir\r\n Volume in drive C\r\n")
        assert con.toPlainText() == f"{CMD}dir\n Volume in drive C\n"
    finally:
        _close(con)


def test_answers_to_a_running_program_are_drawn_once_and_not_hidden(qapp):
    sent = []
    con = _console(sent)
    try:
        _feed(con, CMD)
        _submit(con, 'python -c "print(input(\'Name: \') + \' world\')"')
        _feed(con, "python -c ...\r\n", "Name: ")
        con.set_shell_at_prompt(False)  # the owner: no shell prompt in the tail
        _submit(con, "hello")
        assert con._pending_echo is None
        _feed(con, "hello world\r\n")
        assert con.toPlainText().endswith("Name: hello\nhello world\n")
        _submit(con, "")  # an empty answer draws nothing
        assert con.toPlainText().endswith("hello world\n")
        assert sent[-2:] == [b"hello\r\n", b"\r\n"]
    finally:
        _close(con)


def test_the_owner_verdict_decides_and_a_submit_marks_the_shell_busy(qapp):
    con = _console()
    try:
        assert con.shell_at_prompt() is False  # nothing shown yet
        _feed(con, CMD)
        assert con.shell_at_prompt() is True  # guessed from the tail
        con.set_shell_at_prompt(False)
        assert con.shell_at_prompt() is False
        con.set_shell_at_prompt(True)
        _submit(con, "ver")
        assert con._pending_echo == "ver"  # a command: its echo is hidden
        assert con.shell_at_prompt() is False  # busy until the owner says otherwise
        con.set_shell_at_prompt(None)
        _feed(con, "ver\r\n\r\nMicrosoft Windows\r\n\r\n" + CMD)
        assert con.shell_at_prompt() is True
        assert con.toPlainText() == f"{CMD}ver\n\nMicrosoft Windows\n\n{CMD}"
    finally:
        _close(con)


def test_a_paste_still_waits_for_the_owners_prompt(qapp):
    sent = []
    con = _console(sent)
    try:
        _feed(con, CMD)
        con.set_shell_at_prompt(True)
        con._paste_text("first\nsecond\n")
        assert sent == [b"first\r\n"]
        _feed(con, "first\r\nstill running... C:\\w>")  # looks like a prompt, is not
        con._submitted_at -= 1
        con._last_feed -= 1
        con._pump_paste()
        assert sent == [b"first\r\n"]  # the owner has not seen its prompt yet
        con.set_shell_at_prompt(True)
        con._pump_paste()
        assert sent == [b"first\r\n", b"second\r\n"]
    finally:
        _close(con)


def test_the_saved_history_has_no_doubled_carriage_returns(qapp, tmp_path):
    con = _console()
    try:
        _feed(con, DEVICE)
        _submit(con, "ls")
        _feed(con, "l", "s", "\r\r\r\n", "acct\r\r\n", "bin\r\r", "\n" + DEVICE)
        out = tmp_path / "log.txt"
        con.save_output(str(out))
        saved = out.read_bytes()
        assert b"\r\r" not in saved
        assert saved.decode("utf-8").splitlines() == [DEVICE + "ls", "acct", "bin", DEVICE]
    finally:
        _close(con)
