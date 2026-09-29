"""When the Android shell cannot have a device terminal.

An adb without ``-t`` or a build that allows no terminal ends ``adb shell
-t -t`` at once (or adb reports an error) before any prompt: the shell falls
back to plain pipes, where TurboADB draws the prompt, and the tab keeps
using pipes.  The ``android_shell_pty`` setting forces pipes.  A device
without shell_v2 (Android 6 and older) gives every adb shell a terminal,
also over pipes: its prompt before any command gives that away, and it is
then handled as a terminal (no second prompt, no doubled echo)."""

import threading
from collections import deque

import pytest

pytest.importorskip("PyQt5")

from test_android_shell_pty import (  # noqa: E402
    _Handler,
    _Mksh,
    _close,
    _enter,
    _pump,
    _settle,
    _widget,
)


class _Ends:
    """``adb shell -t -t`` that prints *message* and ends before any prompt
    (*linger*: the process lives on after it)."""

    def __init__(self, message, linger=False):
        self._out = deque([message.encode("utf-8")])
        self._lock = threading.Lock()
        self._read = False
        self.linger = linger
        self.sent = []
        self.closed = False

    @property
    def running(self):
        return not self.closed and (self.linger or not self._read)

    def read(self, _size=65536):
        with self._lock:
            if self._out:
                self._read = True
                return self._out.popleft()
        return b""

    def send(self, data):
        self.sent.append(bytes(data))
        return self.running

    def close(self):
        self.closed = True


def _pipes(monkeypatch):
    from turboadb.gui import settings as settings_mod

    get = settings_mod.get
    monkeypatch.setattr(
        settings_mod, "get",
        lambda key, default=None: False if key == "android_shell_pty" else get(key, default),
    )


@pytest.mark.parametrize("message", [
    "/system/bin/sh: -t: inaccessible or not found\r\n",  # an adb that has no -t
    "error: closed\r\n",                                  # a build that allows no terminal
])
def test_no_device_terminal_falls_back_to_pipes_and_stays_there(qapp, message):
    handler = _Handler(lambda tty: _Ends(message) if tty else _Mksh(tty))
    widget, _ = _widget(qapp, handler)
    lost = []
    widget.disconnected.connect(lambda: lost.append(True))
    try:
        term = widget.term
        assert _pump(qapp, lambda: handler.opened == [True, False])
        assert not widget._pty and term._emulate_prompt and term._alive and lost == []
        prompt = "shell@android:/ $ "
        _settle(qapp, widget, prompt)
        text = term.toPlainText()
        assert "No device terminal (" + message.strip() + ")" in text
        assert "Stop reopens it" in text
        assert handler.sessions[1].sent == []  # no hidden first line over pipes
        _enter(term, "ls")
        assert handler.sessions[1].sent == [b"ls -C\n"]  # in columns, as over pipes before
        _settle(qapp, widget, "acct\ncache\n" + prompt)
        widget.interrupt()  # a reopen stays on pipes
        assert handler.opened == [True, False, False]
    finally:
        _close(widget)


def test_an_adb_error_before_the_first_prompt_does_not_wait_for_adb_to_exit(qapp):
    handler = _Handler(lambda tty: _Ends("error: terminal refused\r\n", linger=True)
                       if tty else _Mksh(tty))
    widget, _ = _widget(qapp, handler)
    try:
        assert _pump(qapp, lambda: handler.opened == [True, False])
        assert handler.sessions[0].closed and not widget._pty
    finally:
        _close(widget)


def test_an_adb_warning_is_not_a_missing_terminal(qapp):
    class Warned(_Mksh):
        def __init__(self, tty):
            super().__init__(tty, prompt_first=False)
            self.emit("adb: warning: something to say\n" + self.prompt())

    handler = _Handler(lambda tty: Warned(tty))
    widget, _ = _widget(qapp, handler)
    try:
        _settle(qapp, widget, "PD2318:/ $ ")
        assert handler.opened == [True] and widget._pty
    finally:
        _close(widget)


def test_a_device_out_of_reach_is_a_lost_shell_not_a_missing_terminal(qapp):
    handler = _Handler(lambda tty: _Ends("error: device 'PD2318' not found\r\n"))
    widget, _ = _widget(qapp, handler)
    lost = []
    widget.disconnected.connect(lambda: lost.append(True))
    try:
        term = widget.term
        assert _pump(qapp, lambda: lost == [True])
        assert handler.opened == [True] and not term._alive
        assert _pump(qapp, lambda: "error: device 'PD2318' not found" in term.toPlainText())
        assert "No device terminal" not in term.toPlainText()
        widget.reconnect(focus=False)  # the device is back: a terminal again
        assert handler.opened == [True, True]
    finally:
        _close(widget)


def test_a_plain_shell_that_fails_as_well_puts_the_terminal_back(qapp):
    """Both ended at once: the device was the problem, not its terminal."""
    handler = _Handler(lambda tty: _Ends("error: closed\r\n"))
    widget, _ = _widget(qapp, handler)
    lost = []
    widget.disconnected.connect(lambda: lost.append(True))
    try:
        assert _pump(qapp, lambda: lost == [True])
        assert handler.opened == [True, False]
        assert not widget._pty_fallback
        handler.make = lambda tty: _Mksh(tty)
        widget.reconnect(focus=False)
        assert handler.opened == [True, False, True]
        _settle(qapp, widget, "PD2318:/ $ ")
    finally:
        _close(widget)


def test_the_setting_puts_the_shell_on_pipes(qapp, monkeypatch):
    _pipes(monkeypatch)
    widget, handler = _widget(qapp, _Handler(lambda tty: _Mksh(tty)))
    try:
        term = widget.term
        assert handler.opened == [False]
        assert not widget._pty and term._emulate_prompt
        _settle(qapp, widget, "shell@android:/ $ ")
        assert handler.sessions[0].sent == []
        _enter(term, "ls")
        assert handler.sessions[0].sent == [b"ls -C\n"]  # in columns, as before
    finally:
        _close(widget)


def test_a_device_without_shell_v2_is_handled_as_a_terminal_over_pipes(qapp, monkeypatch):
    """Android 6 and older give ``adb shell`` a terminal even without -t: its
    prompt comes before any command, so no prompt of TurboADB's is drawn on
    top and the echo is hidden, not doubled."""
    _pipes(monkeypatch)
    widget, handler = _widget(qapp, _Handler(lambda tty: _Mksh(tty, terminal=True)))
    try:
        term = widget.term
        assert handler.opened == [False]
        _settle(qapp, widget, "PD2318:/ $ ")
        assert widget._pty and not term._emulate_prompt
        session = handler.sessions[0]
        assert session.sent[0].startswith(b" stty cols ")  # set up like a terminal
        text = term.toPlainText()
        assert "shell@android" not in text and text.count("PD2318:/ $ ") == 1
        _enter(term, "ls")
        assert session.sent[-1] == b"ls\n"
        _settle(qapp, widget, "cache\nPD2318:/ $ ")
        assert term.toPlainText().count("ls") == 1
    finally:
        _close(widget)
