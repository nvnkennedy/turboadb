"""Colour-coded icons on the GUI pages: file-table icons and sort order, empty
states, and the icon wiring of toolbars, tiles and dialogs."""

from __future__ import annotations

import json
import os


import pytest

pytest.importorskip("PyQt5")


def _rows():
    #       name,         raw,  size,   type,     mtime, perms, owner, is_dir
    return [
        ("zeta.TXT", 10, "10 B", "File", "", "", "", False),
        ("Beta", 0, "<DIR>", "Folder", "", "", "", True),
        ("alpha.PNG", 20, "20 B", "File", "", "", "", False),
        ("app.apk", 30, "30 B", "File", "", "", "", False),
        ("apple", 0, "<DIR>", "Folder", "", "", "", True),
        ("Clip.mp4", 40, "40 B", "File", "", "", "", False),
        ("shortcut", 0, "0 B", "File Link", "", "", "", False),
    ]


def test_file_table_names_are_plain_with_icons_and_sort_by_real_name(qapp):
    from PyQt5.QtCore import Qt
    from turboadb.gui.file_browser import ICON_ROLE, FileBrowser

    browser = FileBrowser(None)
    try:
        table = browser.remote_table
        table.horizontalHeader().setSortIndicator(0, Qt.AscendingOrder)
        browser._populate(table, _rows(), parent_row=True)
        texts = [table.item(r, 0).text() for r in range(table.rowCount())]
        # '..' first, folders before files, names case-insensitive; no emoji prefix
        assert texts == ["..", "apple", "Beta", "alpha.PNG", "app.apk", "Clip.mp4",
                         "shortcut", "zeta.TXT"]
        for r in range(table.rowCount()):
            item = table.item(r, 0)
            assert item.text() == item.data(Qt.UserRole)[0]
            assert not item.data(Qt.DecorationRole).isNull()
        kinds = {table.item(r, 0).text(): table.item(r, 0).data(ICON_ROLE)
                 for r in range(table.rowCount())}
        assert kinds == {
            "..": "arrow-up", "apple": "folder", "Beta": "folder", "alpha.PNG": "image",
            "app.apk": "apps", "Clip.mp4": "video", "shortcut": "link", "zeta.TXT": "file",
        }

        table.sortByColumn(0, Qt.DescendingOrder)
        texts = [table.item(r, 0).text() for r in range(table.rowCount())]
        assert texts == ["..", "Beta", "apple", "zeta.TXT", "shortcut", "Clip.mp4",
                         "app.apk", "alpha.PNG"]
    finally:
        browser.close_panel()


def test_file_table_drag_payload_and_row_entries_use_plain_names(qapp):
    from PyQt5.QtCore import Qt
    from turboadb.gui.file_browser import _MIME, FileBrowser

    browser = FileBrowser(None)
    try:
        table = browser.remote_table
        table.base_dir = "/sdcard"
        browser._populate(table, _rows(), parent_row=True)
        rows = {table.item(r, 0).text(): r for r in range(table.rowCount())}
        assert browser._row_entry(table, rows["Beta"]) == ("Beta", True)
        assert browser._row_entry(table, rows[".."]) is None
        payload = json.loads(bytes(table.mimeData(
            [table.item(rows["app.apk"], 0), table.item(rows["Beta"], 0)]
        ).data(_MIME)).decode("utf-8"))
        assert sorted(payload["paths"]) == ["/sdcard/Beta", "/sdcard/app.apk"]
        assert table._drop_target(table.visualItemRect(table.item(rows["Beta"], 0)).center()) \
            == "/sdcard/Beta"
        assert table.item(rows["Beta"], 0).flags() & Qt.ItemIsDragEnabled
    finally:
        browser.close_panel()


def test_file_browser_buttons_carry_icons(qapp):
    from PyQt5.QtWidgets import QPushButton
    from turboadb.gui.file_browser import FileBrowser

    browser = FileBrowser(None)
    try:
        labels = {b.text(): b for b in browser.findChildren(QPushButton)}
        for text in ("Home", "Up", "Refresh", "New folder", "New file", "Edit", "Copy",
                     "Paste", "Rename", "Delete", "Push", "Pull", "/sdcard"):
            assert not labels[text].icon().isNull(), text
        assert browser.btn_push.property("tone") == "blue"
        assert browser.btn_pull.property("tone") == "green"
    finally:
        browser.close_panel()


