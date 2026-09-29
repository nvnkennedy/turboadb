"""A drop onto the Files tab is always a copy: a Shift-drag from Explorer
proposes a Move, and accepting it made Explorer delete the originals."""
import os

import pytest

pytest.importorskip("PyQt5")


def _mime_with(path):
    from PyQt5.QtCore import QMimeData, QUrl

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path)])
    return mime


@pytest.mark.parametrize("is_remote", [False, True])
def test_a_shift_drag_from_outside_is_accepted_as_a_copy(qapp, tmp_path, is_remote):
    from PyQt5.QtCore import QPoint, QPointF, Qt
    from PyQt5.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
    from turboadb.gui.file_browser import _FileTableWidget

    src = tmp_path / "logs.txt"
    src.write_text("keep me")
    table = _FileTableWidget(is_remote=is_remote)
    table.base_dir = "/sdcard" if is_remote else str(tmp_path / "dst")
    got = []
    table.dropped.connect(lambda paths, is_local, target: got.append((paths, is_local)))
    both = Qt.CopyAction | Qt.MoveAction
    # Qt events keep a raw pointer to their mime data: hold every one here.
    mimes = [_mime_with(str(src)) for _ in range(3)]

    enter = QDragEnterEvent(QPoint(2, 2), both, mimes[0], Qt.LeftButton, Qt.ShiftModifier)
    table.dragEnterEvent(enter)
    assert enter.isAccepted() and enter.dropAction() == Qt.CopyAction

    move = QDragMoveEvent(QPoint(2, 2), both, mimes[1], Qt.LeftButton, Qt.ShiftModifier)
    table.dragMoveEvent(move)
    assert move.isAccepted() and move.dropAction() == Qt.CopyAction

    drop = QDropEvent(QPointF(2, 2), both, mimes[2], Qt.LeftButton, Qt.ShiftModifier)
    table.dropEvent(drop)
    assert drop.isAccepted()
    assert drop.dropAction() == Qt.CopyAction      # never Move: the source keeps its files
    assert len(got) == 1 and got[0][1] is True     # handed on as a local copy/push
    assert [os.path.normcase(p) for p in got[0][0]] == [os.path.normcase(str(src))]
    assert src.read_text() == "keep me"


def test_a_source_that_only_offers_move_is_refused(qapp, tmp_path):
    from PyQt5.QtCore import QPoint, QPointF, Qt
    from PyQt5.QtGui import QDragEnterEvent, QDropEvent
    from turboadb.gui.file_browser import _FileTableWidget

    table = _FileTableWidget(is_remote=True)
    table.base_dir = "/sdcard"
    got = []
    table.dropped.connect(lambda *a: got.append(a))
    mimes = [_mime_with(str(tmp_path / "x")) for _ in range(2)]

    enter = QDragEnterEvent(QPoint(2, 2), Qt.MoveAction, mimes[0], Qt.LeftButton, Qt.NoModifier)
    table.dragEnterEvent(enter)
    assert not enter.isAccepted()

    drop = QDropEvent(QPointF(2, 2), Qt.MoveAction, mimes[1], Qt.LeftButton, Qt.NoModifier)
    table.dropEvent(drop)
    assert not drop.isAccepted()
    assert got == []
