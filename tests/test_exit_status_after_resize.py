"""A command that reads the last command's status still gets that status after
the view was resized.

Once the view is wider or narrower, the next command typed at the prompt
carries the new width in front of it (PowerShell's buffer width, the device
terminal's ``stty cols/rows``).  That statement runs first, so a ``$?`` in the
command answered for it: after a failed command and a resize, ``$?`` said
True (PowerShell) or 0 (the device shell).  Such a command goes as typed, and
the one after it takes the new width."""
import pytest

pytest.importorskip("PyQt5")

from test_android_shell_pty import _close, _enter, _pump, _ready, _settle, _widget  # noqa: E402
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
def powershell(qapp, monkeypatch, tmp_path):
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(lt, "LocalShellSession", _Session)
    widget = _LocalShellWidget("powershell", serial="V2318")
    widget._shell_cwd = str(tmp_path)
    widget.resize(800, 500)
    widget.ensure_started()
    widget._strip_startup_banner = False
    widget.prompt_text = f"PS {tmp_path}> " + MARK.format(tmp_path)
    _out(widget, widget.prompt_text)
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


@pytest.mark.parametrize("line", ["$?", "echo $?", "if (-not $?) { 'failed' }"])
def test_powershell_status_is_not_the_width_statements(powershell, line):
    widget = powershell
    widget.resize(5000, 500)
    width = lt.ps_buffer_width(widget._console_columns())
    assert width > widget._ps_width
    _submit(widget, line)
    assert widget.session.sent[-1] == (line + "\r\n").encode()  # as typed
    _out(widget, line + "\r\nFalse\r\n" + widget.prompt_text)
    _submit(widget, "Get-Date")  # the next command takes the new width
    assert widget.session.sent[-1] == (lt.ps_resize_command(width) + "Get-Date\r\n").encode()


@pytest.mark.parametrize("line", ["echo $?", "echo ${?}", "echo $_", "echo ${PIPESTATUS[0]}"])
def test_the_device_shells_status_is_not_sttys(qapp, line):
    widget, handler = _widget(qapp)
    try:
        _ready(qapp, widget)
        session = handler.sessions[0]
        widget.resize(1400, 500)
        wide = widget._terminal_size()
        assert wide[0] > widget._tty_size[0]
        _enter(widget.term, line)
        assert _pump(qapp, lambda: session.sent[-1:] == [(line + "\n").encode()]), session.sent[-2:]
        _settle(qapp, widget, "PD2318:/ $ ")
        _enter(widget.term, "ls")
        _settle(qapp, widget, "PD2318:/ $ ")
        setting = "stty cols {} rows {} 2>/dev/null; ".format(*wide)
        assert session.sent[-1] == (" " + setting + "ls\n").encode()
    finally:
        _close(widget)
