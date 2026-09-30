"""The device terminal's size is exactly what the view shows.

``stty cols/rows`` came from the average character width of the whole page,
one column less for luck, and the vertical scroll bar appeared later on top
of it: a full-width ``ls`` row wrapped and its columns went out of line.
Now it is the number of whole character cells between the margins, with
room kept for the scroll bar whether it shows yet or not."""

import pytest

pytest.importorskip("PyQt5")


def _console(qapp, width=700, height=300):
    from turboadb.gui.console import AnsiConsole

    term = AnsiConsole(send_fn=lambda _data: None)
    term.set_emulate_prompt(False)
    term.resize(width, height)
    term.show()
    qapp.processEvents()
    return term


def _draw(term, text):
    term.feed(text)
    while term._inq:
        term._drain_tick()


@pytest.mark.parametrize("width, size", [(700, 10), (901, 12), (1234, 14)])
def test_a_line_of_exactly_the_columns_never_scrolls_sideways(qapp, width, size):
    term = _console(qapp, width)
    try:
        term.set_font_size(size)
        qapp.processEvents()
        cols, rows = term.cell_size()
        assert cols > 20 and rows > 5
        _draw(term, "line\r\n" * (rows * 2))  # the vertical scroll bar shows
        qapp.processEvents()
        assert term.verticalScrollBar().isVisible()
        bar = term.horizontalScrollBar()
        _draw(term, "M" * cols)
        qapp.processEvents()
        assert bar.maximum() == 0  # all of it shows
        _draw(term, "M")
        qapp.processEvents()
        # One more does not: cols is every whole cell.  The sideways scroll
        # range counts whole pixels, though, and with a font whose advance is a
        # fraction of a pixel (Linux's DejaVu Sans Mono at some sizes) one more
        # character may overrun the view by less than one; two more always
        # show.  One over, which would scroll every full-width line sideways,
        # is what the check above rules out.
        from PyQt5.QtGui import QFontMetricsF

        advance = QFontMetricsF(term.document().defaultFont()).horizontalAdvance("M")
        if not float(advance).is_integer() and bar.maximum() == 0:
            _draw(term, "M")
            qapp.processEvents()
        assert bar.maximum() > 0
    finally:
        term.close_archive()
        term.deleteLater()


def test_the_size_is_the_same_before_and_after_the_scroll_bar_shows(qapp):
    term = _console(qapp)
    try:
        before = term.cell_size()
        assert not term.verticalScrollBar().isVisible()
        _draw(term, "line\r\n" * (before[1] * 3))
        qapp.processEvents()
        assert term.verticalScrollBar().isVisible()
        assert term.cell_size() == before
        _draw(term, "M" * before[0])  # a full-width row still fits beside the bar
        qapp.processEvents()
        assert term.horizontalScrollBar().maximum() == 0
        # and the rows fill the view without a partial one
        line = term.fontMetrics().lineSpacing()
        margin = term.document().documentMargin()
        assert before[1] * line <= term.viewport().height() - 2 * margin < (before[1] + 1) * line
    finally:
        term.close_archive()
        term.deleteLater()


def test_the_android_shell_tells_the_device_the_views_cells(qapp):
    import re

    from test_android_shell_pty import _close, _ready, _widget

    widget, handler = _widget(qapp)
    try:
        widget.show()
        _ready(qapp, widget)
        first = handler.sessions[0].sent[0].decode()
        cols, rows = map(int, re.search(r"stty cols (\d+) rows (\d+)", first).groups())
        assert (cols, rows) == widget.term.cell_size()
    finally:
        _close(widget)
