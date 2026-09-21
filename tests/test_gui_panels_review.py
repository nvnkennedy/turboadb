"""Headless regression tests for the GUI panel code-review fixes."""

from __future__ import annotations

import os
import shlex
import threading
import time


import pytest

pytest.importorskip("PyQt5")


# ---- file_browser.py ----


class _FakeShellResult:
    def __init__(self, stdout="", ok=True, stderr=""):
        self.stdout = stdout
        self.ok = ok
        self.stderr = stderr
        self.text = stdout.strip()


def test_file_browser_device_commands_quote_every_path():
    from turboadb.gui import file_browser as fb

    nasty = "/sdcard/it's a dir/$(reboot) `id`; rm -rf /"
    other = "/sdcard/new name"
    assert shlex.split(fb._mkdir_cmd(nasty)) == ["mkdir", "-p", nasty]
    assert shlex.split(fb._cp_cmd(nasty, other)) == ["cp", "-r", nasty, other]
    assert shlex.split(fb._mv_cmd(nasty, other)) == ["mv", nasty, other]
    assert shlex.split(fb._rm_cmd([nasty, other])) == ["rm", "-rf", nasty, other]
    touch = shlex.split(fb._touch_cmd(nasty))
    assert touch[:2] == ["touch", nasty] and touch[-1] == nasty
    # Listing uses a trailing slash so a symlinked folder (e.g. /sdcard) lists its target.
    assert shlex.split(fb._ls_cmd("/sdcard")) == ["ls", "-la", "/sdcard/"]
    assert shlex.split(fb._ls_cmd("/storage/my card")) == ["ls", "-la", "/storage/my card/"]
    assert fb._normalize_remote_path("-rf") == "/-rf"
    assert fb._normalize_remote_path("//sdcard/") == "/sdcard"


def test_file_browser_remote_mkdir_quotes_user_input(qapp, monkeypatch):
    from turboadb.gui import file_browser as fb

    browser = fb.FileBrowser(None)
    try:
        captured = []
        monkeypatch.setattr(fb.QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("new $(x) 'q'", True)))
        monkeypatch.setattr(browser, "_run_shell_batch",
                            lambda commands, timeout=30, **_kw: captured.extend(commands))
        browser.remote_cwd = "/sdcard/My Files"
        browser._remote_mkdir()
        assert len(captured) == 1
        assert shlex.split(captured[0][1]) == ["mkdir", "-p", "/sdcard/My Files/new $(x) 'q'"]
    finally:
        browser.close_panel()


def test_file_browser_symlinked_folders_resolve_as_dirs():
    from turboadb.gui import file_browser as fb

    calls = []

    class Handler:
        def shell(self, cmd, timeout=None, safe=None):
            calls.append(cmd)
            if cmd.startswith("ls "):
                return _FakeShellResult(
                    "total 8\n"
                    "lrw-r--r--   1 root root   21 2026-09-02 21:40 sdcard -> /storage/self/primary\n"
                    "lrwxrwxrwx   1 root root    7 2026-09-02 21:40 notes.txt -> x.txt\n"
                    "drwxr-xr-x   2 root root 4096 2026-09-02 21:40 etc\n"
                )
            return _FakeShellResult("d\nf\n")

    rows, error = fb._list_remote_dir(Handler(), "/")
    assert error == ""
    by_name = {row[0]: row for row in rows}
    assert set(by_name) == {"sdcard", "notes.txt", "etc"}
    assert by_name["sdcard"][7] is True
    assert by_name["sdcard"][3] == "Folder Link"
    assert by_name["sdcard"][2] == "<DIR>"
    assert by_name["notes.txt"][7] is False
    assert by_name["etc"][7] is True
    assert calls[0] == "ls -la /"
    assert len(calls) == 2
    assert shlex.split(calls[1])[:4] == ["for", "f", "in", "/sdcard"]


def test_file_browser_parses_device_nodes():
    from turboadb.gui.file_browser import FileBrowser

    parsed = FileBrowser._parse_ls_line("crw-rw-rw-  1 root root   1,   3 2026-09-02 21:40 null")
    assert parsed is not None
    name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir = parsed
    assert name == "null"
    assert raw_size == 0
    assert sz_str == "1, 3"
    assert ftype == "Character Device"
    assert mtime == "2026-09-02 21:40"
    assert perms == "crw-rw-rw-"
    assert owner == "root:root"
    assert is_dir is False

    block = FileBrowser._parse_ls_line("brw-------  1 root root 179,  0 Jan 15 12:00 mmcblk0")
    assert block[0] == "mmcblk0"
    assert block[3] == "Block Device"
    assert block[4] == "Jan 15 12:00"


def test_file_browser_editor_refuses_unsafe_content(tmp_path, monkeypatch):
    from turboadb.gui import file_browser as fb

    latin1 = tmp_path / "latin1.txt"
    latin1.write_bytes("caf\xe9".encode("latin-1"))
    with pytest.raises(fb._EditRefused):
        fb._read_for_edit(str(latin1))
    assert fb._edit_load_result(lambda: fb._read_for_edit(str(latin1)))[0] == "refused"

    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"ab\x00cd")
    with pytest.raises(fb._EditRefused):
        fb._read_for_edit(str(binary))

    monkeypatch.setattr(fb, "_EDIT_MAX_BYTES", 8)
    big = tmp_path / "big.txt"
    big.write_bytes(b"0123456789")
    with pytest.raises(fb._EditRefused):
        fb._read_for_edit(str(big))


def test_file_browser_editor_preserves_crlf_and_utf8(qapp, tmp_path):
    from PyQt5.QtWidgets import QLabel
    from turboadb.gui import file_browser as fb
    from turboadb.gui.fileutil import write_text_file

    original = "line one\r\nünïcödé ✓\r\n\r\nlast\r\n".encode("utf-8")
    path = tmp_path / "config.ini"
    path.write_bytes(original)
    text, newline, mixed = fb._read_for_edit(str(path))
    assert newline == "\r\n"
    assert mixed is False
    assert "\r" not in text

    dlg = fb._FileEditorDialog("config.ini", str(path) + "<b>x</b>", text, None)
    try:
        assert not dlg.is_dirty()
        # Header is plain text, not interpreted HTML.
        assert any("&lt;b&gt;x&lt;/b&gt;" in lbl.text() for lbl in dlg.findChildren(QLabel))
        write_text_file(str(path), fb._text_for_save(dlg.edit.toPlainText(), newline))
        assert path.read_bytes() == original

        dlg.edit.insertPlainText("x")
        assert dlg.is_dirty()
        assert "Modified" in dlg.status.text()
    finally:
        dlg.deleteLater()

    lf_file = tmp_path / "unix.sh"
    lf_file.write_bytes(b"#!/bin/sh\necho hi\n")
    lf_text, lf_newline, _ = fb._read_for_edit(str(lf_file))
    write_text_file(str(lf_file), fb._text_for_save(lf_text, lf_newline))
    assert lf_file.read_bytes() == b"#!/bin/sh\necho hi\n"


def test_file_browser_transfers_queue_behind_one_thread(qapp, monkeypatch):
    from PyQt5.QtCore import QObject, pyqtSignal
    from turboadb.gui import file_browser as fb

    started = []

    class FakeTransfer(QObject):
        progress = pyqtSignal(int)
        done = pyqtSignal(str)
        failed = pyqtSignal(str)
        finished = pyqtSignal()

        def __init__(self, handler, direction, a, b):
            super().__init__()
            self.direction, self.a, self.b = direction, a, b
            self.stopped = False

        def start(self):
            started.append(self)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(fb, "_TransferThread", FakeTransfer)
    monkeypatch.setattr(fb, "park_thread", lambda t: None)

    browser = fb.FileBrowser(None)
    try:
        browser._enqueue_transfers([("/sdcard/a", "a", "pull"), ("/sdcard/b", "b", "pull")], "first")
        browser._enqueue_transfers([("c", "/sdcard/c", "push")], "second")
        assert len(started) == 1
        assert [job[0] for job in browser._queue] == ["/sdcard/b", "c"]

        started[0].done.emit("ok")
        assert len(started) == 2 and started[1].a == "/sdcard/b"
        started[1].failed.emit("boom")
        assert len(started) == 3 and started[2].direction == "push"

        # A late signal from an earlier thread must not start a second transfer.
        started[0].finished.emit()
        assert len(started) == 3
    finally:
        browser.close_panel()
    assert started[2].stopped
    assert browser._queue == []


def test_file_browser_close_panel_never_waits(qapp, monkeypatch):
    from PyQt5.QtCore import QThread
    from turboadb.gui import file_browser as fb

    browser = fb.FileBrowser(None)
    release = threading.Event()
    callbacks = []
    job = browser._job(lambda: release.wait(10), lambda _r: callbacks.append("done"))

    def no_wait(self, *args, **kwargs):
        raise AssertionError("close_panel must not block on QThread.wait()")

    monkeypatch.setattr(QThread, "wait", no_wait)
    browser.close_panel()
    monkeypatch.undo()

    assert browser._jobs == []
    # Result signals are detached, so a late finish can't call into the closed panel.
    assert job.receivers(job.done) == 0 and job.receivers(job.fail) == 0
    release.set()
    assert job.wait(5000)
    assert callbacks == []


