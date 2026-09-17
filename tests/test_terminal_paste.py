"""Multi-line paste in the terminals (Android shell, PowerShell, Command Prompt).

Every pasted line must run once, in order, through the same path as Enter
(echo suppression, history, ``cd`` tracking, prompt), and only after the
previous line has had its turn."""

from __future__ import annotations

import os
import sys
import time

import pytest

pytest.importorskip("PyQt5")

PS = "PS C:\\work> "
CMD = "C:\\work>"
ANDROID = "shell@android:/ $ "


class FakeShell:
    """``send_fn`` for a console: records every write and answers like a shell.

    *reply(line)* returns what the shell writes after reading *line* (its echo,
    output and next prompt), as one string or a list of separately read chunks.
    Nothing reaches the console until the test delivers it with ``_answer``."""

    def __init__(self, reply=None):
        self.sent = []
        self.pending = []
        self.reply = reply

    def __call__(self, data):
        self.sent.append(data)
        if self.reply is not None:
            out = self.reply(data.decode("utf-8")[:-2 if data.endswith(b"\r\n") else -1])
            if out:
                self.pending.extend(out if isinstance(out, list) else [out])

    def lines(self):
        return [data.decode("utf-8") for data in self.sent]


def cmd_reply(line):
    """cmd.exe over a pipe: echo, output, a blank line, then the prompt."""
    output = line[5:] + "\r\n" if line.startswith("echo ") else ""
    return f"{line}\r\n{output}\r\n{CMD}"


class FakePowerShell:
    """powershell.exe over a pipe: a block started by ``{`` answers every line
    with ``>>`` and runs only after an empty line."""

    def __init__(self):
        self.block = []

    def __call__(self, line):
        if self.block or line.rstrip().endswith("{"):
            if line == "":
                self.block = []
                return f"\r\nran block\r\n{PS}"
            self.block.append(line)
            return f"{line}\r\n>> "
        if line.startswith("echo "):
            return f"{line}\r\n{line[5:]}\r\n{PS}"
        return f"{line}\r\n{PS}"


def _console(shell, *, local):
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole(send_fn=shell)
    if local:
        con.set_emulate_prompt(False)
    return con


def _close(con):
    con.close_archive()
    con.close()


def _render(con):
    while con._inq:
        con._drain_tick()


def _answer(con, shell):
    """Deliver what the shell has written so far and draw it."""
    chunks, shell.pending = shell.pending, []
    for chunk in chunks:
        con.feed(chunk.encode("utf-8"))
    _render(con)


def _go_quiet(con, seconds=0.3):
    """Pretend nothing happened for *seconds* (longer than the idle wait,
    shorter than the bounded wait)."""
    con._submitted_at -= seconds
    con._last_feed -= seconds


def _step(con, shell):
    _answer(con, shell)
    _go_quiet(con)
    con._pump_paste()


def _paste_and_run(con, shell, text):
    con._paste_text(text)
    for _ in range(200):
        if not (con._pasting or con._paste_block_watch):
            break
        _step(con, shell)
    _answer(con, shell)
    assert not con._pasting and not con._paste_timer.isActive()


def _key(con, key, text="", mods=None):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, key, mods or Qt.NoModifier, text))


def test_android_paste_runs_each_line_once_in_order_after_idle(qapp):
    shell = FakeShell()
    con = _console(shell, local=False)
    try:
        con.show_prompt()
        con._paste_text("echo one\necho two\necho three\n")
        assert shell.lines() == ["echo one\n"]
        assert con._paste_timer.isActive()

        con._pump_paste()  # sent just now: not idle yet
        assert len(shell.sent) == 1
        con.feed(b"one\n")
        _render(con)
        con._submitted_at -= 1.0  # sent long ago, but its output only just arrived
        con._pump_paste()
        assert len(shell.sent) == 1

        _go_quiet(con)
        con._pump_paste()
        assert shell.lines() == ["echo one\n", "echo two\n"]
        con._pump_paste()  # one line per turn
        assert len(shell.sent) == 2

        con.feed(b"two\n")
        _render(con)
        _go_quiet(con)
        con._pump_paste()
        assert shell.lines()[-1] == "echo three\n"
        con.feed(b"three\n")
        _render(con)
        _go_quiet(con)
        con._pump_paste()
        assert len(shell.sent) == 3
        assert not con._pasting and not con._paste_timer.isActive()
        assert con.toPlainText() == (
            f"{ANDROID}echo one\none\n{ANDROID}echo two\ntwo\n{ANDROID}echo three\nthree\n"
        )
    finally:
        _close(con)


