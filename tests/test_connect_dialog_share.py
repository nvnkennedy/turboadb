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


_UNREACHABLE = (
    "Could not reach the adb server at 10.0.0.7:5037. On that machine run:  adb -a nodaemon "
    "server start  (and allow TCP 5037 through its firewall). (* daemon not running; starting "
    "now at tcp:5037 adb.exe: failed to connect to '10.0.0.7:5037': cannot connect to "
    "10.0.0.7:5037: No connection could be made because the target machine actively refused "
    "it. (10061) * failed to start daemon error: cannot connect to daemon at tcp:10.0.0.7:5037)"
)


@pytest.mark.parametrize("page, host", [(0, None), (2, "10.0.0.7")])
def test_a_failed_scan_does_not_widen_the_dialog(qapp, monkeypatch, page, host):
    """The status line took the whole error text on one line: the dialog grew
    to twice the width of the screen, its Connect button out of reach, and
    stayed that wide."""
    _Scan.made = []
    monkeypatch.setattr(connect_dialog, "_ScanThread", _Scan)
    monkeypatch.setattr(connect_dialog, "park_thread", lambda thread: None)
    dlg = connect_dialog.ConnectDialog()  # scans USB at once
    try:
        dlg.show()  # laid out, on the offscreen platform
        if host:
            _Scan.made[0].done.emit([])
            dlg.rem_host.setCurrentText(host)
        dlg.mode.setCurrentIndex(page)  # the Remote page scans a known host at once
        for _ in range(3):
            qapp.processEvents()
        width = dlg.width()
        assert _Scan.made[-1].host == host
        _Scan.made[-1].fail.emit(_UNREACHABLE)
        for _ in range(3):
            qapp.processEvents()
        status = dlg.rem_status if host else dlg.usb_status
        assert status.text() == _UNREACHABLE  # all of it, on as many lines as it needs
        assert status.toolTip() == _UNREACHABLE  # and in reach when the dialog is short
        assert dlg.width() == width
        assert dlg.minimumSizeHint().width() <= width
    finally:
        dlg.close()
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
        self.logged = []

    def _server_task_running(self):
        return self.busy or (self._share is not None and self._share.isRunning())

    def _device_tabs(self):
        return self.tabs

    def _start_poll_timer(self):
        self._timer.start(60000)

    def _poll_devices(self):
        self.polled += 1

    def _log(self, text):
        self.logged.append(text)


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


_NO_RULE = ("firewall: could not open ports (run TurboADB/`turboadb serve` as Administrator, "
            "or open TCP 5037 + 27183-27199 manually)")
_OPENED = ("firewall: opened TCP 5037, 27183-27199 on Domain and Private networks. The adb "
           "server has no password: share it on trusted networks only.")


@pytest.fixture
def real_share(monkeypatch):
    """The dialog's own share, over a stand-in engine: the server starts, and
    the firewall answers what the test sets as ``world["firewall"]``."""
    from turboadb import devices, tools

    world = {"firewall": _OPENED}
    _Scan.made = []
    monkeypatch.setattr(connect_dialog, "_ScanThread", _Scan)
    monkeypatch.setattr(connect_dialog, "park_thread", lambda thread: None)
    monkeypatch.setattr(connect_dialog.QMessageBox, "question",
                        lambda *args, **kwargs: QMessageBox.Yes)
    monkeypatch.setattr(devices, "start_shared_server",
                        lambda port=5037, adb_path=None: f"shared adb server on 0.0.0.0:{port}")
    monkeypatch.setattr(devices, "open_firewall", lambda ports: world["firewall"])
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda *args, **kwargs: True)
    return world


def _share_from(qapp, dlg):
    dlg.mode.setCurrentIndex(2)
    dlg._start_shared()
    thread = dlg._serve
    assert thread.wait(5000)
    for _ in range(5):
        qapp.processEvents()


def _on_screen(dlg):
    from PyQt5.QtWidgets import QLabel

    return [label.text() for label in dlg.findChildren(QLabel) if not label.isHidden()]


def test_what_the_share_did_stays_in_the_dialog_and_goes_to_the_log(qapp, real_share):
    """Its result was set on the scan's status line and replaced by
    "scanning…" in the same call, and nothing logged it: a firewall rule that
    could not be added (no Administrator rights, the usual case) was never
    seen."""
    real_share["firewall"] = _NO_RULE
    window = _Window()
    dlg = connect_dialog.ConnectDialog(window)
    try:
        _share_from(qapp, dlg)
        assert dlg.rem_host.currentText() == "127.0.0.1" and dlg.rem_status.text() == "scanning…"
        shown = [text for text in _on_screen(dlg) if _NO_RULE in text]
        assert len(shown) == 1 and "shared adb server on 0.0.0.0:5037" in shown[0]
        assert "[OK] shared adb server on 0.0.0.0:5037" in window.logged
        assert window.logged[-1].startswith("[WARNING] ") and _NO_RULE in window.logged[-1]
    finally:
        dlg.close()
        dlg.deleteLater()
        window.deleteLater()


