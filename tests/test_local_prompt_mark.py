"""A prompt the user sets in the terminal keeps the invisible prompt mark.

The PowerShell / CMD terminals know the shell waits for a command from a mark
at the end of every prompt.  ``prompt $G`` in cmd, or ``function prompt {...}``
in PowerShell, replaced the prompt without it: from then on every command
showed twice (the shell's echo was taken for program output), the
conveniences stopped, and Stop at the prompt replaced the whole shell."""

import os
import time

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture
def widget(qapp):
    made = []

    def make(shell_type):
        from turboadb.gui.device_tab import _LocalShellWidget

        w = _LocalShellWidget(shell_type)
        made.append(w)
        return w

    yield make
    for w in made:
        w.close_panel()
        w.deleteLater()


@pytest.mark.parametrize("line, expected", [
    ("prompt $G", "prompt $G$E]7717;$P$E\\"),
    ("PROMPT [$P]$G", "PROMPT [$P]$G$E]7717;$P$E\\"),
    ("prompt", "prompt $P$G$E]7717;$P$E\\"),  # back to the default, still marked
    ("set prompt=$T $G", "set prompt=$T $G$E]7717;$P$E\\"),
    ('set "prompt=$G"', 'set "prompt=$G$E]7717;$P$E\\"'),
    ("set prompt=", "set prompt=$P$G$E]7717;$P$E\\"),
    ("prompt $G & echo x", None),               # more than the prompt command
    ("prompt $G$E]7717;$P$E\\", None),         # marked already
    ("promptx", None),
    ("echo prompt", None),
])
def test_cmd_prompt_commands_keep_the_mark(widget, line, expected):
    assert widget("cmd")._prompt_rewrite(line) == expected


def test_powershell_prompt_function_is_wrapped_again(widget):
    from turboadb.gui.local_terminal import _PS_PROMPT_MARK

    ps = widget("powershell")
    line = "function prompt { '> ' }"
    rewritten = ps._prompt_rewrite(line)
    assert rewritten.startswith(line + ";") and _PS_PROMPT_MARK in rewritten
    assert ps._prompt_rewrite("function global:prompt { 'x> ' };") .startswith(
        "function global:prompt { 'x> ' };if(")
    assert ps._prompt_rewrite("function prompt {") is None  # spread over lines: left alone
    assert ps._prompt_rewrite("Get-Command prompt") is None


@pytest.mark.skipif(os.name != "nt", reason="starts a real cmd.exe")
@pytest.mark.parametrize("shell, change", [
    ("cmd", "prompt $G"),
    ("powershell", "function prompt { '> ' }"),
])
def test_real_custom_prompt_keeps_commands_once(qapp, widget, tmp_path, shell, change):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    w = widget(shell)
    w._shell_cwd = str(tmp_path)

    def pump(until, limit=30.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            qapp.processEvents()
            if until():
                return True
            time.sleep(0.01)
        return False

    def submit(line):
        for ch in line:
            w.term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, 0, Qt.NoModifier, ch))
        w.term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))

    w.ensure_started()
    assert pump(w._at_idle_prompt, 60)
    submit(change)
    assert pump(w._at_idle_prompt, 10)  # the new prompt still carries the mark
    start = len(w.term.toPlainText())
    submit("echo typed-once")
    assert pump(lambda: w._at_idle_prompt() and "\ntyped-once" in w.term.toPlainText()[start:], 10)
    pump(lambda: not w.term._inq, 5)
    assert w.term.toPlainText()[start:].count("echo typed-once") == 1
