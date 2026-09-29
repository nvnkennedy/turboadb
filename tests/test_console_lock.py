"""One lock for attaching this process to another process's console.

A process is attached to one console at a time.  scrcpy's clean stop (a
Ctrl+Break on scrcpy's hidden console) and a local terminal's Stop (a Ctrl+C
on the shell's hidden console) each held a lock of their own, so one of them
could attach, or free the console, while the other was on one.  They share
``proctree.CONSOLE_LOCK`` now; the terminal's shell start keeps holding it too
(it sets the process's "ignore Ctrl+C" flag for the child).
"""
import os
import threading
import time

import pytest

from turboadb import proctree, scrcpy


def test_scrcpy_and_the_local_terminals_hold_the_same_console_lock():
    lt = pytest.importorskip("turboadb.gui.local_terminal")
    assert scrcpy._CTRL_BREAK_LOCK is proctree.CONSOLE_LOCK
    assert lt._SPAWN_LOCK is proctree.CONSOLE_LOCK


class _Call:
    def __init__(self, name, events, result):
        self.name, self.events, self.result = name, events, result

    def __call__(self, *_args):
        self.events.append(self.name)
        return self.result


@pytest.mark.skipif(os.name != "nt", reason="attaching to a console is a Windows thing")
def test_a_ctrl_break_waits_while_a_terminal_is_on_a_shells_console(monkeypatch):
    import ctypes

    lt = pytest.importorskip("turboadb.gui.local_terminal")
    events = []
    kernel32 = type("Kernel32", (), {})()
    for name, result in (("AttachConsole", 1), ("FreeConsole", 1),
                         ("GenerateConsoleCtrlEvent", 1), ("GetStdHandle", 0),
                         ("SetStdHandle", 1)):
        setattr(kernel32, name, _Call(name, events, result))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel32)

    on_console, release = threading.Event(), threading.Event()

    def terminal_stop():  # what _console_ctrl_c holds while on a shell's console
        with lt._SPAWN_LOCK:
            on_console.set()
            release.wait(5)
            events.append("terminal let go")

    terminal = threading.Thread(target=terminal_stop, daemon=True)
    terminal.start()
    assert on_console.wait(5)
    raised = []
    breaker = threading.Thread(target=lambda: raised.append(scrcpy._send_ctrl_break(4321)),
                               daemon=True)
    breaker.start()
    time.sleep(0.3)
    assert "AttachConsole" not in events  # it waits for the console
    release.set()
    terminal.join(5)
    breaker.join(5)
    assert raised == [True]
    assert events.index("terminal let go") < events.index("AttachConsole")
