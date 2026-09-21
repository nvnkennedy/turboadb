"""More than one Files tab on the same device connection.

Headless (Qt offscreen); the fake device from test_adb_processes stands in for
adb, so no device, adb or network is used."""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QMessageBox, QTabBar  # noqa: E402

from test_adb_processes import _Device, _close, _connect, _pump, _tab  # noqa: E402
from turboadb.gui import device_tab as dt  # noqa: E402
from turboadb.gui.file_browser import FileBrowser  # noqa: E402


@pytest.fixture
def tab(qapp):
    widget = _tab(qapp)
    _connect(widget, _Device())
    widget.show()
    qapp.processEvents()
    yield widget
    _close(qapp, widget)


def _labels(tab):
    return [tab.inner.tabText(index) for index in range(tab.inner.count())]


def _close_button(tab, widget):
    return tab.inner.tabBar().tabButton(tab.inner.indexOf(widget), QTabBar.RightSide)


def _middle_click(tab, widget):
    bar = tab.inner.tabBar()
    centre = bar.tabRect(tab.inner.indexOf(widget)).center()
    event = QMouseEvent(QEvent.MouseButtonRelease, QPointF(centre), Qt.MiddleButton,
                        Qt.MiddleButton, Qt.NoModifier)
    return tab.eventFilter(bar, event)


# --------------------------------------------------------------------------- #
# opening
# --------------------------------------------------------------------------- #
def test_a_second_files_tab_opens_next_to_the_first(tab):
    page = tab.open_files_tab("/sdcard/DCIM")
    assert isinstance(page, FileBrowser)
    assert _labels(tab) == ["Terminal", "Logcat", "Files", "Files 2", "Device Control",
                            "Apps", "Phone", "Webcam"]
    assert tab.inner.currentWidget() is page
    assert page.remote_cwd == "/sdcard/DCIM"
    assert page.handler is tab.handler  # the same device session


def test_new_tab_from_a_files_page_starts_in_its_folders(tab, qapp, tmp_path):
    tab.show_subtab("files")
    assert _pump(qapp, lambda: tab._lazy_pages["files"].page is not None)
    first = tab.files
    first.remote_cwd = "/sdcard/Download"
    first.local_cwd = str(tmp_path)
    first._request_new_tab()  # the New tab button / Ctrl+Shift+T
    second = tab._extra_files["files-2"]
    assert (second.remote_cwd, second.local_cwd) == ("/sdcard/Download", str(tmp_path))


def test_each_files_tab_is_independent(tab):
    second = tab.open_files_tab()
    third = tab.open_files_tab()
    assert second is not third
    assert second.transfers is not third.transfers  # its own queue and history
    assert _labels(tab)[2:5] == ["Files", "Files 2", "Files 3"]


def test_extra_files_tabs_can_open_more_tabs_too(tab):
    second = tab.open_files_tab("/sdcard/Music")
    second._request_new_tab()
    assert tab._extra_files["files-3"].remote_cwd == "/sdcard/Music"


def test_numbers_are_reused_after_a_tab_closes(tab):
    second = tab.open_files_tab()
    tab.open_files_tab()
    assert tab.close_files_tab(second)
    again = tab.open_files_tab()
    assert tab._subtab_meta[again][1] == "Files 2"
    assert _labels(tab)[2:5] == ["Files", "Files 3", "Files 2"]


def test_there_is_a_ceiling_on_files_tabs(tab, monkeypatch):
    told = []
    monkeypatch.setattr(dt.QMessageBox, "information",
                        staticmethod(lambda *a, **k: told.append(a[2])))
    for _ in range(tab.MAX_FILES_TABS - 1):
        assert tab.open_files_tab() is not None
    assert tab.open_files_tab() is None
    assert told and f"{tab.MAX_FILES_TABS} Files tabs" in told[0]


def test_extra_files_tabs_get_the_files_icon_and_a_close_button(tab):
    page = tab.open_files_tab()
    assert _close_button(tab, page) is not None
    assert _close_button(tab, tab._lazy_pages["files"]) is None  # the first one stays
    files_icon = tab.inner.tabIcon(tab.inner.indexOf(tab._lazy_pages["files"]))
    assert not tab.inner.tabIcon(tab.inner.indexOf(page)).isNull()
    assert not files_icon.isNull()


def test_nothing_opens_before_connecting_or_on_a_terminal_only_tab(qapp):
    unconnected = _tab(qapp)
    try:
        assert unconnected.open_files_tab() is None
    finally:
        _close(qapp, unconnected)
    terminal = _tab(qapp, terminal_only=True)
    try:
        _connect(terminal, _Device())
        assert terminal.open_files_tab() is None
    finally:
        _close(qapp, terminal)


# --------------------------------------------------------------------------- #
# closing
# --------------------------------------------------------------------------- #
def test_the_close_button_closes_the_tab_and_its_workers(tab):
    page = tab.open_files_tab()
    _close_button(tab, page).click()
    assert "Files 2" not in _labels(tab)
    assert page._closing  # its workers and transfers were stopped
    assert "files-2" not in tab._extra_files and "files-2" not in tab._subtabs


def test_the_first_files_tab_cannot_be_closed(tab):
    assert tab.close_files_tab(tab._lazy_pages["files"]) is False
    assert "Files" in _labels(tab)


