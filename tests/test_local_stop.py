"""Stop / Ctrl+C on a command in the PowerShell / CMD terminals keeps the shell.

The processes the shell started are ended (as Ctrl+C ends them in a console)
and the shell shows its prompt again with its folder and variables; cmd's
"Terminate batch job" question is answered.  A shell that stays busy, or a
second Stop, gets a fresh shell.  Ctrl+C at an idle prompt only starts a new
prompt line.
"""
import os
import sys
import time

import pytest

pytest.importorskip("PyQt5")

MARK = "\x1b]7717;{}\x1b\\"


class FakeSession:
    """A LocalShellSession stand-in whose kill_command answers at once."""

    instances = []
    result = ([4321], True)  # (killed PIDs, the shell's own child among them)

    def __init__(self, shell_type="cmd", serial=None, cwd=None, adb_path=None, **kwargs):
        self.cwd = cwd
        self.sent = []
        self.kills = 0
        self.running = True
        self.env = {}
        FakeSession.instances.append(self)

    def send(self, data):
        self.sent.append(data)
        return True

    def read(self, _size=4096):
        return b""

    def kill_command(self, on_done=None):
        from turboadb.gui.local_terminal import CommandStop

        self.kills += 1
        if on_done is not None:
            on_done(CommandStop(*FakeSession.result))
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
    monkeypatch.setattr(FakeSession, "instances", [])
    monkeypatch.setattr(FakeSession, "result", ([4321], True))
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


def render(widget):
    while widget.term._inq:
        widget.term._drain_tick()


def screen(widget):
    render(widget)  # notices wait behind output (and stream resets) in the queue
    return widget.term.toPlainText()


def out(widget, text):
    widget._feed_from(widget.reader, text.encode("utf-8"))
    render(widget)


def submit(widget, line):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    for ch in line:
        widget.term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, 0, Qt.NoModifier, ch))
    widget.term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))


def ctrl_c(widget):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    widget.term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_C, Qt.ControlModifier, "\x03"))


def prompt(widget, folder):
    if widget.shell_type == "powershell":
        return f"PS {folder}> \x1b]7717;{folder}\x07"
    return f"{folder}>" + MARK.format(folder)


def _ping(widget, folder):
    out(widget, prompt(widget, folder))
    submit(widget, "ping -t 127.0.0.1")
    echo = "ping -t 127.0.0.1\r\n\r\n" if widget.shell_type == "cmd" else "ping -t 127.0.0.1\n\r\n"
    out(widget, echo + "Pinging 127.0.0.1\r\nReply 1\r\nReply 2\r\n")


def test_stop_ends_the_command_and_keeps_powershell(make, tmp_path):
    widget = make("powershell")
    session = widget.session
    _ping(widget, tmp_path)
    ctrl_c(widget)
    assert session.kills == 1 and widget.session is session  # the same shell
    assert widget._stop_pending and widget._stop_timer.isActive()
    out(widget, prompt(widget, tmp_path))  # PowerShell prints its prompt again
    assert not widget._stop_pending and not widget._stop_timer.isActive()
    assert screen(widget).endswith(f"Reply 2\n^C\nPS {tmp_path}> ")
    assert len(FakeSession.instances) == 1 and b"\x03" not in b"".join(session.sent)


def test_cmd_prints_the_ctrl_c_itself(make, tmp_path):
    widget = make("cmd")
    _ping(widget, tmp_path)
    ctrl_c(widget)
    out(widget, "^C\r\n" + prompt(widget, tmp_path))
    text = screen(widget)
    assert text.endswith(f"Reply 2\n^C\n{tmp_path}>") and text.count("^C") == 1
    assert not widget._stop_pending and len(FakeSession.instances) == 1


def test_a_shell_busy_with_itself_is_never_answered_for_the_user(make, tmp_path, monkeypatch):
    """set /p, pause, Read-Host: nothing to end, and no Ctrl+C reached the
    shell (this session cannot raise one).  The empty line Stop used to send
    for a prompt answered the question: a script waiting in it went on as if
    Enter had been pressed.  The shell is replaced instead."""
    monkeypatch.setattr(FakeSession, "result", ([], False))  # set /p, pause: nothing to end
    widget = make("cmd")
    out(widget, prompt(widget, tmp_path))
    submit(widget, "set /p A=Answer: ")
    out(widget, "set /p A=Answer: \r\nAnswer: ")
    ctrl_c(widget)
    assert screen(widget).endswith("Answer: \n^C\n")  # nobody else prints it
    widget._stop_probe_timer.stop()
    widget._probe_local_prompt()
    assert widget.session.sent[-1] == b"set /p A=Answer: \r\n"  # nothing answered
    widget._on_local_stop_timeout()
    assert len(FakeSession.instances) == 2 and not FakeSession.instances[0].running
    assert screen(widget).endswith("Answer: \n^C\nStill running — fresh local shell ready\n")


