"""The Files tab's two panes stay level, with Push and Pull alike between them.

Push and Pull are the same size, one above the other and centred between the
PC and device lists.  Each pane's header and footer are as tall as the
other's, so the two lists start and end at the same height even when a row
wraps on one side only (the PC side also has the drive list; a pane dragged
narrower)."""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from turboadb.gui import file_browser as fb  # noqa: E402


def _settle(qapp):
    for _ in range(10):
        qapp.processEvents()


def _shown(qapp, tmp_path, width):
    browser = fb.FileBrowser(None, local_start=str(tmp_path))
    browser.setAttribute(Qt.WA_DontShowOnScreen, True)
    browser.resize(width, 640)
    browser.show()
    _settle(qapp)
    return browser


def _rect(browser, widget):
    """``(x, y, width, height)`` of *widget* in the Files tab."""
    corner = widget.mapTo(browser, QPoint(0, 0))
    return corner.x(), corner.y(), widget.width(), widget.height()


def _heads(browser):
    return browser.local_path.parentWidget(), browser.remote_path.parentWidget()


@pytest.mark.parametrize("width", [1400, 960, 800])
def test_push_and_pull_are_alike_and_centred_between_the_lists(qapp, tmp_path, width):
    browser = _shown(qapp, tmp_path, width)
    try:
        push, pull = _rect(browser, browser.btn_push), _rect(browser, browser.btn_pull)
        assert push[2:] == pull[2:]  # the same size
        assert push[0] == pull[0]  # one right above the other
        assert pull[1] > push[1] + push[3]
        left, right = _rect(browser, browser.local_table), _rect(browser, browser.remote_table)
        between = (left[0] + left[2] + right[0]) / 2
        assert abs(push[0] + push[2] / 2 - between) <= 1
        assert abs((push[1] + pull[1] + pull[3]) / 2 - (left[1] + left[3] / 2)) <= 1
    finally:
        browser.close_panel()


def test_the_lists_stay_level_when_one_sides_rows_wrap(qapp, tmp_path):
    browser = _shown(qapp, tmp_path, 1400)
    try:
        split = browser.local_table.parentWidget().parentWidget()
        split.setSizes([150, 104, 1100])  # the PC side's rows wrap, the device's do not
        _settle(qapp)
        lhead, rhead = _heads(browser)
        assert lhead.layout().heightForWidth(lhead.width()) > \
            rhead.layout().heightForWidth(rhead.width())
        left, right = _rect(browser, browser.local_table), _rect(browser, browser.remote_table)
        assert (left[1], left[3]) == (right[1], right[3])  # same top, same height

        split.setSizes([640, 104, 640])  # and back: no row wraps, no gap is left
        _settle(qapp)
        lhead, rhead = _heads(browser)
        assert lhead.height() == rhead.height() == rhead.layout().heightForWidth(rhead.width())
        left, right = _rect(browser, browser.local_table), _rect(browser, browser.remote_table)
        assert (left[1], left[3]) == (right[1], right[3])
    finally:
        browser.close_panel()


def _feet(browser):
    return browser.local_status.parentWidget(), browser.remote_status.parentWidget()


def _drag_handle(qapp, split, index, dx):
    """Drag the splitter's handle *index* by *dx* pixels with the mouse, in
    steps, as a hand does: a pane passes through its narrowest width before
    it snaps shut."""
    handle = split.handle(index)
    grip = handle.rect().center()
    origin = handle.mapToGlobal(grip)
    QTest.mousePress(handle, Qt.LeftButton, Qt.NoModifier, grip)
    for moved in range(0, dx, 10 if dx > 0 else -10):
        to = QPoint(origin.x() + moved, origin.y())
        QApplication.sendEvent(handle, QMouseEvent(
            QEvent.MouseMove, QPointF(handle.mapFromGlobal(to)), QPointF(to), Qt.NoButton,
            Qt.LeftButton, Qt.NoModifier))
        _settle(qapp)
    QTest.mouseRelease(handle, Qt.LeftButton, Qt.NoModifier,
                       handle.mapFromGlobal(QPoint(origin.x() + dx, origin.y())))
    _settle(qapp)


@pytest.mark.parametrize("shut", ["pc", "device"])
def test_a_pane_dragged_shut_leaves_the_other_its_own_header_and_footer(qapp, tmp_path, shut):
    """On its way shut a pane is at its narrowest, where its rows wrap the
    most.  The other pane kept a header and footer that tall, its list lost
    half its height, and the page could no longer be made lower."""
    browser = _shown(qapp, tmp_path, 1280)
    try:
        split = browser.local_table.parentWidget().parentWidget()
        lowest = browser.minimumSizeHint().height()
        gone, kept = (0, 1) if shut == "pc" else (1, 0)
        _drag_handle(qapp, split, 1 if shut == "pc" else 2, -700 if shut == "pc" else 700)
        assert split.sizes()[2 * gone] == 0 and split.sizes()[2 * kept] > 0

        head, foot = _heads(browser)[kept], _feet(browser)[kept]
        table = (browser.local_table, browser.remote_table)[kept]
        assert head.height() == head.layout().heightForWidth(head.width())
        assert foot.height() == foot.layout().heightForWidth(foot.width())
        assert browser.minimumSizeHint().height() <= lowest
        # Push and Pull stay centred on the list that is left
        push, pull = _rect(browser, browser.btn_push), _rect(browser, browser.btn_pull)
        rows = _rect(browser, table)
        assert abs((push[1] + pull[1] + pull[3]) / 2 - (rows[1] + rows[3] / 2)) <= 1

        split.setSizes([588, 104, 588])  # opened again: the panes are level as before
        _settle(qapp)
        left, right = _rect(browser, browser.local_table), _rect(browser, browser.remote_table)
        assert (left[1], left[3]) == (right[1], right[3])
        lhead, rhead = _heads(browser)
        assert lhead.height() == rhead.height()
    finally:
        browser.close_panel()
