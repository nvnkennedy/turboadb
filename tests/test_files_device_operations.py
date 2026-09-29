"""The Files tab's device side: telling this PC's refusals from the device's,
names the listing could not read exactly, stopping batches when the tab
closes, Cancel during planning, F5 with two tabs side by side, and one set
of rules for copying and editing device files, shared with the engine.

Headless (Qt offscreen); device calls go to small fakes or the fake adb."""
from __future__ import annotations

import os
import shlex
import stat
import threading
import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QMimeData, QPointF, Qt, QUrl  # noqa: E402
from PyQt5.QtGui import QDragMoveEvent, QDropEvent  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QSplitter, QVBoxLayout, QWidget  # noqa: E402

from turboadb import ADBConfig, ADBHandler, remotefs  # noqa: E402
from turboadb.exceptions import ADBError, ADBTransferError  # noqa: E402
from turboadb.gui import file_browser as fb  # noqa: E402


class _Res:
    def __init__(self, stdout="", ok=True, stderr=""):
        self.stdout, self.ok, self.stderr = stdout, ok, stderr
        self.text = stdout.strip()


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


def _boxes(monkeypatch):
    shown = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(fb.QMessageBox, kind,
                            staticmethod(lambda *a, _k=kind, **k: shown.append((_k, a[2]))))
    return shown


def _page(monkeypatch, handler=None):
    monkeypatch.setattr(fb.FileBrowser, "refresh_local", lambda self: None)
    monkeypatch.setattr(fb.FileBrowser, "refresh_remote", lambda self: None)
    page = fb.FileBrowser(handler)
    page._loaded_remote = True
    page.remote_cwd = page.remote_table.base_dir = "/sdcard"
    return page


def _wait(qapp, done, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    return done()


# --------------------------------------------------------------------------- #
# a push or pull that THIS PC refused is not the device's refusal
# --------------------------------------------------------------------------- #
def _failing_transfer(direction, a, b, error):
    class Handler:
        def push(self, *_args, **_kw):
            raise ADBTransferError(error)

        pull = push

    return fb._TransferThread(Handler(), direction, a, b)


def _finish(page, thread):
    failures = []
    thread.failed.connect(failures.append)
    thread.run()  # on this thread: the signal is delivered at once
    page._transfer = thread
    page._on_transfer_finished(thread, False, failures[0])


@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root may write anything")
def test_a_pull_this_pc_refuses_does_not_offer_device_write_access(qapp, monkeypatch, tmp_path):
    """"cannot create 'C:\\...': Permission denied" is adb failing to write on
    the PC, yet it offered adb root and remount for the device folder."""
    target = tmp_path / "locked.txt"
    target.write_text("old")
    os.chmod(str(target), stat.S_IREAD)
    page = _page(monkeypatch)
    try:
        requests, logs = [], []
        page.write_access_needed.connect(requests.append)
        page.log.connect(logs.append)
        thread = _failing_transfer("pull", "/sdcard/locked.txt", str(target),
                                   f"adb: error: cannot create '{target}': Permission denied")
        _finish(page, thread)
        assert thread.local_refusal == str(target)
        assert requests == []
        assert any(str(target) in line and "on this PC" in line for line in logs)
    finally:
        os.chmod(str(target), stat.S_IREAD | stat.S_IWRITE)
        page.close_panel()


def test_a_refusal_by_the_device_is_still_offered(qapp, monkeypatch, tmp_path):
    target = tmp_path / "x.conf"
    page = _page(monkeypatch)
    try:
        requests = []
        page.write_access_needed.connect(requests.append)
        thread = _failing_transfer(
            "pull", "/data/misc/x.conf", str(target),
            f"adb: error: failed to copy '/data/misc/x.conf' to '{target}': "
            "remote open failed: Permission denied")
        _finish(page, thread)
        assert thread.local_refusal == ""
        assert len(requests) == 1 and requests[0]["path"] == "/data/misc"
    finally:
        page.close_panel()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="permission bits as a normal user on Linux and macOS")
