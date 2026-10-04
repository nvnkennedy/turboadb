"""Stop in the PowerShell / CMD terminals presses a real Ctrl+C.

Stop ended only the processes the shell ran (``kill_command``), so the shell
went on with the rest of the command line or script: ``ping -n 30 x; rm y``
ran ``rm y``, a batch file's ``ping`` stopped and its next line ran anyway.
And a shell waiting in ``pause``, ``set /p`` or ``Read-Host`` was sent an
empty line "for its prompt", which answered the question: a script went on
as if the user had pressed Enter.  Stop now raises Ctrl+C in the shell's own
(hidden) console, as a console window does (the shell abandons the line or
script, ``ping`` prints its summary), ends the processes only when they
ignore it, and never sends a line that could answer a question."""

import os
import time

import pytest

pytest.importorskip("PyQt5")

MARK = "\x1b]7717;{}\x1b\\"


class CtrlCSession:
    """A LocalShellSession stand-in that can raise a console Ctrl+C."""

    instances = []
    ctrl_c = (True, True)   # (raised, the shell ran programs)
    kill = ([4321], True)   # (killed PIDs, the shell's own child among them)

    def __init__(self, shell_type="cmd", serial=None, cwd=None, adb_path=None, **kwargs):
        self.cwd = cwd
        self.sent = []
        self.ctrl_cs = 0
        self.kills = 0
        self.running = True
        self.env = {}
        CtrlCSession.instances.append(self)

    def send(self, data):
        self.sent.append(data)
        return True

    def read(self, _size=4096):
        return b""

    def send_ctrl_c(self, on_done=None):
        from turboadb.gui.local_terminal import CtrlC

        self.ctrl_cs += 1
        if on_done is not None:
            on_done(CtrlC(*CtrlCSession.ctrl_c))
        return True

    def kill_command(self, on_done=None):
        from turboadb.gui.local_terminal import CommandStop

        self.kills += 1
        if on_done is not None:
            on_done(CommandStop(*CtrlCSession.kill))
        return True

    def interrupt(self, on_done=None):
        self.running = False

    def close(self):
        self.running = False


@pytest.fixture
def make(qapp, monkeypatch, tmp_path):
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", CtrlCSession)
    monkeypatch.setattr(CtrlCSession, "instances", [])
    monkeypatch.setattr(CtrlCSession, "ctrl_c", (True, True))
    monkeypatch.setattr(CtrlCSession, "kill", ([4321], True))
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
    render(widget)
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


def prompt(widget, folder):
    if widget.shell_type == "powershell":
        return f"PS {folder}> \x1b]7717;{folder}\x07"
    return f"{folder}>" + MARK.format(folder)


def test_stop_raises_ctrl_c_and_ends_nothing_itself(make, tmp_path):
    widget = make("cmd")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "ping -t 127.0.0.1")
    out(widget, "ping -t 127.0.0.1\r\n\r\nPinging 127.0.0.1\r\nReply 1\r\n")
    widget.interrupt()
    assert session.ctrl_cs == 1 and session.kills == 0
    assert widget._stop_pending and widget._stop_timer.isActive()
    # ping prints its summary, cmd its ^C and prompt: all the shell's own
    out(widget, "\r\nPing statistics\r\nControl-C\r\n^C\r\n" + prompt(widget, tmp_path))
    assert not widget._stop_pending and not widget._stop_timer.isActive()
    assert screen(widget).endswith(f"Reply 1\n\nPing statistics\nControl-C\n^C\n{tmp_path}>")
    assert len(CtrlCSession.instances) == 1 and session.sent[-1] != b"\n"


@pytest.mark.parametrize("shell, line, waits", [
    ("cmd", "build.bat", "build.bat\r\nPress any key to continue . . . "),
    ("cmd", "rd /s build", "rd /s build\r\nbuild, Are you sure (Y/N)? "),
    # -Confirm writes its question to the hidden console; Enter means "Yes"
    ("powershell", "Remove-Item x -Confirm", "Remove-Item x -Confirm\n"),
    ("powershell", "if ($true) { rm x }", "if ($true) { rm x }\n>> "),
])
def test_a_shell_waiting_for_a_line_is_replaced_never_answered(
        make, tmp_path, monkeypatch, shell, line, waits):
    """It notices the Ctrl+C only once it has read a line, and any line could
    answer its question: Enter confirmed Remove-Item -Confirm, ran a ">>"
    block, and let a batch file go on past its pause."""
    monkeypatch.setattr(CtrlCSession, "ctrl_c", (True, False))  # no programs: the shell itself waits
    widget = make(shell)
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, line)
    out(widget, waits)
    widget.interrupt()
    assert session.ctrl_cs == 1 and widget._stop_timer.isActive()
    widget._probe_local_prompt()
    widget._on_local_stop_timeout()  # its prompt is not back
    assert len(CtrlCSession.instances) == 2 and not session.running
    assert session.sent == [(line + "\r\n").encode()]  # nothing typed for the user
    assert screen(widget).endswith("stopped; fresh local shell ready\n" if shell == "cmd"
                                   else "^C\nStopped — fresh local shell ready\n")