def test_android_paste_tracks_cd_and_history(qapp):
    from PyQt5.QtCore import Qt

    shell = FakeShell()
    con = _console(shell, local=False)
    try:
        con.show_prompt()
        _paste_and_run(con, shell, "cd /sdcard\nls\n")
        assert shell.lines() == ["cd /sdcard\n", "ls -C\n"]
        assert con._cwd == "/sdcard"
        assert con._history == ["cd /sdcard", "ls"]
        assert "shell@android:/sdcard $ ls\n" in con.toPlainText()
        archive = con._sb.full_text()
        assert "shell@android:/ $ cd /sdcard\n" in archive
        assert "shell@android:/sdcard $ ls\n" in archive
        _key(con, Qt.Key_Up)
        assert con._line == "ls"
    finally:
        _close(con)


def test_local_paste_hides_each_echo(qapp):
    shell = FakeShell(cmd_reply)
    con = _console(shell, local=True)
    try:
        con.feed(CMD.encode())
        _render(con)
        con._paste_text("echo one\necho two\n")
        # Drawn at once, like a typed command, so the shell's echo is hidden.
        assert con.toPlainText() == f"{CMD}echo one\n"
        assert con._pending_echo == "echo one"
        for _ in range(10):
            _step(con, shell)
        assert not con._pasting
        assert shell.lines() == ["echo one\r\n", "echo two\r\n"]
        assert con.toPlainText() == f"{CMD}echo one\none\n\n{CMD}echo two\ntwo\n\n{CMD}"
        assert con._history == ["echo one", "echo two"]
        assert con._pending_echo is None
    finally:
        _close(con)


def test_local_echo_split_from_its_line_break_is_still_hidden(qapp):
    # PowerShell may write the echoed command and its line break separately.
    shell = FakeShell(lambda line: [line, f"\r\n{line[5:]}\r\n{PS}"])
    con = _console(shell, local=True)
    try:
        con.feed(PS.encode())
        _render(con)
        _paste_and_run(con, shell, "echo one\necho two\n")
        assert con.toPlainText() == f"{PS}echo one\none\n{PS}echo two\ntwo\n{PS}"
    finally:
        _close(con)


def test_local_paste_waits_for_the_prompt_before_the_next_line(qapp):
    shell = FakeShell()
    con = _console(shell, local=True)
    try:
        con.feed(CMD.encode())
        _render(con)
        con._paste_text("adb devices\necho two\n")
        assert len(shell.sent) == 1
        con.feed(b"adb devices\r\nList of devices attached\r\n")  # still running
        _render(con)
        _go_quiet(con)
        con._pump_paste()
        assert len(shell.sent) == 1
        con.feed(b"\r\n" + CMD.encode())  # its prompt is back
        _render(con)
        _go_quiet(con)
        con._pump_paste()
        assert shell.lines() == ["adb devices\r\n", "echo two\r\n"]
    finally:
        _close(con)


def test_command_that_never_goes_quiet_does_not_block_the_paste(qapp):
    shell = FakeShell()
    con = _console(shell, local=True)
    try:
        con.feed(CMD.encode())
        _render(con)
        con._paste_text("ping -t host\necho two\n")
        con.feed(b"ping -t host\r\nReply from host\r\n")
        _render(con)
        con._pump_paste()
        assert len(shell.sent) == 1
        con._submitted_at -= con._PASTE_MAX_WAIT_S + 0.1
        con._pump_paste()
        assert shell.lines() == ["ping -t host\r\n", "echo two\r\n"]
        # Typed ahead of a busy shell: CMD echoes it itself once it reads it.
        assert "echo two" not in con.toPlainText()
        assert con._pending_echo is None
        con.feed(f"Reply from host\r\n\r\n{CMD}echo two\r\ntwo\r\n\r\n{CMD}".encode())
        _render(con)
        assert con.toPlainText().count("echo two") == 1
        assert con._history == ["ping -t host", "echo two"]
    finally:
        _close(con)


