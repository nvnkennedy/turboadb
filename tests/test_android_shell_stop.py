"""Stop and Restart shell in the Android shell on a device terminal.

Stop used to close and reopen ``adb shell``: there was no SIGINT, so a
command's last words (``ping``'s summary) were lost, and the prompt was
drawn twice.  On a device terminal Stop sends Ctrl+C: the command stops and
the shell stays.  Only a command that ignores it (no prompt within
``INTERRUPT_WAIT_MS``), or a second Stop, reopens the shell in the same
folder; Restart shell always reopens it."""

import pytest

pytest.importorskip("PyQt5")

from test_android_shell_pty import (  # noqa: E402
    _Handler,
    _Mksh,
    _close,
    _enter,
    _pump,
    _ready,
    _settle,
    _widget,
)


@pytest.fixture
def quick_stop(monkeypatch):
    """Ctrl+C gets 1 s (not 3) to bring the prompt back."""
    from turboadb.gui.device_tab import _AndroidShellWidget

    monkeypatch.setattr(_AndroidShellWidget, "INTERRUPT_WAIT_MS", 1000)


def _after(text, notice):
    """What the terminal shows after the last *notice*."""
    return text[text.rindex(notice) + len(notice):]


def test_stop_sends_ctrl_c_and_the_shell_stays(qapp, quick_stop):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        session = handler.sessions[0]
        _enter(term, "ping")
        assert _pump(qapp, lambda: "icmp_seq=3" in term.toPlainText())
        widget.interrupt()
        assert session.sent[-1] == b"\x03" and widget._interrupt_pending
        # ping prints its summary, the device its prompt: the Stop is settled
        _settle(qapp, widget, "--- 8.8.8.8 ping statistics ---\nPD2318:/ $ ")
        assert not widget._interrupt_pending
        _pump(qapp, lambda: False, timeout=1.3)  # longer than INTERRUPT_WAIT_MS
        assert handler.opened == [True] and session.running  # never reopened
        text = term.toPlainText()
        assert text.count("^C") == 1  # the device's own echo of it
        assert "stopped" not in text and "reopened" not in text
        _enter(term, "ls")  # the same shell carries on
        _settle(qapp, widget, "cache\nPD2318:/ $ ")
    finally:
        _close(widget)


def test_ctrl_c_at_an_idle_prompt_gives_a_new_prompt_line(qapp, quick_stop):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_C, Qt.ControlModifier, "\x03"))
        assert handler.sessions[0].sent[-1] == b"\x03"
        _settle(qapp, widget, "PD2318:/ $ ^C\nPD2318:/ $ ")
        _pump(qapp, lambda: False, timeout=1.3)
        assert len(handler.sessions) == 1
    finally:
        _close(widget)


def test_a_command_that_ignores_ctrl_c_is_stopped_by_reopening_in_its_folder(qapp, monkeypatch):
    from turboadb.gui.device_tab import _AndroidShellWidget

    monkeypatch.setattr(_AndroidShellWidget, "INTERRUPT_WAIT_MS", 300)
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "cd /sdcard")
        _settle(qapp, widget, "PD2318:/sdcard $ ")
        _enter(term, "hang")
        first = handler.sessions[0]
        widget.interrupt()
        assert first.sent[-1] == b"\x03"
        assert _pump(qapp, lambda: len(handler.sessions) == 2)
        assert not first.running  # closed: its process group ends with it
        assert handler.opened == [True, True]
        again = handler.sessions[1]
        assert b"; cd /sdcard 2>/dev/null; " in again.sent[0]  # back in its folder, hidden
        _settle(qapp, widget, "PD2318:/sdcard $ ")
        text = term.toPlainText()
        # one notice and one prompt after it: nothing of the new shell's start
        assert _after(text, "Still running — Android shell reopened in /sdcard\n") == \
            "PD2318:/sdcard $ "
    finally:
        _close(widget)


