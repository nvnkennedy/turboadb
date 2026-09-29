"""Status notices, stream boundaries and screen clears in the terminals.

A notice (Stop, reopen, reconnect, disconnect) is drawn in order after the
output already received, on a fresh line but never below a blank one.  A form
feed (what cmd's ``cls`` writes to a pipe) clears the screen instead of
leaving a blank line."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt5")

CMD = "C:\\w>"
PS = "PS C:\\w> "
ANDROID = "shell@android:/ $ "


def _console(*, local=True, sent=None):
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole(send_fn=(sent.append if sent is not None else lambda _data: None))
    if local:
        con.set_emulate_prompt(False)
    return con


def _close(con):
    con.close_archive()
    con.close()


def _render(con):
    while con._inq:
        con._drain_tick()


def _out(con, *chunks):
    for chunk in chunks:
        con.feed(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
    _render(con)


def _type(con, text):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    for ch in text:
        con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, 0, Qt.NoModifier, ch))


def _submit(con, line):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    _type(con, line)
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))


def test_a_notice_comes_after_output_received_before_it(qapp):
    con = _console()
    try:
        _out(con, "Reply 1\r\n")
        con.feed(b"Reply 2\r\n")
        con.feed(b"Reply 3\r\n")  # both still waiting to be drawn
        con.notice("^C  — stopped; fresh local shell ready")
        _render(con)
        assert con.toPlainText() == "Reply 1\nReply 2\nReply 3\n^C  — stopped; fresh local shell ready\n"
    finally:
        _close(con)


def test_a_notice_starts_on_a_fresh_line_without_a_blank_one(qapp):
    con = _console()
    try:
        _out(con, "10%")
        con.feed(b"\r20%")
        con.notice("\nRestarting shared ADB server…\n", new_stream=False)
        _render(con)
        assert con.toPlainText() == "20%\nRestarting shared ADB server…\n"
        con.notice("[shell disconnected]")
        _render(con)
        assert con.toPlainText().endswith("server…\n[shell disconnected]\n")
    finally:
        _close(con)


def test_a_notice_keeps_the_edit_line_below_it(qapp):
    con = _console()
    try:
        _out(con, CMD)
        _type(con, "typed")
        con.notice("↻  device back")
        _render(con)
        assert con.toPlainText() == f"{CMD}\n↻  device back\ntyped"
        assert con._line == "typed"
    finally:
        _close(con)


def test_a_notice_under_the_synthetic_prompt_drops_its_line_break(qapp):
    con = _console(local=False)
    try:
        con.show_prompt()
        con._submit_line("cmd")
        _out(con, "partial")
        con._submitted_at -= 1
        con._last_feed -= 1
        con._idle_prompt()
        assert con.toPlainText() == f"{ANDROID}cmd\npartial\n{ANDROID}"
        con.notice("^C  — stopped")
        _render(con)
        assert con.toPlainText() == f"{ANDROID}cmd\npartial\n^C  — stopped\n{ANDROID}"
    finally:
        _close(con)


def test_a_new_stream_starts_with_a_fresh_parser_colour_and_decoder(qapp):
    """What the stopped stream left half-sent never garbles the next one."""
    from PyQt5.QtGui import QColor, QTextCursor

    con = _console()
    try:
        _out(con, b"\x1b[31mred \x1b[1", b"\xe2\x82")  # an escape and a character cut off
        con.reset_stream()
        _out(con, b"plain\r\n")
        assert con.toPlainText() == "red \nplain\n"
        cursor = QTextCursor(con.document())
        cursor.setPosition(con.toPlainText().index("plain") + 1)
        assert cursor.charFormat().foreground().color().name() == QColor(con._fg_default).name()
        assert con._state == 0 and con._csi == ""
    finally:
        _close(con)


def test_notices_survive_a_flood_and_a_stop(qapp):
    con = _console()
    try:
        con._MAX_INQ = 16
        con.feed("a" * 20)
        con.notice("kept")
        con.feed("b" * 20)
        con.feed("tail")
        _render(con)
        text = con.toPlainText()
        assert "kept" in text and text.endswith("tail")
        con.feed(b"stale output\r\n")
        con.notice("^C  — stopped")
        con.feed(b"more stale output\r\n")
        con.discard_output()
        _render(con)
        assert "stale" not in con.toPlainText()
        assert con.toPlainText().endswith("^C  — stopped\n")
    finally:
        _close(con)


def test_the_banner_never_starts_with_a_blank_line(qapp):
    from turboadb.gui.device_tab import _boxed_banner

    con = _console()
    try:
        con.banner(_boxed_banner(["\x1b[1;92m● Command Prompt\x1b[0m"]))
        assert con.toPlainText().startswith("┌")
        _out(con, "\x1b[31mred")
        con.banner("\n\x1b[32mgreen\x1b[0m\n")  # after output that ended mid-line
        _out(con, " still red\r\n")
        assert con.toPlainText().endswith("└──────────────────┘\n\nred\ngreen\n still red\n")
        from PyQt5.QtGui import QColor, QTextCursor
        from turboadb.gui import theme

        cursor = QTextCursor(con.document())
        cursor.setPosition(con.toPlainText().index(" still red") + 2)
        # the banner's own colours never reset the stream's
        assert cursor.charFormat().foreground().color().name() == QColor(theme.ANSI_FG[31]).name()
    finally:
        _close(con)


def test_a_form_feed_clears_the_screen_but_not_the_history(qapp):
    con = _console()
    try:
        _out(con, CMD)
        _submit(con, "echo before")
        _out(con, "echo before\r\nbefore\r\n\r\n" + CMD)
        _submit(con, "cls")
        _out(con, "cls\r\n\x0c\r\n" + CMD)
        assert con.toPlainText() == CMD
        _submit(con, "cls")
        _out(con, "cls\r\n", "\x0c", "\r", "\n", CMD)  # each piece in its own read
        assert con.toPlainText() == CMD
        assert "before" in con._sb.full_text()
    finally:
        _close(con)


def test_a_form_feed_keeps_the_line_being_typed(qapp):
    con = _console()
    try:
        _out(con, CMD)
        _submit(con, "cls")
        _type(con, "dir")
        _out(con, "cls\r\n\x0c")
        assert con.toPlainText() == "dir"
        _out(con, "\r\n" + CMD)
        assert con.toPlainText() == CMD + "dir"
        _out(con, "\x1b[2J")
        assert con.toPlainText() == "dir" and con._line == "dir"
    finally:
        _close(con)


def test_the_android_pipe_shell_ignores_form_feeds(qapp):
    con = _console(local=False)
    try:
        _out(con, "page 1\n\x0cpage 2\n")
        assert con.toPlainText() == "page 1\npage 2\n"
    finally:
        _close(con)


def test_clear_screen_for_clear_host_keeps_the_prompt_line(qapp):
    con = _console()
    try:
        _out(con, PS)
        _submit(con, "echo before")
        _out(con, "echo before\r\nbefore\r\n" + PS)
        _type(con, "dir")
        con.feed(b"\r\nlate\r\n" + PS.encode())  # queued before the clear
        con.clear_screen()
        _render(con)
        assert con.toPlainText() == PS + "dir"
        assert "before" in con._sb.full_text() and "late" in con._sb.full_text()
        _out(con, "\r\nafter\r\n")
        assert con.toPlainText() == f"{PS}\nafter\ndir"
    finally:
        _close(con)


def test_cls_submitted_then_clear_screen_puts_the_next_prompt_on_top(qapp):
    con = _console()
    try:
        _out(con, PS)
        _submit(con, "cls")
        con.clear_screen()  # what an owner does for PowerShell's Clear-Host
        _out(con, "cls\r\n", PS)
        assert con.toPlainText() == PS
    finally:
        _close(con)


def test_clear_keeps_the_prompt_and_the_edit_line(qapp):
    con = _console()
    try:
        _out(con, "old output\r\n" + CMD)
        _type(con, "dir")
        con.clear()
        assert con.toPlainText() == CMD + "dir"
        assert con._sb.full_text() == CMD + "dir"  # the history went (nothing archived)
        con.clear(keep_prompt=False)  # the owner replaces the shell
        assert con.toPlainText() == "" and con._line == ""
    finally:
        _close(con)
    con = _console(local=False)
    try:
        con.show_prompt()
        _type(con, "ls")
        con.clear()
        assert con.toPlainText() == ANDROID + "ls"
    finally:
        _close(con)


def _local_shell(qapp, monkeypatch, tmp_path, shell_type="cmd"):
    from turboadb.gui.device_tab import _LocalShellWidget

    class Session:
        running = True

        def __init__(self):
            self.sent = []

        def send(self, data):
            self.sent.append(data)

        def close(self):
            self.running = False

    widget = _LocalShellWidget(shell_type, serial="V2318")
    widget._shell_cwd = str(tmp_path)
    sessions = []

    def start_session(*, show_banner=True):
        widget.session = Session()
        sessions.append(widget.session)
        widget.term.set_alive(True)

    def stop_session(*, interrupt=False):
        if widget.session is not None:
            widget.session.running = False
        widget.session = None

    monkeypatch.setattr(widget, "_start_session", start_session)
    monkeypatch.setattr(widget, "_stop_session", stop_session)
    widget._started = True
    start_session()
    widget.sessions = sessions
    return widget


def test_stop_on_a_local_command_leaves_no_blank_line(qapp, monkeypatch, tmp_path):
    widget = _local_shell(qapp, monkeypatch, tmp_path)
    try:
        term = widget.term
        _out(term, CMD)
        _submit(term, "ping -t 127.0.0.1")
        term.feed(b"ping -t 127.0.0.1\r\n\r\nPinging\r\nReply 1\r\n")
        term.feed(b"Reply 2\r\n")  # not drawn yet when Stop is pressed
        widget.interrupt()
        _out(term, CMD)
        assert term.toPlainText() == (
            f"{CMD}ping -t 127.0.0.1\n\nPinging\nReply 1\nReply 2\n"
            f"^C  — stopped; fresh local shell ready\n{CMD}"
        )
        assert len(widget.sessions) == 2
    finally:
        widget.close_panel()
        widget.deleteLater()


def test_a_shell_that_exits_shows_one_notice(qapp, monkeypatch, tmp_path):
    widget = _local_shell(qapp, monkeypatch, tmp_path)
    try:
        term = widget.term
        _out(term, CMD)
        _submit(term, "exit")
        term.feed(b"exit\r\nlast words\r\n")
        widget._on_closed()
        _render(term)
        assert term.toPlainText() == f"{CMD}exit\nlast words\n[Process terminated]\n"
    finally:
        widget.close_panel()
        widget.deleteLater()


def test_the_clear_button_keeps_the_prompt_line(qapp, monkeypatch, tmp_path):
    from PyQt5.QtWidgets import QPushButton

    widget = _local_shell(qapp, monkeypatch, tmp_path)
    try:
        _out(widget.term, "old output\r\n" + CMD)
        button = next(b for b in widget.findChildren(QPushButton) if b.text() == "Clear")
        button.click()  # clicked(bool) must not reach clear()'s keyword
        assert widget.term.toPlainText() == CMD
    finally:
        widget.close_panel()
        widget.deleteLater()


def test_reopen_clears_the_old_prompt_too(qapp, monkeypatch, tmp_path):
    widget = _local_shell(qapp, monkeypatch, tmp_path)
    try:
        _out(widget.term, "output\r\n" + CMD)
        widget.reopen()
        assert widget.term.toPlainText() == ""
    finally:
        widget.close_panel()
        widget.deleteLater()


class _AndroidSession:
    def __init__(self):
        self.running = True
        self.sent = []

    def read(self, _size):
        import time

        time.sleep(0.02)
        return b""

    def send(self, data):
        self.sent.append(data)

    def close(self):
        self.running = False


class _AndroidHandler:
    serial = "123"
    config = None

    def __init__(self):
        self.sessions = []

    def open_shell(self, tty=True, safe=None):
        self.sessions.append(_AndroidSession())
        return self.sessions[-1]

    def shell(self, command, **_kwargs):
        return None


def _android(qapp, monkeypatch):
    """The Android shell over plain pipes (the ``android_shell_pty`` setting
    off), where TurboADB draws the prompt: the notices these tests check."""
    from turboadb.gui import settings as settings_mod
    from turboadb.gui.device_tab import _AndroidShellWidget

    get = settings_mod.get
    monkeypatch.setattr(
        settings_mod, "get",
        lambda key, default=None: False if key == "android_shell_pty" else get(key, default),
    )
    widget = _AndroidShellWidget(_AndroidHandler(), device_name="mock", info={"kind": "phone"})
    widget._prompt_state = "known"
    return widget


def test_android_stop_shows_one_prompt_after_the_notice(qapp, monkeypatch):
    widget = _android(qapp, monkeypatch)
    try:
        term = widget.term
        widget._show_banner_and_prompt()
        _render(term)
        prompt = term._shown_prompt
        _submit(term, "ping 8.8.8.8")
        _out(term, "64 bytes from 8.8.8.8\n")
        term.feed(b"64 bytes (stale)\n")
        widget.interrupt()
        _render(term)
        text = term.toPlainText()
        assert text.endswith(f"64 bytes from 8.8.8.8\n^C  — stopped\n{prompt}")
        assert text.count(prompt) == 2 and "stale" not in text
        widget.restart_shell()
        _render(term)
        text = term.toPlainText()
        # Restart shell reopens at once: no Ctrl+C first, and no second notice
        assert text.endswith(f"^C  — stopped\nRestarting Android shell…\n{prompt}")
        assert text.count(prompt) == 2  # the idle prompt still showed: no second one
    finally:
        widget.close_panel()
        widget.deleteLater()


def test_android_disconnect_and_reconnect_leave_no_blank_lines(qapp, monkeypatch):
    widget = _android(qapp, monkeypatch)
    try:
        term = widget.term
        widget._show_banner_and_prompt()
        _submit(term, "logcat")
        _out(term, "log line\n")
        widget._on_reader_closed()
        _render(term)
        assert term.toPlainText().endswith(f"{ANDROID}logcat\nlog line\n[shell disconnected]\n")
        widget.reconnect(focus=False)
        _render(term)
        assert term.toPlainText().endswith(f"log line\n[shell disconnected]\n{ANDROID}")
        widget.pause_for_adb_restart()
        _render(term)
        assert term.toPlainText().endswith(
            f"[shell disconnected]\n{ANDROID}\n"
            "  ↻  ADB server restarting — this shell will reconnect automatically.\n"
        )
    finally:
        widget.close_panel()
        widget.deleteLater()
