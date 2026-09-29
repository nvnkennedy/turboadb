"""The Files tab's PC pane: renaming, saving from the built-in editor, copying
folders, the quick jumps and opening files.

Headless (Qt offscreen); every file lives in the test's temporary folder."""
from __future__ import annotations

import errno
import os
import stat
import types

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QStandardPaths, Qt  # noqa: E402

from turboadb.gui import file_browser as fb  # noqa: E402
from turboadb.gui import fileutil  # noqa: E402


def _sync_jobs(monkeypatch):
    """Run the page's jobs inline (fn, then its callback) instead of on QThreads."""
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


def _boxes(monkeypatch, answer=None):
    shown = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(fb.QMessageBox, kind,
                            staticmethod(lambda *a, _k=kind, **k: shown.append((_k, a[2]))))
    monkeypatch.setattr(fb.QMessageBox, "question",
                        staticmethod(lambda *a, **k: shown.append(("question", a[2]))
                                     or (answer if answer is not None else fb.QMessageBox.No)))
    return shown


@pytest.fixture
def page(qapp, monkeypatch, tmp_path):
    """A Files page on *tmp_path*, its jobs run inline, nothing listed on show."""
    _sync_jobs(monkeypatch)
    monkeypatch.setattr(fb.FileBrowser, "refresh_remote", lambda self: None)
    browser = fb.FileBrowser(None, local_start=str(tmp_path))
    try:
        yield browser
    finally:
        browser.close_panel()


def _select(browser, name):
    table = browser.local_table
    for row in range(table.rowCount()):
        data = table.item(row, 0).data(Qt.UserRole)
        if data and data[0] == name:
            table.selectRow(row)
            return
    raise AssertionError(f"{name!r} is not listed")


def _rename(browser, monkeypatch, old, new):
    monkeypatch.setattr(fb.QInputDialog, "getText", staticmethod(lambda *a, **k: (new, True)))
    browser.refresh_local()
    _select(browser, old)
    browser._local_rename()


