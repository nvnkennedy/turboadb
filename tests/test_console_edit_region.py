"""The terminal's edit region: the synthetic prompt (Android shell over a pipe)
and the line being typed always end the document, and output is written above
them.  Typing while a command streams never mixes with or erases its output."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PyQt5")

CMD = "C:\\w>"
ANDROID = "shell@android:/ $ "


def _console(*, local, sent=None):
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


def _out(con, text):
    con.feed(text.encode("utf-8"))
    _render(con)


def _key(con, key, text="", mods=None):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, key, mods or Qt.NoModifier, text))


def _type(con, text):
    for ch in text:
        _key(con, 0, ch)


def _enter(con):
    from PyQt5.QtCore import Qt

    _key(con, Qt.Key_Return, "\r")


def _quiet(con, seconds=1.0):
    con._submitted_at -= seconds
    con._last_feed -= seconds


def test_typing_during_streaming_output_never_touches_the_output(qapp):
    from PyQt5.QtCore import Qt

    sent = []
    con = _console(local=True, sent=sent)
    try:
        _out(con, CMD)
        _type(con, "ping")
        _enter(con)
        _out(con, "ping\r\n\r\nPinging 127.0.0.1\r\nReply 1\r\n")
        _type(con, "abcdef")
        _out(con, "Reply 2\r\n")
        assert con.toPlainText() == f"{CMD}ping\n\nPinging 127.0.0.1\nReply 1\nReply 2\nabcdef"
        for _ in range(3):
            _key(con, Qt.Key_Backspace)
        _out(con, "Reply 3\r\n")
        assert con.toPlainText().endswith("Reply 1\nReply 2\nReply 3\nabc")
        assert con._line == "abc"
        for key in (Qt.Key_Home, Qt.Key_Delete, Qt.Key_End):
            _key(con, key)
        _out(con, "Reply 4\r\n")
        assert con.toPlainText().endswith("Reply 3\nReply 4\nbc")
        _key(con, Qt.Key_W, "", Qt.ControlModifier)
        _out(con, "\r\nPing statistics\r\n")
        assert con.toPlainText().endswith("Reply 4\n\nPing statistics\n")
        assert con._line == "" and sent == [b"ping\r\n"]
    finally:
        _close(con)


def test_output_lands_above_the_android_edit_line(qapp):
    from PyQt5.QtCore import Qt

    con = _console(local=False)
    try:
        con.show_prompt()
        _type(con, "logcat")
        _enter(con)
        _out(con, "line 1\n")
        _type(con, "abc")
        _out(con, "line 2\n")
        assert con.toPlainText() == f"{ANDROID}logcat\nline 1\nline 2\nabc"
        _key(con, Qt.Key_Backspace)
        _out(con, "line 3\n")
        assert con.toPlainText() == f"{ANDROID}logcat\nline 1\nline 2\nline 3\nab"
        assert con._line == "ab"
    finally:
        _close(con)


def test_the_caret_keeps_its_place_in_the_line_while_output_arrives(qapp):
    from PyQt5.QtCore import Qt

    con = _console(local=True)
    try:
        con.resize(500, 200)
        con.show()
        _out(con, CMD)
        _type(con, "hello")
        _key(con, Qt.Key_Left)
        _key(con, Qt.Key_Left)
        _out(con, "\r\nasync message\r\n")
        end = con.document().characterCount() - 1
        assert con.textCursor().position() == end - 2
        _type(con, "X")
        assert con._line == "helXlo"
        assert con.toPlainText().endswith("async message\nhelXlo")
    finally:
        _close(con)


def test_a_carriage_return_overwrite_continues_while_the_user_types(qapp):
    con = _console(local=True)
    try:
        _out(con, "10%")
        con.feed(b"\r")
        _render(con)
        _type(con, "x")
        _out(con, "20%")
        assert con.toPlainText() == "20%x"
        _out(con, "\r30%\r\n")
        assert con.toPlainText() == "30%\nx"
    finally:
        _close(con)


def test_line_and_screen_erases_stop_at_the_edit_line(qapp):
    con = _console(local=True)
    try:
        _out(con, "progress 1")
        _type(con, "typed")
        _out(con, "\r\x1b[K")  # erase to the end of the line
        assert con.toPlainText() == "typed"
        _out(con, "new")
        assert con.toPlainText() == "newtyped"
        _out(con, "\r\x1b[2K")  # the whole line
        assert con.toPlainText() == "typed"
        _out(con, "ab\b\x1b[1K")  # from the line start through the cursor
        assert con.toPlainText() == "  typed"
        _out(con, "\r\x1b[J")  # everything below the cursor
        assert con.toPlainText() == "typed"
        assert con._line == "typed"
    finally:
        _close(con)


def test_cut_and_history_edit_only_the_edit_line(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QTextCursor

    sent = []
    con = _console(local=True, sent=sent)
    try:
        _out(con, CMD)
        _type(con, "echo one")
        _enter(con)
        _out(con, "echo one\r\none\r\n\r\n" + CMD)
        _type(con, "partial")
        _out(con, "\r\nbackground job done\r\n")
        _key(con, Qt.Key_Up)
        assert con._line == "echo one"
        assert con.toPlainText().endswith("background job done\necho one")
        cursor = con.textCursor()
        end = con.document().characterCount() - 1
        cursor.setPosition(end - 3)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        con.setTextCursor(cursor)
        _key(con, Qt.Key_X, "", Qt.ControlModifier)
        assert con._line == "echo "
        assert con.toPlainText().endswith("background job done\necho ")
    finally:
        _close(con)


def test_a_line_submitted_behind_queued_output_is_drawn_after_it(qapp):
    con = _console(local=True)
    try:
        _out(con, CMD)
        _type(con, "dir")
        con.feed(b"\r\nlate output\r\n" + CMD.encode())  # received, not drawn yet
        _enter(con)
        _render(con)
        assert con.toPlainText() == f"{CMD}\nlate output\n{CMD}dir\n"
    finally:
        _close(con)


def test_typing_never_draws_the_synthetic_prompt(qapp):
    from PyQt5.QtCore import Qt

    con = _console(local=False)
    try:
        con.show_prompt()
        _type(con, "sleep 5")
        _enter(con)
        _type(con, "ls")  # the command still runs: no prompt in front of this
        assert con.toPlainText() == f"{ANDROID}sleep 5\nls"
        _quiet(con)
        con._idle_prompt()  # quiet, but the user is typing: still no prompt
        assert ANDROID not in con.toPlainText().split("\n")[-1]
        _key(con, Qt.Key_Escape)
        _quiet(con)
        con._idle_prompt()
        assert con.toPlainText() == f"{ANDROID}sleep 5\n{ANDROID}"
    finally:
        _close(con)


def test_the_synthetic_prompt_waits_for_the_paste_idle(qapp):
    from turboadb.gui.console import AnsiConsole

    assert AnsiConsole._PROMPT_IDLE_S >= AnsiConsole._PASTE_IDLE_S
    con = _console(local=False)
    try:
        con.show_prompt()
        _type(con, "ping 8.8.8.8")
        _enter(con)
        _out(con, "64 bytes\n")
        con._idle_prompt()  # a streaming command pauses between lines
        assert con.toPlainText() == f"{ANDROID}ping 8.8.8.8\n64 bytes\n"
        assert con._idle.isActive()  # checked again once the output is quiet
        _quiet(con)
        con._idle_prompt()
        assert con.toPlainText().endswith(f"64 bytes\n{ANDROID}")
    finally:
        _close(con)


def test_output_that_stops_mid_line_continues_on_that_line(qapp):
    """The line break drawn before the synthetic prompt goes with it."""
    con = _console(local=False)
    try:
        con.show_prompt()
        con._submit_line("cmd")
        for chunk in ("Waiting...", " done\n", "0 ", "1 ", "done\n"):
            _out(con, chunk)
            _quiet(con)
            con._idle_prompt()  # each pause long enough to show the prompt
        assert con.toPlainText() == f"{ANDROID}cmd\nWaiting... done\n0 1 done\n{ANDROID}"
        con._submit_line("progress")
        for chunk in ("  10%", "\r  50%", "\r 100%\n"):
            _out(con, chunk)
            _quiet(con)
            con._idle_prompt()
        assert con.toPlainText().endswith(f"{ANDROID}progress\n 100%\n{ANDROID}")
    finally:
        _close(con)


def test_the_prompt_never_disturbs_a_half_received_escape_or_colour(qapp):
    """The prompt is drawn with its own formats, not through the parser."""
    from PyQt5.QtGui import QColor, QFont, QTextCursor
    from turboadb.gui import theme

    con = _console(local=False)
    try:
        con.show_prompt()
        con._submit_line("cmd")
        _out(con, "\x1b[31mred \x1b[1")  # the chunk ends inside an escape
        _quiet(con)
        con._idle_prompt()
        assert con.toPlainText().endswith(f"red \n{ANDROID}")
        _out(con, "m bold\n")
        assert con.toPlainText().endswith("red  bold\n")
        cursor = QTextCursor(con.document())
        cursor.setPosition(con.toPlainText().index(" bold") + 3)
        fmt = cursor.charFormat()
        assert fmt.foreground().color().name() == QColor(theme.ANSI_FG[31]).name()
        assert fmt.fontWeight() == QFont.Bold
    finally:
        _close(con)


def test_owner_stop_and_set_alive_leave_exactly_one_prompt(qapp):
    """No double prompt after Stop: the owner redraws, the console does not."""
    from PyQt5.QtCore import Qt

    con = _console(local=False)
    try:
        con.set_interrupt_fn(lambda: (con.notice("^C  — stopped"), con.set_alive(True)))
        con.show_prompt()
        _type(con, "ping 8.8.8.8")
        _enter(con)
        _out(con, "64 bytes\n")
        _type(con, "typed ahead")
        _key(con, Qt.Key_C, "\x03", Qt.ControlModifier)
        _render(con)
        _quiet(con)
        con._idle_prompt()
        assert con.toPlainText() == f"{ANDROID}ping 8.8.8.8\n64 bytes\n^C  — stopped\n{ANDROID}"
        assert con._line == ""
        con.set_alive(True)  # a second owner call still draws no second prompt
        con.show_prompt()
        assert con.toPlainText().count(ANDROID) == 2
    finally:
        _close(con)


def test_plain_ctrl_c_keeps_the_typed_line_with_its_marker(qapp):
    from PyQt5.QtCore import Qt

    sent = []
    con = _console(local=True, sent=sent)
    try:
        _out(con, CMD)
        _type(con, "abc")
        _key(con, Qt.Key_C, "\x03", Qt.ControlModifier)
        assert sent == [b"\x03"]
        assert con.toPlainText() == f"{CMD}abc^C\n" and con._line == ""
    finally:
        _close(con)


def test_completion_list_keeps_the_prompt_and_line_below_it(qapp):
    for local in (True, False):
        con = _console(local=local)
        try:
            con.set_completion_fn(lambda line: (None, ["alpha", "beta"]))
            if local:
                con.set_prompt_provider_fn(lambda: CMD)
                _out(con, CMD)
                prompt = CMD
            else:
                con.show_prompt()
                prompt = ANDROID
            _type(con, "cd a")
            from PyQt5.QtCore import Qt

            _key(con, Qt.Key_Tab)
            lines = con.toPlainText().split("\n")
            assert lines[0] == prompt + "cd a"
            assert "alpha" in lines[1] and "beta" in lines[1]
            assert lines[-1] == prompt + "cd a"
            assert con._line == "cd a" and con._line_len == 4
            _out(con, "\r\nlate\r\n")  # output still goes above the line
            assert con.toPlainText().endswith("\nlate\ncd a")
        finally:
            _close(con)


def test_switching_prompt_emulation_off_removes_a_shown_prompt(qapp):
    con = _console(local=False)
    try:
        _out(con, "partial")
        con.show_prompt()
        assert con.toPlainText() == f"partial\n{ANDROID}"
        con.set_emulate_prompt(False)
        assert con.toPlainText() == "partial"
        _out(con, " line\n")
        assert con.toPlainText() == "partial line\n"
    finally:
        _close(con)


def test_ime_commit_text_goes_into_the_command_line(qapp):
    """Input-method text never lands straight in the output."""
    from PyQt5.QtGui import QInputMethodEvent

    sent = []
    con = _console(local=True, sent=sent)
    try:
        _out(con, CMD)
        _type(con, "echo ")
        event = QInputMethodEvent()
        event.setCommitString("日本")
        con.inputMethodEvent(event)
        assert con._line == "echo 日本"
        _out(con, "\r\nbackground\r\n")
        assert con.toPlainText().endswith("background\necho 日本")
        preedit = QInputMethodEvent("にほ", [])
        con.inputMethodEvent(preedit)  # composition only: nothing is inserted
        assert con._line == "echo 日本"
        assert sent == []
    finally:
        _close(con)


def test_mime_paste_goes_through_the_command_line(qapp):
    """Qt's own pastes (an X11 middle-click) use the terminal paste path."""
    from PyQt5.QtCore import QMimeData

    sent = []
    con = _console(local=True, sent=sent)
    try:
        _out(con, CMD)
        data = QMimeData()
        data.setText("dir")
        assert con.canInsertFromMimeData(data)
        con.insertFromMimeData(data)
        assert con._line == "dir" and con.toPlainText() == f"{CMD}dir"
        data.setText(" /b\necho x\n")
        con.insertFromMimeData(data)
        assert sent == [b"dir /b\r\n"]  # a multi-line paste runs line by line
    finally:
        _close(con)


