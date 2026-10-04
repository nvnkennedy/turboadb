"""The Files tab's device side: telling this PC's refusals from the device's,
names the listing could not read exactly, stopping batches when the tab
closes, Cancel during planning, F5 with two tabs side by side, and one set
of rules for copying and editing device files, shared with the engine.
Also: the editor asking before it saves over a file that changed on the
device, Delete and Rename when the pane changes folder under their dialog,
the retry of a partly refused delete, and devices whose shell gives no exit
status.

Headless (Qt offscreen); device calls go to small fakes or the fake adb."""
from __future__ import annotations

import os
import posixpath
import re
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
from turboadb.results import CommandResult  # noqa: E402


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
    first = fb._marked("cp a")  # as sent: with the mark that says it worked
    assert _wait(qapp, lambda: ran == [first])
    page.close_panel()
    release.set()
    assert job.wait(5000)
    assert ran == [first]  # the one running finishes; nothing after it starts


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
    # the same command, sent with the mark that says it worked
    assert fake_adb.argv_after_adb()[-1] == remotefs._marked(
        "cp -r -- /sdcard/link /sdcard/DCIM/link")


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
        # what the device has is read before the pull and after the save, so a
        # later save can tell whether another program wrote the file meanwhile
        stamp = ("shell", remotefs._stamp_cmd("/data/t/real.conf"))
        assert engine.calls == [stamp, ("pull", "/data/t/real.conf"),
                                ("replace_file", "/data/t/real.conf", "600", b"a=2\n"), stamp]
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


# --------------------------------------------------------------------------- #
# the editor and a device file that another program wrote meanwhile
# --------------------------------------------------------------------------- #
class _TextDevice:
    """A device with one text file: what it holds, when it was last written,
    and the engine calls the editor makes on it."""

    def __init__(self, path, content):
        self.path, self.content, self.modified = path, content, 1000
        self.saves = []

    def stat_path(self, path, safe=None):
        return {"path": path, "real_path": self.path, "size": len(self.content), "mode": "644",
                "type": "regular file"}

    def shell(self, command, timeout=None, safe=None):
        if command == remotefs._stamp_cmd(self.path):
            return _Res(f"{len(self.content)} {self.modified}\r\n")
        return _Res("")

    def pull(self, remote, local, cancel_event=None, safe=None, **_kw):
        with open(local, "wb") as fh:
            fh.write(self.content)

    def replace_file(self, local, path, mode="", safe=None):
        with open(local, "rb") as fh:
            self.written(fh.read())
        self.saves.append(self.content)
        return mode

    def written(self, content):
        """The file is written: by the editor's save, or by an app on the device."""
        self.content, self.modified = content, self.modified + 60


def _queued_jobs(monkeypatch):
    """Queue the page's jobs instead of starting threads; the function returned
    runs them in order, each with its callback, and those they start in turn.
    A save is then still under way when the editor hears of it, as with a
    worker thread."""
    waiting = []
    monkeypatch.setattr(fb, "run_job", lambda jobs, fn, on_done=None, on_fail=None:
                        waiting.append((fn, on_done, on_fail)))

    def run():
        while waiting:
            fn, on_done, on_fail = waiting.pop(0)
            try:
                result = fn()
            except Exception as exc:
                if on_fail is not None:
                    on_fail(f"{type(exc).__name__}: {exc}")
                continue
            if on_done is not None:
                on_done(result)

    return run


def _edit_volume(page, device, run):
    rows = [("audio.conf", len(device.content), "9 B", "File", "", "-rw-r--r--", "root:root",
             False)]
    page._populate(page.remote_table, rows, parent_row=True)
    page.remote_cwd = "/data/misc"
    page._remote_edit_row(1)
    run()
    return page._editors[-1]


def _type(dlg, text):
    """Replace the editor's text as typing does (the document counts as changed)."""
    dlg.edit.selectAll()
    dlg.edit.insertPlainText(text)