def test_paste_merges_with_text_typed_before_the_cursor(qapp):
    shell = FakeShell(cmd_reply)
    con = _console(shell, local=True)
    try:
        con.feed(CMD.encode())
        _render(con)
        for ch in "adb ":
            _key(con, ord(ch), ch)
        _paste_and_run(con, shell, "devices\necho done\n")
        assert shell.lines() == ["adb devices\r\n", "echo done\r\n"]
        assert con.toPlainText().count("adb devices") == 1

        # Text after the cursor stays after the pasted text.
        con._set_line("echo X")
        con._cpos = 5
        _paste_and_run(con, shell, "a\nb")
        assert shell.lines()[-1] == "echo a\r\n"
        assert con._line == "bX"
    finally:
        _close(con)


def test_trailing_newline_decides_whether_the_last_line_runs(qapp):
    from PyQt5.QtCore import Qt

    shell = FakeShell(cmd_reply)
    con = _console(shell, local=True)
    try:
        con.feed(CMD.encode())
        _render(con)
        _paste_and_run(con, shell, "echo a\necho b")
        assert shell.lines() == ["echo a\r\n"]
        assert con._line == "echo b"
        assert con.toPlainText().endswith(f"{CMD}echo b")

        _key(con, Qt.Key_Escape)  # clears the reviewed line
        shell.sent.clear()
        _paste_and_run(con, shell, "echo a\necho b\n")
        assert shell.lines() == ["echo a\r\n", "echo b\r\n"]
        assert con._line == ""
    finally:
        _close(con)


def test_single_line_paste_is_unchanged(qapp):
    shell = FakeShell()
    con = _console(shell, local=False)
    try:
        con.show_prompt()
        con._set_line("ls ")
        con._paste_text("/sdcard")
        assert shell.sent == []
        assert con._line == "ls /sdcard" and con._cpos == len("ls /sdcard")
        assert not con._pasting and not con._paste_timer.isActive()
        assert con.toPlainText() == ANDROID + "ls /sdcard"
    finally:
        _close(con)


def test_paste_normalises_line_breaks_and_keeps_blank_lines(qapp):
    shell = FakeShell()
    con = _console(shell, local=False)
    try:
        con.show_prompt()
        _paste_and_run(con, shell, "echo a\r\n\r\necho b\recho c\r\n")
        assert shell.lines() == ["echo a\n", "\n", "echo b\n", "echo c\n"]
    finally:
        _close(con)


def test_powershell_block_paste_is_completed_with_an_empty_line(qapp):
    shell = FakeShell(FakePowerShell())
    con = _console(shell, local=True)
    try:
        con.feed(PS.encode())
        _render(con)
        _paste_and_run(con, shell, 'foreach ($x in 1..3) {\n  "n$x"\n}\n')
        assert shell.lines() == ['foreach ($x in 1..3) {\r\n', '  "n$x"\r\n', "}\r\n", "\r\n"]
        assert con.toPlainText() == (
            f'{PS}foreach ($x in 1..3) {{\n>>   "n$x"\n>> }}\n>> \nran block\n{PS}'
        )

        # Ordinary commands: no extra empty line and no stray prompt.
        shell.sent.clear()
        _paste_and_run(con, shell, "echo a\necho b\n")
        assert shell.lines() == ["echo a\r\n", "echo b\r\n"]
        assert con.toPlainText().endswith(f"{PS}echo a\na\n{PS}echo b\nb\n{PS}")
    finally:
        _close(con)


def test_enter_on_the_left_over_last_line_completes_a_powershell_block(qapp):
    from PyQt5.QtCore import Qt

    shell = FakeShell(FakePowerShell())
    con = _console(shell, local=True)
    try:
        con.feed(PS.encode())
        _render(con)
        _paste_and_run(con, shell, 'if ($true) {\n  "yes"\n}')
        assert shell.lines() == ["if ($true) {\r\n", '  "yes"\r\n']
        assert con._line == "}"
        _key(con, Qt.Key_Return, "\r")
        assert shell.lines()[-1] == "}\r\n"
        for _ in range(5):
            _step(con, shell)
        assert shell.lines()[-1] == "\r\n"
        assert not con._paste_timer.isActive()
        assert con.toPlainText().endswith(f"ran block\n{PS}")
    finally:
        _close(con)


def test_cmd_block_paste_gets_no_empty_line(qapp):
    shell = FakeShell(cmd_reply)
    con = _console(shell, local=True)
    try:
        con.feed(CMD.encode())
        _render(con)
        _paste_and_run(con, shell, "echo a\necho b\n")
        assert b"\r\n" not in shell.sent
    finally:
        _close(con)