def test_this_pcs_side_is_checked_for_both_directions(tmp_path):
    source = tmp_path / "secret.bin"
    source.write_text("x")
    os.chmod(str(source), 0)
    folder = tmp_path / "readonly"
    folder.mkdir()
    os.chmod(str(folder), 0o500)
    try:
        assert fb._local_refusal("push", str(source), "/sdcard/secret.bin",
                                 "remote couldn't create file: Permission denied") == str(source)
        new = str(folder / "new.txt")
        assert fb._local_refusal("pull", "/sdcard/new.txt", new,
                                 f"cannot create '{new}': Permission denied") == new
        assert fb._local_refusal("pull", "/sdcard/ok.txt", str(tmp_path / "ok.txt"),
                                 "remote Permission denied") == ""
    finally:
        os.chmod(str(source), 0o600)
        os.chmod(str(folder), 0o700)


# --------------------------------------------------------------------------- #
# names the listing could not read exactly
# --------------------------------------------------------------------------- #
def _unsafe_folder(page, qapp):
    table = page.remote_table
    rows = [("caf\ufffd", 0, "<DIR>", "Folder", "", "drwxrwx--x", "", True),
            ("Music", 0, "<DIR>", "Folder", "", "drwxrwx--x", "", True)]
    page._populate(table, remotefs._Listing(rows, {"caf\ufffd"}), parent_row=True)
    page.resize(900, 600)
    page.show()
    qapp.processEvents()
    rows_by_name = {table.item(r, 0).text(): r for r in range(table.rowCount())}
    return table, rows_by_name