def _answers(monkeypatch, answers):
    asked = []

    def question(*args, **_kw):
        asked.append(args[2])
        return answers.pop(0)

    monkeypatch.setattr(fb.QMessageBox, "question", staticmethod(question))
    return asked


def test_the_editor_asks_before_replacing_a_device_file_that_changed(qapp, monkeypatch):
    """An app rewrote the file while it was open in the editor; Ctrl+S put the
    editor's text over it without a word."""
    run = _queued_jobs(monkeypatch)
    shown = _boxes(monkeypatch)
    device = _TextDevice("/data/misc/audio.conf", b"volume=3\n")
    page = _page(monkeypatch, device)
    try:
        logs = []
        page.log.connect(logs.append)
        dlg = _edit_volume(page, device, run)
        device.written(b"volume=9\n")  # by an app on the device
        _type(dlg, "volume=4\n")

        asked = _answers(monkeypatch, [fb.QMessageBox.No])
        dlg._save()
        run()
        assert len(asked) == 1 and "changed on the device" in asked[0]
        assert device.content == b"volume=9\n" and device.saves == []
        assert dlg.is_dirty() and not dlg.is_saving()  # the text stays, to save elsewhere
        assert shown == []  # not an error
        assert any(line.startswith("[INFO] audio.conf was not saved") for line in logs)

        asked = _answers(monkeypatch, [fb.QMessageBox.Yes])
        dlg._save()
        run()
        assert len(asked) == 1 and device.content == b"volume=4\n"
        assert not dlg.is_dirty() and not dlg.is_saving()
        dlg.done(0)
    finally:
        page.close_panel()


def test_the_editor_only_asks_about_changes_it_did_not_make(qapp, monkeypatch):
    run = _queued_jobs(monkeypatch)
    device = _TextDevice("/data/misc/audio.conf", b"volume=3\n")
    page = _page(monkeypatch, device)
    try:
        dlg = _edit_volume(page, device, run)
        asked = _answers(monkeypatch, [])
        for text in ("volume=4\n", "volume=5\n"):  # each save is the file's new state
            _type(dlg, text)
            dlg._save()
            run()
        assert asked == [] and device.saves == [b"volume=4\n", b"volume=5\n"]

        device.written(b"volume=9\n")  # after a Yes the editor's own save is the state again
        asked = _answers(monkeypatch, [fb.QMessageBox.Yes])
        for text in ("volume=6\n", "volume=7\n"):
            _type(dlg, text)
            dlg._save()
            run()
        assert len(asked) == 1 and device.content == b"volume=7\n"
        dlg.done(0)
    finally:
        page.close_panel()


def test_save_and_close_keeps_the_editor_open_when_the_answer_is_no(qapp, monkeypatch):
    run = _queued_jobs(monkeypatch)
    _boxes(monkeypatch)
    device = _TextDevice("/data/misc/audio.conf", b"volume=3\n")
    page = _page(monkeypatch, device)
    try:
        dlg = _edit_volume(page, device, run)
        closed = []
        dlg.finished.connect(closed.append)
        device.written(b"volume=9\n")
        _type(dlg, "volume=4\n")
        _answers(monkeypatch, [fb.QMessageBox.No])
        dlg._save_and_close()
        run()
        assert closed == [] and device.content == b"volume=9\n"
        _answers(monkeypatch, [fb.QMessageBox.Yes])
        dlg._save_and_close()
        run()
        assert closed == [fb.QDialog.Accepted] and device.content == b"volume=4\n"
    finally:
        page.close_panel()


