"""An interactive ``adb shell`` typed in PowerShell / CMD runs on a device terminal.

adb gives a shell a terminal only when its own input is one, so from the
terminal's pipes the device showed no prompt and no echo: ``-t -t`` is added
for the interactive forms of any adb invocation, never for a one-shot command
or a host-side pipe or redirection, and an explicit ``-t``/``-T``/``-x``/``-n``
is kept.  Inside, the device folder follows cd and the device prompt, Ctrl+Z
(end of file to adb.exe) is refused, and a device terminal's CR CR LF leaves no
blank line.
"""
import pytest

pytest.importorskip("PyQt5")


class FakeSession:
    def __init__(self, shell_type="cmd", serial=None, cwd=None, adb_path=None, **kwargs):
        self.sent = []
        self.running = True
        self.env = {}

    def send(self, data):
        self.sent.append(data)
        return True

    def read(self, _size=4096):
        return b""

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


@pytest.mark.parametrize("shell, typed, sent, foreign", [
    ("cmd", "adb shell", "adb shell -t -t", False),
    ("cmd", "ADB.EXE SHELL", "ADB.EXE SHELL -t -t", False),
    ("cmd", r"C:\pt\adb.exe shell", r"C:\pt\adb.exe shell -t -t", False),
    ("cmd", r'"C:\Program Files\pt\adb.exe" shell', r'"C:\Program Files\pt\adb.exe" shell -t -t', False),
    ("cmd", "adb -d shell", "adb -d shell -t -t", False),
    ("cmd", "adb -e shell", "adb -e shell -t -t", False),
    ("cmd", "adb -t 3 shell", "adb -t 3 shell -t -t", False),
    ("cmd", "adb -s V2318 shell", "adb -s V2318 shell -t -t", False),
    ("cmd", "adb -s OTHER shell", "adb -s OTHER shell -t -t", True),
    ("cmd", "adb -H 10.0.0.2 -P 5037 shell", "adb -H 10.0.0.2 -P 5037 shell -t -t", True),
    ("cmd", "adb shell su", "adb shell -t -t su", False),
    ("cmd", "adb shell sh", "adb shell -t -t sh", False),
    ("cmd", "adb shell -e ^ ", "adb shell -t -t -e ^", False),
    ("powershell", "adb shell", "adb shell -t -t", False),
    ("powershell", r"& 'C:\Program Files\pt\adb.exe' shell", r"& 'C:\Program Files\pt\adb.exe' shell -t -t", False),
    ("powershell", r'.\adb.exe shell mksh', r'.\adb.exe shell -t -t mksh', False),
])
def test_interactive_adb_shells_get_a_device_terminal(make, shell, typed, sent, foreign):
    widget = make(shell)
    cwd = widget._shell_cwd
    # at its prompt: a command, whose echo is expected
    out(widget, ("PS " if shell == "powershell" else "") + f"{cwd}>\x1b]7717;{cwd}\x07")
    submit(widget, typed)
    assert widget.session.sent[-1] == sent.encode() + b"\r\n"
    assert widget._in_adb_shell and widget._adb_shell_command == sent
    assert widget._adb_shell_foreign is foreign
    assert widget.term._pending_echo == sent  # the shell echoes what was sent


@pytest.mark.parametrize("shell, typed", [
    ("cmd", "adb shell ls"),
    ("cmd", "adb shell ls > files.txt"),
    ("cmd", "adb shell screencap -p | more"),
    ("cmd", "adb shell && echo done"),
    ("cmd", "adb shell su -c id"),
    ("cmd", "adb shell -T"),
    ("cmd", "adb shell -x"),
    ("cmd", "adb shell -t"),
    ("cmd", "adb shell -n"),
    ("cmd", "adb devices"),
    ("cmd", "adb -s"),
    ("powershell", "adb shell; echo done"),
    ("powershell", "adb shell ls | Select-String x"),
])
def test_one_shot_and_explicit_forms_run_as_typed(make, shell, typed):
    widget = make(shell)
    submit(widget, typed)
    assert widget.session.sent[-1] == typed.encode() + b"\r\n"
    assert not widget._in_adb_shell


def test_an_explicit_device_terminal_is_followed_as_typed(make):
    widget = make("cmd")
    submit(widget, "adb shell -t -t")
    assert widget.session.sent[-1] == b"adb shell -t -t\r\n" and widget._in_adb_shell


