"""A cancelled transfer in the Files tab says what the copy left behind.

It said "a partial copy may remain" after every cancel, but the engine removes
the one file a cancelled pull created, and tells which happened: the file was
removed, part of a copy may remain (a file it overwrote, a folder, anything a
push wrote), or nothing was left.  The Files tab passes that on now.  The
page's dead aliases (``cwd``, ``local_list``, ``remote_list``, ``sortItems``,
``_transfer_name``) are gone as well.
"""
import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QObject, pyqtSignal  # noqa: E402

from turboadb.gui import file_browser as fb  # noqa: E402
from turboadb.gui.transfer_log import CANCELLED  # noqa: E402


@pytest.mark.parametrize("message, left", [
    ("ADBTransferError: adb pull cancelled; the unfinished file was removed",
     "the unfinished file was removed"),
    ("ADBTransferError: adb pull cancelled; a partial copy may remain at C:\\Users\\me\\DCIM",
     "a partial copy may remain at C:\\Users\\me\\DCIM"),
    ("ADBTransferError: adb push cancelled; a partial copy may remain on the device",
     "a partial copy may remain on the device"),
    ("ADBTransferError: adb pull cancelled", ""),  # nothing was written yet
    # it ended some other way while the cancel was on its way
    ("ADBTransferError: adb pull failed (exit 1): /sdcard/my cancelled clip.mp4",
     "a partial copy may remain"),
    ("transfer stopped", "a partial copy may remain"),
])
def test_the_leftover_is_read_from_the_engines_words(message, left):
    assert fb._cancel_leftover(message) == left


class _Transfer(QObject):
    progress = pyqtSignal(int)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)
    finished = pyqtSignal()
    started = []

    def __init__(self, handler, direction, a, b):
        super().__init__()
        self.direction, self.a, self.b = direction, a, b

    def start(self):
        _Transfer.started.append(self)

    def stop(self):
        pass


@pytest.fixture
def browser(qapp, monkeypatch):
    _Transfer.started = []
    monkeypatch.setattr(fb, "_TransferThread", _Transfer)
    monkeypatch.setattr(fb, "park_thread", lambda t: None)
    page = fb.FileBrowser(None)
    page.logs = []
    page.log.connect(page.logs.append)
    yield page
    page.close_panel()


@pytest.mark.parametrize("direction, message, line, error", [
    ("pull", "ADBTransferError: adb pull cancelled; the unfinished file was removed",
     "[CANCELLED] pull clip.mp4 (the unfinished file was removed)",
     "cancelled - the unfinished file was removed"),
    ("pull", "ADBTransferError: adb pull cancelled",
     "[CANCELLED] pull clip.mp4", ""),
    ("push", "ADBTransferError: adb push cancelled; a partial copy may remain on the device",
     "[CANCELLED] push clip.mp4 (a partial copy may remain on the device)",
     "cancelled - a partial copy may remain on the device"),
])
def test_a_cancel_reports_what_the_copy_left(browser, direction, message, line, error):
    job = (("/sdcard/clip.mp4", "C:/x", "pull") if direction == "pull"
           else ("C:/x/clip.mp4", "/sdcard", "push"))
    browser._enqueue_transfers([job], "go")
    browser.cancel_transfers()
    _Transfer.started[0].failed.emit(message)
    assert line in browser.logs
    item = browser.transfers.items[-1]
    assert item.status == CANCELLED and item.error == error


def test_the_dead_aliases_are_gone():
    for name in ("cwd", "local_list", "remote_list"):
        assert not hasattr(fb.FileBrowser, name), name
    assert "sortItems" not in vars(fb._FileTableWidget)
    assert not hasattr(fb, "_transfer_name")
