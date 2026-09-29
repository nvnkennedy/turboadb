"""The Android shell on a device terminal (``adb shell -t -t``).

The Android tab ran ``adb shell`` over plain pipes and drew a prompt of its
own: a command that waits or streams looked finished (a prompt flickered in
between), device pipelines held their output back until the end, and Stop
could only reopen the shell.  It now runs on a device terminal: the device
prints its own prompt and echo, and a hidden first line turns mksh's line
editor off and sets the terminal's size.

These tests drive the real widget, reader thread and console with
:class:`_Mksh`, a stand-in for mksh on a device terminal as adb.exe hands it
over (CR CR LF line ends).
"""

import re
import threading
import time
from collections import deque

import pytest

pytest.importorskip("PyQt5")

from turboadb.results import OperationResult  # noqa: E402

MARK = "\x1b]7718;ready\x07"


def _pump(qapp, until, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return until()


class _Mksh:
    """ShellSession stand-in for ``adb shell -t -t``: mksh on a device terminal.

    Every line end reaches the PC as CR CR LF (the terminal's CR LF, which
    adb.exe writes in text mode).  The terminal echoes each line as it
    arrives, also while a command runs; until the hidden first line turns
    the line editor off, a line longer than 80 columns comes back garbled.
    Commands: ``ls``, ``cd DIR``, ``tick`` (a line per :meth:`tick`),
    ``ping`` (a reply every 20 ms), both printing a summary on Ctrl+C,
    ``hang`` (ignores Ctrl+C), ``ask`` (``Continue? ``, then reads a line)
    and ``exit``.

    Without *tty* it is mksh over plain pipes: no prompt, no echo, CR LF
    line ends, and a Ctrl+C byte is only input; *terminal* forces a terminal
    anyway, as a device without shell_v2 (Android 6 and older) gives one."""

    def __init__(self, tty=True, host="PD2318", prompt_first=True, terminal=None):
        self.tty = tty
        self.terminal = tty if terminal is None else terminal
        self.running = True
        self.sent = []
        self.host = host
        self.cwd = "/"
        self.editor = True
        self.init_line = None
        self._out = deque()
        self._lock = threading.Lock()
        self._buf = ""
        self._job = None           # the command running
        self._ticks = 0
        self._stop = threading.Event()
        if prompt_first and self.terminal:
            self.emit(self.prompt())

    def prompt(self):
        return f"{self.host}:{self.cwd} $ " if self.terminal else ""

    def emit(self, text):
        end = "\r\r\n" if self.terminal else "\r\n"
        with self._lock:
            self._out.append(text.replace("\n", end).encode("utf-8"))

    def read(self, _size=65536):
        with self._lock:
            if self._out:
                return self._out.popleft()
        time.sleep(0.002)
        return b""

    def send(self, data):
        self.sent.append(bytes(data))
        if not self.running:
            return False
        for ch in data.decode("utf-8"):
            if ch == "\x03" and self.terminal:
                self._interrupt()
                continue
            if ch != "\n":
                self._buf += ch
                continue
            line, self._buf = self._buf, ""
            if self.terminal:
                echo = line
                if self.editor and len(line) > 78:
                    echo = "\r" + line[:20] + "\x1b[K<" + line[-50:]  # sideways scrolling
                self.emit(echo + "\n")
            self._line(line)
        return True

    def close(self):
        self.running = False
        self._stop.set()

    def tick(self):
        self._ticks += 1
        self.emit(f"tick {self._ticks}\n")

    # ---- the device side ----
    def _line(self, line):
        if self._job == "ask":
            self._job = None
            self.emit(f"got [{line}]\n" + self.prompt())
            return
        if self._job:
            return  # input for the running command
        resized = re.match(r" stty cols (\d+) rows (\d+) 2>/dev/null; (?!.*7718)", line)
        if resized:  # the view's new size in front of a command: both run
            self.size = (int(resized.group(1)), int(resized.group(2)))
            line = line[resized.end():]
        if line.startswith(" ") and "7718" in line:  # TurboADB's hidden first line
            self.init_line = line
            self.editor = False
            match = re.search(r"cd '?([^';]*?)'? 2>", line)
            if match:
                self.cwd = match.group(1)
            self.emit(MARK + self.prompt())
            return
        word = line.split()
        if word[:1] == ["ls"]:
            self.emit("acct\ncache\n")
        elif word[:1] == ["cd"]:
            self.cwd = word[1] if len(word) > 1 else "/"
        elif line == "ping":
            self._job = "ping"
            self._stop.clear()
            threading.Thread(target=self._ping, daemon=True).start()
            return
        elif line in ("tick", "hang", "ask"):
            self._job = line
            if line == "ask":
                self.emit("Continue? ")
            return
        elif line == "exit":
            self.running = False
            return
        elif line:
            self.emit(f"/system/bin/sh: {line}: inaccessible or not found\n")
        self.emit(self.prompt())

    def _ping(self):
        seq = 0
        while not self._stop.wait(0.02) and seq < 500:
            seq += 1
            self.emit(f"64 bytes from 8.8.8.8: icmp_seq={seq}\n")
        if self.running:
            self.emit("\n--- 8.8.8.8 ping statistics ---\n" + self.prompt())
            self._job = None

    def _interrupt(self):
        self.emit("^C")  # the terminal echoes the Ctrl+C
        if self._job == "ping":
            self._stop.set()
        elif self._job == "tick":
            self._job = None
            self.emit("\n--- summary ---\n" + self.prompt())
        elif self._job != "hang":  # hang ignores SIGINT
            self._job = None
            self._buf = ""
            self.emit("\n" + self.prompt())


class _Handler:
    """A fake ADBHandler whose ``open_shell`` hands out :class:`_Mksh` sessions
    (``make(tty)`` builds another kind)."""

    serial = "PD2318"
    config = None

    def __init__(self, make=None):
        self.opened = []     # the tty flag of every open_shell
        self.sessions = []
        self.commands = []   # one-shot adb shell commands
        self.make = make or (lambda tty: _Mksh(tty))
        self.fail = None     # an error: open_shell fails

    def open_shell(self, tty=True, safe=None):
        self.opened.append(tty)
        if self.fail is not None:
            return OperationResult(False, "open_shell", error=self.fail)
        session = self.make(tty)
        self.sessions.append(session)
        return OperationResult(True, "open_shell", value=session)

    def shell(self, command, **_kwargs):
        self.commands.append(command)
        return None


def _widget(qapp, handler=None, **info):
    from turboadb.gui.device_tab import _AndroidShellWidget

    handler = handler or _Handler()
    # opened on first show, as the Terminal page does (ShellPanel)
    widget = _AndroidShellWidget(handler, device_name="mock",
                                 info=dict({"kind": "phone"}, **info), autostart=False)
    widget.resize(900, 500)
    widget.ensure_started()
    return widget, handler


def _settle(qapp, widget, ending, timeout=3.0):
    """Wait until the terminal shows *ending* last, nothing left to draw."""
    term = widget.term
    assert _pump(qapp, lambda: not term._inq and term.toPlainText().endswith(ending),
                 timeout=timeout), repr(term.toPlainText()[-400:])


def _ready(qapp, widget, prompt="PD2318:/ $ "):
    """Wait until the device prompt shows (the hidden first line has run)."""
    _settle(qapp, widget, prompt)


def _type(term, text):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    for ch in text:
        term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, 0, Qt.NoModifier, ch))