def test_file_browser_local_listing_is_async_and_drops_stale(qapp, tmp_path, monkeypatch):
    from PyQt5.QtCore import Qt
    from turboadb.gui import file_browser as fb

    (tmp_path / "sub").mkdir()
    (tmp_path / "file.txt").write_text("x")
    ui_thread = threading.get_ident()
    pending = []

    # Run each job on a real worker thread but deliver results when the test says
    # so (no event-loop spinning, which would also drain other tests' leftovers).
    def fake_run_job(jobs, fn, on_done=None, on_fail=None):
        box = {}

        def work():
            box["thread"] = threading.get_ident()
            try:
                box["result"] = fn()
            except Exception as exc:
                box["error"] = f"{type(exc).__name__}: {exc}"

        worker = threading.Thread(target=work)
        worker.start()
        worker.join(10)
        pending.append((box, on_done, on_fail))
        return None

    def deliver(entry):
        box, on_done, on_fail = entry
        if "error" in box:
            on_fail(box["error"])
        else:
            on_done(box["result"])

    monkeypatch.setattr(fb, "run_job", fake_run_job)
    browser = fb.FileBrowser(None)
    try:
        # Construction must not list the device: that waits for the first show.
        assert browser._loaded_remote is False and browser._remote_gen == 0
        for entry in list(pending):
            deliver(entry)
        pending.clear()

        target = os.path.abspath(str(tmp_path))
        browser._navigate_local(str(tmp_path / "sub"))   # older request
        browser._navigate_local(target)                  # newer request
        assert browser.local_table.item(0, 0).text() == "Loading…"
        older, newer = pending
        deliver(newer)
        deliver(older)  # arrives late: must be ignored
        assert browser.local_cwd == target
        assert all(box["thread"] != ui_thread for box, _d, _f in (older, newer))
        table = browser.local_table
        names = [table.item(r, 0).data(Qt.UserRole)[0] for r in range(table.rowCount())]
        assert names == ["..", "sub", "file.txt"]
    finally:
        browser.close_panel()


def test_file_browser_first_show_lists_device_once(qapp):
    from PyQt5.QtGui import QShowEvent
    from turboadb.gui.file_browser import FileBrowser

    browser = FileBrowser(None)
    try:
        calls = []
        original = browser.refresh_remote
        browser.refresh_remote = lambda: (calls.append(1), original())
        browser.showEvent(QShowEvent())
        browser.showEvent(QShowEvent())
        assert calls == [1]
    finally:
        browser.close_panel()


def test_file_browser_sort_folders_first_and_numeric_size(qapp):
    from PyQt5.QtCore import Qt
    from turboadb.gui.file_browser import FileBrowser

    browser = FileBrowser(None)
    try:
        table = browser.remote_table
        rows = [
            ("big.bin", 10 * 1024 * 1024, "10.0 MB", "File", "", "", "", False),
            ("small.txt", 500, "500 B", "File", "", "", "", False),
            ("zdir", 0, "<DIR>", "Folder", "", "", "", True),
            ("mid.log", 2048, "2.0 KB", "File", "", "", "", False),
        ]
        table.horizontalHeader().setSortIndicator(1, Qt.DescendingOrder)
        browser._populate(table, rows, parent_row=True)
        names = [table.item(r, 0).data(Qt.UserRole)[0] for r in range(table.rowCount())]
        assert names == ["..", "zdir", "big.bin", "mid.log", "small.txt"]
    finally:
        browser.close_panel()


def test_file_browser_drop_respects_payload_origin(qapp):
    import json
    from PyQt5.QtCore import QMimeData, QPointF, Qt
    from PyQt5.QtGui import QDropEvent
    from PyQt5.QtWidgets import QTableWidgetItem
    from turboadb.gui.file_browser import _MIME, _FileTableWidget

    def drop(table, payload):
        got = []
        table.dropped.connect(lambda paths, is_local, target: got.append((paths, is_local, target)))
        mime = QMimeData()
        mime.setData(_MIME, json.dumps(payload).encode("utf-8"))
        event = QDropEvent(QPointF(2, 2), Qt.CopyAction, mime, Qt.NoButton, Qt.NoModifier)
        table.dropEvent(event)
        table.dropped.disconnect()
        return got

    local = _FileTableWidget(is_remote=False)
    local.base_dir = "C:\\dst"
    local.browser = object()
    remote = _FileTableWidget(is_remote=True)
    remote.base_dir = "/sdcard"
    remote.browser = object()

    # Device paths from an unknown source (e.g. another device's tab) are refused.
    assert drop(local, {"is_remote": True, "paths": ["/sdcard/x"]}) == []
    assert drop(remote, {"is_remote": True, "paths": ["/sdcard/x"]}) == []
    # Local paths are usable anywhere and are flagged as local.
    assert drop(local, {"is_remote": False, "paths": ["D:\\a.txt"]}) == [(["D:\\a.txt"], True, "C:\\dst")]
    assert drop(remote, {"is_remote": False, "paths": ["D:\\a.txt"]}) == [(["D:\\a.txt"], True, "/sdcard")]

    # Dropping on the '..' row of a device folder targets its parent.
    remote.base_dir = "/sdcard/Download"
    remote.insertRow(0)
    dotdot = QTableWidgetItem("..")
    dotdot.setData(Qt.UserRole, ("..", True))
    remote.setItem(0, 0, dotdot)
    assert remote._drop_target(remote.visualItemRect(dotdot).center()) == "/sdcard"


def test_file_browser_local_copy_and_delete_workers(tmp_path):
    from turboadb.gui import file_browser as fb

    src = tmp_path / "src"
    (src / "inner").mkdir(parents=True)
    (src / "inner" / "a.txt").write_text("a")
    dst = tmp_path / "dst"
    dst.mkdir()

    assert fb._find_local_collisions([str(src)], str(dst)) == []
    assert fb._copy_local_items([str(src)], str(dst)) == []
    assert (dst / "src" / "inner" / "a.txt").read_text() == "a"
    assert fb._find_local_collisions([str(src)], str(dst)) == ["src"]

    errors = fb._copy_local_items([str(src)], str(src / "inner"))
    assert errors and "into itself" in errors[0]

    assert fb._delete_local_items([str(dst / "src")]) == []
    assert not (dst / "src").exists()


# ---- settings / dialogs / console / logcat / scrollback / small panels ----


def _process_events(qapp):
    """processEvents() that survives stale callbacks left by EARLIER tests.

    Pending ``QTimer.singleShot`` lambdas of already-deleted widgets from other
    test files (e.g. qtutil.AnimatedTabWidget) raise "wrapped C/C++ object ...
    has been deleted" when the loop next spins; with the default excepthook
    PyQt turns that into a fatal abort of the whole run.  Swallow only that
    error; anything else still fails the test."""
    import sys

    errors = []
    previous = sys.excepthook
    sys.excepthook = lambda etype, value, tb: errors.append(value)
    try:
        qapp.processEvents()
    finally:
        sys.excepthook = previous
    for error in errors:
        if not (isinstance(error, RuntimeError) and "has been deleted" in str(error)):
            raise error


def _pump(qapp, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _process_events(qapp)
        if predicate():
            return True
        time.sleep(0.01)
    _process_events(qapp)
    return predicate()


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    from turboadb.gui import settings

    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "_FILE", str(path))
    monkeypatch.setattr(settings, "_cache", None)
    return path


def test_settings_coerce_loaded_values_to_default_types(settings_file):
    import json
    import warnings
    from turboadb.gui import settings

    settings_file.write_text(json.dumps({
        "scrcpy_audio": "false",
        "term_font_size": 11.0,
        "scrcpy_max_size": "oops",
        "recent_network_hosts": ["a", 5, "b"],
        "custom_key": {"kept": True},
    }), encoding="utf-8")
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        data = settings.load()
    assert data["scrcpy_audio"] is False
    assert data["term_font_size"] == 11 and isinstance(data["term_font_size"], int)
    assert data["scrcpy_max_size"] == 0
    assert data["recent_network_hosts"] == ["a", "b"]
    assert data["custom_key"] == {"kept": True}
    assert "open_docs_first_run" not in settings.DEFAULTS


