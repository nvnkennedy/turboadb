"""A line typed before the shell can read it shows once, where it ran.

* Typed before a new shell's first prompt, it was drawn at once (with no
  prompt) and again in the shell's echo after its prompt.  Now only the echo
  shows it, as in a console window.
* Typed while a command runs, it is drawn at once: it may be the answer the
  command waits for, which nothing echoes.  When the command ends without
  reading it, the shell reads it as a command and echoes it after its prompt;
  the early copy then goes.
* The Android shell on a device terminal sends a line typed before its first
  prompt only once that prompt is out (the terminal echoed it in the middle
  of the shell's start, or it went with the hidden first line).
"""

import pytest

pytest.importorskip("PyQt5")

MARK = "\x1b]7717;{}\x1b\\"


class _Session:
    def __init__(self, *_args, **_kwargs):
        self.sent = []
        self.running = True
        self.env = {}

    def send(self, data):
        self.sent.append(data)
        return True

    def read(self, _size=4096):
        return b""

    def interrupt(self):
        self.running = False

    def close(self):
        self.running = False


@pytest.fixture
def cmd(qapp, monkeypatch, tmp_path):
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", _Session)
    widget = _LocalShellWidget("cmd", serial="V2318")
    widget._shell_cwd = str(tmp_path)
    widget.ensure_started()
    widget._strip_startup_banner = False
    widget.term.clear(keep_prompt=False)  # no banner: the screen is what the shell printed
    widget.prompt = f"{tmp_path}>"
    yield widget
    widget.close_panel()
    widget.deleteLater()


def _out(widget, text):
    widget._feed_from(widget.reader, text.encode("utf-8"))
    while widget.term._inq:
        widget.term._drain_tick()


def _submit(widget, line):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    for ch in line:
        widget.term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, 0, Qt.NoModifier, ch))
    widget.term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))


def _prompt(widget):
    return widget.prompt + MARK.format(widget._shell_cwd)


def test_a_line_typed_before_the_first_prompt_shows_once_after_it(cmd):
    _submit(cmd, "ver")
    assert cmd.session.sent == [b"ver\r\n"]  # sent at once: the shell reads it when ready
    assert cmd.term.toPlainText() == ""  # not drawn with no prompt before it
    _out(cmd, _prompt(cmd) + "ver\r\n\r\nMicrosoft Windows\r\n\r\n" + _prompt(cmd))
    text = cmd.term.toPlainText()
    assert text == f"{cmd.prompt}ver\n\nMicrosoft Windows\n\n{cmd.prompt}"
    _submit(cmd, "dir")  # from its prompt on, as before: drawn, its echo hidden
    assert cmd.term.toPlainText().endswith(f"{cmd.prompt}dir\n")
    assert cmd.term._pending_echo == "dir"


def test_a_line_the_shell_reads_after_the_command_moves_to_its_prompt(cmd):
    _out(cmd, _prompt(cmd))
    _submit(cmd, "ping -n 3 host")
    _out(cmd, "ping -n 3 host\r\n\r\nReply 1\r\n")
    _submit(cmd, "echo next")  # typed while ping runs: drawn at once
    assert cmd.term.toPlainText().endswith("Reply 1\necho next\n")
    _out(cmd, "Reply 2\r\n\r\n" + _prompt(cmd) + "echo next\r\nnext\r\n\r\n" + _prompt(cmd))
    text = cmd.term.toPlainText()
    assert text.count("echo next") == 1
    assert text.endswith(f"Reply 1\nReply 2\n\n{cmd.prompt}echo next\nnext\n\n{cmd.prompt}")


def test_an_answer_the_command_reads_stays_where_it_was_typed(cmd):
    _out(cmd, _prompt(cmd))
    _submit(cmd, "set /p NAME=Name: ")
    _out(cmd, "set /p NAME=Name: \r\nName: ")
    _submit(cmd, "bob")
    _out(cmd, "\r\n" + _prompt(cmd))
    _submit(cmd, "echo %NAME%")
    _out(cmd, "echo %NAME%\r\nbob\r\n\r\n" + _prompt(cmd))
    text = cmd.term.toPlainText()
    assert "Name: bob\n" in text and text.count("bob") == 2  # the answer and echo's output


def test_the_same_line_typed_later_at_the_prompt_leaves_the_answer_alone(cmd):
    _out(cmd, _prompt(cmd))
    _submit(cmd, "choice-like")
    _out(cmd, "choice-like\r\nContinue? ")
    _submit(cmd, "yes")  # read by the command: no echo
    _out(cmd, "\r\n" + _prompt(cmd))
    _submit(cmd, "yes")  # now a command at the prompt; its echo is hidden
    _out(cmd, "yes\r\n'yes' is not recognized\r\n\r\n" + _prompt(cmd))
    text = cmd.term.toPlainText()
    assert "Continue? yes\n" in text  # the answer was not taken back
    assert text.count("yes") == 3


def test_a_typed_line_still_waiting_to_be_drawn_is_never_drawn(cmd):
    _out(cmd, _prompt(cmd))
    _submit(cmd, "ping -n 3 host")
    cmd._feed_from(cmd.reader, b"ping -n 3 host\r\nReply 1\r\n")  # not drawn yet
    _submit(cmd, "echo next")  # queued behind that output
    _out(cmd, _prompt(cmd) + "echo next\r\nnext\r\n" + _prompt(cmd))
    text = cmd.term.toPlainText()
    assert text.count("echo next") == 1
    assert text.endswith(f"Reply 1\n{cmd.prompt}echo next\nnext\n{cmd.prompt}")


def test_the_android_shell_sends_an_early_line_at_its_first_prompt(qapp):
    from test_android_shell_pty import (
        _Handler, _Mksh, _after_banner, _close, _enter, _pump, _settle, _widget,
    )

    widget, handler = _widget(qapp, _Handler(make=lambda tty: _Mksh(tty, prompt_first=False)))
    try:
        session = handler.sessions[0]
        _enter(widget.term, "ls")  # before the shell's first prompt was shown
        assert len(session.sent) == 1  # only the hidden first line so far
        assert _pump(qapp, lambda: session.sent[-1:] == [b"ls\n"])
        _settle(qapp, widget, "cache\nPD2318:/ $ ")
        assert _after_banner(widget.term) == "PD2318:/ $ ls\nacct\ncache\nPD2318:/ $ "
    finally:
        _close(widget)
