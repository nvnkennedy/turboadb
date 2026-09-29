"""A folder of thousands of entries in the Files tab: the rows are built once,
in order, and the status line never walks them - not when the listing
arrives, and not on each mouse move of a selection rectangle.

Headless (Qt offscreen); no device, adb or network is used."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QItemSelection, QItemSelectionModel, Qt  # noqa: E402

from turboadb.gui import file_browser as fb  # noqa: E402

BIG = 5000


def _rows(count=BIG, folders=50):
    """A camera folder: some albums, then photos of growing size."""
    rows = [(f"Album {i:03d}", 3452, "<DIR>", "Folder", "2025-01-01 10:00", "drwxrwx--x",
             "u0_a1", True) for i in range(folders)]
    rows += [(f"IMG_{i:05d}.jpg", 1000 + i, fb._human_size(1000 + i), "JPEG image",
              f"2025-{1 + i % 12:02d}-{1 + i % 28:02d} {i % 24:02d}:{i % 60:02d}",
              "-rw-rw----", "u0_a1", False) for i in range(count - folders)]
    return rows


@pytest.fixture
def browser(qapp, monkeypatch):
    monkeypatch.setattr(fb.FileBrowser, "showEvent", lambda self, event: None)
    monkeypatch.setattr(fb.FileBrowser, "refresh_local", lambda self: None)
    monkeypatch.setattr(fb.FileBrowser, "refresh_remote", lambda self: None)
    page = fb.FileBrowser(None)
    page._loaded_remote = True
    page.remote_cwd = page.remote_table.base_dir = "/sdcard/DCIM/Camera"
    page.resize(1200, 700)
    page.show()
    qapp.processEvents()
    try:
        yield page
    finally:
        page.hide()
        page.close_panel()
        page.deleteLater()
        qapp.processEvents()


def _names(table):
    return [table.item(row, 0).data(Qt.UserRole)[0] for row in range(table.rowCount())]


def _spy(monkeypatch, table, name):
    """Count the Python calls of *table*'s method *name*."""
    calls = []
    real = getattr(table, name)
    monkeypatch.setattr(table, name, lambda *a, **k: calls.append(a) or real(*a, **k))
    return calls


# --------------------------------------------------------------------------- #
# the listing
# --------------------------------------------------------------------------- #
def test_a_listing_is_built_in_the_headers_order_without_moving_rows(browser, monkeypatch):
    """Every row used to be inserted one by one and then taken out and put
    back to sort them: a 20,000-entry folder took seconds to appear."""
    table = browser.remote_table
    table.horizontalHeader().setSortIndicator(1, Qt.DescendingOrder)  # largest first
    taken = _spy(monkeypatch, table, "takeItem")
    inserted = _spy(monkeypatch, table, "insertRow")
    rows = _rows(1000)
    browser._populate(table, rows, parent_row=True)
    assert taken == [] and inserted == []
    names = _names(table)
    files = sorted((r for r in rows if not r[7]), key=lambda r: r[1], reverse=True)
    folders = sorted((r for r in rows if r[7]), key=lambda r: r[1], reverse=True)
    assert names == [".."] + [r[0] for r in folders] + [r[0] for r in files]
    # exactly what sorting the built rows gives
    table.sortByColumn(1, Qt.DescendingOrder)
    assert _names(table) == names


def test_a_listing_sorted_by_date_uses_the_dates_value(browser):
    table = browser.remote_table
    table.horizontalHeader().setSortIndicator(3, Qt.AscendingOrder)
    browser._populate(table, [
        ("b.txt", 1, "1 B", "File", "2026-03-01 10:00", "-rw-r--r--", "", False),
        ("a.txt", 1, "1 B", "File", "Jan  5  2020", "-rw-r--r--", "", False),
        ("c.txt", 1, "1 B", "File", "2026-02-01T09:00:00.000000000 +0100", "-rw-r--r--", "",
         False),
    ], parent_row=True)
    assert _names(table) == ["..", "a.txt", "c.txt", "b.txt"]


