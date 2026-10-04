"""The Files tab's PC pane: renaming, saving from the built-in editor, copying
folders, the quick jumps and opening files.

Headless (Qt offscreen); every file lives in the test's temporary folder."""
from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
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


def _set_created(path, when):
    """Windows: set the creation time of *path* (seconds since 1970)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    kernel32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                     wintypes.LPVOID, wintypes.LPVOID]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), 0x100, 7, None, 3, 0x80, None)  # write attributes
    stamp = int(when) * 10_000_000 + 116444736000000000  # in 100 ns since 1601
    try:
        assert kernel32.SetFileTime(handle, ctypes.byref(wintypes.FILETIME(
            stamp & 0xFFFFFFFF, stamp >> 32)), None, None)
    finally:
        kernel32.CloseHandle(handle)


def _created(path):
    info = os.stat(str(path))
    return getattr(info, "st_birthtime", info.st_ctime)


_USERS = "*S-1-5-32-545"  # the Users group, whatever the language of this Windows


def _has_an_entry_for_users(path):
    found = subprocess.run(["icacls", str(path), "/findsid", _USERS], capture_output=True,
                           text=True, errors="replace")
    return str(path).lower() in found.stdout.lower()


@pytest.mark.skipif(os.name != "nt", reason="data streams, permissions and creation times of NTFS")
def test_saving_keeps_what_a_new_file_would_not_have(tmp_path):
    """A downloaded script.ps1, edited and saved, had lost its "from the
    Internet" mark, a permission given to it alone and its creation time:
    the save had put a new file in its place."""
    path = tmp_path / "script.ps1"
    path.write_text("old")
    try:
        with open(f"{path}:Zone.Identifier", "w") as fh:
            fh.write("[ZoneTransfer]\nZoneId=3\n")
    except OSError:
        pytest.skip("this drive keeps no data streams")
    granted = subprocess.run(["icacls", str(path), "/grant", f"{_USERS}:(R)"],
                             capture_output=True).returncode == 0
    if granted and not _has_an_entry_for_users(path):
        granted = False  # icacls answers in a form this test does not read
    _set_created(path, 1_000_000_000)  # in 2001
    created = _created(path)

    fb._save_local_text(str(path), "new")

    assert path.read_text() == "new"
    with open(f"{path}:Zone.Identifier") as fh:
        assert "ZoneId=3" in fh.read()
    assert _created(path) == created
    if granted:
        assert _has_an_entry_for_users(path)
    assert os.listdir(tmp_path) == ["script.ps1"]  # nothing is left beside it


def _swap_fails(monkeypatch, winerror):
    def refuse(_tmp, _real):
        error = OSError(22, "the drive refused")
        error.winerror = winerror
        raise error

    monkeypatch.setattr(fb, "_swap_keeping_the_file", refuse)


@pytest.mark.skipif(os.name != "nt", reason="how a save replaces a file on Windows")
def test_a_drive_that_cannot_swap_files_gets_the_new_file_renamed_over_the_old(monkeypatch,
                                                                              tmp_path):
    """Some network shares can't: the text still arrives whole or not at all."""
    path = tmp_path / "a.txt"
    path.write_text("old")
    before = os.stat(str(path)).st_ino
    _swap_fails(monkeypatch, 50)  # "the request is not supported"
    fb._save_local_text(str(path), "new")
    assert path.read_text() == "new" and os.stat(str(path)).st_ino != before
    assert os.listdir(tmp_path) == ["a.txt"]


@pytest.mark.skipif(os.name != "nt", reason="how a save replaces a file on Windows")
@pytest.mark.parametrize("winerror", [5, 32], ids=["not-allowed", "in-use"])
def test_a_file_that_may_not_be_swapped_is_written_in_place(monkeypatch, tmp_path, winerror):
    """Renaming a new file over it would drop what the swap was refused for."""
    path = tmp_path / "a.txt"
    path.write_text("old")
    before = os.stat(str(path)).st_ino
    _swap_fails(monkeypatch, winerror)
    fb._save_local_text(str(path), "new")
    assert path.read_text() == "new" and os.stat(str(path)).st_ino == before
    assert os.listdir(tmp_path) == ["a.txt"]


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


