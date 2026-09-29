"""PowerShell / CMD terminals follow whether the shell waits at its own prompt.

Only a line typed at the prompt is a command and gets the conveniences (ls,
clear, where, python, adb shell on a device terminal); a line typed while a
command runs is input for that command and goes out exactly as typed, drawn
once.  The prompt is known from an invisible mark every prompt ends with, or a
default prompt of either shell.  The folder in it is where completion looks.
"""
import os

import pytest

pytest.importorskip("PyQt5")

MARK = "\x1b]7717;{}\x1b\\"


class FakeSession:
    """A LocalShellSession stand-in: records input, never produces output."""

    def __init__(self, shell_type="cmd", serial=None, cwd=None, adb_path=None, **kwargs):
        self.shell_type = shell_type
        self.kwargs = kwargs
        self.sent = []
        self.running = True
        self.env = {}

    def send(self, data):
        self.sent.append(data)
        return True

    def read(self, _size=4096):
        return b""

    def kill_command(self, on_done=None):
        return True

    def interrupt(self, on_done=None):
        self.running = False

    def close(self):
        self.running = False


@pytest.fixture
def make(qapp, monkeypatch, tmp_path):
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", FakeSession)
    made = []

    def make(shell_type="cmd"):
        widget = _LocalShellWidget(shell_type, serial="V2318")
        widget._shell_cwd = str(tmp_path)
        widget.ensure_started()
        widget._strip_startup_banner = False
        made.append(widget)
        return widget

    yield make
    for widget in made:
        widget.close_panel()
        widget.deleteLater()


def out(widget, text):
    widget._feed_from(widget.reader, text.encode("utf-8"))
    while widget.term._inq:
        widget.term._drain_tick()


def submit(widget, line):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    for ch in line:
        widget.term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, 0, Qt.NoModifier, ch))
    widget.term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))


def prompt(widget, folder):
    """What the shell prints when it waits for a command in *folder*."""
    if widget.shell_type == "powershell":
        return f"PS {folder}> \x1b]7717;{folder}\x07"
    return f"{folder}>" + MARK.format(folder)


def test_a_fresh_shell_reads_its_first_line_as_a_command(make):
    widget = make("cmd")
    assert widget._own_prompt and widget.term.shell_at_prompt()
    submit(widget, "ls")  # typed before cmd printed its first prompt
    assert widget.session.sent == [b"dir\r\n"]


def test_conveniences_only_at_the_prompt(make, tmp_path):
    widget = make("cmd")
    out(widget, prompt(widget, tmp_path))
    submit(widget, "set /p A=Answer: ")
    out(widget, "set /p A=Answer: \r\nAnswer: ")
    for answer in ("ls", "clear", "python", "adb shell"):
        submit(widget, answer)
        # input for set /p (or what runs next): exactly as typed
        assert widget.session.sent[-1] == answer.encode() + b"\r\n"
    assert not widget._in_adb_shell
    out(widget, "\r\n" + prompt(widget, tmp_path))
    submit(widget, "ls")
    assert widget.session.sent[-1] == b"dir\r\n"

    ps = make("powershell")
    out(ps, prompt(ps, tmp_path))
    submit(ps, "where adb")
    assert ps.session.sent[-1] == b"where.exe adb\r\n"
    out(ps, "where.exe adb\r\nC:\\pt\\adb.exe\r\n")  # a command runs: no prompt yet
    submit(ps, "where adb")
    assert ps.session.sent[-1] == b"where adb\r\n"


def test_the_console_knows_whether_the_shell_waits(make, tmp_path):
    widget = make("powershell")
    term = widget.term
    out(widget, prompt(widget, tmp_path))
    assert term.shell_at_prompt() is True
    submit(widget, "ping -n 3 host")
    assert term.shell_at_prompt() is False
    out(widget, "ping -n 3 host\r\nReply 1\r\n")
    assert term.shell_at_prompt() is False and not widget._own_prompt
    out(widget, prompt(widget, tmp_path))
    assert term.shell_at_prompt() is True and widget._own_prompt
    submit(widget, "adb shell")
    assert widget._in_adb_shell and term._at_prompt is None  # the device terminal echoes


def test_an_answer_is_drawn_once_and_the_programs_output_is_whole(make, tmp_path):
    widget = make("cmd")
    out(widget, prompt(widget, tmp_path))
    submit(widget, 'python -c "print(input(\'Name: \')+\' world\')"')
    out(widget, 'python -c "print(input(\'Name: \')+\' world\')"\r\nName: ')
    submit(widget, "hello")
    out(widget, "hello world\r\n\r\n" + prompt(widget, tmp_path))
    text = widget.term.toPlainText()
    assert text.count("hello") == 2 and "Name: hello\nhello world\n" in text