# --------------------------------------------------------------------------- #
# rename
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("typed", ["../a.txt", "sub/a.txt", os.path.join("sub", "b.txt")])
def test_rename_takes_a_name_not_a_path(page, monkeypatch, tmp_path, typed):
    """"../a.txt" moved the file out of the folder on screen."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("a")
    shown = _boxes(monkeypatch)
    _rename(page, monkeypatch, "a.txt", typed)
    assert (tmp_path / "a.txt").read_text() == "a"
    assert sorted(os.listdir(tmp_path / "sub")) == []
    assert [kind for kind, _text in shown] == ["warning"]


def test_rename_asks_before_replacing_a_file(page, monkeypatch, tmp_path):
    """On Linux and macOS os.rename replaced b.txt without a word."""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    shown = _boxes(monkeypatch, answer=fb.QMessageBox.No)
    _rename(page, monkeypatch, "a.txt", "b.txt")
    assert shown and shown[0][0] == "question" and "'b.txt' already exists" in shown[0][1]
    assert (tmp_path / "a.txt").read_text() == "a" and (tmp_path / "b.txt").read_text() == "b"

    _boxes(monkeypatch, answer=fb.QMessageBox.Yes)
    _rename(page, monkeypatch, "a.txt", "b.txt")
    assert not (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").read_text() == "a"


def test_rename_never_replaces_a_folder(page, monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "keep.txt").write_text("keep")
    shown = _boxes(monkeypatch, answer=fb.QMessageBox.Yes)
    _rename(page, monkeypatch, "a.txt", "b")
    assert shown == [("warning", "A folder named 'b' already exists here.")]
    assert (tmp_path / "a.txt").read_text() == "a"
    assert (tmp_path / "b" / "keep.txt").read_text() == "keep"


def test_a_case_only_rename_is_not_asked_about(page, monkeypatch, tmp_path):
    """On Windows and macOS "A.txt" exists already: it is a.txt itself."""
    (tmp_path / "a.txt").write_text("a")
    shown = _boxes(monkeypatch)
    _rename(page, monkeypatch, "a.txt", "A.txt")
    assert shown == []
    assert os.listdir(tmp_path) == ["A.txt"]


def test_a_rename_never_replaces_what_appeared_after_the_check(tmp_path):
    old, new = tmp_path / "a.txt", tmp_path / "b.txt"
    old.write_text("a")
    assert fb._local_rename_state(str(old), str(new)) == "free"
    new.write_text("b")  # created while the user looked at the prompt
    with pytest.raises(FileExistsError):
        fb._rename_local_item(str(old), str(new))
    assert new.read_text() == "b" and old.read_text() == "a"


# --------------------------------------------------------------------------- #
# saving from the editor
# --------------------------------------------------------------------------- #
def _open_editor(page, qapp, name):
    page.refresh_local()
    _select(page, name)
    page._local_edit()
    return page._editors[-1]


def test_a_save_that_fails_leaves_the_file_as_it_was(page, qapp, monkeypatch, tmp_path):
    """The editor opened the real file for writing - emptying it - and then
    wrote: whatever failed half-way (a full disk, a dropped network drive, a
    character that can't be written) left the file empty or cut short."""
    path = tmp_path / "app.conf"
    path.write_bytes(b"key=value\n")
    shown = _boxes(monkeypatch)
    dlg = _open_editor(page, qapp, "app.conf")
    try:
        dlg.on_save("key=\ud800\n")  # a lone surrogate: UTF-8 can't write it
        assert [kind for kind, _text in shown] == ["critical"]
        assert path.read_bytes() == b"key=value\n"
    finally:
        dlg.done(0)


def test_saving_replaces_the_text_and_keeps_line_endings(page, qapp, tmp_path):
    path = tmp_path / "win.ini"
    path.write_bytes(b"[a]\r\nx=1\r\n")
    dlg = _open_editor(page, qapp, "win.ini")
    try:
        dlg.edit.setPlainText("[a]\nx=2\n")
        dlg._save()
        assert path.read_bytes() == b"[a]\r\nx=2\r\n"
        assert os.listdir(tmp_path) == ["win.ini"]  # no temporary file left behind
    finally:
        dlg.done(0)


def test_a_full_drive_is_found_before_the_file_is_touched(monkeypatch, tmp_path):
    """Where the file has to be written in place, the drive must have room first."""
    path = tmp_path / "big.txt"
    path.write_text("old")
    monkeypatch.setattr(fb, "_replace_keeps_the_file", lambda _path: False)
    monkeypatch.setattr(fb.shutil, "disk_usage", lambda _path: types.SimpleNamespace(free=0))
    with pytest.raises(OSError) as raised:
        fb._save_local_text(str(path), "much longer than the old text")
    assert raised.value.errno == errno.ENOSPC
    assert path.read_text() == "old"


def test_a_file_with_another_hard_link_is_written_in_place(tmp_path):
    """Replacing it would leave the other name with the old text."""
    path, other = tmp_path / "a.txt", tmp_path / "b.txt"
    path.write_text("old")
    try:
        os.link(str(path), str(other))
    except (OSError, NotImplementedError):
        pytest.skip("no hard links here")
    fb._save_local_text(str(path), "new")
    assert other.read_text() == "new"


@pytest.mark.skipif(os.name == "nt", reason="permission bits and links as on Linux and macOS")
def test_saving_keeps_the_mode_and_a_link(tmp_path):
    real = tmp_path / "real.sh"
    real.write_text("echo old\n")
    os.chmod(str(real), 0o750)
    link = tmp_path / "run.sh"
    os.symlink(str(real), str(link))
    fb._save_local_text(str(link), "echo new\n")
    assert os.path.islink(str(link)) and real.read_text() == "echo new\n"
    assert stat.S_IMODE(os.stat(str(real)).st_mode) == 0o750


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="a read-only file as a normal user on Linux and macOS")
def test_a_read_only_file_is_not_replaced_behind_its_back(tmp_path):
    path = tmp_path / "locked.conf"
    path.write_text("old")
    os.chmod(str(path), 0o444)
    with pytest.raises(PermissionError):
        fb._save_local_text(str(path), "new")
    assert path.read_text() == "old"


