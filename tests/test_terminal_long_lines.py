"""A line that never ends: minified JSON from ``curl``, base64, one-line XML.

Every read of it was appended to one line of the view, which Qt lays out
again whole for every insert: 300,000 characters froze the window for
seconds, a million for half a minute, and each key typed after it took most
of a second.  A line now goes on on the next line of the view once it is
4,096 characters long, as a terminal wraps at its width; copying gives it
back in one piece, and the saved history never had it any other way."""

import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QColor, QKeyEvent, QTextCursor  # noqa: E402

from turboadb.gui import theme  # noqa: E402


def _console(pty=False):
    from turboadb.gui.console import AnsiConsole

    term = AnsiConsole(send_fn=lambda _data: None)
    term.set_emulate_prompt(False)
    if pty:
        term.set_screen_input(lambda _data: True)
    term.resize(900, 500)
    return term


def _render(term):
    while term._inq:
        term._drain_tick()


def _close(term):
    term.close_archive()
    term.deleteLater()


def _lengths(term):
    """How long each line of the view is."""
    return [len(line) for line in term.toPlainText().split("\n")]


def _copied(term, monkeypatch, start=None, end=None):
    """What Copy puts on the clipboard for a selection (all of the view by default)."""
    from turboadb.gui import fileutil

    copied = []
    monkeypatch.setattr(fileutil, "copy_to_clipboard", lambda _widget, text: copied.append(text))
    if start is None:
        term.selectAll()
    else:
        cursor = term.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        term.setTextCursor(cursor)
    assert term._copy_selection()
    return copied[0]


def test_a_line_that_never_ends_is_drawn_in_linear_time(qapp):
    def draw(size):
        term = _console()
        try:
            term.feed("x" * size)
            started = time.perf_counter()
            _render(term)
            elapsed = time.perf_counter() - started
            assert sum(_lengths(term)) == size
            return elapsed
        finally:
            _close(term)

    small, big = draw(100_000), draw(800_000)
    assert big < 20 * small + 0.5, (small, big)  # linear: ~8x; in one line of the view: ~60x


def test_a_long_line_goes_on_on_the_next_line_of_the_view(qapp):
    from turboadb.gui.console import AnsiConsole

    cap = AnsiConsole._LINE_MAX
    assert 2048 <= cap <= 8192  # a few thousand characters
    term = _console()
    try:
        line = "".join(chr(97 + i % 26) for i in range(2 * cap + 1808))
        term.feed("before\r\n" + line + "\r\nafter\r\n")
        _render(term)
        assert _lengths(term) == [6, cap, cap, 1808, 5, 0]
        assert term.toPlainText().replace("\n", "") == "before" + line + "after"
        assert term._sb.full_text() == "before\n" + line + "\nafter\n"  # Save has it as it came
    finally:
        _close(term)


@pytest.mark.parametrize("size", [7, 1000, 8192, 100000])
def test_a_long_line_is_cut_at_the_same_places_however_it_arrives(qapp, size):
    from turboadb.gui.console import AnsiConsole

    cap = AnsiConsole._LINE_MAX
    term = _console()
    try:
        output = "x" * (cap + 300) + "\r\n" + "y" * (cap - 1) + "\r\n" + "z" * (3 * cap) + "\r\nend\r\n"
        for start in range(0, len(output), size):
            term.feed(output[start:start + size])
            _render(term)
        assert _lengths(term) == [cap, 300, cap - 1, cap, cap, cap, 3, 0]
    finally:
        _close(term)


def test_a_line_arriving_a_character_at_a_time_is_cut_in_the_same_place(qapp):
    from turboadb.gui.console import AnsiConsole

    cap = AnsiConsole._LINE_MAX
    term = _console()
    try:
        term.feed("x" * (cap - 40))
        _render(term)
        for _ in range(80):
            term.feed("x")
            _render(term)
        term.feed("\r\nend\r\n")
        _render(term)
        assert _lengths(term) == [cap, 40, 3, 0]
    finally:
        _close(term)


def test_a_line_exactly_as_long_as_the_view_takes_is_left_alone(qapp):
    from turboadb.gui.console import AnsiConsole

    cap = AnsiConsole._LINE_MAX
    term = _console()
    try:
        term.feed("x" * cap + "\r\n" + "y" * cap)
        _render(term)
        assert _lengths(term) == [cap, cap]
        term.feed("\r\n")
        _render(term)
        assert _lengths(term) == [cap, cap, 0]
    finally:
        _close(term)


