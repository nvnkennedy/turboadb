"""Ctrl+C and Stop stop the output at once, and a flood never runs far ahead.

After a logcat flood the console still had up to 8 MB to draw: after Ctrl+C
old lines kept scrolling for seconds, so it looked as if logcat had not
stopped.  Ctrl+C now drops what is not drawn yet (one note says how much;
Save has all of it), on a device terminal also what arrives until the
terminal echoes the Ctrl+C, and the backlog is capped at about a second of
drawing."""

import re
import threading
import time
from collections import deque

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QKeyEvent  # noqa: E402

LINE = "09-29 16:42:56.210  5927  5927 I Finsky  : line {:06d}\r\n"


def _console():
    from turboadb.gui.console import AnsiConsole

    term = AnsiConsole(send_fn=lambda _data: None)
    term.set_emulate_prompt(False)
    term.set_screen_input(lambda _data: True)
    return term


def _render(term):
    while term._inq:
        term._drain_tick()


def _close(term):
    term.close_archive()
    term.deleteLater()


def _flood(n, start=0):
    return "".join(LINE.format(i) for i in range(start, start + n))


# --------------------------------------------------------------------------- #
# the console
# --------------------------------------------------------------------------- #
def test_ctrl_c_drops_what_is_not_drawn_yet_and_says_so(qapp):
    term = _console()
    try:
        term.feed(_flood(5))
        _render(term)
        term.feed(_flood(20000, 5))  # a flood not drawn yet
        term.notice("kept", new_stream=False)
        assert term.interrupt_output(until_echo=False)
        assert term._inq_len == 0
        term.feed("^C\r\n130|V2318:/ $ ")
        _render(term)
        text = term.toPlainText()
        assert "line 000004" in text and "line 000005" not in text
        assert text.count("characters skipped on screen") == 1
        assert text.index("skipped") < text.index("kept") < text.index("^C")
        assert text.endswith("^C\n130|V2318:/ $ ")
        assert "line 020004" in term._sb.full_text()  # Save has everything
    finally:
        _close(term)


def test_output_until_the_terminals_echo_is_dropped_too(qapp):
    term = _console()
    try:
        term.feed(_flood(3))
        _render(term)
        term.interrupt_output()
        term.feed(_flood(5000, 3))  # printed before the device got the Ctrl+C
        term.feed(_flood(5000, 5003) + "^")
        _render(term)
        assert "line 000003" not in term.toPlainText()  # nothing of it drawn meanwhile
        term.feed("C\r\n--- summary ---\r\n130|V2318:/ $ ")  # the echo, cut after its ^
        _render(term)
        text = term.toPlainText()
        assert text.endswith("^C\n--- summary ---\n130|V2318:/ $ ")
        assert text.count("^C") == 1 and "line 000003" not in text
        assert text.count("characters skipped on screen") == 1
        assert "line 010002" in term._sb.full_text()
    finally:
        _close(term)


def test_a_little_output_is_drawn_anyway(qapp):
    """ping's last reply before its summary: drawn in a moment, so kept."""
    term = _console()
    try:
        term.feed("64 bytes: icmp_seq=1\r\n")
        term.interrupt_output()
        term.feed("64 bytes: icmp_seq=2\r\n^C\r\n--- statistics ---\r\n")
        _render(term)
        text = term.toPlainText()
        assert text == "64 bytes: icmp_seq=1\n64 bytes: icmp_seq=2\n^C\n--- statistics ---\n"
        assert "skipped" not in text
    finally:
        _close(term)


def test_without_an_echo_the_output_shows_after_a_moment(qapp, monkeypatch):
    from turboadb.gui.console import AnsiConsole

    monkeypatch.setattr(AnsiConsole, "_CTRL_C_ECHO_S", 0.2)
    term = _console()
    try:
        term.interrupt_output()
        term.feed("still printing\r\n")
        _render(term)
        assert term.toPlainText() == ""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "still printing" not in term.toPlainText():
            qapp.processEvents()
            _render(term)
        assert term.toPlainText() == "still printing\n"
    finally:
        _close(term)


def test_the_shells_prompt_shows_what_waited(qapp):
    term = _console()
    try:
        term.interrupt_output()
        term.feed("last words\r\nV2318:/ $ ")  # a terminal that echoes nothing
        term.mark_prompt()
        _render(term)
        assert term.toPlainText() == "last words\nV2318:/ $ "
    finally:
        _close(term)


def test_a_programs_screen_keeps_a_little_output_and_waits_for_no_echo(qapp):
    """On a program's screen Ctrl+C is a key for it: a flood it printed goes
    (see test_screen_flood), but a little output is drawn, and nothing is
    held for an echo that a program reading its keys never gives."""
    term = _console()
    try:
        term.feed("\x1b[?1049hvi")
        _render(term)
        term.feed("more")
        assert term.interrupt_output() is True
        assert not term._held_until
        term.feed(" still")
        _render(term)
        assert term.screen().display()[0] == "vimore still"
    finally:
        _close(term)


def test_the_backlog_is_about_a_second_of_drawing(qapp):
    term = _console()
    try:
        cap = term._backlog_cap
        term._render_rate = 600000
        assert cap() == 600000
        term._render_rate = 10
        assert cap() == term._MIN_INQ
        term._render_rate = 10 ** 9
        assert cap() == term._MAX_INQ
        term._render_rate = 400000
        term.feed(_flood(3000))  # ~150 KB: kept whole
        assert term._dropped == 0
        term.feed(_flood(20000, 3000))  # a megabyte in one reader batch
        assert term._inq_len <= 400000 and term._dropped > 0
        _render(term)
        text = term.toPlainText()
        assert text.count("skipped on screen") == 1
        after = text[text.index("skipped on screen"):].split("\n")[1]
        assert re.fullmatch(r"09-29 16:42:56\.210 .* line \d{6}", after)  # a whole line
    finally:
        _close(term)