def test_the_shells_own_work_ends_on_the_ctrl_c(make, tmp_path, monkeypatch):
    monkeypatch.setattr(CtrlCSession, "ctrl_c", (True, False))
    widget = make("powershell")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "Start-Sleep 30")
    out(widget, "Start-Sleep 30\n")
    widget.interrupt()
    out(widget, prompt(widget, tmp_path))  # PowerShell stops it at once
    assert not widget._stop_pending and len(CtrlCSession.instances) == 1
    assert session.kills == 0


def test_a_shell_busy_with_itself_gets_time_to_show_its_prompt(qapp, make, tmp_path, monkeypatch):
    """PowerShell stops Start-Sleep at once, but its prompt can take a while:
    a prompt function that asks git (oh-my-posh, posh-git), a .NET call that
    has to return first.  It was replaced, variables and history lost, when
    the prompt was not back within about half a second."""
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(CtrlCSession, "ctrl_c", (True, False))  # nothing runs but PowerShell itself
    monkeypatch.setattr(_LocalShellWidget, "LOCAL_STOP_WAIT_MS", 1000)
    monkeypatch.setattr(_LocalShellWidget, "LOCAL_STOP_PROBE_MS", 50)
    widget = make("powershell")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "Start-Sleep 30")
    out(widget, "Start-Sleep 30\n")
    widget.interrupt()
    _pump(qapp, lambda: False, 0.5)  # its prompt function is still running
    assert widget._stop_pending and len(CtrlCSession.instances) == 1
    out(widget, prompt(widget, tmp_path))
    assert not widget._stop_pending and not widget._stop_timer.isActive()
    assert len(CtrlCSession.instances) == 1 and session.kills == 0


def test_a_shell_whose_prompt_never_comes_back_is_still_replaced(qapp, make, tmp_path, monkeypatch):
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(CtrlCSession, "ctrl_c", (True, False))
    monkeypatch.setattr(_LocalShellWidget, "LOCAL_STOP_WAIT_MS", 150)
    widget = make("powershell")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "Read-Host 'Name'")
    out(widget, "Read-Host 'Name'\nName: ")
    widget.interrupt()
    assert _pump(qapp, lambda: len(CtrlCSession.instances) == 2, 5)  # by itself, after the wait
    assert not session.running and session.sent == [b"Read-Host 'Name'\r\n"]  # nothing answered
    assert screen(widget).endswith("^C\nStopped — fresh local shell ready\n")


def test_a_command_that_ignores_ctrl_c_has_its_processes_ended(make, tmp_path):
    widget = make("powershell")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "stubborn.exe")
    out(widget, "stubborn.exe\n\r\nstill going\r\n")
    widget.interrupt()
    assert session.ctrl_cs == 1 and session.kills == 0
    widget._on_local_stop_timeout()  # no prompt: the Ctrl+C was ignored
    assert session.kills == 1 and widget._stop_timer.isActive()
    assert len(CtrlCSession.instances) == 1
    widget._on_local_stop_timeout()  # still no prompt: a fresh shell
    assert len(CtrlCSession.instances) == 2 and not session.running
    assert screen(widget).endswith("Still running — fresh local shell ready\n")