def test_a_command_that_keeps_running_gets_a_fresh_shell(make, tmp_path):
    widget = make("powershell")
    _ping(widget, tmp_path)
    ctrl_c(widget)
    out(widget, "Reply 3\r\n")  # a loop inside PowerShell itself: no prompt
    widget._on_local_stop_timeout()
    assert len(FakeSession.instances) == 2 and FakeSession.instances[0].running is False
    assert widget.session.cwd == str(tmp_path)  # the fresh shell starts in the same folder
    text = screen(widget)
    assert text.endswith("^C\nReply 3\nStill running — fresh local shell ready\n")
    assert not widget._stop_pending and widget._own_prompt


def test_stop_twice_replaces_the_shell_at_once(make, tmp_path):
    widget = make("cmd")
    _ping(widget, tmp_path)
    widget.interrupt()
    widget.interrupt()
    assert len(FakeSession.instances) == 2
    assert screen(widget).endswith("Stopped — fresh local shell ready\n")


def test_an_unreadable_process_table_replaces_the_shell(make, tmp_path, monkeypatch):
    monkeypatch.setattr(FakeSession, "result", (None, False))
    widget = make("cmd")
    _ping(widget, tmp_path)
    widget.interrupt()
    assert len(FakeSession.instances) == 2
    assert screen(widget).endswith("^C  — stopped; fresh local shell ready\n")


def test_ctrl_c_at_an_idle_prompt_keeps_the_shell_and_its_jobs(make, tmp_path):
    widget = make("cmd")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    ctrl_c(widget)
    assert session.kills == 0 and len(FakeSession.instances) == 1  # nothing ended
    assert session.sent == [b"\n"]  # a new prompt line
    out(widget, "\n" + prompt(widget, tmp_path))
    assert screen(widget).endswith(f"{tmp_path}>^C\n{tmp_path}>")


def test_output_after_the_prompt_is_ended_too(make, tmp_path):
    widget = make("cmd")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    out(widget, "a background job printed this\r\n")  # start /b ...
    widget.interrupt()
    assert session.kills == 1 and widget._stop_pending


@pytest.mark.parametrize("question, answer", [
    ("Terminate batch job (Y/N)? ", "Y"),
    ("Batchvorgang abbrechen (J/N)? ", "J"),
    ("Terminer le programme de commandes (O/N) ? ", "O"),
])
def test_the_batch_file_question_is_answered(make, tmp_path, question, answer):
    widget = make("cmd")
    out(widget, prompt(widget, tmp_path))
    submit(widget, "build.bat")
    out(widget, "build.bat\r\nbuilding\r\n")
    widget.interrupt()
    out(widget, "^C" + question)
    assert widget.session.sent[-1] == answer.encode() + b"\n"
    out(widget, answer + "\n\r\n" + prompt(widget, tmp_path))  # cmd echoes the answer
    text = screen(widget)
    assert text.endswith(f"building\n^C{question}{answer}\n{tmp_path}>"), text
    assert not widget._stop_pending


def test_no_answer_without_a_stop(make, tmp_path):
    widget = make("cmd")
    out(widget, prompt(widget, tmp_path))
    submit(widget, "build.bat")
    sent = len(widget.session.sent)
    out(widget, "build.bat\r\n^CTerminate batch job (Y/N)? ")  # the batch file's own doing
    assert len(widget.session.sent) == sent


@pytest.mark.skipif(os.name != "nt", reason="starts a real cmd.exe")
def test_real_stop_keeps_the_shell_and_its_variables(qapp, tmp_path):
    from turboadb.gui.device_tab import _LocalShellWidget

    widget = _LocalShellWidget("cmd")
    widget._shell_cwd = str(tmp_path)

    def pump(until, limit=20.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            qapp.processEvents()
            if until():
                return True
            time.sleep(0.01)
        return False

    try:
        widget.ensure_started()
        assert pump(lambda: widget._own_prompt and widget._at_idle_prompt())
        pid = widget.session.proc.pid
        submit(widget, "set TADBV=kept-9")
        assert pump(widget._at_idle_prompt)
        submit(widget, f'"{sys.executable}" -c "import time; [time.sleep(1) for _ in range(60)]"')
        assert pump(lambda: not widget._own_prompt, 5)
        time.sleep(0.5)
        widget.interrupt()
        assert pump(lambda: not widget._stop_pending and widget._at_idle_prompt(), 10)
        assert widget.session.proc.pid == pid  # the same cmd.exe
        submit(widget, "echo %TADBV%")
        assert pump(lambda: "kept-9" in widget.term.toPlainText().split("echo %TADBV%")[-1], 10)
    finally:
        widget.close_panel()
