"""PowerShell formats its output (tables, Select-String matches) to its
buffer width, which was set only when the shell started.  Once the view was
resized, the next command typed at PowerShell's prompt now carries the new
width in front of it (its echo stays hidden), so nothing is ever typed into a
running command."""

import os
import time

import pytest

pytest.importorskip("PyQt5")

from turboadb.gui import local_terminal as lt  # noqa: E402

MARK = "\x1b]7717;{}\x07"


class _Session:
    def __init__(self, *_args, **kwargs):
        self.sent = []
        self.running = True
        self.env = {}
        self.columns = kwargs.get("columns")

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
def make(qapp, monkeypatch, tmp_path):
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(lt, "LocalShellSession", _Session)
    made = []

    def make(shell_type="powershell"):
        widget = _LocalShellWidget(shell_type, serial="V2318")
        widget._shell_cwd = str(tmp_path)
        widget.resize(800, 500)
        widget.ensure_started()
        widget._strip_startup_banner = False
        prompt = (f"PS {tmp_path}> " if shell_type == "powershell" else f"{tmp_path}>")
        _out(widget, prompt + MARK.format(tmp_path))
        widget.prompt_text = prompt + MARK.format(tmp_path)
        made.append(widget)
        return widget

    yield make
    for widget in made:
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


def test_the_buffer_width_rule():
    assert lt.ps_buffer_width(None) == lt.ps_buffer_width(80) == 120
    assert lt.ps_buffer_width(200) == 200 and lt.ps_buffer_width(5000) == 1000
    command = lt.ps_resize_command(180)
    assert command.endswith(";") and "Width=180" in command
    assert "LanguageMode -eq 'FullLanguage'" in command and '"' not in command


def test_a_wider_view_reaches_powershell_with_the_next_command(make):
    widget = make()
    started = widget._ps_width
    widget.resize(5000, 500)
    width = lt.ps_buffer_width(widget._console_columns())
    assert width > started
    _submit(widget, "Get-Process")
    line = lt.ps_resize_command(width) + "Get-Process"
    assert widget.session.sent[-1] == (line + "\r\n").encode()
    assert widget.term._pending_echo == line  # the whole echo stays hidden
    _out(widget, line + "\r\nHandles  NPM(K)\r\n" + widget.prompt_text)
    text = widget.term.toPlainText()
    assert "Get-Process\nHandles" in text and "BufferSize" not in text
    _submit(widget, "Get-Date")
    assert widget.session.sent[-1] == b"Get-Date\r\n"  # told once


def test_never_for_cmd_input_to_a_command_or_a_using_statement(make):
    cmd = make("cmd")
    cmd.resize(5000, 500)
    _submit(cmd, "dir")
    assert cmd.session.sent[-1] == b"dir\r\n"

    ps = make()
    ps.resize(5000, 500)
    _submit(ps, "using namespace System.IO")  # must stay the first statement
    assert ps.session.sent[-1] == b"using namespace System.IO\r\n"
    _out(ps, "using namespace System.IO\r\n" + ps.prompt_text)
    _submit(ps, "Read-Host 'Name'")
    _out(ps, "Read-Host 'Name'\r\nName: ")
    ps.term.set_shell_at_prompt(False)
    _submit(ps, "bob")  # the answer to a running command goes as typed
    assert ps.session.sent[-1] == b"bob\r\n"


@pytest.mark.skipif(os.name != "nt", reason="starts a real powershell.exe")
def test_the_real_powershell_takes_the_new_width(qapp):
    from turboadb.gui.device_tab import _LocalShellWidget

    def pump(seconds, until=lambda: False):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not until():
            qapp.processEvents()
            time.sleep(0.01)

    widget = _LocalShellWidget("powershell", serial=None)
    widget.resize(900, 500)
    widget.show()
    try:
        widget.ensure_started()
        pump(15, lambda: widget._prompt_seen)
        widget.resize(5000, 500)
        width = lt.ps_buffer_width(widget._console_columns())
        assert width > widget._ps_width  # wider than the start gave it
        _submit(widget, "'width=' + $Host.UI.RawUI.BufferSize.Width")
        pump(10, lambda: f"width={width}" in widget.term.toPlainText())
        text = widget.term.toPlainText()
        assert f"width={width}" in text
        assert "BufferSize.Width=" not in text and "FullLanguage" not in text  # echo hidden
    finally:
        widget.close_panel()
        widget.deleteLater()