def test_settings_get_is_cached_until_the_file_changes(settings_file, monkeypatch):
    import builtins
    import json
    from turboadb.gui import settings

    settings.save({"theme": "light"})
    opened = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        if str(file) == str(settings_file):
            opened.append(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    for _ in range(5):
        assert settings.get("theme") == "light"
    assert opened == []
    settings.load()["recent_remote_hosts"].append("mutated")
    assert settings.get("recent_remote_hosts") == []
    # an external edit is picked up
    settings_file.write_text(json.dumps({"theme": "dark", "adb_path": "x"}), encoding="utf-8")
    assert settings.get("theme") == "dark"
    assert len(opened) == 1


def test_settings_replace_retries_windows_sharing_violation(settings_file, monkeypatch):
    from turboadb.gui import settings

    real_replace = os.replace
    attempts = []

    def flaky(src, dst):
        attempts.append(dst)
        if len(attempts) < 3:
            raise PermissionError("the file is in use by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(settings.os, "replace", flaky)
    monkeypatch.setattr(settings.time, "sleep", lambda _s: None)
    settings.set("theme", "light")
    assert len(attempts) == 3
    assert settings.get("theme") == "light"


def test_settings_set_later_coalesces_writes(settings_file, monkeypatch):
    from turboadb.gui import settings

    writes = []
    real_update = settings.update
    monkeypatch.setattr(
        settings, "update", lambda changes: (writes.append(dict(changes)), real_update(changes))
    )
    for size in (11, 12, 13):
        settings.set_later("term_font_size", size, delay=60)
    settings.flush_pending()
    assert writes == [{"term_font_size": 13}]
    assert settings.get("term_font_size") == 13


def test_settings_dialog_saves_only_changed_keys(qapp, settings_file):
    from turboadb.gui import settings
    from turboadb.gui.settings_dialog import SettingsDialog

    settings.save({"term_font_size": 10, "auto_update": True})
    dlg = SettingsDialog()
    try:
        settings.set("term_font_size", 14)  # e.g. Ctrl+wheel zoom while the dialog is open
        dlg.autoupd.setChecked(False)
        assert dlg.changed_settings() == {"auto_update": False}
        assert dlg.font_size.maximum() == settings.FONT_SIZE_MAX
        assert dlg.font_size.minimum() == settings.FONT_SIZE_MIN
        assert not hasattr(dlg, "docs")
        dlg.accept()
        assert settings.get("auto_update") is False
        assert settings.get("term_font_size") == 14
        assert dlg.result_settings()["term_font_size"] == 14
    finally:
        dlg.deleteLater()


def test_settings_dropdowns_are_pickers_not_text_fields(qapp, settings_file):
    """Every Settings dropdown chooses from its list. They used to take a text
    caret, so a click landed in the box and a half-typed value could stand as
    the setting (the font one accepted any text at all)."""
    from PyQt5.QtWidgets import QComboBox

    from turboadb.gui import settings
    from turboadb.gui.settings_dialog import SettingsDialog

    # values the lists do not offer, as an older or hand-edited file may hold
    settings.save({"scrcpy_bit_rate": "12M", "scrcpy_audio_bit_rate": "96K"})
    dlg = SettingsDialog()
    try:
        combos = dlg.findChildren(QComboBox)
        assert len(combos) >= 6
        for combo in combos:
            assert not combo.isEditable() and combo.lineEdit() is None
        # the saved values are still shown, and unchanged by opening the dialog
        assert dlg._combo_value(dlg.bit_rate) == "12M"
        assert dlg._combo_value(dlg.audio_bit_rate) == "96K"
        assert dlg.changed_settings() == {}
        # and choosing from the list still saves the list's value, not its label
        dlg._set_combo_value(dlg.bit_rate, "16M")
        assert dlg.changed_settings() == {"scrcpy_bit_rate": "16M"}
    finally:
        dlg.deleteLater()


def test_scrollback_writer_failure_keeps_history_saveable(qapp, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QPlainTextEdit
    from turboadb.gui import scrollback as sb_mod

    def unwritable(*_args, **_kwargs):
        raise OSError("profile not writable")

    monkeypatch.setattr(sb_mod.tempfile, "mkstemp", unwritable)
    sb = sb_mod.Scrollback(QPlainTextEdit())
    chunk = "x" * 40000 + "\n"
    for _ in range(4):  # > 64 KB: spills to the (failing) writer
        sb.archive(chunk)
    out = tmp_path / "saved.log"
    sb.save_to(str(out))
    assert out.read_text(encoding="utf-8") == chunk * 4
    sb.close()


def test_scrollback_save_fails_loudly_when_writer_is_stuck(qapp, tmp_path, monkeypatch):
    import queue
    from PyQt5.QtWidgets import QPlainTextEdit
    from turboadb.gui import scrollback as sb_mod

    sb = sb_mod.Scrollback(QPlainTextEdit())
    monkeypatch.setattr(sb, "_FLUSH_TIMEOUT_S", 0.05)
    sb._writer = object()  # a writer that never drains its queue
    sb._queue = queue.Queue()
    with pytest.raises(sb_mod.ScrollbackSaveError):
        sb.save_to(str(tmp_path / "x.log"))
    assert not (tmp_path / "x.log").exists()
    sb._writer = None
    sb._queue = None
    sb.close()


def _plain_console(send_fn=None):
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole(send_fn=send_fn)
    con.set_emulate_prompt(False)
    return con


def _render(con, data):
    con.feed(data)
    while con._inq:
        con._drain_tick()


def test_console_extended_sgr_colours_are_single_attributes(qapp):
    from PyQt5.QtGui import QColor, QFont
    from turboadb.gui import console as console_mod

    con = _plain_console()
    try:
        con._sgr_params("38;5;1")  # the trailing 1 used to be applied as "bold"
        assert con._fmt.fontWeight() != QFont.Bold
        assert con._fmt.foreground().color().name() == QColor(console_mod._ANSI[31]).name()
        con._sgr_params("0")
        default_fg = con._fmt.foreground().color().name()
        con._sgr_params("48;2;10;20;30")  # 30 used to turn the text black
        assert con._fmt.background().color().getRgb()[:3] == (10, 20, 30)
        assert con._fmt.foreground().color().name() == default_fg
        con._sgr_params("38;5;196")
        assert con._fmt.foreground().color().getRgb()[:3] == (255, 0, 0)
        con._sgr_params("38:2::1:2:3")
        assert con._fmt.foreground().color().getRgb()[:3] == (1, 2, 3)
        con._sgr_params("38;5;232")
        assert con._fmt.foreground().color().getRgb()[:3] == (8, 8, 8)
    finally:
        con.close_archive()
        con.close()


def test_console_escape_parsing_edge_cases(qapp):
    con = _plain_console()
    try:
        _render(con, b"\x1b(Bplain\n")  # charset select must not leave a stray "B"
        assert con.toPlainText() == "plain\n"
        con.clear()
        _render(con, b"hello world" + b"\b" * 5 + b"\x1b[1K")  # EL1: erase to cursor
        assert con.toPlainText() == " " * 7 + "orld"
        con.clear()
        _render(con, b"one\n\b\bX")  # backspace stops at column 0
        assert con.toPlainText() == "one\nX"
    finally:
        con.close_archive()
        con.close()


def test_console_archive_survives_escape_split_across_chunks(qapp):
    con = _plain_console()
    try:
        con.feed(b"\x1b[3")
        con.feed(b"1mred\x1b[0m\n\x1b]0;tit")
        con.feed(b"le\x07done\n")
        assert con._sb.full_text() == "red\ndone\n"
    finally:
        con.close_archive()
        con.close()


def test_console_local_prompt_style_ignores_mid_line_chunks(qapp):
    con = _plain_console()
    try:
        con._feed_at_line_start = False
        assert "\x1b[" not in con._style_local_prompts_stream("C:\\data> not a prompt\n")
        assert con._feed_at_line_start
        assert "\x1b[" in con._style_local_prompts_stream("C:\\Users> ")
    finally:
        con.close_archive()
        con.close()


def test_console_pending_echo_does_not_swallow_later_output(qapp):
    con = _plain_console(send_fn=lambda _data: None)
    try:
        con._pending_echo = "dir"
        con._pending_echo_at = time.monotonic()
        _render(con, b"Volume in drive C\n")  # this shell did not echo the command
        assert con._pending_echo is None
        _render(con, b"dir listing\n")
        assert "dir listing" in con.toPlainText()
        con._pending_echo = "abc"
        con._pending_echo_at = time.monotonic() - 60  # stale
        _render(con, b"abc output\n")
        assert "abc output" in con.toPlainText()
    finally:
        con.close_archive()
        con.close()


def test_console_cd_tracking(qapp):
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole()
    try:
        con._cwd = "/sdcard"
        con._apply_cd('cd "My Files"; ls')
        assert con._cwd == "/sdcard/My Files"
        con._apply_cd("cd /data && ls")
        assert con._cwd == "/data"
        con._apply_cd("cd -")
        assert con._cwd == "/sdcard/My Files"
        con._apply_cd("cd -- /system")
        assert con._cwd == "/system"
        con._apply_cd("cd /nope")
        assert con._cwd == "/nope"
        con.feed(b"/system/bin/sh: cd: /nope: No such file or directory\n")
        assert con._cwd == "/system"
    finally:
        con.close_archive()
        con.close()


def test_console_scroll_keys_and_reliable_interrupt(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent

    calls = []
    con = _plain_console(send_fn=lambda data: calls.append(data))
    try:
        con.set_interrupt_fn(lambda: calls.append("interrupt"))
        con.resize(320, 120)
        con.show()
        con._echo("line\n" * 400)
        _process_events(qapp)
        bar = con.verticalScrollBar()
        bar.setValue(bar.maximum())
        before = bar.value()
        con._line = "typed"
        con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_PageUp, Qt.NoModifier))
        if before > 0:
            assert bar.value() < before
        assert con._line == "typed" and calls == []
        con._send_interrupt()  # the context menu's Ctrl+C uses the same path
        assert calls == ["interrupt"]
    finally:
        con.close_archive()
        con.close()


def test_console_zoom_is_cheap_and_debounced(qapp, monkeypatch):
    from PyQt5.QtGui import QFont, QTextCursor, QTextFormat
    import turboadb.gui.console as console_mod

    later = []
    monkeypatch.setattr(
        console_mod.settings_mod, "set_later", lambda key, value, **_kw: later.append((key, value))
    )
    monkeypatch.setattr(
        console_mod.settings_mod, "save",
        lambda *_a: pytest.fail("zoom must not write settings synchronously"),
    )
    con = console_mod.AnsiConsole()
    try:
        _render(con, b"\x1b[1;31mbold red\x1b[0m\n")
        size = con.font_size()
        con.bump_font(1)
        con.bump_font(1)
        assert later == [("term_font_size", size + 1), ("term_font_size", size + 2)]
        cursor = QTextCursor(con.document())
        cursor.setPosition(2)
        assert not cursor.charFormat().hasProperty(QTextFormat.FontPointSize)
        assert cursor.charFormat().fontWeight() == QFont.Bold
        assert con.document().defaultFont().pointSize() == size + 2
    finally:
        con.close_archive()
        con.close()


class _FakeStdout:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def read1(self, _n):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class _FakeLogcatProc:
    def __init__(self, chunks=()):
        self.stdout = _FakeStdout(chunks)
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_logcat_stop_during_popen_kills_the_new_process(qapp):
    from turboadb.gui.logcat_view import _LogcatThread

    proc = _FakeLogcatProc([b"never read\n"])
    holder = {}

    class Handler:
        def popen(self, _args):
            holder["thread"].stop()  # Stop pressed while adb was still starting
            return proc

    thread = _LogcatThread(Handler(), ["logcat"])
    holder["thread"] = thread
    thread.run()  # synchronously
    assert proc.killed and proc.stdout.closed
    assert thread.take_lines() == []


def test_logcat_reader_joins_partial_lines(qapp):
    from turboadb.gui.logcat_view import _LogcatThread

    proc = _FakeLogcatProc([b"01 I a\n02 W b", b"cd\n03", b" E c\r\n", b"tail"])

    class Handler:
        def popen(self, _args):
            return proc

    thread = _LogcatThread(Handler(), ["logcat"])
    thread.run()
    assert thread.take_lines() == ["01 I a", "02 W bcd", "03 E c", "tail"]
    assert thread.take_lines() == []


def test_logcat_panel_refilter_restores_lines_and_counts_skips(qapp):
    from turboadb.gui.logcat_view import LogcatPanel

    panel = LogcatPanel(None)
    try:
        panel._on_batch(["keep 1", "drop 2", "keep 3"])
        panel._render_pending()
        panel.filt.setText("keep")
        panel._refilter_view()
        assert panel.view.toPlainText().splitlines() == ["keep 1", "keep 3"]
        panel.filt.setText("kee(")  # invalid while typing: the previous filter stays
        assert panel._filter_re.pattern == "keep"
        panel.filt.setText("")
        panel._refilter_view()
        assert panel.view.toPlainText().splitlines() == ["keep 1", "drop 2", "keep 3"]

        panel._PENDING_MAX = 2
        panel._paused = True
        panel._on_batch(["a", "b", "c"])
        panel._on_batch(["d", "e"])
        assert panel._skipped == 3
        panel._paused = False
        panel._render_pending()
        assert panel.view.toPlainText().splitlines()[-3:] == [
            "… (3 lines skipped on screen — saved log has all)", "d", "e",
        ]
    finally:
        panel.close_panel()


def test_logcat_panel_forgets_finished_thread_and_close_never_waits(qapp, monkeypatch):
    from PyQt5.QtCore import QThread
    from turboadb.gui.logcat_view import LogcatPanel

    class Handler:
        def __init__(self):
            self.procs = []

        def popen(self, _args):
            proc = _FakeLogcatProc([b"--------- beginning of main\n"])
            self.procs.append(proc)
            return proc

        def logcat_clear(self):
            pass

    handler = Handler()
    panel = LogcatPanel(handler)
    panel.start()
    assert panel.thread.wait(5000)
    assert _pump(qapp, lambda: panel.thread is None)
    assert "--------- beginning of main" in panel._recent
    panel.toggle()  # a new start must not touch the deleted worker
    assert panel.thread is not None
    worker = panel.thread
    assert _pump(qapp, lambda: len(handler.procs) == 2)

    def no_wait(self, *args, **kwargs):
        raise AssertionError("close_panel must not block on QThread.wait()")

    monkeypatch.setattr(QThread, "wait", no_wait)
    panel.close_panel()
    monkeypatch.undo()
    assert panel.thread is None
    assert worker.wait(5000)


def test_apps_panel_ignores_stale_package_lists(qapp):
    from turboadb.gui.apps_panel import AppsPanel

    panel = AppsPanel(None)
    try:
        panel._list_generation = 2
        panel._on_packages(1, ["com.old"])  # a slower, older refresh
        assert panel._all == []
        panel._on_packages(2, ["com.new"])
        assert panel._all == ["com.new"]
    finally:
        panel.close_panel()


def test_run_job_delivers_and_close_jobs_detaches(qapp):
    from turboadb.gui.qtutil import close_jobs, run_job

    jobs, results = [], []
    first = run_job(jobs, lambda: 42, results.append)
    assert first.wait(5000)
    assert _pump(qapp, lambda: results == [42] and jobs == [])

    release = threading.Event()
    late = run_job(jobs, lambda: release.wait(5) and "late", results.append, results.append)
    close_jobs(jobs)
    assert jobs == []
    release.set()
    assert late.wait(5000)
    _pump(qapp, lambda: False, timeout=0.2)
    assert results == [42]


def test_session_store_renames_instead_of_copying(tmp_path, monkeypatch):
    from turboadb.gui import sessions

    monkeypatch.setattr(sessions, "_FILE", str(tmp_path / "sessions.json"))
    store = sessions.SessionStore()
    store.save({"name": "bench", "type": "network", "host": "10.0.0.7"})
    store.save({"name": "other", "type": "usb", "serial": "abc"})
    store.save({"name": "lab", "type": "network", "host": "10.0.0.8", "previous_name": "bench"})
    assert store.names() == ["lab", "other"]
    reloaded = sessions.SessionStore().get("lab")
    assert reloaded["host"] == "10.0.0.8" and "previous_name" not in reloaded


def test_session_dialog_validates_before_closing(qapp, tmp_path, monkeypatch):
    from turboadb.gui import session_dialog, sessions

    monkeypatch.setattr(sessions, "_FILE", str(tmp_path / "sessions.json"))
    shown = []
    monkeypatch.setattr(
        session_dialog.QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2]))
    )
    dlg = session_dialog.SessionDialog()
    try:
        dlg.name.setText("wifi unit")
        dlg.mode.setCurrentIndex(1)  # network target without a host
        dlg.accept()
        assert dlg.result() != dlg.Accepted and shown
    finally:
        dlg.deleteLater()

    dlg = session_dialog.SessionDialog(existing={"name": "old", "type": "usb", "serial": "x"})
    try:
        dlg.name.setText("new")
        assert dlg.result_session()["previous_name"] == "old"
        dlg.accept()
        assert dlg.result() == dlg.Accepted
    finally:
        dlg.deleteLater()