def test_the_device_folder_follows_cd_and_the_device_prompt(make):
    widget = make("powershell")
    submit(widget, "adb shell")
    out(widget, "PD2318:/ $ ")
    submit(widget, "cd /sdcard && ls")
    assert widget._adb_shell_cwd == "/sdcard"  # at once, for Tab completion
    out(widget, "cd /sdcard && ls\r\r\nDownload\r\r\nPD2318:/sdcard $ ")
    submit(widget, "cd -")
    assert widget._adb_shell_cwd == "/"
    out(widget, "cd -\r\r\n/\r\r\nPD2318:/ $ ")
    submit(widget, "cd /nope")
    assert widget._adb_shell_cwd == "/nope"
    out(widget, "cd /nope\r\r\n/system/bin/sh: cd: /nope: No such file or directory\r\r\n")
    assert widget._adb_shell_cwd == "/"  # the device refused it
    out(widget, "2|PD2318:/ $ ")
    submit(widget, "run-as com.example sh -c 'cd files; sh'")  # a folder cd tracking can't see
    out(widget, "run-as ...\r\r\n130|PD2318:/data/user/0/com.example/files $ ")
    assert widget._adb_shell_cwd == "/data/user/0/com.example/files"
    assert widget._get_prompt() == "V2318:/data/user/0/com.example/files $ "


def test_ctrl_z_is_refused_inside_adb_shell_only(make):
    widget = make("cmd")
    assert widget.term.send_key(b"\x1a") is True  # e.g. to end a Python prompt
    assert widget.session.sent[-1] == b"\x1a"
    submit(widget, "adb shell")
    sent = len(widget.session.sent)
    assert widget.term.send_key(b"\x1a") is False
    assert len(widget.session.sent) == sent
    while widget.term._inq:
        widget.term._drain_tick()
    assert "Ctrl+Z is not sent" in widget.term.toPlainText()
    assert widget.term.send_key(b"\t") is True and widget.session.sent[-1] == b"\t"


def test_another_devices_shell_gets_no_completion_from_this_one(make):
    widget = make("cmd")
    submit(widget, "adb -s OTHER shell")
    assert widget._adb_shell_complete("ls /sd") == (None, [])
    assert widget._completion_thread is None


def test_a_device_terminal_leaves_no_blank_lines(make):
    widget = make("cmd")
    submit(widget, "adb shell")
    out(widget, "adb shell -t -t\r\n")
    out(widget, "PD2318:/ $ ")
    for chunks in (["l", "s", "\r\r\r\n", "acct\r\r\n", "PD2318:/ $ "],
                   ["\r\r\r\n", "PD2318:/ $ "]):
        submit(widget, "".join(chunks[:-3]) if len(chunks) > 3 else "")
        for chunk in chunks:
            out(widget, chunk)
    text = widget.term.toPlainText()
    assert text.endswith("PD2318:/ $ ls\nacct\nPD2318:/ $ \nPD2318:/ $ "), text


def test_input_for_the_device_goes_as_utf8_in_cmd(qapp, monkeypatch, tmp_path):
    """CMD reads its input in the console code page, so a line for it is
    re-encoded; in an adb shell typed there adb reads the input and hands
    every byte to the device, which reads UTF-8 (``echo café 中`` arrived as
    ``echo caf\\x82 ?``).  The line that starts the adb shell is CMD's."""
    from test_local_session import _BlockingPipe, _session, _written
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    pipe = _BlockingPipe()
    pipe.release.set()
    session = _session(local_terminal, monkeypatch, pipe)  # a real session, a stand-in cmd.exe
    session.input_encoding = "cp850"
    session.env = {}  # (no probes of the PATH)
    monkeypatch.setattr(local_terminal, "LocalShellSession", lambda *_args, **_kwargs: session)
    widget = _LocalShellWidget("cmd", serial="V2318")
    try:
        widget._shell_cwd = str(tmp_path)
        widget.ensure_started()
        widget._strip_startup_banner = False
        local = "C:\\w>\x1b]7717;C:\\w\x07"
        out(widget, local)
        submit(widget, "echo café")
        out(widget, "\r\n" + local)
        submit(widget, 'adb -s "Zoë" shell')
        out(widget, "V2318:/ $ ")
        submit(widget, "echo café 中")
        widget._screen_write("é".encode("utf-8"))  # a key for a program on the screen
        out(widget, "\r\n" + local)  # the adb shell ended
        submit(widget, "echo café")
        assert _written(pipe, 5) == [
            "echo café\n".encode("cp850"),
            'adb -s "Zoë" shell -t -t\n'.encode("cp850"),
            "echo café 中\n".encode("utf-8"),
            "é".encode("utf-8"),
            "echo café\n".encode("cp850"),
        ]
    finally:
        widget.close_panel()
        widget.deleteLater()


def test_the_console_expects_the_devices_echo_while_a_command_runs(make):
    widget = make("cmd")
    submit(widget, "adb shell")
    out(widget, "PD2318:/ $ ")
    submit(widget, "logcat")
    out(widget, "logcat\r\r\n01-01 I Tag: hello\r\r\n")
    submit(widget, "q")  # the device terminal echoes it, even now
    assert widget.term._pending_echo == "q"
    out(widget, "q\r\r\n")
    assert widget.term.toPlainText().endswith("hello\nq\n")
