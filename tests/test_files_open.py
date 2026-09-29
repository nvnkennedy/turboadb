"""Open files from the Files tab in the apps this PC has for them.

A double click (or Enter, or Open) only ever showed TurboADB's text editor,
which turned pictures, videos and anything big away, so a PNG or a video on
the device could not even be looked at. Now a file opens in its own app; a
device file as a copy on this PC whose saves go back to the device, keeping
its permissions and asking first when the device file changed meanwhile.
Programs are never started, and scripts, which a double click would run,
open in the editor. Everything is offline: the device is a small fake."""

import os
import shlex

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QObject, Qt, pyqtSignal  # noqa: E402

from turboadb import ADBConfig, ADBHandler  # noqa: E402
from turboadb.exceptions import ADBError  # noqa: E402
from turboadb.gui import file_browser as fb  # noqa: E402
from turboadb.gui import file_open  # noqa: E402

PICTURE = "/sdcard/Pictures/cat.png"


class _Res:
    def __init__(self, stdout="", ok=True):
        self.stdout, self.ok, self.stderr = stdout, ok, ""
        self.text = stdout.strip()


class _Device(ADBHandler):
    """A device with files: stat, pull, push and chmod through a scripted shell."""

    def __init__(self, files=None, mode="660"):
        super().__init__(ADBConfig(serial="x"))
        self.files = dict(files or {PICTURE: b"v1"})
        self.mode, self.mtime = mode, 100
        self.pulls, self.pushes, self.chmods = [], [], []
        self.temps = {}  # copies pushed next to a file, before they are written over it
        self.push_error = None
        self.sdcard_link = False  # /sdcard a link to /storage/emulated/0, as on a phone

    def real(self, path):
        if self.sdcard_link and path.startswith("/sdcard/"):
            return "/storage/emulated/0/" + path[len("/sdcard/"):]
        return path

    def shell(self, cmd, timeout=None, safe=None):
        try:
            path = shlex.split(cmd.split("||")[0].split(";")[0])[-1]
        except (ValueError, IndexError):
            path = ""  # a listing or another command this device does not know
        if "'%s %Y'" in cmd:
            path = self.real(path)
            return _Res(f"{len(self.files[path])} {self.mtime}\r\n") if path in self.files else _Res(ok=False)
        if "'%s %a %F'" in cmd:
            path = self.real(path)
            return _Res(f"{len(self.files[path])} {self.mode} regular file\r\n{path}\r\n")
        if cmd.startswith("stat -c %a"):
            return _Res(f"{self.mode}\r\n")
        if cmd.startswith("chmod"):
            self.chmods.append(shlex.split(cmd)[1:])
        if cmd.startswith("cat "):  # the saved copy written over the file, in place
            src, _into, dst = shlex.split(cmd)[1:4]
            self.files[dst] = self.temps.pop(src)
            self.pushes.append(dst)
            self.mtime += 7
            return _Res("TURBOADB_WRITTEN\n")
        if cmd.startswith("rm -f -- "):
            self.temps.pop(shlex.split(cmd)[-1], None)
        return _Res("")

    def pull(self, remote, local, cancel_event=None, safe=None, **kw):
        self.pulls.append(remote)
        with open(local, "wb") as fh:
            fh.write(self.files[remote])

    def push(self, local, remote, safe=None, **kw):
        if self.push_error:
            raise ADBError(self.push_error)
        with open(local, "rb") as fh:
            self.temps[remote] = fh.read()


class _Transfer(QObject):
    """The transfer thread, done in place: a pull copies at once (or, when
    *hold* is set, waits to be finished or cancelled)."""

    progress = pyqtSignal(int)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)
    finished = pyqtSignal()
    hold = False
    held = []

    def __init__(self, handler, direction, a, b):
        super().__init__()
        self.handler, self.direction, self.a, self.b = handler, direction, a, b

    def start(self):
        if _Transfer.hold:
            _Transfer.held.append(self)
            return
        self.handler.pull(self.a, self.b)
        self.done.emit("ok")

    def stop(self):
        pass