@pytest.mark.parametrize("firewall", [_OPENED, "firewall: not Windows, skipped"])
def test_a_share_with_nothing_in_its_way_is_logged_as_done(qapp, real_share, firewall):
    real_share["firewall"] = firewall
    window = _Window()
    dlg = connect_dialog.ConnectDialog(window)
    try:
        _share_from(qapp, dlg)
        assert any(firewall in text for text in _on_screen(dlg))
        assert window.logged == ["[OK] shared adb server on 0.0.0.0:5037", "[INFO] " + firewall]
    finally:
        dlg.close()
        dlg.deleteLater()
        window.deleteLater()


def test_the_log_has_the_shares_outcome_after_the_dialog_closed(qapp, real_share, monkeypatch):
    from turboadb import devices

    real_share["firewall"] = _NO_RULE
    release = threading.Event()

    def slow_start(port=5037, adb_path=None):
        release.wait(10)
        return f"shared adb server on 0.0.0.0:{port}"

    monkeypatch.setattr(devices, "start_shared_server", slow_start)
    window = _Window()
    dlg = connect_dialog.ConnectDialog(window)
    dlg.mode.setCurrentIndex(2)
    dlg._start_shared()
    thread = dlg._serve
    dlg.reject()  # closed while the share still runs
    release.set()
    assert thread.wait(5000)
    for _ in range(5):
        qapp.processEvents()
    assert window.logged[-1].startswith("[WARNING] ") and _NO_RULE in window.logged[-1]
    window.deleteLater()


def test_a_share_that_failed_says_so_in_the_dialog_and_the_log(qapp, real_share, monkeypatch):
    from turboadb import devices

    def refuse(port=5037, adb_path=None):
        raise RuntimeError("adb server did not come up on port 5037: no response")

    monkeypatch.setattr(devices, "start_shared_server", refuse)
    window = _Window()
    dlg = connect_dialog.ConnectDialog(window)
    try:
        _share_from(qapp, dlg)
        assert dlg.rem_status.text() == (
            "couldn't start server: adb server did not come up on port 5037: no response")
        assert window.logged == [
            "[ERROR] share devices: adb server did not come up on port 5037: no response"]
        assert dlg.btn_serve.isEnabled()
    finally:
        dlg.close()
        dlg.deleteLater()
        window.deleteLater()


@pytest.mark.parametrize("firewall", [_OPENED, _NO_RULE], ids=["firewall-open", "no-rule"])
def test_a_share_whose_login_auto_start_failed_is_shared_all_the_same(
        qapp, real_share, monkeypatch, firewall):
    """The server was shared and the firewall open, yet the dialog said
    "couldn't start server" and the log "[ERROR] share devices": only the
    launcher for the next login could not be written.  The window's own Share
    calls that a warning."""
    from turboadb import devices

    def refuse(port=5037, adb_path=None):
        raise OSError("the Startup folder can't be written")

    monkeypatch.setattr(devices, "install_startup", refuse)
    real_share["firewall"] = firewall
    window = _Window()
    dlg = connect_dialog.ConnectDialog(window)
    try:
        dlg.chk_login.setChecked(True)
        _share_from(qapp, dlg)
        # shared: the result is on screen, and its devices are scanned as usual
        [shown] = [text for text in _on_screen(dlg) if "shared adb server on 0.0.0.0:5037" in text]
        assert firewall in shown and "couldn't start server" not in " ".join(_on_screen(dlg))
        assert "login auto-start" in shown and "the Startup folder can't be written" in shown
        assert dlg.serve_status.property("tone") == "amber"
        assert dlg.rem_host.currentText() == "127.0.0.1" and dlg.rem_status.text() == "scanning…"
        assert dlg.btn_serve.isEnabled()
        # the log: what worked, then the warnings, and nothing called an error
        assert window.logged[0] == "[OK] shared adb server on 0.0.0.0:5037"
        assert not any(line.startswith("[ERROR]") for line in window.logged)
        warned = [line for line in window.logged if line.startswith("[WARNING] ")]
        assert any("login auto-start" in line and "the Startup folder can't be written" in line
                   for line in warned)
        assert window.logged[-1] in warned  # the status bar and the toast end on one
        assert (firewall in window.logged[-1]) == (firewall is _NO_RULE)
    finally:
        dlg.close()
        dlg.deleteLater()
        window.deleteLater()


def test_a_share_with_the_login_auto_start_says_where_it_is(qapp, real_share, monkeypatch):
    from turboadb import devices

    launcher = "C:/Startup/turboadb-shared-adb.bat"
    monkeypatch.setattr(devices, "install_startup", lambda port=5037, adb_path=None: launcher)
    window = _Window()
    dlg = connect_dialog.ConnectDialog(window)
    try:
        dlg.chk_login.setChecked(True)
        _share_from(qapp, dlg)
        assert any(f"auto-starts at login ({launcher})" in text for text in _on_screen(dlg))
        assert dlg.serve_status.property("tone") != "amber"
        assert window.logged == [
            "[OK] shared adb server on 0.0.0.0:5037",
            f"[OK] Auto-start installed (runs at every login): {launcher}",
            "[INFO] " + _OPENED,
        ]
    finally:
        dlg.close()
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