def test_ctrl_c_esc_and_closed_shell_cancel_queued_lines(qapp):
    from PyQt5.QtCore import Qt

    interrupts = []
    shell = FakeShell()
    con = _console(shell, local=False)
    try:
        con.set_interrupt_fn(lambda: interrupts.append(1))
        con.show_prompt()

        def pasted_then(cancel):
            shell.sent.clear()
            con._paste_text("echo 1\necho 2\necho 3\n")
            assert len(shell.sent) == 1 and con._pasting
            cancel()
            assert not con._pasting and not con._paste_lines
            assert not con._paste_timer.isActive()
            _go_quiet(con, 5.0)
            con._pump_paste()
            assert len(shell.sent) == 1

        pasted_then(lambda: _key(con, Qt.Key_C, "\x03", Qt.ControlModifier))
        assert interrupts == [1]
        pasted_then(lambda: _key(con, Qt.Key_Escape))
        pasted_then(lambda: con.set_alive(True))  # how both shells end Stop
        pasted_then(lambda: con.set_alive(False))
    finally:
        _close(con)


def test_stop_button_cancels_queued_lines_in_a_local_terminal(qapp, monkeypatch):
    from PyQt5.QtWidgets import QPushButton
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    sessions = []

    class Session:
        running = True
        env = {}

        def __init__(self, *_args, **_kwargs):
            self.sent = []
            sessions.append(self)

        def send(self, data):
            self.sent.append(data)

        def read(self, _size=4096):
            return b""

        def interrupt(self, on_done=None):
            self.running = False

        def close(self):
            self.running = False

    monkeypatch.setattr(local_terminal, "LocalShellSession", Session)
    widget = _LocalShellWidget("cmd", handler=None)
    try:
        widget.ensure_started()
        widget.term._paste_text("echo 1\necho 2\necho 3\n")
        assert sessions[0].sent == [b"echo 1\r\n"]
        stop = next(b for b in widget.findChildren(QPushButton) if b.text() == "Stop")
        stop.click()
        assert len(sessions) == 2  # a fresh shell replaced the stopped one
        assert not widget.term._pasting and not widget.term._paste_lines
        _go_quiet(widget.term, 5.0)
        widget.term._pump_paste()
        assert sessions[1].sent == [] and sessions[0].sent == [b"echo 1\r\n"]
    finally:
        widget.close_panel()
        widget.close()


def _type(con, text):
    for ch in text:
        _key(con, 0, ch)


def _run_out(con, shell, turns=30):
    for _ in range(turns):
        if not (con._pasting or con._paste_block_watch):
            break
        _step(con, shell)
    _answer(con, shell)


def test_typing_during_a_paste_waits_behind_it(qapp):
    from PyQt5.QtCore import Qt

    shell = FakeShell(cmd_reply)
    con = _console(shell, local=True)
    try:
        con.feed(CMD.encode())
        _render(con)
        con._paste_text("echo 1\necho 2\n")
        _type(con, "cd foo")
        for key in (Qt.Key_Home, None, Qt.Key_Left, Qt.Key_Delete, Qt.Key_End):
            if key is None:
                _type(con, "x")
            else:
                _key(con, key)  # editing keys wait and replay in order too
        assert shell.lines() == ["echo 1\r\n"] and con._line == ""
        _run_out(con, shell)
        assert shell.lines() == ["echo 1\r\n", "echo 2\r\n"]
        assert con._line == "cd foo" and con._cpos == len("cd foo")
        assert con.toPlainText().endswith(f"{CMD}cd foo")
    finally:
        _close(con)


def test_enter_and_paste_typed_during_a_paste_keep_their_order(qapp):
    from PyQt5.QtCore import Qt

    shell = FakeShell(cmd_reply)
    con = _console(shell, local=True)
    try:
        con.feed(CMD.encode())
        _render(con)
        con._paste_text("echo 1\necho 2\n")
        _type(con, "echo 3")
        _key(con, Qt.Key_Return, "\r")
        _type(con, "echo ")
        con._paste_text("4\necho 5")  # a second paste after the typed keys
        _type(con, "!")
        _run_out(con, shell)
        assert shell.lines() == ["echo 1\r\n", "echo 2\r\n", "echo 3\r\n", "echo 4\r\n"]
        assert con._line == "echo 5!"
        assert con.toPlainText().count("echo 3") == 1
    finally:
        _close(con)