def test_copy_gives_a_long_line_back_in_one_piece(qapp, monkeypatch):
    from turboadb.gui.console import AnsiConsole

    cap = AnsiConsole._LINE_MAX
    term = _console()
    try:
        line = "".join(chr(97 + i % 26) for i in range(2 * cap + 500))
        term.feed("first\r\n" + line + "\r\nlast\r\n")
        _render(term)
        assert _copied(term, monkeypatch) == "first\n" + line + "\nlast\n"
        # a selection from inside its first piece into its second, and one that ends the line
        start = len("first\n") + cap - 3
        assert _copied(term, monkeypatch, start, start + 7) == line[cap - 3:cap + 3]
        end = len("first\n") + len(line) + 2
        assert _copied(term, monkeypatch, end - 3, end + 3) == line[-3:] + "\nla"
    finally:
        _close(term)


def test_the_typed_line_stays_below_a_long_line(qapp):
    from turboadb.gui.console import AnsiConsole

    cap = AnsiConsole._LINE_MAX
    term = _console()
    try:
        term.feed("x" * (cap + 10))
        _render(term)
        for ch in "ls":
            term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, 0, Qt.NoModifier, ch))
        assert term.toPlainText() == "x" * cap + "\n" + "x" * 10 + "ls"
        term.feed("y" * (cap - 10) + "zz\r\ndone\r\n")  # the line goes on, and is cut once more
        _render(term)
        assert term.toPlainText() == "x" * cap + "\n" + "x" * 10 + "y" * (cap - 10) + "\nzz\ndone\nls"
        assert term._line == "ls"
    finally:
        _close(term)


def test_a_long_line_survives_the_views_trimming(qapp, monkeypatch):
    """The view keeps its newest lines only: what is cut off its top takes
    no part of a long line's pieces or of the typed line with it."""
    from turboadb.gui.console import AnsiConsole

    cap = AnsiConsole._LINE_MAX
    term = _console()
    try:
        term.setMaximumBlockCount(40)
        for ch in "ls":
            term.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, 0, Qt.NoModifier, ch))
        line = "".join(chr(97 + i % 26) for i in range(2 * cap + 7))
        term.feed("".join(f"line {i}\r\n" for i in range(300)) + line + "\r\nlast\r\n")
        _render(term)
        assert term.document().blockCount() <= 40
        assert term.toPlainText().endswith(
            "line 299\n" + line[:cap] + "\n" + line[cap:2 * cap] + "\n" + line[2 * cap:] + "\nlast\nls")
        assert _copied(term, monkeypatch).endswith("line 299\n" + line + "\nlast\nls")
        assert term._line == "ls"
    finally:
        _close(term)


def test_a_long_line_keeps_its_colour_over_the_cut(qapp):
    from turboadb.gui.console import AnsiConsole

    cap = AnsiConsole._LINE_MAX
    term = _console()
    try:
        term.feed("\x1b[32m" + "x" * (cap + 100) + "\x1b[0m plain\r\n")
        _render(term)
        green = QColor(theme.ANSI_FG[32]).name()
        document = term.document()
        for number, length in ((0, cap), (1, 100)):
            fragment = document.findBlockByNumber(number).begin().fragment()
            assert fragment.length() == length
            assert fragment.charFormat().foreground().color().name() == green
    finally:
        _close(term)


def test_text_written_over_a_line_and_past_its_end_is_cut_too(qapp):
    from turboadb.gui.console import AnsiConsole

    cap = AnsiConsole._LINE_MAX
    term = _console()
    try:
        term.feed("progress 10%\r" + "y" * (cap + 20) + "\r\n")
        _render(term)
        assert term.toPlainText() == "y" * cap + "\n" + "y" * 20 + "\n"
    finally:
        _close(term)


def test_a_line_too_long_to_search_has_no_words_coloured_in_any_piece(qapp):
    from turboadb.gui.console import AnsiConsole

    cap = AnsiConsole._LINE_MAX
    term = _console()
    try:
        term.feed("failed " + "x" * cap + " failed\r\nshort and failed\r\n")
        _render(term)
        plain = QColor(theme.TERM_FG).name()
        document = term.document()
        for number in (0, 1):
            block = document.findBlockByNumber(number)
            colours = set()
            it = block.begin()
            while not it.atEnd():
                colours.add(it.fragment().charFormat().foreground().color().name())
                it += 1
            assert colours == {plain}, number
        last = document.findBlockByNumber(2)
        assert last.text() == "short and failed"
        cursor = QTextCursor(last)
        cursor.setPosition(last.position() + len("short and fail"))
        assert cursor.charFormat().foreground().color().name() != plain
    finally:
        _close(term)