def test_connect_dialog_session_is_side_effect_free(qapp, monkeypatch):
    from turboadb import scrcpy
    from turboadb.gui import connect_dialog

    recent = []
    monkeypatch.setattr(
        connect_dialog.settings_mod, "add_recent", lambda key, value: recent.append((key, value))
    )
    monkeypatch.setattr(connect_dialog.ConnectDialog, "_scan_usb", lambda self: None)
    dlg = connect_dialog.ConnectDialog()
    try:
        dlg.mode.setCurrentIndex(1)
        dlg.net_host.setCurrentText("10.1.2.3:5556")
        assert dlg.session()["port"] == 5556
        assert recent == []
        dlg._accept()
        assert recent == [("recent_network_hosts", "10.1.2.3")]
        assert dlg.session()["host"] == "10.1.2.3"
        assert recent == [("recent_network_hosts", "10.1.2.3")]
        assert dlg._closing  # done() released the scan/serve threads
    finally:
        dlg.deleteLater()
    assert connect_dialog.TUNNEL_PORT_FIREWALL_RANGE == scrcpy.TUNNEL_PORT_FIREWALL_RANGE


def test_local_shell_env_dedupes_and_close_kills_tree_async(monkeypatch):
    import turboadb.tools as tools
    from turboadb.gui import local_terminal as lt

    captured = {}

    class FakeProc:
        pid = 4242
        stdin = None
        stdout = None

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr(lt.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(lt, "find_adb", lambda *_a, **_k: None)
    monkeypatch.setattr(tools, "find_scrcpy", lambda *_a, **_k: None)
    monkeypatch.setattr(lt, "_get_registry_env", lambda: {"OneDrive": "reg", "Brand_New": "v"})
    monkeypatch.setenv("ONEDRIVE", "env")
    killed = threading.Event()
    monkeypatch.setattr(lt, "_kill_process_tree", lambda proc, wait_s=2.0: killed.set())

    sess = lt.LocalShellSession("cmd", adb_path="__missing__")
    env = captured["env"]
    upper = [key.upper() for key in env]
    assert len(upper) == len(set(upper))
    assert env["ONEDRIVE"] == "env" and env["Brand_New"] == "v"
    sess.close()  # returns immediately; the tree kill runs on a thread
    assert not sess.running
    assert killed.wait(5)


def test_device_command_callback_errors_do_not_escape(qapp):
    from turboadb.gui.device_commands import DeviceCommandDispatcher

    def buggy(_result):
        raise ValueError("handler bug")

    DeviceCommandDispatcher._deliver(buggy, 1, None)  # must not raise into Qt


def test_controls_panel_result_messages_and_single_settings_button(qapp):
    from PyQt5.QtWidgets import QAbstractButton
    from turboadb.gui.controls_panel import ControlsPanel
    from turboadb.results import CommandResult, OperationResult

    msg = ControlsPanel._result_msg
    failed = OperationResult(False, "wifi", error=RuntimeError("denied"))
    assert msg("Wi-Fi On", failed) == "[ERROR] Wi-Fi On: denied"
    assert msg("Wi-Fi On", OperationResult(True, "wifi", value=True)) == "[OK] Wi-Fi On"
    assert msg("x", CommandResult("c", 1, "", "boom", 0.0)) == "[ERROR] x: boom"
    assert msg("x", False).startswith("[WARNING]")
    panel = ControlsPanel(None)
    try:
        texts = [button.text() for button in panel.findChildren(QAbstractButton)]
        assert texts.count("Settings") == 1
        assert not hasattr(panel, "_threads")
    finally:
        panel.close_panel()


def test_write_file_async_reports_failure_and_success(qapp, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox, QWidget
    from turboadb.gui import fileutil

    shown, toasts, saved = [], [], []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    monkeypatch.setattr(fileutil, "saved_toast", lambda parent, path, what="file": toasts.append(path))
    owner = QWidget()

    def full_disk(_path):
        raise OSError("disk full")

    fileutil.write_file_async(owner, str(tmp_path / "a.log"), full_disk, what="log")
    assert _pump(qapp, lambda: bool(shown))
    assert "disk full" in shown[0]
    target = str(tmp_path / "b.log")
    fileutil.write_file_async(
        owner, target, lambda p: fileutil.write_text_file(p, "hi"), what="log",
        on_saved=saved.append,
    )
    assert _pump(qapp, lambda: bool(saved))
    assert toasts == [target] and saved == [target]
    owner.deleteLater()


def test_log_panel_save_writes_what_is_shown_off_thread(qapp, tmp_path, monkeypatch):
    from turboadb.gui import fileutil
    from turboadb.gui.log_panel import LogPanel

    target = tmp_path / "log.txt"
    monkeypatch.setattr(fileutil, "ask_save_path", lambda *a, **k: str(target))
    monkeypatch.setattr(fileutil, "saved_toast", lambda *a, **k: None)
    panel = LogPanel()
    try:
        panel.append("[OK] connected")
        panel.append("[DEBUG] $ adb shell getprop")
        panel._save()
        assert _pump(qapp, lambda: "Log saved to" in panel.view.toPlainText())
        text = target.read_text(encoding="utf-8")
        assert "OK   connected" in text and "getprop" not in text
    finally:
        panel.close()


def test_error_popup_is_single_and_non_modal(qapp, monkeypatch):
    # A real QMessageBox.show() crashes Qt's offscreen platform on Windows, so
    # the popup policy is exercised with a stand-in box.
    from PyQt5.QtCore import Qt
    from turboadb.gui import app as app_mod

    class _Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in list(self.slots):
                slot(*args)

    class FakeBox:
        Warning, Ok = 2, 0x400
        created = []

        def __init__(self, icon, title, text, buttons, parent):
            self.text, self.info, self.visible, self.modality = text, "", False, None
            self.finished = _Signal()
            FakeBox.created.append(self)

        def setAttribute(self, *_args):
            pass

        def setWindowModality(self, modality):
            self.modality = modality

        def show(self):
            self.visible = True

        def isVisible(self):
            return self.visible

        def setInformativeText(self, text):
            self.info = text

        def close(self):
            self.visible = False
            self.finished.emit(0)

    monkeypatch.setattr(app_mod, "QMessageBox", FakeBox)
    monkeypatch.setattr(app_mod, "_window", None)
    monkeypatch.setattr(app_mod, "_error_box", None)
    app_mod._show_error_popup(ValueError, ValueError("first"))
    app_mod._show_error_popup(RuntimeError, RuntimeError("second"))
    app_mod._show_error_popup(RuntimeError, RuntimeError("third"))
    assert len(FakeBox.created) == 1  # no stacking
    box = FakeBox.created[0]
    assert box.visible and box.modality == Qt.NonModal
    assert "ValueError: first" in box.text and box.info.startswith("2 more")
    box.close()
    assert app_mod._error_box is None
    app_mod._show_error_popup(OSError, OSError("later"))
    assert len(FakeBox.created) == 2


def test_report_png_render_is_split_from_the_disk_write(qapp, tmp_path):
    from turboadb.gui import report_dialog

    image = report_dialog.render_report_image("Device info", "Model  X\nAndroid  14")
    assert not image.isNull()
    out = tmp_path / "report.png"
    report_dialog.render_report_png(str(out), "Device info", "Model  X")
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_reader_thread_decodes_utf8_split_across_reads(qapp):
    from turboadb.gui.terminal import ReaderThread

    chunks = [b"\xe2\x82", b"\xac!", None]
    reader = ReaderThread(lambda: chunks.pop(0), decode=True)
    got = []
    reader.data.connect(got.append)
    reader.run()  # synchronously: same-thread signals are delivered directly
    assert "".join(got) == "€!"


# ---- camera_widget / remote_webcam / ffmpeg_tools ----

import io  # noqa: E402
import subprocess  # noqa: E402
import types  # noqa: E402
import zipfile  # noqa: E402


def _rw():
    from turboadb.gui import remote_webcam

    return remote_webcam


def test_remote_stream_firewall_rule_is_scoped_to_this_pc(monkeypatch):
    rw = _rw()
    scripts = []

    def fake_run_ps(host, login, password, script, winrm_port=5985):
        scripts.append(script)
        return 0, "ALLOW:10.1.2.3\nPID:4242\nRC:0\n", ""

    monkeypatch.setattr(rw, "_run_ps", fake_run_ps)
    pid = rw.start_remote_stream(
        "remote-host", "user", "pw-secret", "Cam", r"C:\ff\ffmpeg.exe", client_ip="10.1.2.3"
    )
    assert pid == 4242
    script = scripts[0]
    assert "$me = '10.1.2.3'" in script
    add_lines = [ln for ln in script.splitlines() if "firewall add rule" in ln]
    assert add_lines and all('"remoteip=$allow"' in ln for ln in add_lines)
    assert "localport=28100" in add_lines[0]
    assert "tcp://0.0.0.0:28100?listen=1" in script


def test_remote_stream_client_ip_defaults_to_route_address(monkeypatch):
    rw = _rw()
    scripts = []
    monkeypatch.setattr(rw, "local_address_for", lambda host, port=5985: "192.0.2.55")
    monkeypatch.setattr(
        rw, "_run_ps", lambda h, l, p, s, w=5985: scripts.append(s) or (0, "PID:7\nRC:0\n", "")
    )
    rw.start_remote_stream("remote-host", "u", "p", "Cam", "ffmpeg.exe")
    assert "$me = '192.0.2.55'" in scripts[0]


def test_stop_remote_stream_deletes_rule_and_matches_exact_listen_url(monkeypatch):
    rw = _rw()
    scripts = []
    monkeypatch.setattr(rw, "_run_ps", lambda h, l, p, s, w=5985: scripts.append(s) or (0, "OK", ""))
    rw.stop_remote_stream("remote-host", "u", "p", 4242)
    script = scripts[0]
    assert 'firewall delete rule name="TurboADB Webcam 28100"' in script
    assert "Contains('tcp://0.0.0.0:28100?listen=1')" in script
    assert "*28100*" not in script
    assert "ProcessName -eq 'ffmpeg'" in script  # a reused PID isn't killed blindly


def test_kill_matcher_uses_exact_listen_url_only():
    rw = _rw()
    ps = rw._kill_by_port_ps(28100)
    assert "Contains('tcp://0.0.0.0:28100?listen=1')" in ps
    assert "-like" not in ps


def test_failed_remote_launch_removes_firewall_rule(monkeypatch, caplog):
    rw = _rw()
    scripts = []

    def fake_run_ps(host, login, password, script, winrm_port=5985):
        scripts.append(script)
        return 0, "nothing useful", ""

    monkeypatch.setattr(rw, "_run_ps", fake_run_ps)
    with pytest.raises(RuntimeError):
        rw.start_remote_stream("h", "u", "pw-secret", "Cam", "ffmpeg.exe", client_ip="10.0.0.9")
    assert len(scripts) == 2 and "firewall delete rule" in scripts[1]
    # the WMI-refused path closes the port inside the launch script itself
    assert "if ($r.ReturnValue -ne 0)" in scripts[0]

    def boom(*a, **k):
        raise RuntimeError("transport broke")

    monkeypatch.setattr(rw, "_run_ps", boom)
    with pytest.raises(RuntimeError):
        rw.start_remote_stream("h", "u", "pw-secret", "Cam", "ffmpeg.exe", client_ip="10.0.0.9")
    assert "pw-secret" not in caplog.text


def test_share_connection_reuses_and_keeps_existing_mapping(monkeypatch):
    rw = _rw()
    events = []
    monkeypatch.setattr(rw, "_existing_connection", lambda share: True)
    monkeypatch.setattr(rw, "_wnet_connect", lambda *a: events.append("connect") or 0)
    monkeypatch.setattr(rw, "_wnet_disconnect", lambda share: events.append("disconnect") or 0)
    with rw._share_connection(r"\\host\C$", "u", "p"):
        events.append("body")
    assert events == ["body"]


def test_share_connection_removes_only_its_own_connection(monkeypatch):
    rw = _rw()
    events = []
    code = {"value": 0}
    monkeypatch.setattr(rw, "_existing_connection", lambda share: False)
    monkeypatch.setattr(rw, "_wnet_connect", lambda *a: events.append("connect") or code["value"])
    monkeypatch.setattr(rw, "_wnet_disconnect", lambda share: events.append("disconnect") or 0)

    with pytest.raises(OSError):
        with rw._share_connection(r"\\host\C$", "u", "p"):
            events.append("body")
            raise OSError("copy failed")
    assert events == ["connect", "body", "disconnect"]

    events.clear()
    code["value"] = 1219  # credential conflict: someone else's session, leave it
    with rw._share_connection(r"\\host\C$", "u", "p"):
        events.append("body")
    assert events == ["connect", "body"]

    events.clear()
    code["value"] = 5
    with pytest.raises(RuntimeError):
        with rw._share_connection(r"\\host\C$", "u", "p"):
            events.append("body")
    assert events == ["connect"]


def test_existing_connection_parses_net_use_with_timeout(monkeypatch):
    rw = _rw()
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        out = b"OK           \\\\HOST\\C$         Microsoft Windows Network\r\n"
        return types.SimpleNamespace(stdout=out, returncode=0)

    monkeypatch.setattr(
        rw, "subprocess", types.SimpleNamespace(run=fake_run, SubprocessError=subprocess.SubprocessError)
    )
    assert rw._existing_connection(r"\\host\c$") is True
    assert rw._existing_connection(r"\\host\D$") is False
    assert seen.get("timeout")


def _make_ffmpeg_zip(path, payload=b"MZ" + b"x" * 200000):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe", payload)
    return payload


def test_ffmpeg_extract_failure_leaves_no_partial_exe(tmp_path, monkeypatch):
    from turboadb.gui import ffmpeg_tools

    zip_path = tmp_path / "dl.zip"
    _make_ffmpeg_zip(zip_path)
    out = tmp_path / "ffmpeg.exe"

    def broken_copy(src, dst, length=0):
        dst.write(src.read(1000))
        raise OSError("disk full")

    monkeypatch.setattr(ffmpeg_tools.shutil, "copyfileobj", broken_copy)
    with pytest.raises(OSError):
        ffmpeg_tools._install_from_zip(str(zip_path), str(out))
    assert not out.exists()
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".part")]


