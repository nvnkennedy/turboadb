"""What the Android shell writes to the device terminal, and when.

* Input goes out on a thread of its own, in order: once adb.exe stopped
  reading it (a command that reads no input, a long paste) a write on the UI
  thread froze the whole window.  Ctrl+C drops what is still queued, as a
  terminal drops what was typed ahead.
* The device terminal hears the view's new width with the next command typed
  at its prompt (``stty cols C rows R`` in front of it, its echo hidden), so
  ``ls`` lays out its columns for the window as it is now, and nothing is
  ever typed into a running program.
"""

import threading
import time

import pytest

pytest.importorskip("PyQt5")

from test_android_shell_pty import (  # noqa: E402
    _Handler, _Mksh, _after_banner, _close, _enter, _pump, _ready, _settle, _widget,
)


class _StuckSession:
    """A session whose input pipe is full until *free* is set."""

    running = True

    def __init__(self):
        self.free = threading.Event()
        self.sent = []

    def send(self, data):
        self.free.wait(10)
        self.sent.append(data)
        return True

    def read(self, _size=65536):
        time.sleep(0.005)
        return b""

    def close(self):
        self.running = False
        self.free.set()


def test_input_that_is_stuck_never_holds_up_the_window_and_keeps_its_order():
    from turboadb.gui.device_tab import _ShellInput

    session = _StuckSession()
    writer = _ShellInput(session)
    started = time.monotonic()
    assert writer.send(b"first\n") is True  # waits a moment, then leaves it queued
    assert time.monotonic() - started < _ShellInput.WAIT_S + 0.5
    started = time.monotonic()
    for index in range(50):
        assert writer.send(b"line %d\n" % index) is True
    assert time.monotonic() - started < 0.2  # behind a stuck write: no waiting at all
    session.free.set()
    deadline = time.monotonic() + 5
    while len(session.sent) < 51 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.sent == [b"first\n"] + [b"line %d\n" % i for i in range(50)]
    writer.close()


def test_a_full_queue_and_a_closed_shell_refuse_input():
    from turboadb.gui.device_tab import _ShellInput

    session = _StuckSession()
    writer = _ShellInput(session)
    writer.QUEUE_MAX = 100
    assert writer.send(b"x" * 10) is True  # being written: stuck in the pipe
    assert writer.send(b"y" * 60) is True
    assert writer.send(b"y" * 40) is True
    assert writer.send(b"z") is False  # QUEUE_MAX waits behind it already
    writer.discard()
    assert writer.send(b"z") is True
    writer.close()
    assert writer.send(b"after") is False
    session.free.set()


def test_a_shell_that_ended_answers_false_at_once():
    from turboadb.gui.device_tab import _ShellInput

    class Ended:
        def send(self, _data):
            return False

    writer = _ShellInput(Ended())
    assert writer.send(b"ls\n") is False
    assert writer.send(b"ls\n") is False


def test_stop_drops_typed_ahead_input_and_ctrl_c_goes_next(qapp):
    session = _Mksh()
    stuck = threading.Event()
    real_send = session.send

    def send(data):
        if data == b"stuck\n":
            stuck.wait(10)  # adb.exe stopped reading
        return real_send(data)

    session.send = send
    widget, handler = _widget(qapp, _Handler(make=lambda tty: session))
    try:
        _ready(qapp, widget)
        term = widget.term
        _enter(term, "tick")
        started = time.monotonic()
        _enter(term, "stuck")
        _enter(term, "typed ahead")
        assert time.monotonic() - started < 1.0  # the window never waited for the pipe
        widget.interrupt()
        stuck.set()
        assert _pump(qapp, lambda: session.sent[-1:] == [b"\x03"])
        assert b"typed ahead\n" not in session.sent
        assert session.sent[-2:] == [b"stuck\n", b"\x03"]
    finally:
        stuck.set()
        _close(widget)


def test_a_wider_view_reaches_the_device_with_the_next_command(qapp):
    widget, handler = _widget(qapp)
    try:
        _ready(qapp, widget)
        session = handler.sessions[0]
        term = widget.term
        cols, rows = widget._tty_size
        assert f"stty cols {cols} rows {rows}" in session.init_line
        _enter(term, "ls")
        _settle(qapp, widget, "PD2318:/ $ ")
        assert session.sent[-1] == b"ls\n"  # the same width: nothing in front
        widget.resize(1400, 500)
        wide = widget._terminal_size()
        assert wide[0] > cols
        _enter(term, "ls")
        _settle(qapp, widget, "PD2318:/ $ ")
        setting = "stty cols {} rows {} 2>/dev/null; ".format(*wide)
        assert session.sent[-1] == (" " + setting + "ls\n").encode()
        assert session.size == wide
        assert "stty" not in _after_banner(term)  # its echo never shows
        assert _after_banner(term).endswith("PD2318:/ $ ls\nacct\ncache\nPD2318:/ $ ")
        _enter(term, "ls")
        _settle(qapp, widget, "PD2318:/ $ ")
        assert session.sent[-1] == b"ls\n"  # told once
    finally:
        _close(widget)


def test_input_for_a_running_program_is_never_given_a_size(qapp):
    widget, handler = _widget(qapp)
    try:
        _ready(qapp, widget)
        session = handler.sessions[0]
        _enter(widget.term, "ask")
        _settle(qapp, widget, "Continue? ")
        widget.resize(1400, 500)
        _enter(widget.term, "yes")
        _settle(qapp, widget, "got [yes]\nPD2318:/ $ ")
        assert session.sent[-1] == b"yes\n"
    finally:
        _close(widget)


def test_a_line_with_no_room_left_goes_without_the_size(qapp):
    widget, handler = _widget(qapp)
    try:
        _ready(qapp, widget)
        session = handler.sessions[0]
        widget.resize(1400, 500)
        line = "echo " + "x" * (widget._LINE_MAX - 10)
        _enter(widget.term, line)
        assert _pump(qapp, lambda: session.sent[-1] == (line + "\n").encode())
        before = widget._tty_size
        assert before[0] != widget._terminal_size()[0]  # the next command tells it
    finally:
        _close(widget)