# --------------------------------------------------------------------------- #
# the status line
# --------------------------------------------------------------------------- #
def test_selecting_everything_never_walks_the_rows(browser, qapp, monkeypatch):
    """One status update read every row and built an index per selected cell
    (selectedIndexes), on every selection change of a rectangle drag."""
    table = browser.remote_table
    browser._populate(table, _rows(), parent_row=True)
    assert browser.remote_status.text() == "50 folders, 4950 files"
    items = _spy(monkeypatch, table, "item")
    cells = _spy(monkeypatch, table, "selectedIndexes")  # one index per selected cell
    table.select_all_entries()
    qapp.processEvents()
    size = fb._human_size(sum(1000 + i for i in range(BIG - 50)))
    assert browser.remote_status.text() == \
        f"50 folders, 4950 files   ·   {BIG} selected ({size})"
    # a selection rectangle over rows 2..60: 49 albums, then 10 photos
    model = table.model()
    table.selectionModel().select(
        QItemSelection(model.index(2, 0), model.index(60, table.columnCount() - 1)),
        QItemSelectionModel.ClearAndSelect)
    qapp.processEvents()
    photos = fb._human_size(sum(1000 + i for i in range(10)))
    assert browser.remote_status.text() == \
        f"50 folders, 4950 files   ·   59 selected ({photos})"
    assert len(items) < 10  # Ctrl+A looks at the first rows; nothing reads them all
    assert cells == []


def test_the_status_follows_a_burst_of_changes_once(browser, qapp):
    table = browser.remote_table
    browser._populate(table, _rows(200, folders=0), parent_row=True)
    before = browser.remote_status.text()
    for row in range(1, 40):  # a drag: one change per mouse move
        table.selectRow(row)
    assert browser.remote_status.text() == before  # not yet: the drag is still going
    qapp.processEvents()
    assert browser.remote_status.text().endswith("1 selected (1.0 KB)")


def test_overlapping_selection_ranges_count_each_row_once(browser, qapp):
    table = browser.remote_table
    browser._populate(table, _rows(20, folders=0), parent_row=True)
    model, last = table.model(), table.columnCount() - 1
    both = QItemSelection(model.index(1, 0), model.index(4, last))
    both.select(model.index(3, 0), model.index(6, last))  # rows 3 and 4 twice
    table.selectionModel().select(both, QItemSelectionModel.ClearAndSelect)
    qapp.processEvents()
    assert table.selection_summary() == (6, sum(1000 + i for i in range(6)))
    assert browser._selected_rows(table) == [1, 2, 3, 4, 5, 6]


def test_the_counts_follow_a_header_sort(browser, qapp):
    table = browser.remote_table
    browser._populate(table, _rows(30, folders=3), parent_row=True)
    table._on_header_clicked(1)  # by size: ascending
    table._on_header_clicked(1)  # and descending
    table.selectRow(4)  # '..', three albums, then the largest photo
    qapp.processEvents()
    assert _names(table)[4] == "IMG_00026.jpg"
    assert browser.remote_status.text() == "3 folders, 27 files   ·   1 selected (1.0 KB)"
    assert table.selection_summary() == (1, 1026)


def test_rows_changed_some_other_way_are_read_again(browser):
    table = browser.remote_table
    browser._populate(table, _rows(10, folders=2), parent_row=True)
    row = table.rowCount()
    table.insertRow(row)
    browser._set_row(table, row, ("late.bin", 4096, "4.0 KB", "File", "", "", "", False), 0.0, "")
    table.selectRow(row)
    summary = table.row_summary()
    assert (summary.folders, summary.files) == (2, 9)
    assert table.selection_summary() == (1, 4096)


# --------------------------------------------------------------------------- #
# dates, as ls writes them
# --------------------------------------------------------------------------- #
def test_every_date_form_the_listing_accepts_sorts_by_its_value():
    now = time.mktime((2026, 9, 14, 12, 0, 0, 0, 0, -1))
    minute = fb._mtime_sort_key("2026-09-02 21:40")
    assert minute == time.mktime((2026, 9, 2, 21, 40, 0, 0, 0, -1))
    # toybox --full-time and the ISO form with a T: the same minute
    assert fb._mtime_sort_key("2026-09-02 21:40:05.123456789 +0200") == minute
    assert fb._mtime_sort_key("2026-09-02T21:40:05") == minute
    # the day first, as some busybox builds write it
    assert fb._mtime_sort_key("5 Jan 2024") == fb._mtime_sort_key("Jan  5  2024") > 0
    assert fb._mtime_sort_key("5 Jan 12:34", now=now) == \
        fb._mtime_sort_key("Jan  5 12:34", now=now) > 0
    assert fb._mtime_sort_key("2026-13-02 21:40") == 0.0
    assert fb._mtime_sort_key("") == fb._mtime_sort_key("okt. 5 2024") == 0.0