def test_ensure_local_ffmpeg_atomic_download(tmp_path, monkeypatch):
    from turboadb.gui import ffmpeg_tools

    cache = tmp_path / "ffmpeg"
    zbuf = tmp_path / "src.zip"
    payload = _make_ffmpeg_zip(zbuf)
    data = zbuf.read_bytes()

    class Resp(io.BytesIO):
        headers = {"Content-Length": str(len(data))}

    monkeypatch.setattr(ffmpeg_tools, "_CACHE", str(cache))
    monkeypatch.setattr(ffmpeg_tools, "_auto_download_supported", lambda: True)
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(ffmpeg_tools.urllib.request, "urlopen", lambda req, timeout=0: Resp(data))

    got = ffmpeg_tools.ensure_local_ffmpeg()
    assert got == str(cache / "ffmpeg.exe")
    assert (cache / "ffmpeg.exe").read_bytes() == payload
    # temp zip / part files gone; the checksum record stays next to the binary
    assert sorted(os.listdir(cache)) == ["ffmpeg.exe", "ffmpeg.exe.sha256"]

    def offline(req, timeout=0):
        raise OSError("offline")

    (cache / "ffmpeg.exe").unlink()
    monkeypatch.setattr(ffmpeg_tools.urllib.request, "urlopen", offline)
    with pytest.raises(RuntimeError):
        ffmpeg_tools.ensure_local_ffmpeg()
    assert os.listdir(cache) == []