def test_paste_timer_keeps_the_users_selection_and_scroll(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QTextCursor
    from PyQt5.QtWidgets import QApplication

    for local in (False, True):
        shell = FakeShell(cmd_reply if local else None)
        con = _console(shell, local=local)
        interrupts = []
        try:
            con.set_interrupt_fn(lambda: interrupts.append(1))
            con.resize(600, 200)
            con.show()
            if not local:
                con.show_prompt()
            con.feed("".join(f"output line {i}\n" for i in range(200)).encode())
            if local:
                con.feed(CMD.encode())
            _render(con)
            qapp.processEvents()
            con._paste_text("echo 1\necho 2\necho 3\n")
            con._paste_timer.stop()  # this test drives the turns itself
            qapp.processEvents()
            bar = con.verticalScrollBar()
            assert bar.value() == bar.maximum()  # the paste itself shows the end

            cursor = con.textCursor()
            cursor.setPosition(0)
            cursor.setPosition(11, QTextCursor.KeepAnchor)
            con.setTextCursor(cursor)
            bar.setValue(10)
            _step(con, shell)  # the timer runs "echo 2"
            qapp.processEvents()
            assert len(shell.sent) == 2
            assert con.textCursor().selectedText() == "output line"
            assert bar.value() == 10
            _key(con, Qt.Key_C, "\x03", Qt.ControlModifier)
            assert interrupts == [] and con._pasting  # Ctrl+C copied
            assert QApplication.clipboard().text() == "output line"

            # A view already at the bottom follows the new line.
            cursor.clearSelection()
            con.setTextCursor(cursor)
            bar.setValue(bar.maximum())
            _step(con, shell)
            qapp.processEvents()
            assert len(shell.sent) == 3
            assert bar.value() == bar.maximum()
        finally:
            _close(con)


def test_output_paused_mid_line_is_not_taken_for_a_prompt(qapp):
    from turboadb.gui.console import _PROMPT_END_RE

    for line in ("PS C:\\x> ", "C:\\x>", ">> ", "More? ", "dev:/ $ ", "root# ", ">>> "):
        assert _PROMPT_END_RE.search(line), line
    for line in ("working", "50%", "Loading...", "cost 5$", ""):
        assert not _PROMPT_END_RE.search(line), line

    shell = FakeShell()
    con = _console(shell, local=True)
    try:
        con.feed(CMD.encode())
        _render(con)
        con._paste_text("build\necho two\n")
        con.feed(b"build\r\nworking")
        _render(con)
        _go_quiet(con)
        con._pump_paste()
        assert len(shell.sent) == 1  # quiet, but no prompt yet
        con.feed(f" finished\r\n\r\n{CMD}".encode())
        _render(con)
        _go_quiet(con)
        con._pump_paste()
        assert shell.lines() == ["build\r\n", "echo two\r\n"]
        assert "working finished" in con.toPlainText()
    finally:
        _close(con)


def test_cleared_or_replaced_left_over_line_sends_no_empty_line(qapp):
    from PyQt5.QtCore import Qt

    def leave_block_opener(clear):
        shell = FakeShell(FakePowerShell())
        con = _console(shell, local=True)
        try:
            con.feed(PS.encode())
            _render(con)
            _paste_and_run(con, shell, "echo a\necho b")
            assert con._line == "echo b" and con._paste_tail_open
            clear(con)
            shell.sent.clear()
            con._set_line("")
            _type(con, "foreach ($i in 1..2) {")
            _key(con, Qt.Key_Return, "\r")
            _run_out(con, shell)
            for _ in range(5):
                _step(con, shell)
            assert shell.lines() == ["foreach ($i in 1..2) {\r\n"], clear
        finally:
            _close(con)

    leave_block_opener(lambda con: _key(con, Qt.Key_Escape))
    leave_block_opener(lambda con: _key(con, Qt.Key_Up))
    leave_block_opener(lambda con: con.clear())
    leave_block_opener(lambda con: [_key(con, Qt.Key_Backspace) for _ in "echo b"])


def test_a_later_paste_moves_the_block_check_to_its_own_last_line(qapp):
    shell = FakeShell(FakePowerShell())
    con = _console(shell, local=True)
    try:
        con.feed(PS.encode())
        _render(con)
        con._paste_text("echo a\necho b\n")
        _step(con, shell)
        assert shell.lines()[-1] == "echo b\r\n" and con._paste_block_watch
        con._paste_text('if ($true) {\n  "yes"\n}')  # while "echo b" still runs
        _run_out(con, shell)
        assert shell.lines() == ["echo a\r\n", "echo b\r\n", "if ($true) {\r\n", '  "yes"\r\n']
        assert con._line == "}"
    finally:
        _close(con)


def test_single_line_paste_in_the_middle_keeps_the_caret_there(qapp):
    shell = FakeShell()
    con = _console(shell, local=False)
    try:
        con.show_prompt()
        con._set_line("ls /sdcard")
        con._cpos = 3
        con._sync_cursor()
        con._paste_text("-la ")
        assert con._line == "ls -la /sdcard" and con._cpos == 7
        end = con.document().characterCount() - 1
        assert con.textCursor().position() == end - len("/sdcard")
    finally:
        _close(con)


def test_large_paste_is_drained_incrementally(qapp):
    shell = FakeShell()
    con = _console(shell, local=False)
    try:
        con.show_prompt()
        text = "".join(f"echo {i}\n" for i in range(2000)) + "echo " + "x" * 50000 + "\n"
        started = time.monotonic()
        con._paste_text(text)
        assert time.monotonic() - started < 1.0
        assert shell.lines() == ["echo 0\n"]
        assert len(con._paste_lines) == 2000
        assert con._paste_timer.isActive()
        for turn in range(1, 6):
            _go_quiet(con)
            con._pump_paste()
            assert len(shell.sent) == turn + 1  # one line per turn
        assert shell.lines()[-1] == "echo 5\n"
    finally:
        _close(con)


def _run_real_paste(qapp, shell_type, text):
    """Paste *text* into a genuine local shell and return the rendered console."""
    from turboadb.gui.console import AnsiConsole
    from turboadb.gui.local_terminal import LocalShellSession

    session = LocalShellSession(shell_type, adb_path="__missing__")
    con = AnsiConsole(send_fn=session.send)
    con.set_emulate_prompt(False)

    def pump_until(done, limit):
        deadline = time.monotonic() + limit
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            data = session.read(65536)
            if data:
                con.feed(data)
                quiet_since = time.monotonic()
            _render(con)
            con._pump_paste()
            qapp.processEvents()
            if done() and time.monotonic() - quiet_since > 0.4:
                return True
            time.sleep(0.01)
        return False

    try:
        assert pump_until(lambda: con.toPlainText().rstrip().endswith(">"), 30.0)
        con._paste_text(text)
        assert pump_until(lambda: not (con._pasting or con._paste_block_watch), 30.0)
        return con.toPlainText()
    finally:
        session.close()
        _close(con)


@pytest.mark.skipif(os.name != "nt", reason="needs cmd.exe")
def test_real_cmd_output_paused_mid_line_keeps_the_next_line_out(qapp):
    # Writes part of a line, pauses (no prompt yet), then finishes the line.
    code = (
        "import sys,time;sys.stdout.write('working');sys.stdout.flush();"
        "time.sleep(0.8);print(' finished')"
    )
    out = _run_real_paste(qapp, "cmd", f'"{sys.executable}" -c "{code}"\necho two\n')
    lines = [line.rstrip() for line in out.splitlines()]
    assert "working finished" in lines, out
    assert lines.count("two") == 1, out
    assert sum(line.endswith(">echo two") for line in lines) == 1, out
    assert not any("workingecho" in line for line in lines), out


@pytest.mark.skipif(os.name != "nt", reason="needs cmd.exe and powershell.exe")
def test_real_cmd_and_powershell_paste_each_command_once(qapp):
    out = _run_real_paste(qapp, "cmd", "echo one\necho two\n")
    lines = [line.rstrip() for line in out.splitlines()]
    assert lines.count("one") == 1 and lines.count("two") == 1
    assert sum(line.endswith(">echo one") for line in lines) == 1
    assert sum(line.endswith(">echo two") for line in lines) == 1

    out = _run_real_paste(
        qapp, "powershell", 'echo one\necho two\nforeach ($x in 1..3) {\n  "n$x"\n}\n'
    )
    lines = [line.rstrip() for line in out.splitlines()]
    for word in ("one", "two", "n1", "n2", "n3"):
        assert lines.count(word) == 1, out
    assert sum(line.endswith("> echo one") for line in lines) == 1, out
    assert lines[-1].startswith("PS ") and lines[-1].endswith(">"), out