def test_a_drop_on_a_folder_the_listing_could_not_read_is_refused(qapp, monkeypatch, tmp_path):
    """Its real name is unknown: pushing to the name shown made a new folder."""
    local = tmp_path / "a.txt"
    local.write_text("a")
    page = _page(monkeypatch)
    try:
        table, rows = _unsafe_folder(page, qapp)
        dropped, logs = [], []
        table.dropped.connect(lambda *args: dropped.append(args))
        page.log.connect(logs.append)
        mimes = []
        for name in ("caf\ufffd", "Music"):
            point = QPointF(table.visualRect(table.model().index(rows[name], 0)).center())
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(local))])
            mimes.append(mime)  # an event keeps a raw pointer to its mime data
            move = QDragMoveEvent(point.toPoint(), Qt.CopyAction, mime, Qt.LeftButton,
                                  Qt.NoModifier)
            table.dragMoveEvent(move)
            drop = QDropEvent(point, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
            table.dropEvent(drop)
            assert move.isAccepted() == drop.isAccepted() == (name == "Music")
        assert [target for _paths, _local, target in dropped] == ["/sdcard/Music"]
        assert any("caf\ufffd" in line and "refused" in line for line in logs)
    finally:
        page.hide()
        page.close_panel()


def test_opening_a_folder_the_listing_could_not_read_is_refused(qapp, monkeypatch):
    page = _page(monkeypatch)
    try:
        table, rows = _unsafe_folder(page, qapp)
        shown = _boxes(monkeypatch)
        page._on_remote_double_click(rows["caf\ufffd"], 0)
        assert page.remote_cwd == "/sdcard" and [k for k, _t in shown] == ["warning"]
        page._on_remote_double_click(rows["Music"], 0)
        assert page.remote_cwd == "/sdcard/Music"
    finally:
        page.hide()
        page.close_panel()


# --------------------------------------------------------------------------- #
# a closed tab stops its paste and delete batches
# --------------------------------------------------------------------------- #
def _new_job(page, before):
    [job] = [job for job in page._jobs if job not in before]
    return job


def test_closing_the_tab_stops_a_paste_batch(qapp, monkeypatch):
    """The copies went on for hours after the tab was closed."""
    release, ran = threading.Event(), []

    class Handler:
        def shell(self, command, timeout=None, safe=None):
            ran.append(command)
            release.wait(5)
            return _Res()

    page = _page(monkeypatch, Handler())
    before = set(page._jobs)
    page._run_shell_batch([("copy a", "cp a"), ("copy b", "cp b"), ("copy c", "cp c")],
                          timeout=600, summary="Pasted")
    job = _new_job(page, before)
    assert _wait(qapp, lambda: ran == ["cp a"])
    page.close_panel()
    release.set()
    assert job.wait(5000)
    assert ran == ["cp a"]  # the one running finishes; nothing after it starts


def test_closing_the_tab_stops_a_long_delete(qapp, monkeypatch):
    release, removed = threading.Event(), []

    class Handler:
        def remove(self, paths, *, recursive=False, safe=None):
            removed.append(list(paths))
            release.wait(5)
            return list(paths)

    page = _page(monkeypatch, Handler())
    before = set(page._jobs)
    targets = [f"/sdcard/DCIM/IMG_{i:04d}.jpg" for i in range(120)]
    page._delete_remote_targets(targets)
    job = _new_job(page, before)
    assert _wait(qapp, lambda: len(removed) == 1)
    page.close_panel()
    release.set()
    assert job.wait(5000)
    [first] = removed  # the group being deleted finishes; the rest are never asked for
    assert first == targets[:len(first)] and len(first) < len(targets)


# --------------------------------------------------------------------------- #
# Cancel also drops a push or pull still being planned
# --------------------------------------------------------------------------- #
def test_cancel_drops_a_push_that_is_still_being_planned(qapp, monkeypatch, tmp_path):
    deferred = []
    monkeypatch.setattr(fb, "run_job", lambda jobs, fn, done=None, fail=None:
                        deferred.append((fn, done)))
    monkeypatch.setattr(fb, "_plan_push", lambda handler, sources, dst:
                        ([(sources[0], "/sdcard/a.txt", "push")], [], []))
    page = _page(monkeypatch)
    try:
        queued = []
        monkeypatch.setattr(page, "_enqueue_transfers", lambda jobs, _msg: queued.append(jobs))
        source = str(tmp_path / "a.txt")
        page._start_push([source], "/sdcard")
        page.cancel_transfers()  # while the device is being checked
        fn, done = deferred[-1]
        done(fn())
        assert queued == []
        page._start_push([source], "/sdcard")  # a new push after the Cancel goes ahead
        fn, done = deferred[-1]
        done(fn())
        assert queued == [[(source, "/sdcard/a.txt", "push")]]
    finally:
        page.close_panel()


# --------------------------------------------------------------------------- #
# F5 with two Files tabs side by side
# --------------------------------------------------------------------------- #
def test_f5_refreshes_the_focused_pane_with_two_files_tabs_in_one_window(qapp, monkeypatch):
    """Two window-wide F5 shortcuts were ambiguous: neither fired."""
    window = QWidget()
    split = QSplitter(Qt.Horizontal)
    QVBoxLayout(window).addWidget(split)
    pages, hits = [], []
    for index in range(2):
        page = fb.FileBrowser(None)
        page._loaded_remote = True
        monkeypatch.setattr(page, "refresh_local", lambda i=index: hits.append(("local", i)))
        monkeypatch.setattr(page, "refresh_remote", lambda i=index: hits.append(("device", i)))
        split.addWidget(page)
        pages.append(page)
    try:
        window.resize(1600, 800)
        window.show()
        window.activateWindow()
        QApplication.setActiveWindow(window)
        QTest.qWaitForWindowActive(window, 2000)
        pages[1].remote_table.setFocus()
        qapp.processEvents()
        assert QApplication.focusWidget() is pages[1].remote_table
        hits.clear()
        QTest.keyClick(pages[1].remote_table, Qt.Key_F5)
        assert hits == [("device", 1)]
        pages[0].local_table.setFocus()
        qapp.processEvents()
        QTest.keyClick(pages[0].local_table, Qt.Key_F5)
        assert hits == [("device", 1), ("local", 0)]
    finally:
        window.hide()
        for page in pages:
            page.close_panel()
        window.deleteLater()
        qapp.processEvents()


# --------------------------------------------------------------------------- #
# copying on the device: one set of rules for Paste and `turboadb cp`
# --------------------------------------------------------------------------- #
PROBES = {
    "/sdcard/DCIM": ("d", False, "/storage/emulated/0/DCIM"),
    "/sdcard/DCIM/sub": ("d", False, "/storage/emulated/0/DCIM/sub"),
    "/sdcard/link": ("d", True, "/storage/emulated/0/DCIM"),  # a link to DCIM
    "/sdcard/DCIM/link": ("n", False, ""),
    "/sdcard/Backup": ("d", False, "/storage/emulated/0/Backup"),
    "/sdcard/Backup/link": ("d", False, "/storage/emulated/0/Backup/link"),
    "/sdcard/Backup/DCIM": ("n", False, ""),
    "/sdcard/DCIM/sub/DCIM": ("n", False, ""),
    "/sdcard/a.txt": ("f", False, ""),
    "/sdcard/Backup/a.txt": ("d", False, "/storage/emulated/0/Backup/a.txt"),
}


@pytest.mark.parametrize("source, folder, decision", [
    # cp -r copies a link as a link: no recursion, so it may go into its own target...
    ("/sdcard/link", "/sdcard/DCIM", ("", False, False)),
    # ...but never merges into (or replaces) an item of its name
    ("/sdcard/link", "/sdcard/Backup", ("different", True, False)),
    ("/sdcard/DCIM", "/sdcard/DCIM/sub", ("inside", False, False)),
    ("/sdcard/DCIM", "/sdcard/Backup", ("", False, False)),
    ("/sdcard/a.txt", "/sdcard/Backup", ("different", True, False)),
])
def test_one_decision_for_every_device_copy(source, folder, decision):
    target = f"{folder}/{os.path.basename(source)}"
    assert remotefs._copy_decision(source, target, PROBES, folder) == decision


def test_paste_and_the_engine_agree_about_a_linked_folder(monkeypatch, fake_adb):
    """Paste copied a folder link into its own target; `turboadb cp` refused it
    as "into itself" - the engine followed the link, cp -r does not."""
    monkeypatch.setattr(fb, "_probe_remote",
                        lambda handler, paths: {p: PROBES[p] for p in paths})
    commands, collisions, errors = fb._plan_remote_copy(None, ["/sdcard/link"], "/sdcard/DCIM")
    assert errors == [] and collisions == []
    assert [shlex.split(c) for _label, c in commands] == \
        [["cp", "-r", "--", "/sdcard/link", "/sdcard/DCIM/link"]]

    fake_adb.add("for f in /sdcard/link /sdcard/DCIM;",
                 stdout="dL /storage/emulated/0/DCIM\nd- /storage/emulated/0/DCIM\n")
    fake_adb.add("for f in /sdcard/DCIM/link;", stdout="n- \n")
    handler = ADBHandler(ADBConfig(serial="x"))
    assert handler.copy("/sdcard/link", "/sdcard/DCIM") == "/sdcard/DCIM/link"
    assert fake_adb.argv_after_adb()[-1] == "cp -r -- /sdcard/link /sdcard/DCIM/link"


def test_the_engine_never_copies_over_another_kind_of_item(fake_adb):
    """A file copied onto a folder of its name landed INSIDE that folder."""
    fake_adb.add("for f in /sdcard/a.txt /sdcard/Backup;",
                 stdout="f- \nd- /storage/emulated/0/Backup\n")
    fake_adb.add("for f in /sdcard/Backup/a.txt;", stdout="d- /storage/emulated/0/Backup/a.txt\n")
    handler = ADBHandler(ADBConfig(serial="x"))
    with pytest.raises(ADBError, match="different kind"):
        handler.copy("/sdcard/a.txt", "/sdcard/Backup")
    assert not any("cp -r" in " ".join(call) for call in fake_adb.calls)


# --------------------------------------------------------------------------- #
# editing a device file: the Files tab saves the way `turboadb edit` does
# --------------------------------------------------------------------------- #
def test_a_saved_file_keeps_its_owner_and_mode(fake_adb, monkeypatch):
    """adb push deletes the file it writes, so the saved file had adb's owner
    and mode; the copy now goes next to it and is written over it in place."""
    fake_adb.add("stat -L", stdout="4 640 regular file\n/data/app.ini\n")
    fake_adb.add("stat -c %a", stdout="755\n")
    fake_adb.add("TURBOADB_WRITTEN", stdout="TURBOADB_WRITTEN\n")
    handler = ADBHandler(ADBConfig(serial="x"))
    pushed = []

    def transfer(direction, a, b, *_rest):
        if direction == "pull":
            with open(b, "wb") as fh:
                fh.write(b"old\n")
        else:
            pushed.append(b)
        return "ok"

    monkeypatch.setattr(handler, "_transfer", transfer)

    def edit(path):
        with open(path, "wb") as fh:
            fh.write(b"new\n")
        return 0

    assert handler.edit_file("/data/app.ini", edit)["changed"] is True
    (temp,) = pushed
    assert temp.startswith("/data/.turboadb-save-")
    commands = [" ".join(argv[argv.index("shell") + 1:]) for argv in
                (fake_adb.argv_after_adb(i) for i in range(len(fake_adb.calls)))
                if "shell" in argv]
    assert f"cat {temp} > /data/app.ini && echo TURBOADB_WRITTEN" in commands
    assert commands[-1] == f"rm -f -- {temp}"
    assert not any(c.startswith("chmod") for c in commands)


def test_a_save_that_fails_leaves_the_device_file_as_it_was(fake_adb, monkeypatch, tmp_path):
    handler = ADBHandler(ADBConfig(serial="x"))
    local = tmp_path / "x"
    local.write_text("x")

    def cut_off(*_args):
        raise ADBError("adb: error: failed to copy: connection reset")

    monkeypatch.setattr(handler, "_transfer", cut_off)
    with pytest.raises(ADBError, match="connection reset"):
        handler.replace_file(str(local), "/sdcard/x.txt", mode="660")
    commands = [" ".join(c[1:]) for c in fake_adb.calls]
    assert not any("cat " in c for c in commands)  # the file itself was never touched
    assert any("rm -f -- /sdcard/.turboadb-save-" in c for c in commands)  # nor left a stray copy
    # the push worked but the device could not write the file: said so, copy removed
    monkeypatch.setattr(handler, "_transfer", lambda *args: "ok")
    fake_adb.add("TURBOADB_WRITTEN", stderr="sh: can't create /system/x: Read-only file system",
                 returncode=1)
    with pytest.raises(ADBError, match="was not saved: .*Read-only file system"):
        handler.replace_file(str(local), "/system/x", mode="644")
    assert "rm -f -- /system/.turboadb-save-" in " ".join(fake_adb.calls[-1])


class _Engine:
    """The engine calls the editor makes, recorded."""

    def __init__(self, stat_info=None):
        self.stat_info = stat_info
        self.calls = []

    def stat_path(self, path, safe=None):
        if self.stat_info is None:
            raise ADBError(f"{path}: stat: not found")
        return dict(self.stat_info, path=path)

    def shell(self, command, timeout=None, safe=None):
        self.calls.append(("shell", command))
        return _Res("x\n" if command.startswith("if [ -f") else "")

    def pull(self, remote, local, cancel_event=None, safe=None, **_kw):
        self.calls.append(("pull", remote))
        with open(local, "wb") as fh:
            fh.write(b"a=1\n")

    def replace_file(self, local, path, mode="", safe=None):
        with open(local, "rb") as fh:
            self.calls.append(("replace_file", path, mode, fh.read()))
        return mode


def _link_row(page, name):
    rows = [(name, 12, "12 B", "File Link", "", "lrwxrwxrwx", "shell:shell", False)]
    page._populate(page.remote_table, rows, parent_row=True)
    table = page.remote_table
    return [r for r in range(table.rowCount()) if table.item(r, 0).text() == name][0]


def test_the_editor_saves_through_the_engine(qapp, monkeypatch):
    _sync_jobs(monkeypatch)
    _boxes(monkeypatch)
    engine = _Engine({"real_path": "/data/t/real.conf", "size": 4, "mode": "600",
                      "type": "regular file"})
    page = _page(monkeypatch, engine)
    try:
        page.remote_cwd = "/data/t"
        page._remote_edit_row(_link_row(page, "app.conf"))
        dlg = page._editors[-1]
        dlg.edit.setPlainText("a=2\n")
        dlg._save()
        assert engine.calls == [("pull", "/data/t/real.conf"),
                                ("replace_file", "/data/t/real.conf", "600", b"a=2\n")]
        dlg.done(0)
    finally:
        page.close_panel()


def test_a_link_to_a_pipe_is_not_pulled_where_stat_is_missing(qapp, monkeypatch):
    """Without stat the link's target was pulled blind: reading a pipe never
    ends, and the pull held one of the tab's adb slots meanwhile."""
    _sync_jobs(monkeypatch)
    shown = _boxes(monkeypatch)
    engine = _Engine(stat_info=None)  # an old device: no stat
    page = _page(monkeypatch, engine)
    try:
        page.remote_cwd = "/data/local/tmp"
        page._remote_edit_row(_link_row(page, "fifo-link"))
        assert not any(call[0] == "pull" for call in engine.calls)
        assert shown and "other than a regular file" in shown[-1][1]
    finally:
        page.close_panel()