def test_middle_click_closes_only_extra_files_tabs(tab):
    page = tab.open_files_tab()
    assert _middle_click(tab, tab._lazy_pages["files"]) is False
    assert "Files" in _labels(tab)
    assert _middle_click(tab, page) is True
    assert "Files 2" not in _labels(tab)


def test_closing_a_tab_that_is_copying_asks_first(tab, monkeypatch):
    page = tab.open_files_tab()
    page.transfers.add([("/sdcard/a.mp4", "C:/a.mp4", "pull")])  # still queued
    answers = [QMessageBox.No, QMessageBox.Yes]
    asked = []

    def question(parent, title, text, *args):
        asked.append(text)
        return answers.pop(0)

    monkeypatch.setattr(dt.QMessageBox, "question", staticmethod(question))
    assert tab.close_files_tab(page) is False  # "No": it stays open
    assert "Files 2" in _labels(tab) and not page._closing
    assert "still copying (1 transfer running or queued)" in asked[0]
    assert tab.close_files_tab(page) is True  # "Yes": closed, transfers cancelled
    assert page._closing


def test_closing_the_session_closes_every_files_tab(qapp):
    widget = _tab(qapp)
    _connect(widget, _Device())
    second = widget.open_files_tab()
    third = widget.open_files_tab()
    _close(qapp, widget)
    assert second._closing and third._closing


# --------------------------------------------------------------------------- #
# menus
# --------------------------------------------------------------------------- #
def _menu_labels(tab, widget, monkeypatch):
    shown = []
    monkeypatch.setattr(dt.QMenu, "exec_",
                        lambda menu, *a: shown.append([x.text() for x in menu.actions()]))
    bar = tab.inner.tabBar()
    index = tab.inner.indexOf(widget)
    tab._tab_context_menu(bar.tabRect(index).center() if index >= 0 else QPoint(-5, -5))
    return shown[0] if shown else None


def test_files_tabs_have_a_right_click_menu(tab, monkeypatch):
    page = tab.open_files_tab()
    assert _menu_labels(tab, tab._lazy_pages["files"], monkeypatch) == ["New Files tab here"]
    assert _menu_labels(tab, page, monkeypatch) == ["New Files tab here",
                                                     "Close this Files tab"]


def test_other_tabs_get_no_menu(tab, monkeypatch):
    # (the Terminal tab has its own: see test_terminal_session.py)
    assert _menu_labels(tab, tab.combo_view, monkeypatch) is None


def test_more_menu_opens_a_files_tab_from_the_one_on_screen(tab):
    page = tab.open_files_tab("/sdcard/Pictures")
    tab.inner.setCurrentWidget(page)
    tab._new_files_tab_here()
    assert tab._extra_files["files-3"].remote_cwd == "/sdcard/Pictures"


# --------------------------------------------------------------------------- #
# split view: two Files panes side by side
# --------------------------------------------------------------------------- #
def test_two_files_tabs_can_share_a_split_view(tab, qapp):
    page = tab.open_files_tab("/sdcard/DCIM")
    keys = [key for key, _icon, _label in tab._split_choices()]
    assert keys[-1] == "files-2" and "files" in keys
    tab._activate_split(["files", "files-2"], Qt.Horizontal)
    assert tab._split_panes["files-2"].layout().itemAt(1).widget() is page
    title = tab._split_panes["files-2"].layout().itemAt(0).widget().text()
    assert "Files 2" in title
    tab._leave_split()
    # the strip comes back in order, the close button with it
    assert _labels(tab)[2:4] == ["Files", "Files 2"]
    assert _close_button(tab, page) is not None


def test_leaving_a_split_keeps_extra_files_tabs_in_their_place(tab):
    tab.open_files_tab()
    tab.open_files_tab()
    before = _labels(tab)
    tab._activate_split(["shell", "logcat"], Qt.Horizontal)
    tab._leave_split()
    assert _labels(tab) == before


def test_opening_a_tab_during_a_split_returns_to_the_tabs(tab):
    tab._activate_split(["shell", "logcat"], Qt.Horizontal)
    page = tab.open_files_tab()
    assert not tab._split_keys
    assert tab._content_stack.currentWidget() is tab.inner
    assert tab.inner.currentWidget() is page


def test_closing_a_files_tab_that_is_in_a_split(tab):
    page = tab.open_files_tab()
    tab._activate_split(["files", "files-2"], Qt.Horizontal)
    assert tab.close_files_tab(page)
    assert not tab._split_keys and "Files 2" not in _labels(tab)


def test_show_subtab_focuses_an_extra_files_pane_in_a_split(tab):
    page = tab.open_files_tab()
    tab._activate_split(["files", "files-2"], Qt.Horizontal)
    tab.show_subtab("files-2")  # already on screen: the split stays
    assert tab._split_keys == ("files", "files-2")
    assert page.isVisible()


# --------------------------------------------------------------------------- #
# device-wide actions reach every Files tab
# --------------------------------------------------------------------------- #
def test_files_pages_lists_every_built_files_tab(tab, qapp):
    assert tab._files_pages() == []  # the first one was never shown
    second = tab.open_files_tab()
    assert tab._files_pages() == [second]
    tab.show_subtab("files")
    assert _pump(qapp, lambda: tab._lazy_pages["files"].page is not None)
    assert tab._files_pages() == [tab.files, second]