def test_a_save_the_device_refuses_asks_again_about_the_change(qapp, monkeypatch):
    """Yes was for the save that failed: the next one compares again."""
    run = _queued_jobs(monkeypatch)
    shown = _boxes(monkeypatch)
    device = _TextDevice("/data/misc/audio.conf", b"volume=3\n")
    page = _page(monkeypatch, device)
    try:
        dlg = _edit_volume(page, device, run)
        device.written(b"volume=9\n")
        _type(dlg, "volume=4\n")
        refused = [ADBError("adb: error: failed to copy: connection reset")]

        def replace_file(local, path, mode="", safe=None):
            if refused:
                raise refused.pop()
            return _TextDevice.replace_file(device, local, path, mode)

        monkeypatch.setattr(device, "replace_file", replace_file)
        asked = _answers(monkeypatch, [fb.QMessageBox.Yes, fb.QMessageBox.No])
        dlg._save()
        run()
        assert [kind for kind, _text in shown] == ["critical"] and device.content == b"volume=9\n"
        dlg._save()
        run()
        assert len(asked) == 2 and device.content == b"volume=9\n"
        dlg.done(0)
    finally:
        page.close_panel()


# --------------------------------------------------------------------------- #
# the pane changes folder while Delete's or Rename's dialog is open
# --------------------------------------------------------------------------- #
class _Recorder:
    """A device that records what it is asked to delete and to run."""

    def __init__(self):
        self.removed, self.commands = [], []

    def remove(self, paths, *, recursive=False, safe=None):
        self.removed.append(list(paths))
        return list(paths)

    def shell(self, command, timeout=None, safe=None):
        self.commands.append(command)
        return _Res("free\n")


def _listed(page, folder, names):
    """Show *names* (folders) as the listing of the device *folder*."""
    page.remote_cwd = page.remote_table.base_dir = folder
    rows = [(name, 0, "<DIR>", "Folder", "2026-09-01 10:00", "drwxrwx--x", "u0_a1", True)
            for name in names]
    page._populate(page.remote_table, rows, parent_row=True)


def _select_remote(page, name):
    table = page.remote_table
    [row] = [r for r in range(table.rowCount()) if table.item(r, 0).text() == name]
    table.selectRow(row)


def test_a_delete_confirmed_after_the_pane_jumped_deletes_nothing(qapp, monkeypatch):
    """A write-access retry opened /data/data while "Permanently delete?" was
    up; Yes then removed the selected name from /data/data, as root."""
    _sync_jobs(monkeypatch)
    device = _Recorder()
    page = _page(monkeypatch, device)
    try:
        _listed(page, "/sdcard/Android/data", ["com.example.app"])
        _select_remote(page, "com.example.app")
        logs = []
        page.log.connect(logs.append)

        def question(*_args, **_kw):
            page._jump_remote("/data/data")  # the retry, while the question is open
            return fb.QMessageBox.Yes

        monkeypatch.setattr(fb.QMessageBox, "question", staticmethod(question))
        page._remote_delete()
        assert device.removed == []
        assert any("Delete cancelled" in line and "/data/data" in line for line in logs)
    finally:
        page.close_panel()


def test_a_rename_typed_after_the_pane_jumped_renames_nothing(qapp, monkeypatch):
    _sync_jobs(monkeypatch)
    device = _Recorder()
    page = _page(monkeypatch, device)
    try:
        _listed(page, "/sdcard/Android/data", ["com.example.app"])
        _select_remote(page, "com.example.app")
        logs = []
        page.log.connect(logs.append)

        def typed(*_args, **_kw):
            page._jump_remote("/data/data")
            return "com.example.old", True

        monkeypatch.setattr(fb.QInputDialog, "getText", staticmethod(typed))
        page._remote_rename()
        assert device.commands == []  # neither the check nor the mv
        assert any("Rename cancelled" in line and "/data/data" in line for line in logs)
    finally:
        page.close_panel()


