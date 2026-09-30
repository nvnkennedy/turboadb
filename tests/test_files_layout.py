"""The Files tab's two panes stay level, with Push and Pull alike between them.

Push and Pull are the same size, one above the other and centred between the
PC and device lists.  Each pane's header and footer are as tall as the
other's, so the two lists start and end at the same height even when a row
wraps on one side only (the PC side also has the drive list; a pane dragged
narrower)."""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QPoint, Qt  # noqa: E402

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