@pytest.fixture
def page(app, monkeypatch):
    """A Files page on the fake device, jobs run in place, boxes recorded."""
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
    monkeypatch.setattr(fb, "_TransferThread", _Transfer)
    monkeypatch.setattr(fb, "park_thread", lambda t: None)
    _Transfer.hold, _Transfer.held = False, []
    boxes = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(fb.QMessageBox, kind,
                            staticmethod(lambda *a, _k=kind, **k: boxes.append((_k, a[2]))))
    answers = []
    monkeypatch.setattr(fb.QMessageBox, "question",
                        staticmethod(lambda *a, **k: boxes.append(("question", a[2]))
                                     or (answers.pop(0) if answers else fb.QMessageBox.No)))
    started = []
    monkeypatch.setattr(file_open, "start", lambda path, choose=False: started.append((path, choose)))
    monkeypatch.setattr(file_open, "app_for", lambda name: "Photos")
    handler = _Device()
    browser = fb.FileBrowser(handler)
    logs = []
    browser.log.connect(logs.append)
    browser.remote_cwd = "/sdcard/Pictures"
    browser.test = type("T", (), dict(handler=handler, boxes=boxes, answers=answers,
                                      started=started, logs=logs))
    yield browser
    browser.close_panel()


def _rows(table, browser, names, perms="-rw-rw----"):
    rows = [(n, 2, "2 B", "File", "", perms, "u0_a1:media_rw", False) for n in names]
    browser._populate(table, rows, parent_row=False)


def _row(table, name):
    for r in range(table.rowCount()):
        it = table.item(r, 0)
        if it and it.data(Qt.UserRole) and it.data(Qt.UserRole)[0] == name:
            table.clearSelection()
            table.selectRow(r)
            return r
    raise AssertionError(f"{name!r} not listed")


_SAVES = []


def _saved_in_app(path, content):
    """Write *content* as an app saving *path* would, each save seconds after
    the last (Windows keeps file times in steps of about 15 ms, and two saves
    that close together would get the same one)."""
    with open(path, "wb") as fh:
        fh.write(content)
    _SAVES.append(path)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + len(_SAVES) * 5_000_000_000))


def _open_picture(page):
    _rows(page.remote_table, page, ["cat.png"])
    page._remote_open_row(_row(page.remote_table, "cat.png"))
    (local, choose), = page.test.started
    return local


# --------------------------------------------------------------------------- #
# what a file is
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name, kind", [
    ("setup.exe", file_open.PROGRAM), ("Setup.MSI", file_open.PROGRAM),
    ("tool.pyz", file_open.PROGRAM), ("addin.xll", file_open.PROGRAM),
    ("desk.rdp", file_open.PROGRAM), ("start.jnlp", file_open.PROGRAM),
    ("app-release.apk", file_open.PROGRAM), ("libfoo.so", file_open.PROGRAM),
    ("Link.lnk", file_open.PROGRAM), ("run.bat", file_open.SCRIPT), ("tool.sh", file_open.SCRIPT),
    ("fix.reg", file_open.SCRIPT), ("x.py", file_open.SCRIPT), ("cat.png", file_open.DOCUMENT),
    ("notes.txt", file_open.DOCUMENT), ("clip.mp4", file_open.DOCUMENT), ("hosts", file_open.DOCUMENT),
])
def test_what_kind_of_file_it_is(name, kind):
    assert file_open.kind_of(name) == kind


def test_a_type_no_app_opens_has_no_app():
    assert file_open.app_for("build.qqqzzz") is None
    assert file_open.app_for("Makefile") is None


# --------------------------------------------------------------------------- #
# files on this PC
# --------------------------------------------------------------------------- #
def test_a_pc_file_opens_in_its_app(page, tmp_path):
    (tmp_path / "cat.png").write_bytes(b"png")
    page.local_cwd = str(tmp_path)
    _rows(page.local_table, page, ["cat.png"])
    row = _row(page.local_table, "cat.png")
    page._on_local_double_click(row, 0)
    page._local_open(choose=True)
    path = str(tmp_path / "cat.png")
    assert page.test.started == [(path, False), (path, True)]
    assert page.test.boxes == []