def test_a_program_that_answers_the_ctrl_c_is_not_ended_under_the_user(make, tmp_path):
    """A REPL prints KeyboardInterrupt and its prompt and runs on: the shell's
    prompt never comes back, and ending the REPL after the wait killed it."""
    widget = make("cmd")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "python")
    out(widget, "python -i -u\r\n>>> ")
    submit(widget, "import time; time.sleep(30)")
    widget.interrupt()
    assert session.ctrl_cs == 1
    out(widget, "Traceback (most recent call last):\r\nKeyboardInterrupt\r\n>>> ")
    widget._on_local_stop_timeout()
    assert session.kills == 0 and len(CtrlCSession.instances) == 1
    submit(widget, "print(42)")  # talking to the REPL ends that Stop
    assert not widget._stop_pending
    widget.interrupt()  # a later Stop starts with a Ctrl+C again
    assert session.ctrl_cs == 2 and session.kills == 0


@pytest.fixture
def reports(monkeypatch):
    """The session's Ctrl+C reports are held back: each test hands them over
    itself, as the real session does a moment after it raised the Ctrl+C."""
    held = []
    monkeypatch.setattr(CtrlCSession, "send_ctrl_c",
                        lambda self, on_done=None: held.append(on_done) or True)
    return held


def test_an_answer_that_is_there_before_the_ctrl_c_is_reported_counts(make, tmp_path, reports):
    """The session reports a Ctrl+C a moment after raising it (it has to
    leave the shell's console again), and a REPL answers within milliseconds:
    its KeyboardInterrupt and prompt were there before the report, did not
    count as an answer, and the REPL was ended after the wait."""
    from turboadb.gui.local_terminal import CtrlC

    widget = make("cmd")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "python")
    out(widget, "python -i -u\r\n>>> ")
    submit(widget, "import time; time.sleep(60)")
    widget.interrupt()
    raised = time.monotonic()
    out(widget, "Traceback (most recent call last):\r\nKeyboardInterrupt\r\n>>> ")
    reports[0](CtrlC(True, True, raised))
    widget._on_local_stop_timeout()
    assert session.kills == 0 and len(CtrlCSession.instances) == 1
    submit(widget, "print(42)")  # the REPL is still there to talk to
    assert not widget._stop_pending and session.sent[-1] == b"print(42)\r\n"


def test_output_from_before_the_ctrl_c_was_raised_is_no_answer(make, tmp_path, reports):
    """What the command printed before the Ctrl+C reached it (it got here
    after Stop was pressed) says nothing about the Ctrl+C."""
    from turboadb.gui.local_terminal import CtrlC

    widget = make("cmd")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "stubborn.exe")
    out(widget, "stubborn.exe\r\nworking\r\n")
    widget.interrupt()
    out(widget, "still working\r\n")
    time.sleep(0.05)
    reports[0](CtrlC(True, True, time.monotonic()))  # raised only now
    widget._on_local_stop_timeout()
    assert session.kills == 1  # not a word since: it ignored the Ctrl+C


def test_stop_again_ends_the_processes_before_it_replaces_the_shell(make, tmp_path):
    widget = make("cmd")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "tool.exe")
    out(widget, "tool.exe\r\nworking\r\n")
    widget.interrupt()
    out(widget, "still working\r\n")  # it answered, but runs on
    widget.interrupt()  # Stop again: end its processes, the shell stays
    assert session.kills == 1 and len(CtrlCSession.instances) == 1
    widget.interrupt()  # and again: a fresh shell
    assert len(CtrlCSession.instances) == 2
    assert screen(widget).endswith("Stopped — fresh local shell ready\n")


def test_stop_at_a_continuation_prompt_never_runs_the_block(make, tmp_path, monkeypatch):
    """PowerShell reading the rest of a block (">>") ignores Ctrl+C, and an
    empty line would end the block and run it: `if (...) { rm x }` ran when
    the user pressed Stop to cancel it."""
    monkeypatch.setattr(CtrlCSession, "ctrl_c", (True, False))  # PowerShell itself reads
    widget = make("powershell")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "if ($true) {")
    out(widget, "if ($true) {\n>> ")
    submit(widget, "rm x }")
    out(widget, "rm x }\n>> ")
    widget.interrupt()
    widget._probe_local_prompt()
    widget._on_local_stop_timeout()
    assert len(CtrlCSession.instances) == 2  # dropped with the old shell
    assert b"\n" not in session.sent
    assert screen(widget).endswith("^C\nStopped — fresh local shell ready\n")


def test_no_console_ctrl_c_falls_back_to_ending_the_processes(make, tmp_path, monkeypatch):
    monkeypatch.setattr(CtrlCSession, "ctrl_c", (False, None))
    widget = make("cmd")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "ping -t 127.0.0.1")
    out(widget, "ping -t 127.0.0.1\r\n\r\nReply 1\r\n")
    widget.interrupt()
    assert session.ctrl_cs == 1 and session.kills == 1
    out(widget, "^C\r\n" + prompt(widget, tmp_path))
    assert not widget._stop_pending