def _enter(term, line=""):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    _type(term, line)
    term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))


def _close(widget):
    widget.close_panel()
    widget.deleteLater()


def _after_banner(term):
    """The document below the welcome banner."""
    text = term.toPlainText()
    return text[text.index("┘") + 1:].lstrip("\n")


# --------------------------------------------------------------------------- #
# opening: -t -t, the hidden first line, nothing of it on screen
# --------------------------------------------------------------------------- #
def test_the_shell_opens_on_a_device_terminal_with_a_hidden_first_line(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        assert handler.opened == [True]  # adb shell -t -t
        assert widget._pty and not term._emulate_prompt
        first = handler.sessions[0].sent[0]
        assert first.startswith(b" ") and first.endswith(b"\n") and b"\r" not in first
        assert re.search(rb"^ stty cols \d+ rows \d+ 2>/dev/null; ", first)
        assert b"; set +o emacs 2>/dev/null; set +o vi 2>/dev/null; " in first
        # the device's first prompt, the line's garbled echo and its mark are
        # not shown: only the prompt after it, below the banner
        assert _after_banner(term) == "PD2318:/ $ "
        assert "stty" not in term.toPlainText() and "7718" not in term.toPlainText()
        assert term.shell_at_prompt()
        assert handler.commands == []  # the device prints its prompt: no user@host query
    finally:
        _close(widget)


def test_the_terminal_gets_the_size_of_the_view(qapp):
    widget, handler = _widget(qapp)
    try:
        _ready(qapp, widget)
        first = handler.sessions[0].sent[0].decode()
        cols, rows = map(int, re.search(r"stty cols (\d+) rows (\d+)", first).groups())
        # (the layout may have settled a little since the shell opened)
        assert cols >= 20 and abs(cols - widget._console_columns()) <= 2
        assert rows >= 2 and abs(rows - widget._console_rows()) <= 2
    finally:
        _close(widget)


def test_a_first_line_that_never_answers_shows_everything_after_a_while(qapp, monkeypatch):
    from turboadb.gui.device_tab import _AndroidShellWidget

    class Plain(_Mksh):
        """A shell without printf: the mark never comes."""

        def _line(self, line):
            if line.startswith(" ") and "7718" in line:
                self.emit("/system/bin/sh: printf: inaccessible or not found\n" + self.prompt())
                return
            super()._line(line)

    monkeypatch.setattr(_AndroidShellWidget, "INIT_WAIT_MS", 800)
    widget, handler = _widget(qapp, _Handler(lambda tty: Plain(tty)))
    try:
        term = widget.term
        _pump(qapp, lambda: False, timeout=0.1)
        assert "printf" not in term.toPlainText()  # held back for now
        _settle(qapp, widget, "PD2318:/ $ ")
        assert "printf: inaccessible" in term.toPlainText()
        assert term.shell_at_prompt()  # the prompt is recognised all the same
    finally:
        _close(widget)


def test_the_banner_comes_before_the_first_prompt(qapp):
    """Opened before the device details arrived, the banner waits (up to
    2.5 s): the device's prompt waits with it instead of landing above it."""
    from turboadb.gui.device_tab import _AndroidShellWidget

    handler = _Handler()
    widget = _AndroidShellWidget(handler, device_name="mock", info={})
    try:
        term = widget.term
        assert handler.sessions and handler.sessions[0].init_line
        _pump(qapp, lambda: False, timeout=0.2)
        assert "$ " not in term.toPlainText()  # the banner is still due
        widget.update_identity({"kind": "phone", "model": "V2318", "manufacturer": "vivo"})
        _settle(qapp, widget, "PD2318:/ $ ")
        text = term.toPlainText()
        assert text.index("vivo V2318") < text.index("PD2318:/ $ ")
        assert text.count("PD2318:/ $ ") == 1
    finally:
        _close(widget)


class _OwnPrompt(_Mksh):
    """mksh with a PS1 of the device's own, which does not look like a prompt here."""

    def prompt(self):
        return f"[shell@{self.host} {self.cwd}]$ " if self.terminal else ""


def test_a_prompt_that_does_not_look_like_one_still_takes_lines(qapp):
    """The hidden first line's mark says the shell is ready.  Lines waited
    for a prompt this terminal recognises, so with a PS1 like
    ``[shell@PD2318 /]$ `` nothing typed was ever sent."""
    widget, handler = _widget(qapp, _Handler(lambda tty: _OwnPrompt(tty)))
    try:
        term = widget.term
        _enter(term, "ls")  # typed before the first prompt shows
        _settle(qapp, widget, "cache\n[shell@PD2318 /]$ ")
        session = handler.sessions[0]
        assert session.sent[1:] == [b"ls\n"]
        assert _after_banner(term) == "[shell@PD2318 /]$ ls\nacct\ncache\n[shell@PD2318 /]$ "
        _enter(term, "cd /sdcard")  # and at that prompt
        assert session.sent[-1] == b"cd /sdcard\n"
        _settle(qapp, widget, "[shell@PD2318 /]$ cd /sdcard\n[shell@PD2318 /sdcard]$ ")
    finally:
        _close(widget)


# --------------------------------------------------------------------------- #
# typing: LF only, the echo hidden, no blank lines
# --------------------------------------------------------------------------- #
def test_a_command_goes_out_with_a_lf_and_its_echo_is_hidden(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "ls")
        assert handler.sessions[0].sent[-1] == b"ls\n"
        _settle(qapp, widget, "cache\nPD2318:/ $ ")
        assert _after_banner(term) == "PD2318:/ $ ls\nacct\ncache\nPD2318:/ $ "
        _enter(term)  # an empty Enter: one new prompt, no blank line
        assert handler.sessions[0].sent[-1] == b"\n"
        _settle(qapp, widget, "cache\nPD2318:/ $ \nPD2318:/ $ ")
        # the saved log has the same lines: no CR CR LF (a blank line in Notepad)
        assert "\r\r" not in term._sb.full_text()
    finally:
        _close(widget)


def test_output_streams_and_no_prompt_is_drawn_while_a_command_runs(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        session = handler.sessions[0]
        _enter(term, "tick")
        _settle(qapp, widget, "PD2318:/ $ tick\n")
        for number in (1, 2):
            session.tick()
            _settle(qapp, widget, f"tick {number}\n")  # each line as it comes
        assert not term.shell_at_prompt()  # and no prompt in between
        assert term._at_prompt is None  # "unknown", never "no": the terminal echoes
        # a line typed now is input for the command: shown once, its echo hidden
        _enter(term, "abc")
        assert session.sent[-1] == b"abc\n"
        session.tick()
        _settle(qapp, widget, "tick 2\nabc\ntick 3\n")
        assert term.toPlainText().count("abc") == 1
    finally:
        _close(widget)


def test_enter_for_a_programs_own_prompt_adds_no_blank_line(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "ask")
        _settle(qapp, widget, "Continue? ")
        _enter(term)  # the answer is empty
        _settle(qapp, widget, "got []\nPD2318:/ $ ")
        assert _after_banner(term).endswith("PD2318:/ $ ask\nContinue? \ngot []\nPD2318:/ $ ")
        _enter(term, "ask")
        _settle(qapp, widget, "Continue? ")
        _enter(term, "yes")
        _settle(qapp, widget, "got [yes]\nPD2318:/ $ ")
        assert term.toPlainText().endswith("Continue? yes\ngot [yes]\nPD2318:/ $ ")
    finally:
        _close(widget)


def test_the_device_prompt_names_the_folder_for_tab_completion(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "cd /sdcard")
        _settle(qapp, widget, "PD2318:/sdcard $ ")
        assert term._cwd == "/sdcard"
        # mksh's exit status in front ("1|") and a root shell ("#") too
        widget._show(b"\r\n1|PD2318:/data/local/tmp # ")
        assert term._cwd == "/data/local/tmp" and term.shell_at_prompt()
        seen = []
        widget._query_android_completion = lambda line, cwd: seen.append(cwd) or (None, [])
        widget._query_complete("ls fi")
        assert seen == ["/data/local/tmp"]
    finally:
        _close(widget)


def test_a_completion_list_draws_the_device_prompt_again(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        _type(term, "ls s")
        term.apply_completion_result("ls s", None, ["sdcard", "system"])
        assert term.toPlainText().endswith("PD2318:/ $ ls s\nsdcard   system\nPD2318:/ $ ls s")
    finally:
        _close(widget)


def test_ctrl_z_is_never_sent(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        session = handler.sessions[0]
        sent = len(session.sent)
        assert term.send_key(b"\x1a") is False  # the context menu's Send key
        assert len(session.sent) == sent
        assert _pump(qapp, lambda: "Ctrl+Z is not sent" in term.toPlainText())
        assert term.send_key(b"\t") is True and session.sent[-1] == b"\t"
        term._paste_text("echo a\x1ab\n")  # nor inside a pasted line
        assert session.sent[-1] == b"echo ab\n"
    finally:
        _close(widget)


def test_a_line_too_long_for_the_device_terminal_is_not_cut_and_run(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        session = handler.sessions[0]
        term._paste_text("echo " + "x" * 5000)  # no line break: into the edit line
        _enter(term)
        assert session.sent[-1] == b"\n"  # a fresh prompt instead
        _settle(qapp, widget, "run it from a script file\nPD2318:/ $ ")
        assert not any(b"xxx" in data for data in session.sent)
    finally:
        _close(widget)


def test_pasted_lines_run_one_by_one_once_the_prompt_is_back(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        session = handler.sessions[0]
        term._paste_text("ls\ncd /sdcard\nls\n")
        _settle(qapp, widget, "cache\nPD2318:/sdcard $ ")
        assert session.sent[1:] == [b"ls\n", b"cd /sdcard\n", b"ls\n"]
        assert _after_banner(term) == (
            "PD2318:/ $ ls\nacct\ncache\nPD2318:/ $ cd /sdcard\n"
            "PD2318:/sdcard $ ls\nacct\ncache\nPD2318:/sdcard $ "
        )
    finally:
        _close(widget)


def test_a_pasted_line_waits_for_the_running_command(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        session = handler.sessions[0]
        term._paste_text("ask\nls\n")
        _settle(qapp, widget, "Continue? ")
        _pump(qapp, lambda: False, timeout=0.3)
        assert session.sent[1:] == [b"ask\n"]  # "Continue? " is not the shell's prompt
    finally:
        _close(widget)


# --------------------------------------------------------------------------- #
# the read path: CR CR LF, form feeds
# --------------------------------------------------------------------------- #
def test_cr_runs_before_a_line_feed_fold_across_reads():
    from turboadb.gui.device_tab import _fold_crs

    assert _fold_crs(b"acct\r\r\ncache\r\r\n") == (b"acct\r\ncache\r\n", b"")
    data, carry = _fold_crs(b"acct\r")
    assert (data, carry) == (b"acct", b"\r")
    data, carry = _fold_crs(b"\r", carry)
    assert (data, carry) == (b"", b"\r\r")
    assert _fold_crs(b"\ncache", carry) == (b"\r\ncache", b"")
    assert _fold_crs(b"50%\r75%\r\r\n") == (b"50%\r75%\r\n", b"")  # a lone CR stays


def test_line_ends_split_between_reads_leave_no_blank_line(qapp):
    class Split(_Mksh):
        def emit(self, text):
            data = text.replace("\n", "\r\r\n").encode("utf-8")
            with self._lock:
                self._out.extend(data[i:i + 1] for i in range(len(data)))  # byte by byte

    widget, handler = _widget(qapp, _Handler(lambda tty: Split(tty)))
    try:
        term = widget.term
        _ready(qapp, widget)
        _enter(term, "ls")
        _settle(qapp, widget, "cache\nPD2318:/ $ ")
        assert _after_banner(term) == "PD2318:/ $ ls\nacct\ncache\nPD2318:/ $ "
    finally:
        _close(widget)


def test_a_form_feed_moves_down_a_line_instead_of_clearing(qapp):
    widget, handler = _widget(qapp)
    try:
        term = widget.term
        _ready(qapp, widget)
        handler.sessions[0].emit("page 1\x0cpage 2\n" + "PD2318:/ $ ")
        _settle(qapp, widget, "page 2\nPD2318:/ $ ")
        assert "PD2318:/ $ page 1\npage 2\n" in term.toPlainText()
    finally:
        _close(widget)
