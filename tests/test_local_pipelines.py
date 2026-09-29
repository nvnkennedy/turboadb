"""Filters that show matches as they come, and screen clears, in PowerShell / CMD.

Over the terminal's pipes findstr and GNU grep hold their output until 4 KB
gathered or the command ended (``adb logcat | findstr X`` showed nothing).
At the shell's own prompt CMD runs a plain ``| findstr word`` through
find.exe and gives GNU grep ``--line-buffered``; PowerShell (which hands one
program's output to the next only when it ends) runs Select-String.  Other
forms get a one-time hint.  ``cls`` / ``clear`` / ``Clear-Host`` clear.
"""
import pytest

pytest.importorskip("PyQt5")

MARK = "\x1b]7717;{}\x1b\\"


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

    def make(shell_type="cmd", gnu_grep=None):
        widget = _LocalShellWidget(shell_type, serial="V2318")
        widget._shell_cwd = str(tmp_path)
        widget.ensure_started()
        widget._strip_startup_banner = False
        if gnu_grep is not None:
            widget._grep_probe = {"gnu": gnu_grep}
        made.append(widget)
        return widget

    yield make
    for widget in made:
        widget.close_panel()
        widget.deleteLater()


def render(widget):
    while widget.term._inq:
        widget.term._drain_tick()


def out(widget, text):
    widget._feed_from(widget.reader, text.encode("utf-8"))
    render(widget)


def submit(widget, line):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    for ch in line:
        widget.term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, 0, Qt.NoModifier, ch))
    widget.term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))


def run(widget, line):
    """Type *line* at the prompt; returns what went to the shell."""
    submit(widget, line)
    return widget.session.sent[-1].decode("utf-8")[:-2]


def find_exe():
    from turboadb.gui.local_terminal import _system_exe

    return _system_exe("find.exe")


@pytest.mark.parametrize("typed, flags, word", [
    ("adb logcat | findstr NEEDLE", "", "NEEDLE"),
    ("adb logcat | findstr /i needle", " /I", "needle"),
    ("adb logcat | findstr /V /i ActivityManager", " /V /I", "ActivityManager"),
    ('adb logcat -v brief | findstr "E/AndroidRuntime"', None, None),
    ('adb logcat | findstr "FATAL"', "", "FATAL"),
])
def test_cmd_plain_findstr_runs_find_exe(make, typed, flags, word):
    widget = make("cmd")
    cwd = widget._shell_cwd
    out(widget, f"{cwd}>" + MARK.format(cwd))  # at its prompt: a command, its echo expected
    sent = run(widget, typed)
    if flags is None:  # "E/AndroidRuntime" is not one plain word (the / is findstr syntax)
        assert sent == typed
        return
    expected = typed[: typed.index("| findstr")] + f"| {find_exe()}{flags} \"{word}\""
    assert sent == expected
    assert widget.term._pending_echo == expected  # cmd echoes what it got


def test_other_findstr_forms_get_one_hint(make):
    widget = make("cmd")
    for line in ("adb logcat | findstr /r N.EDLE", "adb logcat | findstr a b"):
        assert run(widget, line) == line
        out(widget, "\r\nC:\\w>" + MARK.format("C:\\w"))  # the prompt is back
    assert widget.term.toPlainText().count("findstr holds its output back") == 1


def test_a_findstr_into_a_file_is_left_alone(make):
    widget = make("cmd")
    line = "adb logcat -d | findstr NEEDLE > crash.txt"
    assert run(widget, line) == line
    render(widget)
    assert "findstr holds" not in widget.term.toPlainText()


def test_cmd_gnu_grep_gets_line_buffered(make):
    widget = make("cmd", gnu_grep=True)
    assert run(widget, "adb logcat | grep -i fatal") == "adb logcat | grep --line-buffered -i fatal"
    for line in (
        "adb logcat | grep --line-buffered x",  # already
        'adb shell "logcat | grep x"',  # the grep runs on the device
        "dir || grep x",  # no pipe
    ):
        out(widget, "\r\nC:\\w>" + MARK.format("C:\\w"))
        assert run(widget, line) == line
    other = make("cmd", gnu_grep=False)  # BusyBox grep has no --line-buffered
    assert run(other, "adb logcat | grep x") == "adb logcat | grep x"
    unknown = make("cmd")  # the probe has not answered yet
    assert run(unknown, "adb logcat | grep x") == "adb logcat | grep x"


@pytest.mark.parametrize("typed, expected", [
    ("adb logcat | findstr NEEDLE",
     "adb logcat | Select-String -SimpleMatch -CaseSensitive 'NEEDLE' | ForEach-Object Line"),
    ("adb logcat | findstr /i /v noise",
     "adb logcat | Select-String -SimpleMatch -NotMatch 'noise' | ForEach-Object Line"),
])
def test_powershell_plain_findstr_runs_select_string(make, typed, expected):
    widget = make("powershell")
    assert run(widget, typed) == expected


def test_powershell_native_filters_get_one_hint(make):
    widget = make("powershell", gnu_grep=True)
    for line in ("adb logcat | grep x", "adb logcat | findstr /r x.y"):
        assert run(widget, line) == line  # no --line-buffered: PowerShell holds it anyway
        out(widget, "PS C:\\w> \x1b]7717;C:\\w\x07")  # the prompt is back
    assert widget.term.toPlainText().count("| Select-String text") == 1


def test_no_rewrite_for_input_or_inside_adb_shell(make):
    widget = make("cmd", gnu_grep=True)
    submit(widget, "set /p A=Filter: ")
    out(widget, "set /p A=Filter: \r\nFilter: ")
    assert run(widget, "x | findstr y") == "x | findstr y"  # an answer, as typed
    adb = make("cmd", gnu_grep=True)
    submit(adb, "adb shell")
    assert run(adb, "logcat | grep x") == "logcat | grep x"


def test_powershell_clear_host_clears_the_screen(make, tmp_path):
    widget = make("powershell")
    prompt = f"PS {tmp_path}> \x1b]7717;{tmp_path}\x07"
    out(widget, prompt)
    for word in ("cls", "clear", "Clear-Host"):
        submit(widget, "echo before")
        out(widget, "echo before\nbefore\r\n" + prompt)
        submit(widget, word)
        out(widget, word + "\n" + prompt)  # Clear-Host writes nothing to a pipe
        assert widget.term.toPlainText() == f"PS {tmp_path}> "
        assert "before" in widget.term._sb.full_text()  # Save keeps the history


def test_clear_while_a_command_runs_is_input(make, tmp_path):
    widget = make("powershell")
    out(widget, f"PS {tmp_path}> \x1b]7717;{tmp_path}\x07")
    submit(widget, "Read-Host 'Name'")
    out(widget, "Read-Host 'Name'\nName: ")
    submit(widget, "cls")
    render(widget)
    assert "Name: cls" in widget.term.toPlainText()


def test_cmd_clear_runs_cls_and_its_form_feed_clears(make, tmp_path):
    widget = make("cmd")
    prompt = f"{tmp_path}>" + MARK.format(tmp_path)
    out(widget, prompt)
    submit(widget, "echo before")
    out(widget, "echo before\r\nbefore\r\n\r\n" + prompt)
    assert run(widget, "clear") == "cls"
    out(widget, "cls\r\n\x0c\r\n" + prompt)
    assert widget.term.toPlainText() == f"{tmp_path}>"