def test_record_writer_never_blocks_the_reader():
    from turboadb.gui.camera_widget import _RecordWriter

    class BlockingFh:
        def __init__(self):
            self.gate = threading.Event()
            self.data = []
            self.closed = False

        def write(self, b):
            self.gate.wait(5)
            self.data.append(b)

        def close(self):
            self.closed = True

    fh = BlockingFh()
    writer = _RecordWriter(fh, max_frames=4)
    writer.start()
    t0 = time.monotonic()
    for i in range(50):
        writer.put(b"frame%d" % i)
    assert time.monotonic() - t0 < 1.0
    assert writer.dropped >= 40
    fh.gate.set()
    writer.close()
    writer.join(5)
    assert not writer.is_alive() and fh.closed and len(fh.data) >= 4


class _FakeEncoder:
    def __init__(self, polls):
        self.polls = polls
        self.killed = False
        self.stdin = io.BytesIO()

    def wait(self, timeout=None):
        if self.polls > 0:
            self.polls -= 1
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return 0

    def kill(self):
        self.killed = True


def test_record_finalize_waits_for_slow_encoder_without_killing(tmp_path):
    from turboadb.gui.camera_widget import _FfmpegFinalizeThread

    path = tmp_path / "rec.mp4"
    path.write_bytes(b"\0" * 1024)
    proc = _FakeEncoder(polls=30)  # would have exceeded the old 10 s hard cap
    fin = _FfmpegFinalizeThread(proc, None, str(path))
    results = []
    fin.done.connect(lambda p, ok: results.append((p, ok)))
    fin.run()  # synchronously: exercises the wait loop
    assert not proc.killed and proc.stdin.closed
    assert results == [(str(path), True)]

    stuck = _FakeEncoder(polls=10 ** 6)
    fin = _FfmpegFinalizeThread(stuck, None, str(path))
    fin.BASE_S = fin.SECONDS_PER_MB = 0.0
    results.clear()
    fin.done.connect(lambda p, ok: results.append((p, ok)))
    fin.run()
    assert stuck.killed and results == [(str(path), False)]


@pytest.fixture
def camera_panel(qapp, monkeypatch):
    from turboadb.gui import camera_widget

    monkeypatch.setattr(camera_widget.CameraPanel, "_auto_scan", lambda self: None)
    panel = camera_widget.CameraPanel()
    try:
        yield panel
    finally:
        from PyQt5.QtCore import QCoreApplication, QEvent

        panel.close_panel()
        panel.close()
        # Deliver queued signals and delete the panel now, while its test's
        # monkeypatches are still in place; leaving it for a later test's event
        # loop crashed that test natively.
        qapp.processEvents()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()


class _FakeSock:
    closed = False

    def close(self):
        self.closed = True


def _record_remote_stops(monkeypatch):
    rw = _rw()
    stops = []
    hit = threading.Event()

    def fake_stop(host, login, password, pid, **kw):
        stops.append((host, pid, kw.get("stream_port")))
        hit.set()

    monkeypatch.setattr(rw, "stop_remote_stream", fake_stop)
    return stops, hit


def _starter(camera_widget):
    return camera_widget._RemoteStart("rhost", "user", "pw", "Cam", "ffmpeg.exe", 640, 480, 25, 28100)