def test_logcat_empty_hint_hides_once_output_arrives(qapp):
    from turboadb.gui.logcat_view import LogcatPanel

    panel = LogcatPanel(None)
    try:
        assert not panel._empty_hint.isHidden()
        assert panel._empty_hint.text() == "Press Start to stream the device log"
        panel.hist.setCurrentIndex(panel.hist.count() - 1)  # a dump mode
        assert "Dump" in panel._empty_hint.text()
        panel.hist.setCurrentIndex(0)
        panel._on_batch([])
        assert not panel._empty_hint.isHidden()  # nothing arrived yet
        panel._on_batch(["09-14 10:00:00.000  1  1 I Tag: hello"])
        assert panel._empty_hint.isHidden()
        panel._render_pending()
        assert "hello" in panel.view.toPlainText()
        panel._clear_view()
        assert not panel._empty_hint.isHidden()
        assert not panel.btn_start.icon().isNull()
        assert all(not panel.level.itemIcon(i).isNull() for i in range(panel.level.count()))
    finally:
        panel.close_panel()


def test_apps_panel_icons_and_empty_state(qapp):
    from turboadb.gui.apps_panel import AppsPanel

    panel = AppsPanel(None)
    try:
        assert panel.empty.isHidden()
        panel._on_packages(panel._list_generation, ["com.example.alpha", "org.sample.beta"])
        assert panel.list.count() == 2 and not panel.list.item(0).icon().isNull()
        panel.filt.setText("nothing-matches")
        assert panel.list.isHidden() and not panel.empty.isHidden()
        assert "nothing-matches" in panel.empty_title.text()
        panel.filt.setText("alpha")
        assert not panel.list.isHidden() and panel.empty.isHidden()
        assert panel.list.count() == 1
        panel._on_packages(panel._list_generation, [])
        assert not panel.empty.isHidden() and panel.empty_title.text() == "No packages found"
        for b in (panel.btn_install, panel.btn_refresh, panel.btn_start, panel.btn_stop,
                  panel.btn_clear, panel.btn_uninstall):
            assert not b.icon().isNull()
    finally:
        panel.close_panel()


def test_welcome_tiles_have_tinted_icon_badges(qapp):
    from turboadb.gui.welcome import WelcomeScreen

    class Win:
        def new_session(self): pass
        def open_selected(self): pass
        def open_webcam_tab(self): pass
        def upgrade_tools_gui(self): pass

    ws = WelcomeScreen(Win())
    try:
        tones = [tile.badge.property("tone") for tile in ws.tiles]
        assert tones == ["green", "blue", "red", "amber"]
        assert all(not tile.badge.icon().isNull() for tile in ws.tiles)
        assert not ws.brand_mark.icon().isNull()
    finally:
        ws.close()


def test_camera_idle_view_paints_icon_and_buttons_have_icons(qapp, monkeypatch):
    from turboadb.gui import camera_widget

    monkeypatch.setattr(camera_widget.CameraPanel, "_auto_scan", lambda self: None)
    panel = camera_widget.CameraPanel()
    try:
        panel.resize(640, 480)
        assert panel.view.idle()
        assert not panel.view.grab().isNull()  # the idle paint path runs cleanly
        assert panel.start_btn.text() == "Start camera"
        for b in (panel.start_btn, panel.refresh_btn, panel.snap_btn, panel.rec_btn,
                  panel.pause_btn):
            assert not b.icon().isNull()
    finally:
        panel.close_panel()
        panel.close()


def test_dialog_transport_choices_and_settings_pages_have_icons(qapp, monkeypatch):
    from turboadb.gui import connect_dialog
    from turboadb.gui.session_dialog import SessionDialog
    from turboadb.gui.settings_dialog import SettingsDialog

    monkeypatch.setattr(connect_dialog.ConnectDialog, "_scan_usb", lambda self: None)
    dlg = connect_dialog.ConnectDialog()
    try:
        assert all(not dlg.mode.itemIcon(i).isNull() for i in range(dlg.mode.count()))
        assert not dlg.connect_btn.icon().isNull()
    finally:
        dlg.deleteLater()
    session = SessionDialog()
    try:
        assert all(not session.mode.itemIcon(i).isNull() for i in range(session.mode.count()))
    finally:
        session.deleteLater()
    settings = SettingsDialog()
    try:
        assert all(not settings.nav.item(i).icon().isNull() for i in range(settings.nav.count()))
        assert all(not b.icon().isNull() for b in settings._theme_buttons.values())
    finally:
        settings.deleteLater()
