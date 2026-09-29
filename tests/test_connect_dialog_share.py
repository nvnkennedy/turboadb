"""The Connect dialog: the name it saves a target under, the scans of its
pages, and its "Start shared server on THIS PC", which restarts the adb server.

That button used to do what the main window's Share does without its care:
no question first, the window's device poll running on (its `adb devices`
starts a localhost-only server in the gap between the stop and the shared
start), and the device tabs' shells retrying against a server that was gone.
No test here starts a server or a scan: the share and the scans are fakes.
"""
import threading

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QObject, QTimer, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QListWidgetItem, QMessageBox, QWidget  # noqa: E402

from turboadb.gui import connect_dialog  # noqa: E402
from turboadb.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture
def no_scans(monkeypatch):
    monkeypatch.setattr(connect_dialog.ConnectDialog, "_scan_usb", lambda self: None)
    monkeypatch.setattr(connect_dialog.ConnectDialog, "_scan_remote", lambda self: None)


# --------------------------------------------------------------------------- #
# the saved name
# --------------------------------------------------------------------------- #
def test_a_port_typed_with_the_host_is_not_doubled_in_the_name(qapp, no_scans):
    dlg = connect_dialog.ConnectDialog()
    try:
        dlg.mode.setCurrentIndex(1)
        dlg.net_host.setCurrentText("192.168.1.7:5556")
        assert dlg.save_name.text() == "192.168.1.7:5556"  # was 192.168.1.7:5556:5555
        assert dlg.session()["port"] == 5556
        dlg.net_host.setCurrentText("10.0.0.9")
        dlg.net_port.setValue(5557)  # the port box alone renames it too
        assert dlg.save_name.text() == "10.0.0.9:5557"
        dlg.net_host.setCurrentText("fe80::1")
        assert dlg.save_name.text() == "[fe80::1]:5557"
    finally:
        dlg.deleteLater()


def test_a_remote_target_is_named_after_the_server_it_uses(qapp, no_scans):
    dlg = connect_dialog.ConnectDialog()
    try:
        dlg.mode.setCurrentIndex(2)
        item = QListWidgetItem("R58M123")
        item.setData(connect_dialog.Qt.UserRole, "R58M123")
        dlg.rem_list.addItem(item)
        dlg.rem_list.setCurrentItem(item)
        dlg.rem_host.setCurrentText("lab-pc:5038")
        assert dlg.save_name.text() == "R58M123 @ lab-pc:5038"
        dlg.rem_host.setCurrentText("lab-pc")
        assert dlg.save_name.text() == "R58M123 @ lab-pc"  # the usual port stays out
        dlg.rem_port.setValue(5039)  # the port box is part of the server too
        assert dlg.save_name.text() == "R58M123 @ lab-pc:5039"
        assert dlg.session()["adb_port"] == 5039
    finally:
        dlg.deleteLater()


# --------------------------------------------------------------------------- #
# scans of the USB and Remote pages
# --------------------------------------------------------------------------- #
class _Scan(QObject):
    """A device scan that answers when the test says so."""
    done = pyqtSignal(list)
    fail = pyqtSignal(str)
    made = []

    def __init__(self, host, port, adb_path):
        super().__init__()
        self.host = host
        _Scan.made.append(self)

    def start(self):
        pass

    def isRunning(self):
        return True


def test_a_scan_asked_for_meanwhile_on_another_page_is_not_dropped(qapp, monkeypatch):
    """Only the newest waiting scan was kept: the Remote page's auto-scan,
    queued behind the first USB scan, was dropped by a USB Refresh, and the
    Remote page said "scanning…" for good."""
    _Scan.made = []
    monkeypatch.setattr(connect_dialog, "_ScanThread", _Scan)
    monkeypatch.setattr(connect_dialog, "park_thread", lambda thread: None)
    dlg = connect_dialog.ConnectDialog()  # scans USB at once
    try:
        dlg.rem_host.setCurrentText("lab-pc")
        dlg.mode.setCurrentIndex(2)  # auto-scans the known remote host: waits
        dlg.mode.setCurrentIndex(0)
        dlg._scan_usb()  # Refresh: waits too
        dlg._scan_usb()  # a second Refresh replaces only the USB page's own
        assert [scan.host for scan in _Scan.made] == [None]
        _Scan.made[0].done.emit([])
        assert [scan.host for scan in _Scan.made] == [None, "lab-pc"]
        assert dlg.usb_status.text() == "0 device(s)" and dlg.rem_status.text() == "scanning…"
        _Scan.made[1].done.emit([])
        assert dlg.rem_status.text() == "0 device(s)"
        assert [scan.host for scan in _Scan.made] == [None, "lab-pc", None]
        assert dlg.usb_status.text() == "scanning…"
        _Scan.made[2].done.emit([])
        assert dlg.usb_status.text() == "0 device(s)" and len(_Scan.made) == 3
    finally:
        dlg.deleteLater()