def test_camera_timer_runs_only_while_streaming(camera_panel):
    assert not camera_panel._timer.isActive()
    camera_panel._after_start("Cam")
    assert camera_panel._timer.isActive()
    camera_panel._stop_stream()
    assert not camera_panel._timer.isActive()


def test_stale_stream_generation_callbacks_are_ignored(camera_panel, monkeypatch):
    from turboadb.gui import camera_widget

    shown = []
    monkeypatch.setattr(camera_widget.QMessageBox, "information", lambda *a, **k: shown.append(a))
    calls = []
    gen = camera_panel._stream_gen
    assert camera_panel._run_if_current(gen, lambda: calls.append("current"))
    camera_panel._stop_stream()  # a newer stream generation
    assert not camera_panel._run_if_current(gen, lambda: calls.append("stale"))
    assert calls == ["current"]
    camera_panel._remote_no_video("Cam", "diag", gen)  # probe result from the old stream
    assert shown == []


def test_close_during_remote_start_discards_delivered_stream(camera_panel, monkeypatch):
    from turboadb.gui import camera_widget

    stops, hit = _record_remote_stops(monkeypatch)
    starter = _starter(camera_widget)
    sock = _FakeSock()
    starter._result = (4321, sock)  # emitted by the worker, not yet claimed by the UI
    camera_panel._starter = starter
    camera_panel.close_panel()
    assert hit.wait(5), "remote stream was not stopped"
    assert stops == [("rhost", 4321, 28100)]
    assert sock.closed and camera_panel.reader is None
    assert starter.cancelled() and starter.take_result() is None


def test_late_remote_start_after_close_stops_remote_not_reader(camera_panel, monkeypatch):
    from turboadb.gui import camera_widget

    stops, hit = _record_remote_stops(monkeypatch)
    starter = _starter(camera_widget)
    camera_panel._starter = starter
    gen = camera_panel._stream_gen
    camera_panel.close_panel()
    assert starter.cancelled()

    # worker side: finishing after the cancel tears the stream down itself
    emitted = []
    starter.ok.connect(lambda pid, s: emitted.append(pid))
    sock = _FakeSock()
    assert starter._deliver(99, sock) is False
    assert emitted == [] and sock.closed and stops == [("rhost", 99, 28100)]

    # UI side: a result that still reaches the slot after close is discarded on a worker
    hit.clear()
    late = _starter(camera_widget)
    late_sock = _FakeSock()
    late._result = (100, late_sock)
    camera_panel._remote_started(late, "Cam", gen)
    assert hit.wait(5)
    assert ("rhost", 100, 28100) in stops
    assert late_sock.closed and camera_panel.reader is None and camera_panel._sock is None


# ---- second review round: shared helpers, buffers and device-path safety ----


def test_fileutil_alive_treats_none_as_alive(qapp, tmp_path, monkeypatch):
    from PyQt5 import sip
    from PyQt5.QtWidgets import QWidget
    from turboadb.gui import fileutil

    # A second, widget-only definition used to shadow this one and raise
    # AttributeError for the unparented case write_file_async relies on.
    assert fileutil._alive(None) is True
    widget = QWidget()
    assert fileutil._alive(widget) is True
    sip.delete(widget)
    assert fileutil._alive(widget) is False

    saved = []
    monkeypatch.setattr(fileutil, "saved_toast", lambda parent, path, what="file": saved.append(path))
    target = str(tmp_path / "unparented.log")
    fileutil.write_file_async(None, target, lambda p: fileutil.write_text_file(p, "hi"), what="log")
    assert _pump(qapp, lambda: saved == [target])


def test_file_browser_remote_delete_goes_through_the_engine(qapp, monkeypatch):
    from turboadb.gui import file_browser as fb

    removed = []

    class Handler:
        def remove(self, paths, *, recursive=False, safe=None):
            removed.append((sorted(paths), recursive, safe))
            return list(paths)

    jobs = []
    monkeypatch.setattr(
        fb, "run_job",
        lambda tracked, fn, on_done=None, on_fail=None: jobs.append((fn, on_done, on_fail)),
    )
    monkeypatch.setattr(fb.QMessageBox, "question", staticmethod(lambda *a, **k: fb.QMessageBox.Yes))
    browser = fb.FileBrowser(Handler())
    try:
        browser.remote_cwd = "/sdcard"
        browser._populate(browser.remote_table, [
            ("keep.txt", 1, "1 B", "File", "", "-rw-rw-rw-", "", False),
            ("logs", 0, "<DIR>", "Folder", "", "drwx------", "", True),
        ], parent_row=True)
        browser.remote_table.selectAll()  # the '..' row is never a target
        batches = []
        monkeypatch.setattr(browser, "_run_shell_batch",
                            lambda commands, timeout=30, **_kw: batches.append(commands))
        jobs.clear()
        browser._remote_delete()
        assert batches == []  # no hand-built, unbounded `rm -rf` any more
        work, _done, _fail = jobs[-1]
        work()
        assert removed == [(["/sdcard/keep.txt", "/sdcard/logs"], True, False)]
    finally:
        browser.close_panel()


def test_file_browser_new_item_names_cannot_escape_the_shown_folder(qapp, tmp_path, monkeypatch):
    from turboadb.gui import file_browser as fb

    warned = []
    monkeypatch.setattr(fb.QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    browser = fb.FileBrowser(None)
    try:
        ran = []
        created = []
        monkeypatch.setattr(browser, "_run_shell_batch",
                            lambda commands, timeout=30, **_kw: ran.extend(commands))
        monkeypatch.setattr(browser, "_local_job",
                            lambda label, fn, error_title="": created.append(label))
        browser.remote_cwd = "/sdcard"
        browser._remote_loading_path = None
        browser.local_cwd = str(tmp_path)
        browser._local_loading_path = None
        for typed in ("/etc/passwd", "../..", ".", "sub/name"):
            monkeypatch.setattr(fb.QInputDialog, "getText",
                                staticmethod(lambda *a, _text=typed, **k: (_text, True)))
            browser._remote_mkdir()
            browser._remote_newfile()
            browser._local_mkdir()
            browser._local_newfile()
        assert ran == [] and created == []
        assert len(warned) == 16

        # Plain names still work, on both sides.
        monkeypatch.setattr(fb.QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("notes.txt", True)))
        browser._remote_newfile()
        assert shlex.split(ran[-1][1])[1] == "/sdcard/notes.txt"
        browser._local_newfile()
        assert created == ["create notes.txt"]

        assert browser._valid_new_name("notes.txt", "t", local=True)
        assert not browser._valid_new_name("..", "t", local=False)
        if os.name == "nt":  # a backslash or a drive letter escapes just as well
            assert not browser._valid_new_name("sub" + os.sep + "x", "t", local=True)
            assert not browser._valid_new_name("C:x", "t", local=True)
    finally:
        browser.close_panel()


def test_file_browser_imports_only_the_remotefs_helpers_it_uses():
    from turboadb.gui import file_browser as fb

    for gone in ("_ANSI_CSI_RE", "_as_text", "_chunks", "_dir_arg", "_EDIT_STAT_RE",
                 "_link_dirs_cmd", "_Listing", "_LS_B_ESCAPES", "_LS_DATE", "_ls_escaped_cmd",
                 "_LS_FALLBACK_REGEX", "_LS_PERMS", "_LS_REGEX", "_LS_TOOLBOX_REGEX",
                 "_LS_TOTAL_RE", "_parse_ls_entry", "_parse_ls_output", "_PERMS_RE",
                 "_probe_cmd", "_split_link", "_unescape_ls_b"):
        assert not hasattr(fb, gone), f"{gone} is unused here and must not be re-exported"
    # Still reached through this module: by the panel itself, or patched by tests.
    for kept in ("_probe_remote", "_parse_ls_line", "_parse_ls_listing", "_rm_cmd", "_ls_cmd",
                 "_cp_cmd", "_mv_cmd", "_human_size"):
        assert hasattr(fb, kept)


def test_file_item_has_no_dead_comparison():
    from turboadb.gui.file_browser import _FileItem

    # The table keeps Qt's own sorting disabled and orders its rows itself in
    # _FileTableWidget.sortByColumn, so an item never compares against another.
    assert "__lt__" not in vars(_FileItem)


def _fake_local_shell(monkeypatch):
    """A LocalShellSession whose process and environment never touch the system."""
    import turboadb.tools as tools
    from turboadb.gui import local_terminal as lt

    class FakeProc:
        pid = 4242
        stdin = None
        stdout = None

        def poll(self):
            return None

    monkeypatch.setattr(lt.subprocess, "Popen", lambda argv, **kwargs: FakeProc())
    monkeypatch.setattr(lt, "find_adb", lambda *_a, **_k: None)
    monkeypatch.setattr(tools, "find_scrcpy", lambda *_a, **_k: None)
    monkeypatch.setattr(lt, "_get_registry_env", lambda: {})
    return lt, lt.LocalShellSession("cmd", adb_path="__missing__")


