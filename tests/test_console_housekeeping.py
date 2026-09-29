"""The terminal console's own bookkeeping: its command history stays bounded,
Stop says how much output it left undrawn, and Clear brings the Android
shell's prompt back by itself."""

import time

import pytest

pytest.importorskip("PyQt5")


def _console(emulate=False):
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole(send_fn=lambda _data: None)
    con.set_emulate_prompt(emulate)
    return con


def _close(con):
    con.close_archive()
    con.close()


def _render(con):
    while con._inq:
        con._drain_tick()


def _pump(qapp, until, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return until()


def test_the_history_keeps_the_newest_lines_only(qapp):
    con = _console()
    try:
        con._paste_text("".join(f"echo {i}\n" for i in range(3)))
        for i in range(con._HISTORY_MAX + 250):
            con._submit_line(f"line {i}")
        assert len(con._history) == con._HISTORY_MAX
        assert con._history[-1] == f"line {con._HISTORY_MAX + 249}"
        assert con._history[0] == "line 250"
        assert con._hidx == con._HISTORY_MAX  # Up recalls the newest line
    finally:
        _close(con)


def test_stop_forgets_a_drop_that_was_part_of_what_it_discards(qapp):
    con = _console()
    try:
        con._MAX_INQ = 16
        con.feed(b"x" * 20 + b"\n")
        con.feed(b"y" * 20 + b"\n")  # over the cap: the x line is dropped
        assert con._dropped == 21
        con.notice("Stopped")
        con.discard_output()
        _render(con)
        text = con.toPlainText()
        assert "yyy" not in text and "skipped" not in text  # only half the story
        assert text == "Stopped\n"
        assert "x" * 20 in con._sb.full_text()  # the history has everything
    finally:
        _close(con)


def test_clear_brings_the_emulated_prompt_back_by_itself(qapp):
    con = _console(emulate=True)
    try:
        con.set_prompt("shell@x")
        con.show_prompt()
        con._submit_line("ls")
        con.feed(b"acct\ncache\n")  # still waiting to be drawn
        con.clear()
        assert _pump(qapp, lambda: con.toPlainText() == "shell@x:/ $ ")
    finally:
        _close(con)