def test_send_key_menu_goes_through_the_owner(qapp, monkeypatch):
    from PyQt5.QtCore import QPoint
    from PyQt5.QtWidgets import QMenu

    sent, offered = [], []
    con = _console(local=True, sent=sent)
    try:
        assert con.send_key(b"\t") and sent == [b"\t"]  # no owner: sent as is
        con.set_send_key_fn(lambda data: offered.append(data) or data != b"\x1a")
        assert con.send_key(b"\x1a") is False
        assert con.send_key(b"\x04") is True
        assert offered == [b"\x1a", b"\x04"] and sent == [b"\t"]

        menus = []
        monkeypatch.setattr(QMenu, "exec_", lambda self, *_args: menus.append(self))
        con._menu(QPoint(0, 0))
        keys = next(a.menu() for a in menus[0].actions() if a.text() == "Send key")
        ctrl_z = next(a for a in keys.actions() if a.text() == "Ctrl+Z")
        ctrl_z.trigger()
        assert offered[-1] == b"\x1a" and sent == [b"\t"]
        con._alive = False
        assert con.send_key(b"\t") is False  # a dead shell takes nothing
    finally:
        _close(con)


def test_the_edit_region_survives_display_trimming(qapp):
    con = _console(local=True)
    try:
        con.setMaximumBlockCount(50)
        _type(con, "still here")
        _out(con, "".join(f"line {i}\r\n" for i in range(500)))
        assert con.toPlainText().endswith("line 499\nstill here")
        assert con.document().blockCount() <= 50
        assert con._line == "still here"
    finally:
        _close(con)


def test_output_above_a_typed_line_renders_in_linear_time(qapp):
    """A line break written on its own in front of the typed line made Qt's
    work per line grow with the document: a flood took minutes, not seconds."""

    def render(lines):
        con = _console(local=True)
        try:
            _type(con, "typed")
            con.feed("".join(f"\x1b[32m{i}\x1b[0m output line\r\n" for i in range(lines)))
            started = time.perf_counter()
            _render(con)
            elapsed = time.perf_counter() - started
            assert con.toPlainText().endswith(f"{lines - 1} output line\ntyped")
            return elapsed
        finally:
            _close(con)

    small, big = render(2000), render(16000)
    assert big < 20 * small + 0.5, (small, big)  # linear: ~8x; the old way: ~50x