def test_delete_and_rename_act_in_the_folder_the_rows_were_in(qapp, monkeypatch):
    """A refresh of the same folder while the dialog is open changes nothing:
    the names and their folder were taken before it opened."""
    _sync_jobs(monkeypatch)
    device = _Recorder()
    page = _page(monkeypatch, device)
    try:
        def relisted(answer):
            def dialog(*_args, **_kw):
                _listed(page, "/sdcard/Android/data", ["other.app"])  # the folder, listed again
                return answer
            return staticmethod(dialog)

        _listed(page, "/sdcard/Android/data", ["com.example.app"])
        _select_remote(page, "com.example.app")
        monkeypatch.setattr(fb.QMessageBox, "question", relisted(fb.QMessageBox.Yes))
        page._remote_delete()
        assert device.removed == [["/sdcard/Android/data/com.example.app"]]

        _listed(page, "/sdcard/Android/data", ["com.example.app"])
        _select_remote(page, "com.example.app")
        monkeypatch.setattr(fb.QInputDialog, "getText", relisted(("com.example.old", True)))
        page._remote_rename()
        old, new = "/sdcard/Android/data/com.example.app", "/sdcard/Android/data/com.example.old"
        check, rename = device.commands[-2:]
        assert check == fb._rename_check_cmd(old, new)
        assert rename == fb._marked(fb._rename_cmd(old, new))
    finally:
        page.close_panel()


# --------------------------------------------------------------------------- #
# a device that refuses changes: what was refused is said, offered and retried
# --------------------------------------------------------------------------- #
class _Phone(ADBHandler):
    """A device with files behind a scripted shell that understands what the
    Files tab and the engine send: the path probe, rm, mkdir, touch, cp and mv.

    *protected* items can't be removed and *read_only* folders take no change
    until :attr:`rooted` is set.  With *exit_codes* off every command ends 0
    with its errors on standard output, as on a device without the shell
    protocol (before Android 7)."""

    def __init__(self, files, *, exit_codes=True):
        super().__init__(ADBConfig(serial="x"))
        self.files = dict(files)  # path -> "d" or "f"
        self.protected, self.read_only = set(), set()
        self.rooted = False
        self.exit_codes = exit_codes
        self.commands = []

    def shell(self, command, timeout=None, safe=None, **_kw):
        self.commands.append(command)
        inner, mark = command, ""
        head, found, tail = command.rpartition(" && echo ")
        if found and head.startswith("{ ") and head.endswith("; }"):
            inner, mark = head[2:-3], tail  # "{ command; } && echo MARK"
        said = self._run(inner)
        ok = not said or inner.startswith(("for f in ", "if [ -e "))
        out = said + "\n" if said else ""
        if ok and mark:
            out += mark + "\n"
        if ok or not self.exit_codes:
            return CommandResult(command, 0, out, "", 0.0)
        return CommandResult(command, 1, "", said, 0.0)

    def _run(self, command):
        """What the command prints: the probe's lines, or a tool's complaint."""
        if command.startswith("for f in "):
            paths = shlex.split(command.split("; do ", 1)[0])[3:]
            return "\n".join(self._probed(path) for path in paths)
        if command.startswith("if [ -e "):
            return "free"  # the rename check
        if command.startswith("rm -rf "):
            said = [self._remove(path) for path in shlex.split(command)[2:]]
            return "\n".join(line for line in said if line)
        if command.startswith("mkdir -p "):
            return self._create(shlex.split(command)[2], "d", "mkdir")
        if command.startswith("touch "):
            return self._create(shlex.split(command)[1], "f", "touch")
        if command.startswith("cp -r -- "):
            source, target = shlex.split(command)[3:5]
            return self._create(target, self.files[source], "cp")
        moved = re.search(r"mv -n -T -- (\S+) (\S+)", command)
        if moved:
            said = self._create(moved.group(2), self.files[moved.group(1)], "mv")
            return said or self._remove(moved.group(1))
        return ""

    def _probed(self, path):
        kind = self.files.get(path, "n")
        return f"{kind}- {path if kind == 'd' else ''}"

    def _refuses(self, path):
        folder = posixpath.dirname(path)
        return not self.rooted and (path in self.protected or folder in self.read_only)

    def _remove(self, path):
        if self._refuses(path):
            why = "Read-only file system" if path not in self.protected else "Permission denied"
            return f"rm: {path}: {why}"
        for item in [p for p in self.files if p == path or p.startswith(path + "/")]:
            del self.files[item]
        return ""

    def _create(self, path, kind, tool):
        if self._refuses(path):
            return f"{tool}: '{path}': Read-only file system"
        self.files[path] = kind
        return ""