# --------------------------------------------------------------------------- #
# starting a shared server from the dialog
# --------------------------------------------------------------------------- #
class _Tab:
    def __init__(self):
        self.events = []

    def prepare_for_adb_restart(self):
        self.events.append("paused")

    def finish_adb_restart(self, success):
        self.events.append(("resumed", success))


class _Window(QWidget):
    """What the dialog needs of the main window: its own share helpers, over a
    stand-in poll timer, device tabs and server-task state."""

    server_busy = MainWindow.server_busy
    run_share_task = MainWindow.run_share_task

    def __init__(self, busy=False):
        super().__init__()
        self.busy = busy
        self._share = None
        self._timer = QTimer(self)
        self._timer.start(60000)
        self.tabs = [_Tab()]
        self.polled = 0

    def _server_task_running(self):
        return self.busy or (self._share is not None and self._share.isRunning())

    def _device_tabs(self):
        return self.tabs

    def _start_poll_timer(self):
        self._timer.start(60000)

    def _poll_devices(self):
        self.polled += 1


@pytest.fixture
def share(monkeypatch, no_scans):
    """The share runs until released; the local server answers afterwards."""
    world = {"release": threading.Event(), "running": threading.Event(), "asked": []}

    def run(thread):
        world["running"].set()
        world["release"].wait(10)
        thread.server_up = True
        thread.done.emit("shared")

    def question(parent, title, text, *args, **kwargs):
        world["asked"].append(text)
        return world.get("answer", QMessageBox.Yes)

    monkeypatch.setattr(connect_dialog._ServeThread, "run", run)
    monkeypatch.setattr(connect_dialog.QMessageBox, "question", question)
    yield world
    world["release"].set()


def _finish(qapp, thread, world):
    world["release"].set()
    assert thread.wait(5000)
    for _ in range(5):
        qapp.processEvents()


def test_the_share_is_held_like_the_windows_own(qapp, share):
    window = _Window()
    dlg = connect_dialog.ConnectDialog(window)
    try:
        dlg._start_shared()
        thread = dlg._serve
        assert share["running"].wait(5)
        assert "no password" in share["asked"][0]
        # meanwhile: the window's server task, its poll paused, the tabs' shells too
        assert window._share is thread and window._server_task_running()
        assert not window._timer.isActive()
        assert window.tabs[0].events == ["paused"]
        _finish(qapp, thread, share)
        assert window.tabs[0].events == ["paused", ("resumed", True)]
        assert window._share is None and window._timer.isActive() and window.polled == 1
    finally:
        dlg.deleteLater()
        window.deleteLater()


def test_the_window_gets_everything_back_after_the_dialog_closed(qapp, share):
    window = _Window()
    dlg = connect_dialog.ConnectDialog(window)
    dlg._start_shared()
    thread = dlg._serve
    assert share["running"].wait(5)
    dlg.reject()  # closed while the share still runs
    _finish(qapp, thread, share)
    assert window.tabs[0].events == ["paused", ("resumed", True)]
    assert window._share is None and window._timer.isActive()
    window.deleteLater()


def test_saying_no_starts_nothing(qapp, share):
    share["answer"] = QMessageBox.No
    window = _Window()
    dlg = connect_dialog.ConnectDialog(window)
    try:
        dlg._start_shared()
        assert dlg._serve is None and window._share is None
        assert window.tabs[0].events == [] and window._timer.isActive()
    finally:
        dlg.deleteLater()
        window.deleteLater()


def test_a_busy_server_is_not_shared_on_top(qapp, share):
    window = _Window(busy=True)  # a restart or a tools download is running
    dlg = connect_dialog.ConnectDialog(window)
    try:
        dlg._start_shared()
        assert dlg._serve is None and share["asked"] == []
        assert "busy" in dlg.rem_status.text()
    finally:
        dlg.deleteLater()
        window.deleteLater()


def test_a_share_on_another_port_leaves_the_tabs_alone(qapp, share):
    window = _Window()
    dlg = connect_dialog.ConnectDialog(window)
    try:
        dlg.rem_port.setValue(5050)
        dlg._start_shared()
        thread = dlg._serve
        assert share["running"].wait(5)
        assert window.tabs[0].events == []  # their server on 5037 is not touched
        _finish(qapp, thread, share)
        assert window.tabs[0].events == []
    finally:
        dlg.deleteLater()
        window.deleteLater()
