"""Stop in the Android shell must not kill a program that answered Ctrl+C.

On a device terminal Stop sends Ctrl+C and waits for the device prompt.  A
program that handles Ctrl+C and carries on with a prompt of its own
(``sqlite3``'s ``sqlite> ``, a device Python), or a shell whose prompt does
not look like mksh's (``bash``'s ``user@host:~$``), never brings that prompt
back: the shell was reopened after 3 s anyway, which killed the program and
everything the shell held (``su``, exported variables).  Only a command that
ignores Ctrl+C (nothing but the terminal's ``^C`` comes back) is ended by
reopening the shell; a second Stop still reopens at once."""

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


class _WithRepl(_Mksh):
    """mksh plus ``sqlite3``: a program with its own prompt that answers
    Ctrl+C with a new prompt line and keeps running."""

    def _line(self, line):
        if self._job == "sqlite3":
            if line == ".quit":
                self._job = None
                self.emit(self.prompt())
            else:
                self.emit("sqlite> ")
            return
        if line == "sqlite3":
            self._job = "sqlite3"
            self.emit("sqlite> ")
            return
        super()._line(line)

    def _interrupt(self):
        if self._job == "sqlite3":
            self.emit("^C\nsqlite> ")  # interrupted, still running
            return
        super()._interrupt()


@pytest.fixture
def quick_stop(monkeypatch):
    from turboadb.gui.device_tab import _AndroidShellWidget

    monkeypatch.setattr(_AndroidShellWidget, "INTERRUPT_WAIT_MS", 300)


def test_a_program_that_answers_ctrl_c_is_not_killed(qapp, quick_stop):
    widget, handler = _widget(qapp, _Handler(lambda tty: _WithRepl(tty)))
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "sqlite3")
        _settle(qapp, widget, "sqlite> ")
        widget.interrupt()
        assert handler.sessions[0].sent[-1] == b"\x03"
        _settle(qapp, widget, "^C\nsqlite> ")
        _pump(qapp, lambda: False, timeout=0.8)  # well past INTERRUPT_WAIT_MS
        assert len(handler.sessions) == 1 and handler.sessions[0].running
        assert "reopened" not in term.toPlainText()
        _enter(term, ".quit")  # the program is still there
        _settle(qapp, widget, "PD2318:/ $ ")
        assert len(handler.sessions) == 1
    finally:
        _close(widget)


class _Bash(_Mksh):
    """A shell whose prompt is not mksh's (``bash``: ``user@host:~$``)."""

    def prompt(self):
        return "u@car:~$ "


def test_ctrl_c_at_a_prompt_that_does_not_look_like_mksh_keeps_the_shell(qapp, quick_stop):
    widget, handler = _widget(qapp, _Handler(lambda tty: _Bash(tty)))
    try:
        term = widget.term
        _ready(qapp, widget, prompt="u@car:~$ ")
        assert not widget._at_device_prompt  # not a prompt the widget knows
        widget.interrupt()
        _settle(qapp, widget, "^C\nu@car:~$ ")
        _pump(qapp, lambda: False, timeout=0.8)
        assert len(handler.sessions) == 1 and handler.sessions[0].running
        assert "reopened" not in term.toPlainText()
    finally:
        _close(widget)


def test_a_command_that_only_echoes_ctrl_c_is_still_ended_by_a_reopen(qapp, quick_stop):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "hang")  # ignores SIGINT: the terminal's "^C" is all that comes
        widget.interrupt()
        assert _pump(qapp, lambda: len(handler.sessions) == 2)
        _settle(qapp, widget, "Still running — Android shell reopened\nPD2318:/ $ ")
    finally:
        _close(widget)


def test_a_second_stop_after_an_answer_still_reopens(qapp, quick_stop):
    widget, handler = _widget(qapp, _Handler(lambda tty: _WithRepl(tty)))
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "sqlite3")
        _settle(qapp, widget, "sqlite> ")
        widget.interrupt()
        _settle(qapp, widget, "^C\nsqlite> ")
        _pump(qapp, lambda: False, timeout=0.5)
        assert len(handler.sessions) == 1
        widget.interrupt()  # Stop again, nothing typed since: the hard way
        assert len(handler.sessions) == 2 and not handler.sessions[0].running
    finally:
        _close(widget)
