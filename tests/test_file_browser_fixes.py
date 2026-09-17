"""Regression tests for the dual-pane file browser fixes (names with spaces,
recursive paste, ambiguous shortcuts, nested transfers, rename/edit safety …).

Everything is offline: device calls go to small fake handlers."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import types

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QObject, Qt, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from turboadb.gui import file_browser as fb  # noqa: E402


class _Res:
    def __init__(self, stdout="", ok=True, stderr=""):
        self.stdout = stdout
        self.ok = ok
        self.stderr = stderr
        self.text = stdout.strip()


def _sync_jobs(monkeypatch):
    """Run browser jobs inline (fn, then its callback) instead of on QThreads."""
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


def _quiet_boxes(monkeypatch, answer=None):
    """Record message boxes instead of showing them."""
    boxes = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(fb.QMessageBox, kind,
                            staticmethod(lambda *a, _k=kind, **k: boxes.append((_k, a[1:]))))
    monkeypatch.setattr(fb.QMessageBox, "question",
                        staticmethod(lambda *a, **k: boxes.append(("question", a[1:]))
                                     or (answer if answer is not None else fb.QMessageBox.No)))
    return boxes


def _rows(table):
    out = []
    for r in range(table.rowCount()):
        it = table.item(r, 0)
        data = it.data(Qt.UserRole) if it else None
        if data:
            out.append(data[0])
    return out


def _select_name(table, name):
    table.clearSelection()
    for r in range(table.rowCount()):
        it = table.item(r, 0)
        if it and it.data(Qt.UserRole) and it.data(Qt.UserRole)[0] == name:
            table.selectRow(r)
            return r
    raise AssertionError(f"{name!r} not listed")


# Real toybox 0.8.12 output (adb shell ends lines with \r\n).
TOYBOX_LS = (
    "total 21\r\n"
    "-rw-rw-rw- 1 shell shell    0 2026-09-14 18:21  lead\r\n"
    "drwxrwxrwx 3 shell shell 3452 2026-09-14 18:21 .\r\n"
    "drwxrwx--x 4 shell shell 3452 2026-09-14 18:21 ..\r\n"
    "lrwxrwxrwx 1 shell shell    6 2026-09-14 18:21 arrow -> a -> b\r\n"
    "-rw-rw-rw- 1 shell shell    0 2026-09-14 18:21 dup\r\n"
    "-rw-rw-rw- 1 shell shell    8 2026-09-14 18:21 dup \r\n"
    "lrwxrwxrwx 1 shell shell    6 2026-09-14 18:21 lnk -> odd -> target\r\n"
    "-rw-rw-rw- 1 shell shell    1 2026-09-14 18:21 nl\r\n"
    "name\r\n"
    "-rw-rw-rw- 1 shell shell    0 2026-09-14 18:21 two  spaces\r\n"
)
TOYBOX_LS_B = (
    "total 21\r\n"
    "-rw-rw-rw- 1 shell shell    0 2026-09-14 18:21 \\ lead\r\n"
    "drwxrwxrwx 3 shell shell 3452 2026-09-14 18:21 .\r\n"
    "drwxrwx--x 4 shell shell 3452 2026-09-14 18:21 ..\r\n"
    "lrwxrwxrwx 1 shell shell    6 2026-09-14 18:21 arrow -> a -> b\r\n"
    "-rw-rw-rw- 1 shell shell    0 2026-09-14 18:21 dup\r\n"
    "-rw-rw-rw- 1 shell shell    8 2026-09-14 18:21 dup\\ \r\n"
    "lrwxrwxrwx 1 shell shell    6 2026-09-14 18:21 lnk\\ ->\\ odd -> target\r\n"
    "-rw-rw-rw- 1 shell shell    1 2026-09-14 18:21 nl\\nname\r\n"
    "-rw-rw-rw- 1 shell shell    0 2026-09-14 18:21 tab\\tesc\\ex\r\n"
    "-rw-rw-rw- 1 shell shell    0 2026-09-14 18:21 two\\ \\ spaces\r\n"
)


# ---- 1 / 10 / 13: lossless ls parsing ---------------------------------------


def test_ls_names_keep_leading_and_trailing_spaces():
    rows, uncertain, clean = fb._parse_ls_listing(
        "total 1\r\n"
        "-rw-rw-rw- 1 shell shell    0 2026-09-14 18:21  lead\r\n"
        "-rw-rw-rw- 1 shell shell    0 2026-09-14 18:21 dup\r\n"
        "-rw-rw-rw- 1 shell shell    8 2026-09-14 18:21 dup \r\n"
        "-rw-rw-rw- 1 shell shell    0 2026-09-14 18:21 two  spaces\r\n")
    assert [r[0] for r in rows] == [" lead", "dup", "dup ", "two  spaces"]
    assert [r[1] for r in rows] == [0, 0, 8, 0]
    assert clean and not uncertain
    assert fb._parse_ls_line("-rw-rw-rw- 1 shell shell 8 2026-09-14 18:21 dup \r\n")[0] == "dup "


def test_symlink_name_containing_arrow_is_split_by_target_length():
    row = fb._parse_ls_line("lrwxrwxrwx 1 shell shell    6 2026-09-14 18:21 lnk -> odd -> target")
    assert row[0] == "lnk -> odd"
    row = fb._parse_ls_line("lrwxrwxrwx 1 shell shell    6 2026-09-14 18:21 arrow -> a -> b")
    assert row[0] == "arrow"


def test_list_remote_dir_relists_with_escapes_for_unparseable_names():
    calls = []

    class Handler:
        def shell(self, cmd, timeout=None, safe=None):
            calls.append(cmd)
            if cmd.startswith("ls -lab "):
                return _Res(TOYBOX_LS_B)
            if cmd.startswith("ls -la "):
                return _Res(TOYBOX_LS)
            return _Res("f\nf\n")  # symlink type check: both links are files

    rows, error = fb._list_remote_dir(Handler(), "/data/local/tmp/x")
    assert error == ""
    names = [r[0] for r in rows]
    assert names == [" lead", "arrow", "dup", "dup ", "lnk -> odd", "nl\nname", "tab\tesc\x1bx",
                     "two  spaces"]
    assert rows.uncertain == set()
    assert [shlex.split(c)[:2] for c in calls[:2]] == [["ls", "-la"], ["ls", "-lab"]]


def test_newline_name_without_ls_b_support_is_marked_unsafe():
    class Handler:
        def shell(self, cmd, timeout=None, safe=None):
            if cmd.startswith("ls -lab "):
                return _Res("", ok=False, stderr="ls: invalid option -- b")
            if cmd.startswith("ls -la "):
                return _Res(TOYBOX_LS)
            return _Res("f\nf\n")

    rows, _error = fb._list_remote_dir(Handler(), "/x")
    assert "nl" in rows.uncertain
    assert "lnk -> odd" in [r[0] for r in rows]


def test_unstatable_entries_are_listed_as_unknown():
    row = fb._parse_ls_line("-?????????  ? ?    ?           ?                ? apexd")
    assert row is not None
    name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir = row
    assert (name, raw_size, sz_str, ftype, mtime, is_dir) == ("apexd", 0, "Unknown", "Unknown",
                                                              "Unknown", False)
    assert perms == "-?????????"
    folder = fb._parse_ls_line("d?????????  ? ?    ?           ?                ? secret dir")
    assert folder[0] == "secret dir" and folder[7] is True


def test_old_toolbox_ls_format_parses():
    d = fb._parse_ls_line("drwxr-xr-x root     root              2012-01-01 00:00 acct")
    assert d[0] == "acct" and d[7] is True and d[6] == "root:root"
    f = fb._parse_ls_line("-rw-r--r-- root     root         1234 2012-01-01 00:00 my file.txt")
    assert f[0] == "my file.txt" and f[1] == 1234
    ln = fb._parse_ls_line("lrwxrwxrwx root     root              2012-01-01 00:00 etc -> /system/etc")
    assert ln[0] == "etc" and ln[3] == "File Link"
    dev = fb._parse_ls_line("crw-rw-rw- root     root       1,   3 2012-01-01 00:00 null")
    assert dev[0] == "null" and dev[3] == "Character Device"


def test_busybox_dates_get_a_real_sort_key():
    import time

    now = time.mktime((2026, 9, 14, 12, 0, 0, 0, 0, -1))
    recent = fb._mtime_sort_key("Jan  5 12:34", now=now)
    old = fb._mtime_sort_key("Dec 31  2020", now=now)
    iso = fb._mtime_sort_key("2026-09-02 21:40")
    assert recent > old > 0.0 and iso > recent
    # "Mon DD HH:MM" in the future means last year.
    assert fb._mtime_sort_key("Dec 25 10:00", now=now) < now
    assert fb._mtime_sort_key("Unknown") == 0.0


def test_duplicate_names_refuse_destructive_actions(app, monkeypatch):
    boxes = _quiet_boxes(monkeypatch, answer=fb.QMessageBox.Yes)
    browser = fb.FileBrowser(None)
    try:
        ran = []
        monkeypatch.setattr(browser, "_run_shell_batch", lambda cmds, timeout=30: ran.extend(cmds))
        row = ("dup", 0, "0 B", "File", "", "-rw-rw-rw-", "shell:shell", False)
        browser._populate(browser.remote_table, [row, row], parent_row=True)
        assert browser.remote_table.unsafe_names == {"dup"}
        _select_name(browser.remote_table, "dup")
        browser._remote_delete()
        browser._remote_copy()
        assert ran == [] and browser._clipboard == []
        assert boxes and boxes[0][0] == "warning"
    finally:
        browser.close_panel()


# ---- 2: device paste never recurses ------------------------------------------


def _fake_probe(monkeypatch, table):
    def probe(handler, paths):
        return {p: table.get(p, ("n", False, "")) for p in paths}

    monkeypatch.setattr(fb, "_probe_remote", probe)


def test_remote_copy_refuses_folder_into_itself(monkeypatch):
    _fake_probe(monkeypatch, {
        "/data/t/pd": ("d", False, "/data/t/pd"),
        "/data/t/pd/sub": ("d", False, "/data/t/pd/sub"),
        "/data/t": ("d", False, "/data/t"),
        "/data/t/pd/sub/pd": ("n", False, ""),
        # /sdcard/pd is the same folder through a symlinked path
        "/sdcard/pd": ("d", False, "/storage/emulated/0/pd"),
        "/storage/emulated/0/pd": ("d", False, "/storage/emulated/0/pd"),
        "/sdcard/pd/pd": ("n", False, ""),
    })
    commands, collisions, errors = fb._plan_remote_copy(None, ["/data/t/pd"], "/data/t/pd/sub")
    assert commands == [] and "itself" in errors[0]
    commands, _c, errors = fb._plan_remote_copy(None, ["/data/t/pd"], "/data/t/pd")
    assert commands == [] and errors
    commands, _c, errors = fb._plan_remote_copy(None, ["/data/t/pd"], "/data/t")
    assert commands == [] and "already in this folder" in errors[0]
    commands, _c, errors = fb._plan_remote_copy(None, ["/storage/emulated/0/pd"], "/sdcard/pd")
    assert commands == [] and "itself" in errors[0]


def test_remote_paste_merges_existing_folder_after_asking(app, monkeypatch):
    _sync_jobs(monkeypatch)
    _fake_probe(monkeypatch, {
        "/a/pd": ("d", False, "/a/pd"), "/b": ("d", False, "/b"), "/b/pd": ("d", False, "/b/pd"),
        "/a/f.txt": ("f", False, ""), "/b/f.txt": ("n", False, ""),
    })
    boxes = _quiet_boxes(monkeypatch, answer=fb.QMessageBox.Yes)
    browser = fb.FileBrowser(None)
    try:
        ran = []
        monkeypatch.setattr(browser, "_run_shell_batch", lambda cmds, timeout=30: ran.extend(cmds))
        browser.remote_cwd = "/b"
        browser._clipboard, browser._clipboard_src = ["/a/pd", "/a/f.txt"], "remote"
        browser._remote_paste()
        assert [k for k, _a in boxes] == ["question"]
        assert [shlex.split(c) for _l, c in ran] == [["cp", "-r", "--", "/a/pd/.", "/b/pd"],
                                                     ["cp", "-r", "--", "/a/f.txt", "/b/f.txt"]]
    finally:
        browser.close_panel()


# ---- 3: shortcuts are scoped per table ----------------------------------------


def test_delete_key_in_remote_table_deletes(app, monkeypatch):
    from PyQt5.QtTest import QTest

    _sync_jobs(monkeypatch)
    _quiet_boxes(monkeypatch, answer=fb.QMessageBox.Yes)

    removed = []

    class Handler:
        """The delete goes through the engine, which chunks and guards the paths."""

        def remove(self, paths, *, recursive=False, safe=None):
            removed.append((list(paths), recursive))
            return list(paths)

    browser = fb.FileBrowser(Handler())
    try:
        browser._loaded_remote = True  # no device listing on show
        browser.remote_cwd = "/sdcard"
        rows = [("a.txt", 1, "1 B", "File", "", "-rw-rw-rw-", "", False),
                ("dup ", 8, "8 B", "File", "", "-rw-rw-rw-", "", False)]
        browser._populate(browser.remote_table, rows, parent_row=True)
        browser.show()
        browser.activateWindow()
        QApplication.setActiveWindow(browser)
        QTest.qWaitForWindowActive(browser, 2000)
        _select_name(browser.remote_table, "dup ")
        browser.remote_table.setFocus()
        app.processEvents()
        assert QApplication.focusWidget() is browser.remote_table
        QTest.keyClick(browser.remote_table, Qt.Key_Delete)
        assert removed == [(["/sdcard/dup "], True)]  # the trailing space survives
    finally:
        browser.hide()
        browser.close_panel()


# ---- 4: transfers onto existing folders merge instead of nesting -------------


def test_push_plan_merges_into_existing_device_folder(monkeypatch, tmp_path):
    (tmp_path / "pd").mkdir()
    (tmp_path / "pd" / "f.txt").write_text("hi")
    (tmp_path / "new.txt").write_text("x")
    _fake_probe(monkeypatch, {"/sdcard/x/pd": ("d", False, "/sdcard/x/pd")})
    jobs, collisions, errors = fb._plan_push(
        None, [str(tmp_path / "pd"), str(tmp_path / "new.txt")], "/sdcard/x")
    assert errors == [] and collisions == ["pd"]
    assert jobs == [(os.path.join(str(tmp_path / "pd"), "."), "/sdcard/x/pd", "push"),
                    (str(tmp_path / "new.txt"), "/sdcard/x/new.txt", "push")]
    # A file can't replace a device folder (adb would put it inside).
    _fake_probe(monkeypatch, {"/sdcard/x/new.txt": ("d", False, "/sdcard/x/new.txt")})
    jobs, _c, errors = fb._plan_push(None, [str(tmp_path / "new.txt")], "/sdcard/x")
    assert jobs == [] and "folder" in errors[0]


def test_pull_plan_merges_resolves_links_and_sanitises(monkeypatch, tmp_path):
    (tmp_path / "sub").mkdir()
    _fake_probe(monkeypatch, {
        "/d/sub": ("d", False, "/d/sub"),
        "/d/link": ("f", True, "/d/real.log"),
        "/d/a:b": ("f", False, ""),
        "/d/nul": ("f", False, ""),
        "/d/dup ": ("f", False, ""),
        "/d/gone": ("n", False, ""),
    })
    sources = ["/d/sub", "/d/link", "/d/a:b", "/d/nul", "/d/dup ", "/d/gone"]
    jobs, collisions, errors, notes = fb._plan_pull(None, sources, str(tmp_path), windows=True)
    assert collisions == ["sub"]
    assert jobs == [
        ("/d/sub/.", str(tmp_path / "sub"), "pull"),
        ("/d/real.log", str(tmp_path / "link"), "pull"),
        ("/d/a:b", str(tmp_path / "a_b"), "pull"),
        ("/d/nul", str(tmp_path / "_nul"), "pull"),
        ("/d/dup ", str(tmp_path / "dup_"), "pull"),
    ]
    assert len(errors) == 1 and "gone" in errors[0]
    assert any("real.log" in n for n in notes) and any("a_b" in n for n in notes)


def test_safe_local_name():
    assert fb._safe_local_name("a:b?.txt", windows=True) == "a_b_.txt"
    assert fb._safe_local_name("CON.txt", windows=True) == "_CON.txt"
    assert fb._safe_local_name("com1", windows=True) == "_com1"
    assert fb._safe_local_name("trail. ", windows=True) == "trail__"
    assert fb._safe_local_name("fine name", windows=True) == "fine name"
    assert fb._safe_local_name("a:b", windows=False) == "a:b"


# ---- 5: rename never overwrites or moves into a folder silently --------------


def test_rename_commands_use_no_clobber_and_T():
    assert shlex.split(fb._rename_cmd("/s/a", "/s/b", overwrite=True)) == [
        "mv", "-f", "-T", "--", "/s/a", "/s/b"]
    free = shlex.split(fb._rename_cmd("/s/a", "/s/b"))
    assert free[:6] == ["mv", "-n", "-T", "--", "/s/a", "/s/b"] and "exit" in free


def test_rename_asks_before_replacing_and_refuses_folders(app, monkeypatch):
    _sync_jobs(monkeypatch)
    browser = fb.FileBrowser(None)
    try:
        ran = []
        monkeypatch.setattr(browser, "_run_shell_batch", lambda cmds, timeout=30: ran.extend(cmds))
        boxes = _quiet_boxes(monkeypatch, answer=fb.QMessageBox.No)
        browser._on_rename_checked("a", "b", "/s/a", "/s/b", _Res("file\n"))
        assert ran == [] and boxes[-1][0] == "question"
        browser._on_rename_checked("a", "b", "/s/a", "/s/b", _Res("dir\n"))
        assert ran == [] and boxes[-1][0] == "warning"
        _quiet_boxes(monkeypatch, answer=fb.QMessageBox.Yes)
        browser._on_rename_checked("a", "b", "/s/a", "/s/b", _Res("file\r\n"))
        assert shlex.split(ran[-1][1]) == ["mv", "-f", "-T", "--", "/s/a", "/s/b"]
        browser._on_rename_checked("a", " b", "/s/a", "/s/b", _Res("free\n"))
        assert shlex.split(ran[-1][1])[:3] == ["mv", "-n", "-T"]
    finally:
        browser.close_panel()


# ---- 6 / 7 / editor guards: edit through links, keep the mode ----------------


class _EditHandler:
    def __init__(self, stat_reply):
        self.stat_reply = stat_reply
        self.shells, self.pulls, self.pushes = [], [], []

    def shell(self, cmd, timeout=None, safe=None):
        self.shells.append(cmd)
        if cmd.startswith("stat -L"):
            return _Res(self.stat_reply)
        if cmd.startswith("stat -c"):
            return _Res("755\r\n")
        return _Res("")

    def pull(self, remote, local, cancel_event=None, safe=None, **kw):
        self.pulls.append(remote)
        with open(local, "wb") as fh:
            fh.write(b"#!/bin/sh\necho hi\n")

    def push(self, local, remote, safe=None, **kw):
        with open(local, "rb") as fh:
            self.pushes.append((remote, fh.read()))


def _remote_link_row(browser, name="run.sh"):
    rows = [(name, 7, "7 B", "File Link", "", "lrwxrwxrwx", "shell:shell", False)]
    browser._populate(browser.remote_table, rows, parent_row=True)
    return _select_name(browser.remote_table, name)


def test_remote_edit_follows_symlink_and_restores_mode(app, monkeypatch):
    _sync_jobs(monkeypatch)
    _quiet_boxes(monkeypatch)
    handler = _EditHandler("18 755 regular file\r\n/data/t/real.sh\r\n")
    browser = fb.FileBrowser(handler)
    try:
        browser.remote_cwd = "/data/t"
        row = _remote_link_row(browser)
        browser._remote_edit_row(row)
        assert handler.pulls == ["/data/t/real.sh"]
        dlg = browser._editors[-1]
        assert dlg.path == "/data/t/real.sh"
        dlg.edit.setPlainText("#!/bin/sh\necho saved\n")
        dlg._save()
        assert handler.pushes == [("/data/t/real.sh", b"#!/bin/sh\necho saved\n")]
        chmods = [shlex.split(c) for c in handler.shells if c.startswith("chmod")]
        assert chmods == [["chmod", "755", "--", "/data/t/real.sh"]]
        dlg.done(0)
    finally:
        browser.close_panel()


@pytest.mark.parametrize("reply, word", [
    ("0 666 character device\n/dev/zero\n", "regular"),
    (f"{fb._EDIT_MAX_BYTES + 1} 644 regular file\n/data/t/big.log\n", "too large"),
])
def test_remote_edit_refuses_devices_and_big_link_targets(app, monkeypatch, reply, word):
    _sync_jobs(monkeypatch)
    boxes = _quiet_boxes(monkeypatch)
    handler = _EditHandler(reply)
    browser = fb.FileBrowser(handler)
    try:
        browser.remote_cwd = "/data/t"
        browser._remote_edit_row(_remote_link_row(browser, "zero"))
        assert handler.pulls == []
        assert boxes and word in boxes[-1][1][1]
    finally:
        browser.close_panel()


def test_double_click_edits_the_clicked_row(app, monkeypatch):
    browser = fb.FileBrowser(None)
    try:
        rows = [("a.txt", 1, "1 B", "File", "", "", "", False),
                ("b.txt", 1, "1 B", "File", "", "", "", False)]
        browser._populate(browser.local_table, rows, parent_row=False)
        browser._populate(browser.remote_table, rows, parent_row=False)
        got = []
        monkeypatch.setattr(browser, "_local_edit_row", lambda r: got.append(("local", r)))
        monkeypatch.setattr(browser, "_remote_edit_row", lambda r: got.append(("remote", r)))
        _select_name(browser.local_table, "a.txt")
        _select_name(browser.remote_table, "a.txt")
        b_local = [r for r in range(2) if browser.local_table.item(r, 0).data(Qt.UserRole)[0] == "b.txt"][0]
        browser._on_local_double_click(b_local, 0)
        browser._on_remote_double_click(b_local, 0)
        assert got == [("local", b_local), ("remote", b_local)]
    finally:
        browser.close_panel()


# ---- 8: a bad timestamp doesn't hide the folder -----------------------------


def test_local_listing_survives_unrepresentable_mtime(monkeypatch, tmp_path):
    import time

    (tmp_path / "old.txt").write_text("x")
    (tmp_path / "new.txt").write_text("y")

    def localtime(value=None):
        if value is not None and value < 1:
            raise OSError(22, "Invalid argument")
        return time.localtime(value)

    os.utime(str(tmp_path / "old.txt"), (0, 0))
    monkeypatch.setattr(fb, "time", types.SimpleNamespace(strftime=time.strftime, localtime=localtime))
    _path, rows = fb._list_local_dir(str(tmp_path))
    by_name = {r[0]: r for r in rows}
    assert set(by_name) == {"old.txt", "new.txt"}
    assert by_name["old.txt"][4] == "" and by_name["new.txt"][4] != ""


# ---- 9: editor keeps NBSP / U+2028 and refuses U+2029 ------------------------


def test_editor_text_is_raw(app):
    content = "a\u00a0b\u2028c\nd"
    dlg = fb._FileEditorDialog("x", "x", content, None)
    try:
        assert dlg.text() == content
        got = []
        dlg.on_save = lambda text: got.append(text) or True
        assert dlg._save() is True and got == [content]
    finally:
        dlg.deleteLater()
    with pytest.raises(fb._EditRefused):
        fb._decode_for_edit("para\u2029graph".encode("utf-8"))


# ---- 11 / loading guards: never act on a folder that isn't shown ------------


def test_failed_remote_listing_restores_previous_folder(app, monkeypatch):
    _sync_jobs(monkeypatch)
    _quiet_boxes(monkeypatch)

    class Handler:
        def shell(self, cmd, timeout=None, safe=None):
            if cmd == "ls -la /sdcard/":
                return _Res("total 0\n-rw-rw-rw- 1 u g 1 2026-09-14 18:21 a.txt\n")
            if cmd.startswith("ls "):
                return _Res("", ok=False, stderr="ls: /sdcard/typo/: No such file or directory")
            return _Res("")

    browser = fb.FileBrowser(Handler())
    try:
        ran = []
        monkeypatch.setattr(browser, "_run_shell_batch", lambda cmds, timeout=30: ran.extend(cmds))
        browser._jump_remote("/sdcard")
        assert browser._remote_shown == "/sdcard" and _rows(browser.remote_table) == ["..", "a.txt"]
        browser.remote_path.setText("/sdcard/typo")
        browser._go_remote()
        assert browser.remote_cwd == "/sdcard" and browser.remote_path.text() == "/sdcard"
        assert _rows(browser.remote_table) == ["..", "a.txt"]
        monkeypatch.setattr(fb.QInputDialog, "getText", staticmethod(lambda *a, **k: ("new", True)))
        browser._remote_mkdir()
        assert shlex.split(ran[-1][1])[-1] == "/sdcard/new"
    finally:
        browser.close_panel()


def test_actions_wait_for_the_listing_being_loaded(app, monkeypatch, tmp_path):
    boxes = _quiet_boxes(monkeypatch)
    browser = fb.FileBrowser(None)
    try:
        browser.local_cwd = str(tmp_path)
        browser._local_loading_path = str(tmp_path / "elsewhere")
        assert browser._local_dir_for_action("Paste") is None
        browser._local_loading_path = str(tmp_path)  # refreshing the shown folder is fine
        assert browser._local_dir_for_action("Paste") == str(tmp_path)
        browser.remote_cwd = "/sdcard/next"
        browser._remote_shown = "/sdcard"
        browser._remote_loading_path = "/sdcard/next"
        assert browser._remote_dir_for_action("Paste") is None
        browser._remote_loading_path = None
        browser._remote_failed = True
        assert browser._remote_dir_for_action("Paste") is None
        assert len(boxes) == 3
    finally:
        browser.close_panel()


# ---- 12: local delete handles read-only files and junctions -----------------


def test_local_delete_read_only_items(tmp_path):
    ro = tmp_path / "ro.txt"
    ro.write_text("x")
    os.chmod(str(ro), stat.S_IREAD)
    tree = tmp_path / "tree"
    (tree / "inner").mkdir(parents=True)
    (tree / "inner" / "locked.txt").write_text("y")
    os.chmod(str(tree / "inner" / "locked.txt"), stat.S_IREAD)
    assert fb._delete_local_items([str(ro), str(tree)]) == []
    assert not ro.exists() and not tree.exists()


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_local_delete_junction_keeps_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = tmp_path / "junction"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True)
    if made.returncode != 0 or not link.exists():
        pytest.skip("could not create a junction here")
    assert fb._is_junction(str(link))
    assert fb._delete_local_items([str(link)]) == []
    assert not os.path.lexists(str(link))
    assert (target / "keep.txt").read_text() == "keep"


# ---- plausible fixes: UI text, UNC drive combo, transfer cancel --------------


def test_hints_no_longer_claim_f5_transfers(app):
    browser = fb.FileBrowser(None)
    try:
        assert "F5 Push" not in browser.hint.text() and "F5 Refresh" in browser.hint.text()
        assert "F5" not in browser.btn_push.toolTip() and "F5" not in browser.btn_pull.toolTip()
    finally:
        browser.close_panel()


@pytest.mark.skipif(os.name != "nt", reason="drive letters are Windows-only")
def test_drive_combo_clears_on_unc_path(app):
    browser = fb.FileBrowser(None)
    try:
        browser._on_drives(["C:\\", "D:\\"])
        browser.local_cwd = "\\\\server\\share\\dir"
        browser._sync_drive_combo()
        assert browser.cmb_drives.currentIndex() == -1
    finally:
        browser.close_panel()


def test_cancel_stops_transfer_and_clears_queue(app, monkeypatch):
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
        logs = []
        browser.log.connect(logs.append)
        browser._enqueue_transfers([("/d/a/.", "a", "pull"), ("/d/b", "b", "pull"),
                                    ("/d/c", "c", "pull")], "go")
        assert len(started) == 1 and len(browser._queue) == 2
        assert not browser.btn_cancel_transfer.isHidden()
        browser.cancel_transfers()
        assert started[0].stopped and browser._queue == []
        started[0].failed.emit("adb pull cancelled")
        assert len(started) == 1
        assert browser.btn_cancel_transfer.isHidden() and browser.bar.isHidden()
        assert any(line.startswith("[CANCELLED] pull a") for line in logs)
    finally:
        browser.close_panel()