def _show(page, phone, folder):
    """List what *phone* holds in *folder*, as the device pane would."""
    names = sorted(posixpath.basename(path) for path in phone.files
                   if posixpath.dirname(path) == folder)
    rows = [(name, 0, "<DIR>" if phone.files[f"{folder}/{name}"] == "d" else "0 B",
             "Folder" if phone.files[f"{folder}/{name}"] == "d" else "File",
             "2026-09-01 10:00", "drwxrwx--x", "root", phone.files[f"{folder}/{name}"] == "d")
            for name in names]
    page.remote_cwd = page.remote_table.base_dir = folder
    page._populate(page.remote_table, rows, parent_row=True)


def _yes(monkeypatch):
    monkeypatch.setattr(fb.QMessageBox, "question",
                        staticmethod(lambda *_args, **_kw: fb.QMessageBox.Yes))


TMP = "/data/local/tmp"


def test_a_delete_retried_with_root_removes_what_the_first_try_left(qapp, monkeypatch):
    """rm removed two files and was refused a root-owned folder.  The retry
    after adb root asked for all three again, found the first one gone and
    stopped with "no such file or folder": the folder was never deleted."""
    _sync_jobs(monkeypatch)
    _yes(monkeypatch)
    phone = _Phone({TMP: "d", f"{TMP}/a.log": "f", f"{TMP}/b.log": "f", f"{TMP}/rootdir": "d",
                    f"{TMP}/rootdir/core": "f"})
    phone.protected = {f"{TMP}/rootdir"}
    page = _page(monkeypatch, phone)
    try:
        requests, logs = [], []
        page.write_access_needed.connect(requests.append)
        page.log.connect(logs.append)
        _show(page, phone, TMP)
        page.remote_table.select_all_entries()
        page._remote_delete()
        assert sorted(phone.files) == [TMP, f"{TMP}/rootdir", f"{TMP}/rootdir/core"]
        [request] = requests
        assert request["path"] == TMP and "Permission denied" in request["error"]

        phone.rooted = True  # adb root, then "try again"
        logs.clear()
        request["retry"]()
        assert sorted(phone.files) == [TMP]
        assert logs == ["[OK] delete 3 device item(s)"] and len(requests) == 1
    finally:
        page.close_panel()


# --------------------------------------------------------------------------- #
# a device whose shell gives no exit status (before Android 7)
# --------------------------------------------------------------------------- #
def _new_folder(page, monkeypatch):
    monkeypatch.setattr(fb.QInputDialog, "getText", staticmethod(lambda *a, **k: ("x", True)))
    page._remote_mkdir()


def _new_file(page, monkeypatch):
    monkeypatch.setattr(fb.QInputDialog, "getText", staticmethod(lambda *a, **k: ("x", True)))
    page._remote_newfile()


def _paste(page, _monkeypatch):
    page._clipboard, page._clipboard_src = ["/sdcard/x"], "remote"
    page._remote_paste()


def _rename(page, monkeypatch):
    monkeypatch.setattr(fb.QInputDialog, "getText", staticmethod(lambda *a, **k: ("x", True)))
    [row] = [r for r in range(page.remote_table.rowCount())
             if page.remote_table.item(r, 0).text() == "old"]
    page.remote_table.selectRow(row)
    page._remote_rename()