def test_without_ctrl_c_a_script_is_never_answered(make, tmp_path, monkeypatch):
    """The processes were ended, the script went on into Read-Host: an empty
    line would answer it."""
    monkeypatch.setattr(CtrlCSession, "ctrl_c", (False, None))
    widget = make("powershell")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "& .\\deploy.ps1")
    out(widget, "& .\\deploy.ps1\n\r\nReply 1\r\n")
    widget.interrupt()
    out(widget, "Continue (Enter = yes): ")
    widget._stop_probe_timer.stop()
    widget._probe_local_prompt()
    assert b"\n" not in session.sent[1:]  # only the typed line went out
    widget._on_local_stop_timeout()
    assert len(CtrlCSession.instances) == 2  # replaced: the script ends with it


def test_a_background_job_ended_at_the_prompt_is_asked_for_a_new_prompt(make, tmp_path, monkeypatch):
    monkeypatch.setattr(CtrlCSession, "ctrl_c", (False, None))
    widget = make("cmd")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    out(widget, "a background job printed this\r\n")  # start /b ...
    widget.interrupt()
    widget._stop_probe_timer.stop()
    widget._probe_local_prompt()
    assert session.sent[-1] == b"\n" and widget.term._pending_echo == "\n"


def _prompt_is_back_and_quiet(widget):
    """Let the paste timer have its turn, as it does once the shell's prompt
    has been showing for a moment."""
    widget.term._submitted_at -= 1.0
    widget.term._last_feed -= 1.0
    widget.term._pump_paste()


def test_the_stop_button_drops_the_rest_of_a_paste(make, tmp_path):
    """The lines pasted after the one that hangs were for the command being
    stopped.  Ctrl+C dropped them; the Stop button let them run as soon as
    the prompt was back."""
    widget = make("cmd")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    widget.term._paste_text("ping -t 127.0.0.1\necho SECOND\necho THIRD\n")
    out(widget, "ping -t 127.0.0.1\r\n\r\nPinging 127.0.0.1\r\nReply 1\r\n")
    widget.btn_stop.click()
    assert session.ctrl_cs == 1
    out(widget, "\r\nPing statistics\r\nControl-C\r\n^C\r\n" + prompt(widget, tmp_path))
    _prompt_is_back_and_quiet(widget)
    assert session.sent == [b"ping -t 127.0.0.1\r\n"]
    assert not widget.term._pasting and not widget.term._paste_timer.isActive()


def test_the_stop_button_drops_the_rest_of_a_paste_in_an_adb_shell_too(make, tmp_path):
    widget = make("cmd")
    session = widget.session
    out(widget, prompt(widget, tmp_path))
    submit(widget, "adb shell")
    out(widget, "adb shell -t -t\r\nPD2318:/ $ ")
    widget.term._paste_text("logcat\nrm -r /sdcard/x\n")
    out(widget, "logcat\r\r\n01-01 12:00:00.000 I Tag: hello\r\r\n")
    widget.btn_stop.click()
    assert session.sent[-1] == b"\x03"  # to the device command; the adb shell stays
    out(widget, "^C\r\r\n130|PD2318:/ $ ")
    _prompt_is_back_and_quiet(widget)
    assert session.sent[-1] == b"\x03" and not widget.term._pasting


def _real_widget(qapp, tmp_path, shell):
    from turboadb.gui.device_tab import _LocalShellWidget

    widget = _LocalShellWidget(shell)
    widget._shell_cwd = str(tmp_path)
    widget.ensure_started()
    return widget