def test_local_shell_interrupt_kills_the_tree_off_the_ui_thread(monkeypatch):
    lt, session = _fake_local_shell(monkeypatch)
    gate = threading.Event()
    killed = threading.Event()

    def slow_kill(proc, wait_s=2.0):
        gate.wait(5)
        killed.set()

    monkeypatch.setattr(lt, "_kill_process_tree", slow_kill)
    landed = threading.Event()
    started = time.monotonic()
    session.interrupt(on_done=landed.set)
    # Ctrl+C used to block the UI thread here for taskkill plus two waits.
    assert time.monotonic() - started < 1.0
    assert not killed.is_set() and not landed.is_set()
    assert not session.running
    gate.set()
    assert landed.wait(5) and killed.is_set()


def test_local_shell_read_holds_a_split_character_across_idle_polls(monkeypatch):
    _lt, session = _fake_local_shell(monkeypatch)
    cafe = "café".encode("utf-8")
    chunks = [cafe[:-1], b"", b"", cafe[-1:]]
    monkeypatch.setattr(session, "_read_raw", lambda size: chunks.pop(0) if chunks else b"")
    assert session.read() == b"caf"
    # The idle polls used to force final=True, printing the half character as mojibake.
    assert session.read() == b""
    assert session.read() == b""
    assert session.read() == cafe[-2:]


def test_output_transcoder_releases_held_bytes_only_after_the_grace_period():
    from turboadb.gui.local_terminal import OutputTranscoder

    transcoder = OutputTranscoder("utf-8")
    assert transcoder.feed(b"x\xc3") == b"x"
    assert not transcoder.held_expired()
    assert transcoder.feed(b"") == b""  # an idle poll must not restart the clock either
    assert not transcoder.held_expired()
    assert transcoder.feed(b"\xa9") == b"\xc3\xa9"
    assert not transcoder.held_expired()

    transcoder.HOLD_GRACE_S = 0.0  # the rest is clearly never coming
    assert transcoder.feed(b"y\xc3") == b"y"
    assert transcoder.held_expired()
    assert transcoder.feed(b"", final=True)


def test_logcat_worker_line_buffer_is_capped_and_counts_drops(qapp):
    from turboadb.gui.logcat_view import _LogcatThread

    thread = _LogcatThread(None, ["logcat"])
    thread._LINES_MAX = 5
    thread._publish([f"line {i}" for i in range(8)])
    assert thread.take_dropped() == 3
    assert thread.take_lines() == [f"line {i}" for i in range(3, 8)]
    assert thread.take_dropped() == 0 and thread.take_lines() == []


def test_logcat_render_timer_only_ticks_while_there_is_output(qapp):
    from PyQt5.QtGui import QHideEvent, QShowEvent
    from turboadb.gui.logcat_view import LogcatPanel

    panel = LogcatPanel(None)
    try:
        # A device tab whose logcat was never started used to tick every 350 ms.
        assert not panel._render_timer.isActive()
        panel._ensure_rendering()
        panel._on_batch(["one"])
        assert panel._render_timer.isActive()
        panel._render_pending()
        assert panel.view.toPlainText().splitlines() == ["one"]
        assert not panel._render_timer.isActive()  # nothing running, nothing to paint

        panel._ensure_rendering()
        panel._on_batch(["two"])
        panel.hideEvent(QHideEvent())
        assert not panel._render_timer.isActive()
        panel.showEvent(QShowEvent())
        assert panel._render_timer.isActive()
    finally:
        panel.close_panel()
    assert not panel._render_timer.isActive()


def test_logcat_refilter_keeps_the_skip_count(qapp):
    from turboadb.gui.logcat_view import LogcatPanel

    panel = LogcatPanel(None)
    try:
        panel._PENDING_MAX = 2
        panel._paused = True
        panel._on_batch(["a", "b", "c", "d"])
        assert panel._skipped == 2
        panel._paused = False
        panel._refilter_view()  # editing the filter must not discard the marker
        assert panel._skipped == 2
        panel._render_pending()
        assert panel._skipped == 0
        assert panel.view.toPlainText().splitlines()[-1] == (
            "… (2 lines skipped on screen — saved log has all)"
        )
    finally:
        panel.close_panel()


def test_scrollback_memory_fallback_is_bounded_and_says_so(qapp, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QPlainTextEdit
    from turboadb.gui import scrollback as sb_mod

    def unwritable(*_args, **_kwargs):
        raise OSError("profile not writable")

    monkeypatch.setattr(sb_mod.tempfile, "mkstemp", unwritable)
    sb = sb_mod.Scrollback(QPlainTextEdit())
    sb._MEMORY_LIMIT = 150            # spill (and so fail) almost immediately
    sb._MEMORY_FALLBACK_LIMIT = 500   # then keep only a bounded tail in RAM
    chunk = "x" * 100 + "\n"
    for _ in range(20):
        sb.archive(chunk)
    assert sb._flush(10)
    out = tmp_path / "saved.log"
    sb.save_to(str(out))
    text = out.read_text(encoding="utf-8")
    assert sb._truncated > 0
    assert len(text) < 20 * len(chunk)  # the whole session is no longer held in memory
    assert text.startswith("[TurboADB: the history file could not be written")
    assert text.endswith(chunk)  # the newest output is what survived
    sb.close()


def test_console_input_queue_overflow_is_marked_and_resets_the_parser(qapp):
    con = _plain_console()
    try:
        con._MAX_INQ = 16
        con.feed("old" * 8)
        con.feed("tail-kept")
        assert con._dropped == 24
        # The hole can cut an escape in half; a half-parsed CSI used to eat
        # everything that followed it.
        con._state, con._csi = 2, "31"
        while con._inq:
            con._drain_tick()
        assert con._state == 0 and con._csi == ""
        text = con.toPlainText()
        assert "24 characters skipped on screen" in text
        assert text.endswith("tail-kept")
        assert con._sb.full_text().endswith("tail-kept")  # the archive kept everything
    finally:
        con.close_archive()
        con.close()


def test_console_overwrite_after_carriage_return_is_one_edit(qapp):
    con = _plain_console()
    try:
        _render(con, b"100% done\rok\n")
        assert con.toPlainText() == "ok0% done\n"
        con.clear()
        _render(con, b"ab\rlonger\n")
        assert con.toPlainText() == "longer\n"

        con.clear()
        _render(con, b"abcdefghij\r")
        changes = []
        con.document().contentsChange.connect(lambda *args: changes.append(args))
        _render(con, b"XYZ")
        # One selection + one insert for the overlap, not one edit per character.
        assert len(changes) == 1
        assert con.toPlainText() == "XYZdefghij"
    finally:
        con.close_archive()
        con.close()


def test_phone_answer_without_telephony_is_reported_as_a_hint(qapp, monkeypatch):
    from turboadb.gui import phone_panel

    jobs = []
    monkeypatch.setattr(
        phone_panel, "run_job",
        lambda tracked, fn, on_done=None, on_fail=None: jobs.append((fn, on_done, on_fail)),
    )
    panel = phone_panel.PhonePanel(object())
    try:
        logged = []
        panel.log.connect(logged.append)
        # Answer/End stay enabled on purpose: they send the call key events a
        # head unit forwards to the phone paired over Bluetooth.
        assert panel.btn_answer.isEnabled() and panel.btn_end.isEnabled()
        assert "key event" in panel.btn_answer.toolTip()
        assert "key event" in panel.btn_end.toolTip()

        panel._set_state("unsupported")
        jobs.clear()
        panel._answer_call()
        jobs[-1][2]("Can't find service: phone")
        assert logged[-1].startswith("[INFO] answer:")

        panel._set_state("idle")  # a real phone: a failure is still a failure
        panel._end_call()
        jobs[-1][2]("adb: device offline")
        assert logged[-1].startswith("[ERROR] end call:")
    finally:
        panel.close_panel()


def test_panels_share_one_unwrap_icon_cache_and_page_toolbar(qapp):
    from turboadb.gui import apps_panel, file_browser, logcat_view, phone_panel, qtutil
    from turboadb.results import CommandResult, OperationResult

    assert apps_panel.unwrap is qtutil.unwrap and phone_panel.unwrap is qtutil.unwrap
    assert file_browser.cached_icon is qtutil.cached_icon
    assert phone_panel.cached_icon is qtutil.cached_icon
    for module in (apps_panel, phone_panel):
        assert not hasattr(module, "_unwrap")
    for module in (file_browser, phone_panel):
        assert not hasattr(module, "_cached_icon")
    for module in (apps_panel, file_browser, logcat_view):
        assert module.page_toolbar is qtutil.page_toolbar

    assert qtutil.unwrap(OperationResult(True, "x", value=7)) == 7
    with pytest.raises(RuntimeError):
        qtutil.unwrap(CommandResult("c", 1, "", "boom", 0.0))
    assert qtutil.cached_icon("apps", "orange") is qtutil.cached_icon("apps", "orange")
    toolbar, row = qtutil.page_toolbar()
    assert toolbar.objectName() == "pageToolbar" and toolbar.layout() is row