@pytest.mark.parametrize("name, word", [
    ("setup.exe", "program"), ("app.apk", "Apps tab"), ("Link.lnk", "shortcut"),
])
def test_programs_are_never_started(page, tmp_path, name, word):
    page.local_cwd = str(tmp_path)
    for table in (page.local_table, page.remote_table):
        _rows(table, page, [name])
    page._local_open_row(_row(page.local_table, name))
    page._remote_open_row(_row(page.remote_table, name))
    assert page.test.started == [] and page.test.handler.pulls == []
    assert [k for k, _t in page.test.boxes] == ["information", "information"]
    assert all(word in text for _k, text in page.test.boxes)
    page._local_open_row(_row(page.local_table, name), choose=True)  # the user picks the app
    assert page.test.started == [(os.path.join(str(tmp_path), name), True)]


def test_scripts_and_types_without_an_app_open_in_the_editor(page, monkeypatch, tmp_path):
    page.local_cwd = str(tmp_path)
    edited = []
    monkeypatch.setattr(page, "_local_edit_row", lambda row, hint="": edited.append(("local", hint)))
    monkeypatch.setattr(page, "_remote_edit_row", lambda row, hint="": edited.append(("remote", hint)))
    for table in (page.local_table, page.remote_table):
        _rows(table, page, ["run.bat", "build.prop"])
    page._local_open_row(_row(page.local_table, "run.bat"))
    page._remote_open_row(_row(page.remote_table, "run.bat"))
    monkeypatch.setattr(file_open, "app_for", lambda name: None)
    page._local_open_row(_row(page.local_table, "build.prop"))
    page._remote_open_row(_row(page.remote_table, "build.prop"))
    hint = page._NO_APP_HINT
    assert edited == [("local", ""), ("remote", ""), ("local", hint), ("remote", hint)]
    assert page.test.started == []
    assert any("run.bat is a script" in line for line in page.test.logs)