def _pump(qapp, until, limit=20.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.skipif(os.name != "nt", reason="starts a real powershell.exe")
def test_real_stop_abandons_the_rest_of_a_powershell_line(qapp, tmp_path):
    marker = tmp_path / "ran.txt"
    widget = _real_widget(qapp, tmp_path, "powershell")
    try:
        assert _pump(qapp, widget._at_idle_prompt, 60)
        pid = widget.session.proc.pid
        submit(widget, f"ping -n 30 127.0.0.1; 'x' | Out-File '{marker}'")
        assert _pump(qapp, lambda: widget.term.toPlainText().count("Reply from") >= 2, 20)
        widget.interrupt()
        assert _pump(qapp, lambda: not widget._stop_pending and widget._at_idle_prompt(), 10)
        # as a console's Ctrl+C: ping's summary (drawn once the queue is rendered)
        assert _pump(qapp, lambda: "Ping statistics" in widget.term.toPlainText(), 5)
        time.sleep(0.5)
        assert not marker.exists()  # the second statement never ran
        assert widget.session.proc.pid == pid  # the same shell
    finally:
        widget.close_panel()


@pytest.mark.skipif(os.name != "nt", reason="starts a real cmd.exe")
def test_real_stop_leaves_a_repl_that_answered_running(qapp, tmp_path):
    """python's own prompt: Stop interrupts what it runs, and the REPL is
    still there, with its variables, after the wait that ends a command which
    ignored the Ctrl+C."""
    import sys

    widget = _real_widget(qapp, tmp_path, "cmd")
    try:
        assert _pump(qapp, widget._at_idle_prompt, 30)
        submit(widget, f'"{sys.executable}" -i -u')
        assert _pump(qapp, lambda: widget._prompt_tail.endswith(">>> "), 30)
        submit(widget, "kept = 41; import time; time.sleep(60)")
        _pump(qapp, lambda: False, 0.5)
        widget.interrupt()
        assert _pump(qapp, lambda: "KeyboardInterrupt" in widget._prompt_tail, 10)
        _pump(qapp, lambda: False, widget.LOCAL_STOP_WAIT_MS / 1000 + 1.0)
        submit(widget, "print(kept + 1)")
        assert _pump(qapp, lambda: "42" in widget._prompt_tail.split("KeyboardInterrupt")[-1], 10)
    finally:
        widget.close_panel()


@pytest.mark.skipif(os.name != "nt", reason="starts a real powershell.exe")
def test_real_stop_keeps_a_powershell_that_is_slow_to_show_its_prompt(qapp, tmp_path):
    """A .NET call takes the Ctrl+C only once it returns: PowerShell was
    replaced meanwhile, although it stopped by itself a second later."""
    marker = tmp_path / "ran.txt"
    widget = _real_widget(qapp, tmp_path, "powershell")
    try:
        assert _pump(qapp, widget._at_idle_prompt, 60)
        pid = widget.session.proc.pid
        submit(widget, "$tadbv = 'kept-6'")
        assert _pump(qapp, widget._at_idle_prompt, 20)
        submit(widget, f"[Threading.Thread]::Sleep(1500); 'x' | Out-File '{marker}'")
        assert _pump(qapp, lambda: "Sleep(1500)" in widget._prompt_tail, 10)  # PowerShell read it
        widget.interrupt()
        assert _pump(qapp, lambda: not widget._stop_pending and widget._at_idle_prompt(), 15)
        assert widget.session.proc.pid == pid  # the same shell
        assert not marker.exists()  # the rest of the line was abandoned
        submit(widget, "echo $tadbv")
        assert _pump(qapp, lambda: "kept-6" in widget.term.toPlainText().split("echo $tadbv")[-1], 10)
    finally:
        widget.close_panel()


@pytest.mark.skipif(os.name != "nt", reason="starts a real cmd.exe")
def test_real_stop_at_a_batch_files_pause_ends_the_batch_file(qapp, tmp_path):
    marker = tmp_path / "ran.txt"
    script = tmp_path / "act.cmd"
    script.write_text(f'@echo off\r\necho about to act\r\npause\r\necho ACTED > "{marker}"\r\n')
    widget = _real_widget(qapp, tmp_path, "cmd")
    try:
        assert _pump(qapp, widget._at_idle_prompt, 30)
        pid = widget.session.proc.pid
        submit(widget, f'"{script}"')
        assert _pump(qapp, lambda: "Press any key" in widget.term.toPlainText(), 10)
        widget.interrupt()
        assert _pump(qapp, lambda: not widget._stop_pending and widget._at_idle_prompt(), 10)
        time.sleep(0.5)
        assert not marker.exists()  # Stop did not press a key for the user
        # pause waits for a line: only a fresh shell (same folder) ends it
        assert widget.session.proc.pid != pid and widget._shell_cwd == str(tmp_path)
    finally:
        widget.close_panel()


_NO_CONSOLE_CHILD = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
from turboadb.gui import local_terminal as lt

def read_until(session, needle, limit):
    out = ""
    end = time.monotonic() + limit
    while time.monotonic() < end and needle not in out:
        out += session.read(65536).decode("utf-8", "replace")
        time.sleep(0.02)
    return out

lines = []
for shell in ("cmd", "cmd"):  # the second starts after this process ignores Ctrl+C
    session = lt.LocalShellSession(shell, adb_path="__missing__")
    read_until(session, ">", 30)
    session.send(b"ping -t 127.0.0.1\n")
    read_until(session, "Reply from", 15)
    results = []
    session.send_ctrl_c(results.append)
    text = read_until(session, "Control-C", 10)
    time.sleep(0.3)
    lines.append(f"{results} {'Control-C' in text} {session.running} {lt._ignoring_ctrl_c}")
    session.close()
lines.append("survived")
open(sys.argv[2], "w").write("\n".join(lines))
'''


@pytest.mark.skipif(os.name != "nt", reason="starts real cmd.exe shells")
def test_real_ctrl_c_from_a_process_without_a_console(tmp_path):
    """The one-file GUI exe has no console: it attaches to the shell's
    console itself, and must ignore that Ctrl+C, and later shells must still
    get theirs (a child inherits "ignore Ctrl+C")."""
    import subprocess
    import sys

    import turboadb

    script = tmp_path / "child.py"
    script.write_text(_NO_CONSOLE_CHILD)
    report = tmp_path / "report.txt"
    root = os.path.dirname(os.path.dirname(os.path.abspath(turboadb.__file__)))
    # The interpreter itself: a venv's python.exe is a launcher, and the
    # interpreter it starts under a detached launcher gets a console again.
    python = getattr(sys, "_base_executable", None) or sys.executable
    proc = subprocess.Popen([python, str(script), root, str(report)],
                            creationflags=0x00000008)  # DETACHED_PROCESS: no console
    assert proc.wait(120) == 0
    lines = report.read_text().splitlines()
    assert lines[-1] == "survived"  # the Ctrl+C it raised did not end this process
    for line in lines[:2]:
        # [CtrlC(sent, programs)] ping's summary, the shell alive, ignoring
        assert line == "[(True, True)] True True True", lines


@pytest.mark.skipif(os.name != "nt", reason="starts a real cmd.exe")
def test_real_console_ctrl_c_reaches_the_command(qapp, tmp_path):
    from turboadb.gui import local_terminal as lt

    session = lt.LocalShellSession("cmd", cwd=str(tmp_path), adb_path="__missing__")
    try:
        out = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "Reply from" not in out:
            if ">" in out and "ping" not in out:
                session.send(b"ping -t 127.0.0.1\n")
                out += "ping sent"
            chunk = session.read(65536)
            out += chunk.decode("utf-8", "replace")
            time.sleep(0.02)
        assert "Reply from" in out
        results = []
        assert session.send_ctrl_c(results.append)
        deadline = time.monotonic() + 10
        # ping may print its summary before the worker thread reports back
        while time.monotonic() < deadline and not ("Control-C" in out and results):
            out += session.read(65536).decode("utf-8", "replace")
            time.sleep(0.02)
        assert results and results[0].sent and results[0].programs
        assert "Control-C" in out and session.running  # ping's own summary; cmd stays
    finally:
        session.close()


@pytest.mark.skipif(os.name != "nt", reason="starts a real cmd.exe")
def test_real_ctrl_c_report_names_the_moment_it_was_raised(tmp_path):
    """The command's answer never comes before that moment, though often
    before the report itself."""
    from turboadb.gui import local_terminal as lt

    session = lt.LocalShellSession("cmd", cwd=str(tmp_path), adb_path="__missing__")
    try:
        out = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "Reply from" not in out:
            if ">" in out and "ping" not in out:
                session.send(b"ping -t 127.0.0.1\n")
                out += "ping sent"
            out += session.read(65536).decode("utf-8", "replace")
            time.sleep(0.02)
        assert "Reply from" in out
        results, answered = [], None
        pressed = time.monotonic()
        assert session.send_ctrl_c(results.append)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (answered and results):
            out += session.read(65536).decode("utf-8", "replace")
            if answered is None and "Control-C" in out:
                answered = time.monotonic()  # ping's summary
            time.sleep(0.005)
        assert results and results[0].sent and answered
        assert pressed <= results[0].at <= answered
    finally:
        session.close()
