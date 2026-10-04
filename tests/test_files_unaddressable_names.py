"""PC names that Windows can't address: a file or folder whose name ends in a
dot or a space.  WSL, Git Bash and NTFS-3G make such names and a listing shows
them, but Windows drops the dot or space from a path, so every action on such
a row reached the item without it: Shift+Delete on an empty ``photos.``
removed the real ``photos`` folder.  The Files tab leaves these rows alone
and says why.

Headless (Qt offscreen); every file lives in the test's temporary folder."""
from __future__ import annotations

import os
import shutil

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QMimeData, QPointF, Qt, QUrl  # noqa: E402
from PyQt5.QtGui import QDragMoveEvent, QDropEvent  # noqa: E402

from turboadb.gui import file_browser as fb  # noqa: E402
from turboadb.gui import file_open  # noqa: E402

windows_only = pytest.mark.skipif(os.name != "nt", reason="how Windows reads a path")

# Past this prefix Windows takes a path as it is written, so the test can make
# (and remove) the names an ordinary path can't reach.
EXACT = "\\\\?\\"


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
                                     or (answer if answer is not None else fb.QMessageBox.Yes)))
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


@pytest.fixture
def as_on_windows(monkeypatch):
    """Names are judged the way Windows reads them, whatever runs the test."""
    judge = fb._inexact_local_name
    monkeypatch.setattr(fb, "_inexact_local_name", lambda name, windows=None: judge(name, True))


@pytest.fixture
def folder(tmp_path):
    """``photos`` beside an empty ``photos.``, ``todo`` beside ``todo.`` and
    ``notes.txt`` beside ``notes.txt `` (with a space), made past Windows'
    own reading of the names."""
    exact = EXACT + str(tmp_path)
    os.makedirs(exact + "\\photos")
    with open(exact + "\\photos\\keep.jpg", "w") as fh:
        fh.write("keep")
    os.makedirs(exact + "\\photos.")
    for name, text in (("todo", "real"), ("todo.", "stray"),
                       ("notes.txt", "real"), ("notes.txt ", "stray")):
        with open(exact + "\\" + name, "w") as fh:
            fh.write(text)
    try:
        yield tmp_path
    finally:
        shutil.rmtree(exact + "\\photos.", ignore_errors=True)  # an ordinary path can't
        for name in ("todo.", "notes.txt "):
            if os.path.lexists(exact + "\\" + name):
                os.remove(exact + "\\" + name)


def _read(tmp_path, name):
    with open(EXACT + str(tmp_path) + "\\" + name) as fh:
        return fh.read()


def _listed(page, rows):
    """List *rows* (``(name, is a folder)``) in the PC pane without touching the disk."""
    page._populate(page.local_table,
                   [(name, 0, "<DIR>" if is_dir else "0 B", "Folder" if is_dir else "File",
                     "2026-09-01 10:00", "666", "", is_dir) for name, is_dir in rows],
                   parent_row=True)


def _row(table, name):
    for row in range(table.rowCount()):
        data = table.item(row, 0).data(Qt.UserRole)
        if data and data[0] == name:
            return row
    raise AssertionError(f"{name!r} is not listed")


def _select(page, *names):
    table = page.local_table
    table.clearSelection()
    table.setSelectionMode(table.MultiSelection)
    for name in names:
        table.selectRow(_row(table, name))
    table.setSelectionMode(table.ExtendedSelection)


def _refused(shown):
    """True when the one box shown says the rows were left alone."""
    return len(shown) == 1 and shown[0][0] == "warning" and "Nothing was done" in shown[0][1]


# --------------------------------------------------------------------------- #
# which names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["photos.", "notes.txt ", "todo..", "a. .", "...", " "])
def test_a_name_ending_in_a_dot_or_a_space_is_not_what_a_windows_path_says(name):
    assert fb._inexact_local_name(name, windows=True)
    assert not fb._inexact_local_name(name, windows=False)  # an ordinary name elsewhere


@pytest.mark.parametrize("name", ["photos", ".gitignore", " lead", "a.b", "two  spaces.txt", "…"])
def test_other_names_are_exact(name):
    assert not fb._inexact_local_name(name, windows=True)


