"""Sharing this PC's devices from the main window (ADB server menu).

- Share and Stop sharing restart the adb server, which ends every device tab's
  shell and logcat.  They now pause and resume them the way Restart ADB server
  does, through the one helper the Connect dialog's share uses as well.
- Neither starts on top of another server task (a restart, a download).
- The question names what the firewall rule covers and that the adb server
  has no password.
- The login launcher runs the adb the shared server was started with.
- The Connect dialog offers "at login" on Windows only.
No test starts a server: the shares are stand-ins.
"""
import os
import threading
import types

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QThread, QTimer, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QWidget  # noqa: E402

import turboadb.gui.main_window as mw  # noqa: E402
from turboadb import devices, tools  # noqa: E402
from turboadb.gui import connect_dialog  # noqa: E402


class _Tab:
    def __init__(self):
        self.events = []

    def prepare_for_adb_restart(self):
        self.events.append("paused")

    def finish_adb_restart(self, success):
        self.events.append(("resumed", success))


class _Task(QThread):
    """A share (or its undoing) that runs until released; the server answers after."""
    msg = pyqtSignal(str)
    made = []

    def __init__(self, install_startup=False, adb_path=None):
        super().__init__()
        self.install_startup, self.adb_path = install_startup, adb_path
        self.release = threading.Event()
        self.server_up = False
        _Task.made.append(self)

    def run(self):
        self.release.wait(10)
        self.server_up = True


class _Box:
    """QMessageBox as share_devices / stop_sharing use it."""
    Question = AcceptRole = YesRole = RejectRole = 0
    Yes, No = 1, 0
    texts = []
    answer = "Start once"

    def __init__(self, parent):
        self.buttons = []

    def setWindowTitle(self, _title):
        pass

    def setIcon(self, _icon):
        pass

    def setText(self, text):
        _Box.texts.append(text)

    def addButton(self, text, _role):
        button = types.SimpleNamespace(text=text)
        self.buttons.append(button)
        return button

    def exec_(self):
        pass

    def clickedButton(self):
        return next((b for b in self.buttons if b.text == _Box.answer), None)

    @staticmethod
    def question(*_args, **_kwargs):
        return _Box.Yes


class _Window(QWidget):
    """The main window's share actions, over a stand-in poll timer and tabs."""

    share_devices = mw.MainWindow.share_devices
    stop_sharing = mw.MainWindow.stop_sharing
    run_share_task = mw.MainWindow.run_share_task
    server_busy = mw.MainWindow.server_busy
    _server_task_running = mw.MainWindow._server_task_running

    def __init__(self):
        super().__init__()
        self._timer = QTimer(self)
        self._timer.start(60000)
        self.tabs = [_Tab()]
        self.polled = 0
        self.logged = []

    def _device_tabs(self):
        return self.tabs

    def _start_poll_timer(self):
        self._timer.start(60000)

    def _poll_devices(self):
        self.polled += 1

    def _log(self, text):
        self.logged.append(text)


@pytest.fixture
def window(qapp, monkeypatch):
    _Task.made, _Box.texts, _Box.answer = [], [], "Start once"
    monkeypatch.setattr(mw, "QMessageBox", _Box)
    monkeypatch.setattr(mw, "_ShareThread", _Task)
    monkeypatch.setattr(mw, "_StopShareThread", lambda adb_path=None: _Task(adb_path=adb_path))
    monkeypatch.setattr(mw, "gui_adb_path", lambda: "C:/tools/adb.exe")
    win = _Window()
    yield win
    for task in _Task.made:
        task.release.set()
        task.wait(5000)
    win.deleteLater()


def _finish(qapp, task):
    task.release.set()
    assert task.wait(5000)
    for _ in range(5):
        qapp.processEvents()


@pytest.mark.parametrize("action, attr", [("share_devices", "_share"),
                                          ("stop_sharing", "_unshare")])
def test_the_windows_share_holds_the_device_tabs_like_a_restart(qapp, window, action, attr):
    getattr(window, action)()
    task = _Task.made[0]
    assert task.adb_path == "C:/tools/adb.exe"
    assert getattr(window, attr) is task and window.server_busy()
    assert not window._timer.isActive()  # no `adb devices` in the stop→start gap
    assert window.tabs[0].events == ["paused"]
    _finish(qapp, task)
    assert window.tabs[0].events == ["paused", ("resumed", True)]
    assert getattr(window, attr) is None and not window.server_busy()
    assert window._timer.isActive() and window.polled == 1


def test_nothing_is_shared_on_top_of_another_server_task(qapp, window):
    restart = _Task()
    window._as = restart  # a Restart ADB server is running
    restart.start()
    try:
        window.share_devices()
        window.stop_sharing()
        assert _Task.made == [restart] and _Box.texts == []
        assert sum("busy" in line for line in window.logged) == 2
        assert window.tabs[0].events == []
    finally:
        _finish(qapp, restart)


def test_the_question_says_what_the_firewall_rule_covers_and_that_there_is_no_password(window):
    _Box.answer = "Cancel"
    window.share_devices()
    text = _Box.texts[0]
    assert "no password" in text and "networks you trust" in text
    if os.name == "nt":
        assert "Domain and Private networks only" in text
    assert _Task.made == []


# --------------------------------------------------------------------------- #
# the login launcher pins the adb that started the shared server
# --------------------------------------------------------------------------- #
@pytest.fixture
def pinned(monkeypatch):
    installed = []
    monkeypatch.setattr(devices, "start_shared_server",
                        lambda port=5037, adb_path=None: "shared on 0.0.0.0")
    monkeypatch.setattr(devices, "open_firewall", lambda ports: "firewall: opened")
    monkeypatch.setattr(devices, "install_startup",
                        lambda port=5037, adb_path=None: installed.append((port, adb_path))
                        or "turboadb-shared-adb.bat")
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda *a, **k: True)
    return installed


def test_the_windows_share_pins_its_adb_in_the_login_launcher(qapp, pinned):
    worker = mw._ShareThread(install_startup=True, adb_path="C:/pinned/adb.exe")
    worker.run()  # the worker's body, on this thread
    assert pinned == [(5037, "C:/pinned/adb.exe")] and worker.server_up


def test_the_connect_dialogs_share_pins_its_adb_in_the_login_launcher(qapp, pinned):
    worker = connect_dialog._ServeThread(port=5038, install_login=True,
                                         adb_path="C:/pinned/adb.exe")
    worker.run()
    assert pinned == [(5038, "C:/pinned/adb.exe")]


# --------------------------------------------------------------------------- #
# "at login" in the Connect dialog
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("system, offered", [("nt", True), ("posix", False)])
def test_the_connect_dialog_offers_at_login_on_windows_only(qapp, monkeypatch, system, offered):
    monkeypatch.setattr(connect_dialog.ConnectDialog, "_scan_usb", lambda self: None)
    monkeypatch.setattr(connect_dialog, "os", types.SimpleNamespace(name=system))
    dlg = connect_dialog.ConnectDialog()
    try:
        assert dlg.chk_login.isHidden() is not offered
        assert not dlg.chk_login.isChecked()
    finally:
        dlg.deleteLater()