def _fuse(monkeypatch, limit=20):
    """End a copy that nests into itself after *limit* folders, where it would
    otherwise run for minutes, until the path is too long."""
    real, folders = shutil.copytree, []

    def copytree(src, dst, *args, **kw):  # shutil calls it again for every folder
        folders.append(src)
        assert len(folders) <= limit, "the copy nests into itself"
        return real(src, dst, *args, **kw)

    monkeypatch.setattr(fb.shutil, "copytree", copytree)


def test_a_folder_is_never_copied_into_itself_through_a_link(tmp_path, monkeypatch):
    """J is a junction to A\\sub: A pasted into J went into A\\sub\\A, and from
    there into itself, level after level, for minutes."""
    src = tmp_path / "A"
    (src / "sub").mkdir(parents=True)
    (src / "top.txt").write_text("top")
    link, own = tmp_path / "J", tmp_path / "A-again"
    try:
        _link_folder(src / "sub", link)
        _link_folder(src, own)
    except OSError:
        pytest.skip("folder links can't be made here")
    _fuse(monkeypatch)
    assert fb._find_local_collisions([str(src)], str(link)) == []
    for target in (link, own):  # into its subfolder, and into itself
        errors = fb._copy_local_items([str(src)], str(target))
        assert len(errors) == 1 and "into itself" in errors[0]
    assert os.listdir(src / "sub") == [] and sorted(os.listdir(src)) == ["sub", "top.txt"]


def test_a_link_to_the_folder_the_copy_goes_into_is_left_out(tmp_path, monkeypatch):
    """A holds a link to "backup"; copied into "backup", the link was followed
    and the copy, by then inside "backup", was copied into itself."""
    src = tmp_path / "A"
    src.mkdir()
    (src / "a.txt").write_text("a")
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "old.txt").write_text("old")
    try:
        _link_folder(backup, src / "out")
    except OSError:
        pytest.skip("folder links can't be made here")
    _fuse(monkeypatch)
    errors = fb._copy_local_items([str(src)], str(backup))
    assert len(errors) == 1 and errors[0].startswith(str(src / "out"))
    assert "not copied" in errors[0] and "the copy goes into" in errors[0]
    assert os.listdir(backup / "A") == ["a.txt"]
    assert sorted(os.listdir(backup)) == ["A", "old.txt"]


def test_pasting_into_the_same_folder_says_the_item_is_already_there(page, tmp_path):
    """Ctrl+C then Ctrl+V in one folder logged "[OK] paste 1 item(s)" and
    copied nothing."""
    (tmp_path / "a.txt").write_text("a")
    logs = []
    page.log.connect(logs.append)
    page.refresh_local()
    _select(page, "a.txt")
    page._local_copy()
    page._local_paste()
    pasted = [line for line in logs if "paste" in line]
    assert len(pasted) == 1 and pasted[0].startswith("[ERROR] paste 1 item(s)")
    assert "already in this folder" in pasted[0]
    assert os.listdir(tmp_path) == ["a.txt"]


def test_two_items_of_one_name_never_overwrite_each_other(page, monkeypatch, tmp_path):
    """2024\\report.txt and 2025\\report.txt dropped together (an Explorer
    search result): nothing was asked, OK was logged for 2 items, and one
    report.txt was left, the second one's."""
    for year in ("2024", "2025"):
        (tmp_path / year / "photos").mkdir(parents=True)
        (tmp_path / year / "photos" / f"{year}.jpg").write_text(year)
        (tmp_path / year / "report.txt").write_text(year)
    target = tmp_path / "target"
    target.mkdir()
    sources = [str(tmp_path / year / name) for name in ("report.txt", "photos")
               for year in ("2024", "2025")]
    shown = _boxes(monkeypatch)
    logs = []
    page.log.connect(logs.append)
    page._on_local_dropped(sources, True, str(target))
    assert shown == []  # nothing is there yet: nothing to ask
    assert (target / "report.txt").read_text() == "2024"
    assert os.listdir(target / "photos") == ["2024.jpg"]  # the second folder is not merged in
    errors = [line for line in logs if line.startswith("[ERROR] local copy 4 item(s)")]
    assert len(errors) == 2 and all("has the same name" in line for line in errors)
    assert sources[1] in errors[0] and sources[3] in errors[1]
    assert not any(line.startswith("[OK] local copy") for line in logs)
    # dropped again, what is there now is asked about once per name
    assert fb._find_local_collisions(sources, str(target)) == ["report.txt", "photos"]


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