def test_the_drawing_rate_is_measured(qapp):
    term = _console()
    try:
        term._render_rate = 1.0
        term.feed(_flood(4000))
        _render(term)
        assert term._render_rate > 1000
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# the Android shell during a logcat flood
# --------------------------------------------------------------------------- #
class _Flood:
    """mksh on a device terminal whose ``logcat`` floods until Ctrl+C: what
    was on its way still comes, then the terminal's ``^C`` and the prompt."""

    def __init__(self, tty=True):
        self.running = True
        self.sent = []
        self._out = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._buf = ""
        self.emit("V2318:/ $ ")

    def prompt(self, status=0):
        return (f"{status}|" if status else "") + "V2318:/ $ "

    def emit(self, text):
        with self._lock:
            self._out.append(text.replace("\n", "\r\r\n").encode())

    def read(self, _size=65536):
        with self._lock:
            if self._out:
                parts = list(self._out)
                self._out.clear()
                return b"".join(parts)
        time.sleep(0.002)
        return b""

    def send(self, data):
        self.sent.append(bytes(data))
        for ch in data.decode():
            if ch == "\x03":
                self._stop.set()
            elif ch == "\n":
                line, self._buf = self._buf, ""
                self.emit(line + "\n")
                if line.startswith(" ") and "7718" in line:
                    self.emit("\x1b]7718;ready\x07" + self.prompt())
                elif line == "logcat":
                    threading.Thread(target=self._logcat, daemon=True).start()
                else:
                    self.emit(self.prompt())
            else:
                self._buf += ch
        return True

    def _logcat(self):
        n = 0
        while not self._stop.is_set() and n < 400000:
            self.emit(_flood(2000, n).replace("\r\n", "\n"))
            n += 2000
            time.sleep(0.01)
        self.emit(_flood(3000, n).replace("\r\n", "\n"))  # already on its way
        self.emit("^C\n" + self.prompt(130))

    def close(self):
        self.running = False
        self._stop.set()


@pytest.mark.parametrize("how", ["key", "stop", "menu"])
def test_ctrl_c_during_a_flood_stops_it_at_once_with_one_echo(qapp, how):
    from test_android_shell_pty import _Handler, _close as close_widget, _enter, _pump, _widget

    widget, handler = _widget(qapp, _Handler(lambda tty: _Flood(tty)))
    try:
        term = widget.term
        assert _pump(qapp, lambda: term.toPlainText().endswith("V2318:/ $ ") and not term._inq)
        _enter(term, "logcat")
        assert _pump(qapp, lambda: "line 004000" in term._sb.full_text()
                     and "Finsky" in term.toPlainText(), timeout=10)
        if how == "key":
            term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_C, Qt.ControlModifier, "\x03"))
        elif how == "stop":
            widget.interrupt()
        else:
            term._send_interrupt()  # the context menu's Send key > Ctrl+C
        pressed = time.monotonic()
        assert _pump(qapp, lambda: term.toPlainText().endswith("^C\n130|V2318:/ $ ")
                     and not term._inq, timeout=5)
        assert time.monotonic() - pressed < 1.5
        session = handler.sessions[0]
        assert b"".join(session.sent).count(b"\x03") == 1
        text = term.toPlainText()
        assert text.count("^C") == 1
        tail = text[text.rindex("Finsky"):]
        assert "^C" in tail  # no flood line drawn after the echo
        full = term._sb.full_text()
        assert full.count("Finsky") > text.count("Finsky")  # Save kept what the view skipped
    finally:
        close_widget(widget)


def test_ctrl_c_in_an_adb_shell_in_cmd_stops_the_output_too(qapp, monkeypatch, tmp_path):
    from test_local_adb_shell import FakeSession, out, submit
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", FakeSession)
    widget = _LocalShellWidget("cmd", serial="V2318")
    try:
        widget._shell_cwd = str(tmp_path)
        widget.ensure_started()
        widget._strip_startup_banner = False
        submit(widget, "adb shell")
        out(widget, "V2318:/ $ ")
        submit(widget, "logcat")
        widget._feed_from(widget.reader, _flood(20000).encode())  # not drawn yet
        widget.interrupt()
        assert widget.session.sent[-1] == b"\x03" and widget.term._inq_len == 0
        out(widget, _flood(3000, 20000) + "^C\r\r\n130|V2318:/ $ ")
        text = widget.term.toPlainText()
        assert text.endswith("^C\n130|V2318:/ $ ") and "line 0" not in text
    finally:
        widget.close_panel()
        widget.deleteLater()


def test_stop_in_cmd_drops_a_flood_that_is_not_drawn_yet(qapp, monkeypatch, tmp_path):
    from test_local_adb_shell import FakeSession, out
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", FakeSession)
    widget = _LocalShellWidget("cmd", serial="V2318")
    try:
        widget._shell_cwd = str(tmp_path)
        widget.ensure_started()
        widget._strip_startup_banner = False
        out(widget, "C:\\w>\x1b]7717;C:\\w\x07")
        widget._own_prompt = False  # `adb logcat` runs
        widget.term.set_shell_at_prompt(False)
        widget._feed_from(widget.reader, _flood(20000).encode())
        widget.interrupt()
        assert widget.term._inq_len == 0 and widget.term._dropped > 0
        assert "line 019999" in widget.term._sb.full_text()
    finally:
        widget.close_panel()
        widget.deleteLater()