# --------------------------------------------------------------------------- #
# copying on the PC
# --------------------------------------------------------------------------- #
def test_a_file_is_never_copied_into_a_folder_of_its_name(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "notes").write_text("n")
    dst = tmp_path / "dst"
    (dst / "notes").mkdir(parents=True)
    assert fb._find_local_collisions([str(src / "notes")], str(dst)) == []  # nothing to ask
    errors = fb._copy_local_items([str(src / "notes")], str(dst))
    assert len(errors) == 1 and "a folder with that name is already there" in errors[0]
    assert os.listdir(dst / "notes") == []


def _link_folder(target, link):
    """A folder link (a junction on Windows, which needs no privilege)."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link))


def test_a_link_back_into_the_folder_being_copied_is_left_out(tmp_path):
    """Followed, it copied the folder into itself until the path was too long."""
    src = tmp_path / "album"
    (src / "inner").mkdir(parents=True)
    (src / "inner" / "a.jpg").write_text("a")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "b.jpg").write_text("b")
    try:
        _link_folder(src, src / "inner" / "up")
        _link_folder(elsewhere, src / "inner" / "other")
    except OSError:
        pytest.skip("folder links can't be made here")
    dst = tmp_path / "dst"
    dst.mkdir()
    errors = fb._copy_local_items([str(src)], str(dst))
    assert len(errors) == 1 and "links back into the folder" in errors[0]
    copied = dst / "album" / "inner"
    assert sorted(os.listdir(copied)) == ["a.jpg", "other"]
    assert (copied / "other" / "b.jpg").read_text() == "b"  # other links are still followed


# --------------------------------------------------------------------------- #
# quick jumps and openers
# --------------------------------------------------------------------------- #
def test_quick_jumps_open_the_folders_where_the_system_keeps_them(qapp, monkeypatch, tmp_path):
    """With OneDrive backing up the Desktop it is ~\\OneDrive\\Desktop, and
    ~/Desktop does not exist."""
    desktop = tmp_path / "OneDrive" / "Desktop"
    desktop.mkdir(parents=True)
    downloads = tmp_path / "Downloads here"
    downloads.mkdir()
    where = {QStandardPaths.DesktopLocation: str(desktop),
             QStandardPaths.DownloadLocation: str(downloads)}
    monkeypatch.setattr(QStandardPaths, "writableLocation",
                        staticmethod(lambda kind: where.get(kind, "")))
    browser = fb.FileBrowser(None)
    try:
        jumps = []
        monkeypatch.setattr(browser, "_jump_local", jumps.append)
        for label in ("Desktop", "Downloads"):
            [button] = [b for b in browser.findChildren(fb.QPushButton) if b.text() == label]
            button.click()
        assert jumps == [str(desktop), str(downloads)]
    finally:
        browser.close_panel()


def test_a_missing_known_folder_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(QStandardPaths, "writableLocation",
                        staticmethod(lambda kind: str(tmp_path / "gone")))
    assert fileutil.desktop_dir() == os.path.expanduser("~")
    (tmp_path / "Desktop").mkdir()
    assert fileutil.desktop_dir() == os.path.join(os.path.expanduser("~"), "Desktop")


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_openers_are_waited_for(monkeypatch, tmp_path, platform):
    """xdg-open and open exit at once; nobody waited for them, so each one
    stayed behind as a zombie process."""
    import threading

    waited = threading.Event()

    class Opener:
        def __init__(self, argv):
            self.argv = argv

        def wait(self):
            waited.set()
            return 0

    monkeypatch.setattr(fileutil, "sys", types.SimpleNamespace(platform=platform))
    monkeypatch.setattr(fileutil, "subprocess", types.SimpleNamespace(Popen=Opener))
    fileutil.open_path(str(tmp_path / "a.txt"))
    assert waited.wait(5)
    waited.clear()
    fileutil.reveal_path(str(tmp_path / "a.txt"))
    assert waited.wait(5)