def test_the_editor_names_open_with_when_it_turns_a_file_away(page, tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary")
    page.local_cwd = str(tmp_path)
    _rows(page.local_table, page, ["blob.bin"])
    page._local_edit_row(_row(page.local_table, "blob.bin"), page._NO_APP_HINT)
    (kind, text), = page.test.boxes
    assert "binary" in text and "Open with" in text


# --------------------------------------------------------------------------- #
# files on the device
# --------------------------------------------------------------------------- #
def test_a_device_file_opens_as_a_copy_on_this_pc(page):
    local = _open_picture(page)
    assert page.test.handler.pulls == [PICTURE]
    assert os.path.basename(local) == "cat.png"
    assert local.startswith(file_open.copies_root())
    with open(local, "rb") as fh:
        assert fh.read() == b"v1"
    assert any(f"sends it back to {PICTURE}" in line for line in page.test.logs)
    # it went through the transfer queue, so a big video shows its progress
    assert page.transfers.stats().done == 1


def test_saving_the_copy_sends_it_back_to_the_device(page):
    local = _open_picture(page)
    device = page.test.handler
    page._copies.check()
    assert device.pushes == []  # nothing saved yet
    _saved_in_app(local, b"v2")
    page._copies.check()  # it changed: looked at again next time
    assert device.pushes == []
    page._copies.check()  # it stayed put: saved
    assert device.files[PICTURE] == b"v2" and device.pushes == [PICTURE]
    assert device.chmods == [] and device.temps == {}  # written over in place: mode kept, no stray copy
    assert any(line.startswith("[OK] Saved cat.png to the device") for line in page.test.logs)
    page._copies.check()
    page._copies.check()
    assert device.pushes == [PICTURE]  # once per save
    _saved_in_app(local, b"v3")
    page._copies.check()
    page._copies.check()
    assert device.files[PICTURE] == b"v3" and len(device.pushes) == 2
    assert page.test.boxes == []


def test_a_device_file_that_changed_meanwhile_is_only_replaced_when_the_user_says_so(page):
    local = _open_picture(page)
    device = page.test.handler
    device.mtime += 30  # something else wrote it on the device
    _saved_in_app(local, b"mine")
    page.test.answers.append(fb.QMessageBox.No)
    page._copies.check()
    page._copies.check()
    assert device.pushes == [] and device.files[PICTURE] == b"v1"
    assert [k for k, _t in page.test.boxes] == ["question"]
    assert "changed on the device" in page.test.boxes[0][1]
    _saved_in_app(local, b"mine, again")
    page.test.answers.append(fb.QMessageBox.Yes)
    page._copies.check()
    page._copies.check()
    assert device.files[PICTURE] == b"mine, again"
    _saved_in_app(local, b"mine, third")  # in step with the device again: no question
    page._copies.check()
    page._copies.check()
    assert device.files[PICTURE] == b"mine, third"
    assert [k for k, _t in page.test.boxes] == ["question", "question"]


def test_a_save_the_device_refuses_says_so_and_keeps_the_copy(page):
    local = _open_picture(page)
    device = page.test.handler
    device.push_error = "adb: error: failed to copy: remote couldn't create file: Is a directory"
    _saved_in_app(local, b"v2")
    page._copies.check()
    page._copies.check()
    (kind, text), = page.test.boxes
    assert kind == "warning" and "could not be saved" in text and "save it again" in text
    assert os.path.exists(local) and device.files[PICTURE] == b"v1"
    device.push_error = None
    _saved_in_app(local, b"v3")
    page._copies.check()
    page._copies.check()
    assert device.files[PICTURE] == b"v3"


def test_cancelling_the_copy_opens_nothing_and_leaves_nothing(page):
    _Transfer.hold = True
    _rows(page.remote_table, page, ["cat.png"])
    page._remote_open_row(_row(page.remote_table, "cat.png"))
    (transfer,) = _Transfer.held
    folder = os.path.dirname(transfer.b)
    assert os.path.isdir(folder)
    page.cancel_transfers()
    transfer.failed.emit("adb pull cancelled")
    assert not os.path.exists(folder) and page.test.started == [] and page.test.boxes == []
    assert page.transfers.retryable() == []  # its folder is gone: nothing to retry


def test_a_copy_that_could_not_be_made_says_so_without_holding_anything_up(page):
    _Transfer.hold = True
    _rows(page.remote_table, page, ["cat.png"])
    page._remote_open_row(_row(page.remote_table, "cat.png"))
    (transfer,) = _Transfer.held
    transfer.failed.emit("adb: error: remote object does not exist")
    assert page.test.started == [] and page.test.boxes == []  # no box stopping the queue
    assert any(line.startswith("[ERROR] cat.png could not be copied") for line in page.test.logs)
    assert page.transfers.retryable() == []  # a Retry would only fail again


def test_closing_the_tab_stops_sending_saves_and_says_so(page):
    local = _open_picture(page)
    page.close_panel()
    _saved_in_app(local, b"v2")
    page._copies.check()
    page._copies.check()
    assert page.test.handler.pushes == []
    assert any("cat.png in its app no longer sends it" in line for line in page.test.logs)


def test_a_save_made_while_the_question_is_open_waits_for_its_answer(page, monkeypatch):
    local = _open_picture(page)
    device = page.test.handler
    device.mtime += 30  # changed on the device meanwhile
    asked = []

    def question(*a, **k):
        asked.append(a[2])
        if len(asked) == 1:  # the user saves again before answering
            _saved_in_app(local, b"newer")
            page._copies.check()
            page._copies.check()
        return fb.QMessageBox.Yes

    monkeypatch.setattr(fb.QMessageBox, "question", staticmethod(question))
    _saved_in_app(local, b"mine")
    page._copies.check()
    page._copies.check()
    assert len(asked) == 1  # one question, not a second one on top
    assert device.files[PICTURE] == b"newer" and device.pushes == [PICTURE]  # the newest save
    page._copies.check()
    page._copies.check()
    assert device.pushes == [PICTURE]


def test_replacing_anyway_is_asked_again_after_a_failed_save(page):
    local = _open_picture(page)
    device = page.test.handler
    device.mtime += 30
    device.push_error = "adb: error: failed to copy: connection reset"
    page.test.answers.append(fb.QMessageBox.Yes)
    _saved_in_app(local, b"one")
    page._copies.check()
    page._copies.check()
    assert [k for k, _t in page.test.boxes] == ["question", "warning"]
    device.push_error = None
    _saved_in_app(local, b"two")  # the answer was for the save that failed
    page._copies.check()
    page._copies.check()
    assert [k for k, _t in page.test.boxes] == ["question", "warning", "question"]
    assert device.files[PICTURE] == b"v1"  # No this time: the device keeps its own


def test_saving_a_file_opened_through_a_link_refreshes_its_folder(page, monkeypatch):
    device = page.test.handler
    device.sdcard_link = True
    real = "/storage/emulated/0/Pictures/cat.png"
    device.files = {real: b"v1"}
    local = _open_picture(page)
    assert device.pulls == [real]
    refreshed = []
    monkeypatch.setattr(page, "refresh_remote", lambda: refreshed.append(page.remote_cwd))
    _saved_in_app(local, b"v2")
    page._copies.check()
    page._copies.check()
    assert device.files[real] == b"v2" and refreshed == ["/sdcard/Pictures"]


def test_types_this_pc_would_run_are_kept_from_their_apps(page, monkeypatch, tmp_path):
    page.local_cwd = str(tmp_path)
    edited = []
    monkeypatch.setattr(page, "_local_edit_row", lambda row, hint="": edited.append(row))
    monkeypatch.setattr(file_open, "runs_what_it_opens",
                        lambda name: {"job.xyz": "py.exe", "tool.abc": "itself"}.get(name, ""))
    _rows(page.local_table, page, ["job.xyz", "tool.abc"])
    page._local_open_row(_row(page.local_table, "job.xyz"))
    page._local_open_row(_row(page.local_table, "tool.abc"))
    assert len(edited) == 1 and page.test.started == []
    assert any("job.xyz would be run by py.exe" in line for line in page.test.logs)
    (kind, text), = page.test.boxes
    assert kind == "information" and "tool.abc is a program" in text


def test_a_name_ending_in_a_line_break_is_never_taken_for_its_neighbour():
    from turboadb.remotefs import _parse_ls_listing

    text = ("drwxrwx--x 2 root sdcard_rw 4096 2026-09-29 16:00 out\n"
            "drwxrwx--x 2 root sdcard_rw 4096 2026-09-29 16:01 out\r\n"
            "-rw-rw---- 1 root sdcard_rw 12 2026-09-29 16:02 notes.txt\n")
    rows, uncertain, clean = _parse_ls_listing(text)
    assert [r[0] for r in rows] == ["out", "out\r", "notes.txt"]
    # "out\r" is refused by the tab: a path to it would be trimmed to "out"
    assert uncertain == {"out\r"} and clean is False


# --------------------------------------------------------------------------- #
# the watcher and the copies' folder
# --------------------------------------------------------------------------- #
def test_the_watcher_reports_a_save_once_it_is_complete(app, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"one")
    copy = file_open.DeviceCopy("notes.txt", str(path), "/sdcard/notes.txt")
    watcher = file_open.CopyWatcher()
    saved = []
    watcher.saved.connect(saved.append)
    watcher.watch(copy)
    watcher.check()
    assert saved == []
    _saved_in_app(str(path), b"two")
    watcher.check()
    assert saved == []  # it may still be being written
    watcher.check()
    assert saved == [copy]
    watcher.check()
    assert saved == [copy]  # once per save
    copy.synced = file_open.stat_key(str(path))  # sent: this is what the device has now
    os.remove(path)  # an app replacing it: gone for a moment
    watcher.check()
    assert saved == [copy]
    _saved_in_app(str(path), b"three")
    watcher.check()
    watcher.check()
    assert saved == [copy, copy]
    watcher.stop()


def test_the_watcher_never_reads_a_copy(app, tmp_path, monkeypatch):
    """Size and time are enough: a copy of a big video must not stall the window."""
    import builtins

    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * 1000)
    copy = file_open.DeviceCopy("clip.mp4", str(path), "/sdcard/clip.mp4")
    watcher = file_open.CopyWatcher()
    saved = []
    watcher.saved.connect(saved.append)
    watcher.watch(copy)
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: (_ for _ in ()).throw(
        AssertionError(f"read {p}")) if str(p) == str(path) else real_open(p, *a, **k))
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
    watcher.check()
    watcher.check()
    assert saved == [copy]
    watcher.stop()


def test_old_copies_are_tidied(tmp_path, monkeypatch):
    monkeypatch.setattr(file_open, "copies_root", lambda: str(tmp_path))
    old, new = file_open.new_copy_folder(), file_open.new_copy_folder()
    past = os.stat(old).st_mtime - 8 * 86400
    os.utime(old, (past, past))
    file_open.forget_old_copies()
    assert not os.path.exists(old) and os.path.isdir(new)