@pytest.mark.parametrize("exit_codes", [False, True], ids=["no-exit-status", "shell-protocol"])
@pytest.mark.parametrize("change, label", [
    (_new_folder, "mkdir x"), (_new_file, "create x"), (_paste, "copy x"), (_rename, "rename old"),
])
def test_a_change_the_device_refused_is_never_reported_as_done(qapp, monkeypatch, change, label,
                                                              exit_codes):
    """Before Android 7 `adb shell` ends 0 whatever the command did: New
    folder, Paste and Rename in a read-only folder logged "[OK]", changed
    nothing, and adb root / remount was never offered."""
    _sync_jobs(monkeypatch)
    _boxes(monkeypatch)
    phone = _Phone({"/system": "d", "/system/old": "f", "/sdcard": "d", "/sdcard/x": "f"},
                   exit_codes=exit_codes)
    phone.read_only = {"/system"}
    page = _page(monkeypatch, phone)
    try:
        requests, logs = [], []
        page.write_access_needed.connect(requests.append)
        page.log.connect(logs.append)
        _show(page, phone, "/system")
        change(page, monkeypatch)
        assert "/system/x" not in phone.files
        [said] = logs
        assert said.startswith(f"[ERROR] {label}: ") and said.endswith(
            "'/system/x': Read-only file system")
        [request] = requests
        assert request["path"] == "/system" and "Read-only file system" in request["error"]

        phone.rooted = True  # adb root and remount, then "try again"
        logs.clear()
        request["retry"]()
        assert "/system/x" in phone.files
        assert logs == [f"[OK] {label}"] and len(requests) == 1
    finally:
        page.close_panel()


@pytest.mark.parametrize("exit_codes", [False, True], ids=["no-exit-status", "shell-protocol"])
def test_a_delete_the_device_refused_is_never_reported_as_done(qapp, monkeypatch, exit_codes):
    """…and Delete logged "[OK] delete 2 device item(s)" with both still there."""
    _sync_jobs(monkeypatch)
    _yes(monkeypatch)
    phone = _Phone({TMP: "d", f"{TMP}/a.log": "f", f"{TMP}/rootdir": "d"}, exit_codes=exit_codes)
    phone.protected = {f"{TMP}/rootdir"}
    page = _page(monkeypatch, phone)
    try:
        requests, logs = [], []
        page.write_access_needed.connect(requests.append)
        page.log.connect(logs.append)
        _show(page, phone, TMP)
        page.remote_table.select_all_entries()
        page._remote_delete()
        assert sorted(phone.files) == [TMP, f"{TMP}/rootdir"]
        assert len(logs) == 1 and logs[0].startswith("[ERROR] delete 2 device item(s): ")
        assert f"rm: {TMP}/rootdir: Permission denied" in logs[0]
        [request] = requests
        assert request["path"] == TMP and "Permission denied" in request["error"]

        phone.rooted = True
        logs.clear()
        request["retry"]()
        assert sorted(phone.files) == [TMP]
        assert logs == ["[OK] delete 2 device item(s)"] and len(requests) == 1
    finally:
        page.close_panel()


def test_a_change_that_worked_is_reported_as_done_without_an_exit_status(qapp, monkeypatch):
    _sync_jobs(monkeypatch)
    _yes(monkeypatch)
    phone = _Phone({TMP: "d", f"{TMP}/a.log": "f"}, exit_codes=False)
    page = _page(monkeypatch, phone)
    try:
        logs = []
        page.log.connect(logs.append)
        _show(page, phone, TMP)
        _new_folder(page, monkeypatch)
        _show(page, phone, TMP)
        page.remote_table.select_all_entries()
        page._remote_delete()
        assert logs == ["[OK] mkdir x", "[OK] delete 2 device item(s)"]
        assert sorted(phone.files) == [TMP]
    finally:
        page.close_panel()


def test_a_name_that_holds_the_mark_does_not_pass_for_success(qapp, monkeypatch):
    """The device's complaint names the path: only the mark on a line of its
    own says the command worked."""
    _sync_jobs(monkeypatch)
    phone = _Phone({"/system": "d"}, exit_codes=False)
    phone.read_only = {"/system"}
    page = _page(monkeypatch, phone)
    try:
        logs = []
        page.log.connect(logs.append)
        _show(page, phone, "/system")
        monkeypatch.setattr(fb.QInputDialog, "getText",
                            staticmethod(lambda *a, **k: (fb._DONE_MARK, True)))
        page._remote_mkdir()
        assert len(logs) == 1 and logs[0].startswith(f"[ERROR] mkdir {fb._DONE_MARK}: ")
    finally:
        page.close_panel()