@windows_only
def test_the_listing_marks_the_names_a_path_cannot_reach(page, folder):
    page.refresh_local()
    table = page.local_table
    assert table.unsafe_names == {"photos.", "todo.", "notes.txt "}
    assert {table.item(row, 0).text() for row in range(table.rowCount())} >= {
        "photos", "photos.", "todo", "todo.", "notes.txt", "notes.txt "}  # all still shown


# --------------------------------------------------------------------------- #
# the neighbour is never deleted, recycled or renamed
# --------------------------------------------------------------------------- #
@windows_only
def test_shift_delete_on_a_dotted_folder_leaves_its_neighbour_alone(page, folder, monkeypatch):
    """It removed the whole real ``photos`` tree, left ``photos.`` and said OK."""
    shown = _boxes(monkeypatch)
    page.refresh_local()
    _select(page, "photos.")
    page._local_delete_permanently()
    assert _refused(shown)
    assert _read(folder, "photos\\keep.jpg") == "keep"
    assert os.path.isdir(EXACT + str(folder) + "\\photos.")


@windows_only
def test_delete_never_sends_the_neighbour_to_the_recycle_bin(page, folder, monkeypatch):
    shown = _boxes(monkeypatch)
    recycled = []
    monkeypatch.setattr(fb, "_recycle_local_items", lambda paths: recycled.append(list(paths)) or [])
    page.refresh_local()
    _select(page, "todo.", "notes.txt")  # one exact name beside it changes nothing
    page._local_delete()
    assert _refused(shown) and "'todo.'" in shown[0][1]
    assert recycled == []


@windows_only
def test_rename_never_renames_the_neighbour(page, folder, monkeypatch):
    shown = _boxes(monkeypatch)
    monkeypatch.setattr(fb.QInputDialog, "getText", staticmethod(lambda *a, **k: ("done", True)))
    page.refresh_local()
    _select(page, "todo.")
    page._local_rename()
    assert _refused(shown)
    assert _read(folder, "todo") == "real" and _read(folder, "todo.") == "stray"
    assert not os.path.exists(str(folder / "done"))


@windows_only
def test_an_exact_name_beside_them_is_still_deleted(page, folder, monkeypatch):
    shown = _boxes(monkeypatch)
    page.refresh_local()
    _select(page, "todo")
    page._local_delete_permanently()
    assert [kind for kind, _text in shown] == ["question"]
    assert not os.path.exists(EXACT + str(folder) + "\\todo")
    assert _read(folder, "todo.") == "stray"


# --------------------------------------------------------------------------- #
# nor opened, edited, copied, pushed, entered or dragged
# --------------------------------------------------------------------------- #
def test_edit_and_open_never_show_the_neighbour(page, as_on_windows, monkeypatch, tmp_path):
    (tmp_path / "notes.txt").write_text("real")
    started = []
    monkeypatch.setattr(file_open, "start", lambda path, choose=False: started.append(path))
    _listed(page, [("notes.txt", False), ("notes.txt ", False)])
    row = _row(page.local_table, "notes.txt ")
    for action in (lambda: page._local_edit_row(row), lambda: page._local_open_row(row),
                   lambda: page._local_open_row(row, choose=True),
                   lambda: page._on_local_double_click(row, 0)):
        shown = _boxes(monkeypatch)
        action()
        assert _refused(shown)
    assert page._editors == [] and started == []


def test_copy_and_push_leave_the_rows_alone(page, as_on_windows, monkeypatch):
    shown = _boxes(monkeypatch)
    pushed = []
    monkeypatch.setattr(page, "_start_push", lambda sources, dst: pushed.append(list(sources)))
    _listed(page, [("photos", True), ("photos.", True)])
    _select(page, "photos.", "photos")
    page._local_copy()
    page.push_selected()
    assert page._clipboard == [] and pushed == []
    assert [kind for kind, _text in shown] == ["warning", "warning"]

    _select(page, "photos")  # the exact name alone goes ahead
    page._local_copy()
    page.push_selected()
    target = os.path.join(page.local_cwd, "photos")
    assert page._clipboard == [target] and pushed == [[target]]


def test_a_dotted_folder_is_not_entered(page, as_on_windows, monkeypatch, tmp_path):
    """It showed the neighbour's files under the neighbour's path."""
    (tmp_path / "photos").mkdir()
    shown = _boxes(monkeypatch)
    _listed(page, [("photos", True), ("photos.", True)])
    _select(page, "photos.")
    page._on_local_double_click(_row(page.local_table, "photos."), 0)
    page._local_open()
    assert [kind for kind, _text in shown] == ["warning", "warning"]
    assert page.local_cwd == str(tmp_path)