def test_a_second_stop_reopens_at_once(qapp, monkeypatch):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "hang")
        widget.interrupt()
        assert len(handler.sessions) == 1 and widget._interrupt_pending
        widget.interrupt()  # Stop again before the prompt came back
        assert len(handler.sessions) == 2 and not handler.sessions[0].running
        assert handler.sessions[0].sent.count(b"\x03") == 1
        _settle(qapp, widget, "PD2318:/ $ ")
        assert _after(term.toPlainText(), "Stopped — Android shell reopened\n") == "PD2318:/ $ "
    finally:
        _close(widget)


def test_input_after_ctrl_c_keeps_the_shell(qapp, quick_stop):
    """A program that handles Ctrl+C and asks something: the next line is its
    answer, and the shell is not reopened under it."""
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "hang")
        widget.interrupt()
        _enter(term, "y")
        assert not widget._interrupt_pending
        _pump(qapp, lambda: False, timeout=1.3)
        assert len(handler.sessions) == 1
        widget.interrupt()  # a Stop after that sends Ctrl+C again
        assert handler.sessions[0].sent[-1] == b"\x03"
    finally:
        _close(widget)


def test_stop_drops_the_rest_of_a_paste(qapp, quick_stop):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        term._paste_text("ping\nls\nls\n")
        assert _pump(qapp, lambda: "icmp_seq=2" in term.toPlainText())
        widget.interrupt()
        _settle(qapp, widget, "statistics ---\nPD2318:/ $ ")
        _pump(qapp, lambda: False, timeout=0.4)
        assert handler.sessions[0].sent[1:] == [b"ping\n", b"\x03"]
    finally:
        _close(widget)


def test_restart_shell_reopens_without_ctrl_c(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "cd /sdcard")
        _settle(qapp, widget, "PD2318:/sdcard $ ")
        first = handler.sessions[0]
        widget.restart_shell()
        assert b"\x03" not in first.sent and not first.running
        assert len(handler.sessions) == 2
        assert b"cd /sdcard" in handler.sessions[1].sent[0]
        _settle(qapp, widget, "PD2318:/sdcard $ ")
        assert _after(term.toPlainText(), "Restarting Android shell…\n") == "PD2318:/sdcard $ "
    finally:
        _close(widget)


def test_stop_on_a_shell_that_ended_opens_a_new_one(qapp, quick_stop):
    widget, handler = _widget(qapp)
    lost = []
    widget.disconnected.connect(lambda: lost.append(True))
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "exit")
        assert _pump(qapp, lambda: not term._alive)
        assert lost == [True]  # the device tab brings it back (or Stop does)
        widget.interrupt()
        assert len(handler.sessions) == 2 and term._alive
        _settle(qapp, widget, "PD2318:/ $ ")
    finally:
        _close(widget)


def test_over_pipes_stop_reopens_the_shell_in_its_folder(qapp, monkeypatch):
    from turboadb.gui import settings as settings_mod

    get = settings_mod.get
    monkeypatch.setattr(
        settings_mod, "get",
        lambda key, default=None: False if key == "android_shell_pty" else get(key, default),
    )
    widget, handler = _widget(qapp, _Handler(lambda tty: _Mksh(tty, prompt_first=False)))
    try:
        term = widget.term
        assert handler.opened == [False] and term._emulate_prompt
        _enter(term, "cd /sdcard")
        prompt = "shell@android:/sdcard $ "
        _settle(qapp, widget, prompt)
        widget.interrupt()
        assert handler.opened == [False, False]
        assert b"\x03" not in handler.sessions[0].sent
        assert handler.sessions[1].sent == [b"cd /sdcard\n"]
        _settle(qapp, widget, "^C  — stopped\n" + prompt)
        assert term.toPlainText().count("$ ") == 2  # one before, one after: never two

        # Ctrl+C from the keyboard takes the same path: one prompt again
        from PyQt5.QtCore import Qt
        from PyQt5.QtGui import QKeyEvent

        term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_C, Qt.ControlModifier, "\x03"))
        assert len(handler.sessions) == 3
        # the prompt still showing stays below the new notice: no second one
        _settle(qapp, widget, "^C  — stopped\n^C  — stopped\n" + prompt)
        _pump(qapp, lambda: False, timeout=0.4)
        assert term.toPlainText().count("$ ") == 2
    finally:
        _close(widget)