def test_either_shells_default_prompt_counts(make, tmp_path):
    if os.name == "nt":  # a Command Prompt prompt is a Windows path (C:\...>)
        ps = make("powershell")
        submit(ps, "cmd")
        out(ps, f"cmd\r\nMicrosoft Windows\r\n\r\n{tmp_path}>")  # cmd typed in PowerShell
        assert ps._own_prompt
        submit(ps, "ls")  # now cmd reads it, and echoes it
        assert ps.session.sent[-1] == b"ls\r\n" and ps.term._pending_echo == "ls"

    cmd = make("cmd")
    submit(cmd, "powershell")
    out(cmd, f"powershell\r\nPS {tmp_path}> ")
    assert cmd._own_prompt

    other = make("cmd")
    submit(other, "type prompts.txt")
    out(other, "type prompts.txt\r\nC:\\definitely\\not\\here>")  # output, not a prompt
    assert not other._own_prompt


def test_a_continuation_line_is_echoed_but_gets_no_conveniences(make, tmp_path):
    widget = make("powershell")
    out(widget, prompt(widget, tmp_path))
    submit(widget, "foreach ($x in 1..2) {")
    out(widget, "foreach ($x in 1..2) {\n>> ")
    assert widget.term.shell_at_prompt() and not widget._own_prompt
    submit(widget, "where x")  # inside the block: Where-Object, not where.exe
    assert widget.session.sent[-1] == b"where x\r\n"
    out(widget, "where x\n>> ")  # PowerShell echoes the block's lines too
    submit(widget, "}")
    out(widget, "}\n>> ")
    submit(widget, "")
    out(widget, "\n" + prompt(widget, tmp_path))
    text = widget.term.toPlainText()
    assert text.count("where x") == 1 and text.count("}") == 1, text
    assert text.endswith(f">> where x\n>> }}\n>> \nPS {tmp_path}> ")


def test_the_prompt_names_the_folder(make, tmp_path):
    (tmp_path / "sub").mkdir()
    widget = make("powershell")
    out(widget, prompt(widget, tmp_path))
    submit(widget, "cd sub")
    assert widget._shell_cwd == str(tmp_path)  # a typed cd is not the folder yet
    out(widget, "cd sub\r\n" + prompt(widget, tmp_path / "sub"))
    assert widget._shell_cwd == str(tmp_path / "sub") and widget.term._cwd == widget._shell_cwd
    submit(widget, "cd HKLM:\\Software")
    out(widget, "cd HKLM:\\Software\r\n" + prompt(widget, "HKEY_LOCAL_MACHINE\\Software"))
    assert widget._own_prompt  # a prompt, although its folder is not a directory
    assert widget._shell_cwd == str(tmp_path / "sub")


def test_folder_checks_never_wait_on_a_network_share(monkeypatch, tmp_path):
    pytest.importorskip("PyQt5")
    from turboadb.gui import device_tab

    def no_disk(path):
        raise AssertionError(f"isdir({path!r}) on the UI thread")

    if os.name == "nt":
        monkeypatch.setattr(device_tab.os.path, "isdir", no_disk)
        assert device_tab._quick_isdir("\\\\server\\share\\folder")
        monkeypatch.setattr(device_tab, "_is_network_drive", lambda drive: drive.upper() == "Z:")
        assert device_tab._quick_isdir("Z:\\projects")
        monkeypatch.undo()
    assert device_tab._quick_isdir(str(tmp_path))
    assert not device_tab._quick_isdir(str(tmp_path / "missing"))
    assert not device_tab._quick_isdir("")


def test_enter_for_a_programs_prompt_adds_no_second_line_break(make, tmp_path):
    widget = make("cmd")
    out(widget, prompt(widget, tmp_path))
    submit(widget, "pause")
    out(widget, "pause\r\nPress any key to continue . . . ")
    submit(widget, "")
    # pause ends its line after the key, then cmd prints its usual blank line
    out(widget, "\r")
    out(widget, "\n\r\n" + prompt(widget, tmp_path))
    text = widget.term.toPlainText()
    assert text.endswith(f"Press any key to continue . . . \n\n{tmp_path}>"), text

    submit(widget, 'python -c "input(\'Name: \'); print(\'done\')"')
    out(widget, "python -c ...\r\nName: ")
    submit(widget, "")
    out(widget, "done\r\n")  # a program that does not end the line itself
    assert widget.term.toPlainText().endswith("Name: \ndone\n")


def test_cmds_banner_goes_however_it_is_split(make):
    widget = make("cmd")
    widget._strip_startup_banner = True
    widget._banner_chunks = 0
    for chunk in ("Microsoft Windows [Version 10.0.26200.9457]",
                  "\r\n(c) Microsoft Corporation. All rights reserved.\r\n", "\r\n",
                  "C:\\w>" + MARK.format("C:\\w")):
        out(widget, chunk)
    text = widget.term.toPlainText()
    assert "Microsoft" not in text and text.endswith("\nC:\\w>")
    assert not widget._strip_startup_banner
