"""Selecting and acting on files the way a file manager does: a selection
rectangle beside the names, Ctrl+A, Enter, Backspace, Delete / Shift+Delete,
the per-pane status line, and asking for write access when the device refuses."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PyQt5.QtGui import QKeyEvent, QKeySequence, QMouseEvent  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QShortcut  # noqa: E402

from turboadb.gui import file_browser as fb  # noqa: E402

ROWS = [
    ("Android", 0, "<DIR>", "Folder", "2026-09-01 10:00", "drwxrwx--x", "root", True),
    ("DCIM", 0, "<DIR>", "Folder", "2026-09-01 10:00", "drwxrwx--x", "root", True),
    ("build.prop", 2048, "2.0 KB", "File", "2026-09-01 10:00", "-rw-r--r--", "root", False),
    ("notes.txt", 20, "20 B", "File", "2026-09-01 10:00", "-rw-rw----", "u0_a1", False),
    ("song.mp3", 5000, "4.9 KB", "File", "2026-09-01 10:00", "-rw-rw----", "u0_a1", False),
]


class _Handler:
    serial = "V2318"

    def __init__(self, error=""):
        self.error = error
        self.removed = []
        self.shell_calls = []

    def remove(self, paths, *, recursive=False, safe=None):
        self.removed.append(list(paths))
        if self.error:
            raise RuntimeError(self.error)
        return list(paths)

    def shell(self, command, timeout=None, safe=None, **_kw):
        self.shell_calls.append(command)
        ok = not self.error

        class _Res:
            def __init__(self, ok, error):
                self.ok = ok
                self.stdout = ""
                self.stderr = "" if ok else error
                self.text = ""

        return _Res(ok, self.error)


def _sync_jobs(monkeypatch):
    def run(jobs, fn, on_done=None, on_fail=None):
        try:
            result = fn()
        except Exception as exc:
            if on_fail is not None:
                on_fail(f"{type(exc).__name__}: {exc}")
            return None
        if on_done is not None:
            on_done(result)
        return None

    monkeypatch.setattr(fb, "run_job", run)


@pytest.fixture
def browser(qapp, monkeypatch, tmp_path):
    """A listed, shown Files page whose jobs run inline."""
    monkeypatch.setattr(fb.FileBrowser, "showEvent", lambda self, event: None)
    monkeypatch.setattr(fb.FileBrowser, "refresh_local", lambda self: None)
    monkeypatch.setattr(fb.FileBrowser, "refresh_remote", lambda self: None)
    _sync_jobs(monkeypatch)
    page = fb.FileBrowser(_Handler())
    page._loaded_remote = True
    page.remote_cwd = "/sdcard"
    page.remote_table.base_dir = "/sdcard"
    page.local_cwd = str(tmp_path)
    page.local_table.base_dir = str(tmp_path)
    page._populate(page.remote_table, ROWS, parent_row=True)
    page._populate(page.local_table, ROWS, parent_row=True)
    page.resize(1200, 700)
    page.show()
    page.activateWindow()
    QApplication.setActiveWindow(page)
    QTest.qWaitForWindowActive(page, 2000)
    qapp.processEvents()
    try:
        yield page
    finally:
        page.hide()
        page.close_panel()
        page.deleteLater()
        qapp.processEvents()


def _selected(table):
    return sorted({index.row() for index in table.selectedIndexes()})


def _row_of(table, name):
    """The row listing *name* (rows are sorted: folders first, then files)."""
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        data = item.data(Qt.UserRole) if item is not None else None
        if data and data[0] == name:
            return row
    raise AssertionError(f"{name!r} is not listed")


def _select(table, name):
    table.selectRow(_row_of(table, name))
    return _row_of(table, name)


def _press(widget, sequence):
    """Fire the shortcut a key press finds from *widget* (its own, or its pane's).

    Real key events would do, but a shortcut only matches while the window is
    active, and an offscreen window loses that once other test windows have
    come and gone — the key would then quietly do nothing."""
    wanted = QKeySequence(sequence)
    scope = widget
    while scope is not None:
        for shortcut in scope.findChildren(QShortcut):
            if shortcut.key() == wanted and shortcut.parentWidget() is scope:
                shortcut.activated.emit()
                return shortcut
        scope = scope.parentWidget()
    raise AssertionError(f"no {sequence} shortcut for {widget}")


def _quiet_boxes(monkeypatch, answer=None):
    boxes = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(fb.QMessageBox, kind,
                            staticmethod(lambda *a, _k=kind, **k: boxes.append((_k, a[1:]))))
    monkeypatch.setattr(fb.QMessageBox, "question",
                        staticmethod(lambda *a, **k: boxes.append(("question", a[1:]))
                                     or (answer if answer is not None else fb.QMessageBox.No)))
    return boxes


# ---- selection ------------------------------------------------------------- #
def test_ctrl_a_selects_every_entry_but_the_parent_row(browser, qapp):
    table = browser.remote_table
    table.setFocus()
    qapp.processEvents()
    _press(table, QKeySequence.SelectAll)
    assert _selected(table) == [1, 2, 3, 4, 5]  # row 0 is ".."
    assert [name for name, _is_dir in browser._selected_remote()] == \
        ["Android", "DCIM", "build.prop", "notes.txt", "song.mp3"]


def test_ctrl_a_works_after_clicking_a_pane_button(browser, qapp):
    """Up, Refresh and the pane buttons no longer take the keyboard away."""
    table = browser.remote_table
    table.setFocus()
    refresh = [b for b in browser.findChildren(fb.QPushButton) if b.text() == "Refresh"][1]
    QTest.mouseClick(refresh, Qt.LeftButton)
    qapp.processEvents()
    # the click left the keyboard on the table, so the pane shortcuts still apply
    assert QApplication.focusWidget() is table
    _press(QApplication.focusWidget(), QKeySequence.SelectAll)
    assert _selected(table) == [1, 2, 3, 4, 5]


def test_the_path_field_keeps_its_own_ctrl_a_backspace_and_enter(browser):
    """A focused path field answers the shortcut-override, so Select all,
    Backspace and Enter edit the path instead of acting on the files."""
    field = browser.remote_path
    field.setText("/sdcard/DCIM")
    field.setFocus()
    for key, modifiers in ((Qt.Key_A, Qt.ControlModifier), (Qt.Key_Backspace, Qt.NoModifier),
                           (Qt.Key_Delete, Qt.NoModifier)):
        event = QKeyEvent(QEvent.ShortcutOverride, key, modifiers)
        QApplication.sendEvent(field, event)
        assert event.isAccepted(), key
    # Enter is not a pane shortcut at all: it is scoped to the tables themselves
    assert all(shortcut.parentWidget() in (browser.remote_table, browser.local_table)
               for shortcut in browser.findChildren(QShortcut)
               if shortcut.key() == QKeySequence("Return"))


def _drag(table, start, end, qapp, modifiers=Qt.NoModifier):
    QTest.mousePress(table.viewport(), Qt.LeftButton, modifiers, start)
    for step in range(1, 6):
        point = QPoint(start.x() + (end.x() - start.x()) * step // 5,
                       start.y() + (end.y() - start.y()) * step // 5)
        QApplication.sendEvent(table.viewport(), QMouseEvent(
            QEvent.MouseMove, point, Qt.NoButton, Qt.LeftButton, modifiers))
    qapp.processEvents()
    QTest.mouseRelease(table.viewport(), Qt.LeftButton, modifiers, end)


def _cell(table, row, column):
    return table.visualRect(table.model().index(row, column)).center()


def test_dragging_beside_the_names_selects_a_range(browser, qapp):
    table = browser.remote_table
    _drag(table, _cell(table, 2, 1), _cell(table, 4, 3), qapp)
    assert _selected(table) == [2, 3, 4]
    # the rectangle never picks up the ".." row
    _drag(table, _cell(table, 0, 1), _cell(table, 2, 1), qapp)
    assert _selected(table) == [1, 2]
    # …and the names of those rows are what the pane acts on
    assert [name for name, _is_dir in browser._selected_remote()] == ["Android", "DCIM"]


def test_ctrl_and_a_rectangle_add_to_the_selection(browser, qapp):
    table = browser.remote_table
    table.selectRow(1)
    _drag(table, _cell(table, 3, 1), _cell(table, 4, 1), qapp, modifiers=Qt.ControlModifier)
    assert _selected(table) == [1, 3, 4]


def test_a_click_beside_a_name_still_selects_only_that_row(browser, qapp):
    table = browser.remote_table
    QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, _cell(table, 3, 1))
    assert _selected(table) == [3]
    assert table._band_origin is None and not table._band_active


def test_a_drag_from_a_name_stays_a_file_drag(browser):
    table = browser.remote_table
    QTest.mousePress(table.viewport(), Qt.LeftButton, Qt.NoModifier, _cell(table, 2, 0))
    assert table._band_origin is None  # no rectangle: this press starts a drag
    QTest.mouseRelease(table.viewport(), Qt.LeftButton, Qt.NoModifier, _cell(table, 2, 0))


def test_band_rows_ignores_a_rectangle_below_the_last_row(browser):
    table = browser.remote_table
    below = table.verticalHeader().sectionPosition(5) + table.rowHeight(5) + 40
    assert table.band_rows(below, below + 20) == (0, -1)


# ---- keys ------------------------------------------------------------------ #
def test_enter_opens_the_current_folder_and_backspace_goes_up(browser, qapp):
    table = browser.remote_table
    table.setCurrentCell(_row_of(table, "Android"), 0)
    table.setFocus()
    qapp.processEvents()
    _press(table, QKeySequence("Return"))
    assert browser.remote_cwd == "/sdcard/Android"
    _press(table, QKeySequence("Backspace"))
    assert browser.remote_cwd == "/sdcard"


def test_escape_clears_the_selection(browser, qapp):
    table = browser.remote_table
    _select(table, "build.prop")
    table.setFocus()
    qapp.processEvents()
    _press(table, QKeySequence("Esc"))
    assert _selected(table) == []


def test_delete_and_shift_delete_both_delete_on_the_device(browser, qapp, monkeypatch):
    boxes = _quiet_boxes(monkeypatch, answer=fb.QMessageBox.Yes)
    table = browser.remote_table
    _select(table, "notes.txt")
    table.setFocus()
    qapp.processEvents()
    _press(table, QKeySequence.Delete)
    _press(table, QKeySequence("Shift+Del"))
    assert browser.handler.removed == [["/sdcard/notes.txt"], ["/sdcard/notes.txt"]]
    assert [kind for kind, _args in boxes] == ["question", "question"]


@pytest.mark.skipif(os.name != "nt", reason="the Recycle Bin is Windows-only")
def test_delete_recycles_locally_and_shift_delete_removes_for_good(browser, qapp, monkeypatch):
    _quiet_boxes(monkeypatch, answer=fb.QMessageBox.Yes)
    recycled, deleted = [], []
    monkeypatch.setattr(fb, "_recycle_local_items", lambda paths: recycled.append(list(paths)) or [])
    monkeypatch.setattr(fb, "_delete_local_items", lambda paths: deleted.append(list(paths)) or [])
    table = browser.local_table
    _select(table, "notes.txt")
    table.setFocus()
    qapp.processEvents()
    _press(table, QKeySequence.Delete)
    _press(table, QKeySequence("Shift+Del"))
    target = os.path.join(browser.local_cwd, "notes.txt")
    assert recycled == [[target]] and deleted == [[target]]


def test_the_recycle_list_is_nul_separated_and_double_nul_terminated(tmp_path):
    first, second = str(tmp_path / "a.txt"), str(tmp_path / "b.txt")
    assert fb._recycle_list([first, second]) == first + "\0" + second + "\0\0"
    assert fb._recycle_list([]) == "" and fb._recycle_list(["", None]) == ""
    assert fb._recycle_list(["a.txt"]) == os.path.abspath("a.txt") + "\0\0"


@pytest.mark.skipif(os.name != "nt", reason="the Recycle Bin is Windows-only")
def test_recycling_asks_the_shell_to_allow_undo_and_reports_refusals(monkeypatch, tmp_path):
    import ctypes

    operations, result = [], [0, False]

    class _Shell32:
        @staticmethod
        def SHFileOperationW(pointer):
            operation = pointer._obj
            operations.append(operation)
            operation.fAnyOperationsAborted = result[1]
            return result[0]

    monkeypatch.setattr(ctypes, "windll", type("W", (), {"shell32": _Shell32})())
    assert fb._recycle_local_items([str(tmp_path / "a.txt")]) == []
    operation = operations[0]
    assert operation.wFunc == 3  # FO_DELETE
    assert operation.fFlags & 0x0040 and operation.fFlags & 0x4000  # ALLOWUNDO, NUKE WARNING
    assert operation.pFrom.startswith(str(tmp_path))  # the field stops at the first NUL
    result[0] = 0x78
    assert "Shift+Delete" in fb._recycle_local_items([str(tmp_path / "a.txt")])[0]
    result[0], result[1] = 0, True
    assert fb._recycle_local_items([str(tmp_path / "a.txt")])[0].startswith("cancelled")
    assert fb._recycle_local_items([]) == [] and len(operations) == 3


# ---- what each pane says --------------------------------------------------- #
def test_the_status_line_counts_entries_and_the_selection(browser, qapp):
    table = browser.remote_table
    assert browser.remote_status.text() == "2 folders, 3 files"
    _select(table, "build.prop")  # 2048 bytes
    qapp.processEvents()
    assert browser.remote_status.text() == "2 folders, 3 files   ·   1 selected (2.0 KB)"
    _select(table, "DCIM")  # a folder has no size to add up
    qapp.processEvents()
    assert browser.remote_status.text() == "2 folders, 3 files   ·   1 selected"
    table.select_all_entries()
    qapp.processEvents()
    assert "5 selected" in browser.remote_status.text()


def test_an_empty_folder_says_so_and_loading_does_not(browser):
    table = browser.remote_table
    browser._populate(table, [], parent_row=True)
    assert table.empty_text == "This folder is empty"
    assert browser.remote_status.text() == ""  # the table itself says it is empty
    browser._set_loading(table)
    assert table.empty_text == ""


def test_a_narrow_pane_gives_the_names_the_room(browser, qapp):
    table = browser.remote_table
    table.resize(900, 400)
    qapp.processEvents()
    assert not table.isColumnHidden(2) and not table.isColumnHidden(5)
    table.resize(700, 400)  # Type goes first (the icon already says it)
    qapp.processEvents()
    assert table.isColumnHidden(2) and not table.isColumnHidden(5)
    table.resize(600, 400)  # then Owner
    qapp.processEvents()
    assert table.isColumnHidden(2) and table.isColumnHidden(5)
    table.resize(900, 400)
    qapp.processEvents()
    assert not table.isColumnHidden(2) and not table.isColumnHidden(5)


# ---- write access ---------------------------------------------------------- #
def test_a_refused_delete_asks_for_write_access_and_retries(browser, qapp, monkeypatch):
    _quiet_boxes(monkeypatch, answer=fb.QMessageBox.Yes)
    browser.handler.error = "rm: /system/app/x: Read-only file system"
    requests = []
    browser.write_access_needed.connect(requests.append)
    browser.remote_cwd = "/system/app"
    browser.remote_table.base_dir = "/system/app"
    _select(browser.remote_table, "notes.txt")
    browser._remote_delete()
    assert len(requests) == 1
    request = requests[0]
    assert request["path"] == "/system/app" and "Read-only" in request["error"]
    assert request["action"] == "deleting 1 item(s)"
    browser.handler.removed.clear()
    request["retry"]()  # after adb root / remount
    assert browser.handler.removed == [["/system/app/notes.txt"]]


def test_a_refused_device_command_asks_once_and_retries_only_what_failed(browser, monkeypatch):
    browser.handler.error = "mkdir: '/system/x': Read-only file system"
    requests = []
    browser.write_access_needed.connect(requests.append)
    browser.remote_cwd = "/system"
    browser._run_shell_batch([("mkdir x", "mkdir /system/x"), ("mkdir y", "mkdir /system/y")])
    assert len(requests) == 1 and requests[0]["action"] == "mkdir x"
    browser.handler.shell_calls.clear()
    browser.handler.error = ""
    requests[0]["retry"]()
    assert browser.handler.shell_calls == ["mkdir /system/x", "mkdir /system/y"]


def test_other_errors_are_not_offered_as_a_permission_problem(browser, monkeypatch):
    _quiet_boxes(monkeypatch, answer=fb.QMessageBox.Yes)
    browser.handler.error = "rm: /sdcard/x: No such file or directory"
    requests = []
    browser.write_access_needed.connect(requests.append)
    _select(browser.remote_table, "notes.txt")
    browser._remote_delete()
    assert requests == []


def test_a_refused_push_is_offered_once_the_queue_is_done(browser, monkeypatch):
    requests = []
    browser.write_access_needed.connect(requests.append)
    transfer = type("T", (), {"a": "C:/x/app.apk", "b": "/system/app/app.apk",
                              "direction": "push"})()
    browser._transfer = transfer
    browser._on_transfer_finished(
        transfer, False,
        "adb: error: failed to copy 'app.apk' to '/system/app/app.apk': "
        "remote couldn't create file: Read-only file system")
    assert len(requests) == 1 and requests[0]["path"] == "/system/app"
    assert requests[0]["action"] == "the push of 1 item(s)"
    queued = []
    monkeypatch.setattr(browser, "_enqueue_transfers", lambda jobs, msg: queued.append(list(jobs)))
    requests[0]["retry"]()
    assert queued == [[("C:/x/app.apk", "/system/app/app.apk", "push")]]