class _Drag:
    """QDrag, recorded instead of run."""

    made = []

    def __init__(self, source):
        self.mime = None
        _Drag.made.append(self)

    def setMimeData(self, mime):
        self.mime = mime

    def exec_(self, _actions):
        return Qt.IgnoreAction


def test_no_drag_carries_a_path_to_the_neighbour(page, as_on_windows, monkeypatch):
    monkeypatch.setattr(fb, "QDrag", _Drag)
    _Drag.made = []
    logs = []
    page.log.connect(logs.append)
    _listed(page, [("todo", False), ("todo.", False)])
    _select(page, "todo", "todo.")
    page.local_table.startDrag(Qt.CopyAction)
    assert _Drag.made == []
    assert any("Drag" in line and "'todo.'" in line for line in logs)

    _select(page, "todo")
    page.local_table.startDrag(Qt.CopyAction)
    [drag] = _Drag.made
    assert [url.toLocalFile() for url in drag.mime.urls()] == \
        [QUrl.fromLocalFile(os.path.join(page.local_cwd, "todo")).toLocalFile()]


def test_nothing_is_dropped_into_a_dotted_folder(page, qapp, as_on_windows, monkeypatch, tmp_path):
    """The drop went into the folder without the dot."""
    source = tmp_path / "a.txt"
    source.write_text("a")
    (tmp_path / "photos").mkdir()
    _listed(page, [("photos", True), ("photos.", True)])
    page.resize(900, 600)
    page.setAttribute(Qt.WA_DontShowOnScreen, True)
    page.show()
    qapp.processEvents()
    try:
        table = page.local_table
        dropped, logs, mimes = [], [], []
        table.dropped.connect(lambda *args: dropped.append(args))
        page.log.connect(logs.append)
        for name in ("photos.", "photos"):
            point = QPointF(table.visualRect(table.model().index(_row(table, name), 0)).center())
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(source))])
            mimes.append(mime)  # an event keeps a raw pointer to its mime data
            move = QDragMoveEvent(point.toPoint(), Qt.CopyAction, mime, Qt.LeftButton,
                                  Qt.NoModifier)
            table.dragMoveEvent(move)
            drop = QDropEvent(point, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
            table.dropEvent(drop)
            assert move.isAccepted() == drop.isAccepted() == (name == "photos")
            if name == "photos.":  # nothing was copied anywhere for it
                assert os.listdir(tmp_path / "photos") == []
        assert [target for _paths, _local, target in dropped] == \
            [os.path.join(page.local_cwd, "photos")]
        assert any("'photos.'" in line and "refused" in line and "Windows" in line
                   for line in logs)
        assert (tmp_path / "photos" / "a.txt").read_text() == "a"  # the drop on "photos" itself
    finally:
        page.hide()


# --------------------------------------------------------------------------- #
# a path from outside (a drop from Explorer) is not acted on either
# --------------------------------------------------------------------------- #
def test_a_dropped_path_windows_cannot_address_is_not_pushed(as_on_windows, monkeypatch, tmp_path):
    (tmp_path / "photos").mkdir()
    monkeypatch.setattr(fb, "_probe_remote", lambda handler, paths: {p: ("n", False, "")
                                                                     for p in paths})
    dotted, plain = str(tmp_path / "photos") + ".", str(tmp_path / "photos")
    jobs, collisions, errors = fb._plan_push(None, [dotted, plain], "/sdcard")
    assert jobs == [(plain, "/sdcard/photos", "push")] and collisions == []
    assert len(errors) == 1 and errors[0].startswith(dotted) and "dot or a space" in errors[0]


def test_a_dropped_path_windows_cannot_address_is_not_copied(as_on_windows, tmp_path):
    (tmp_path / "todo").write_text("real")
    target = tmp_path / "target"
    target.mkdir()
    (target / "todo").write_text("there")
    dotted = str(tmp_path / "todo") + "."
    assert fb._find_local_collisions([dotted], str(target)) == []  # nothing to ask about
    errors = fb._copy_local_items([dotted], str(target))
    assert len(errors) == 1 and errors[0].startswith(dotted) and "dot or a space" in errors[0]
    assert (target / "todo").read_text() == "there"
