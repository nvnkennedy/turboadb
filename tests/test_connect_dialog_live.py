"""The Connect dialog's USB page follows this PC's devices while it is open (6.4.0).

It listed what one scan found when it opened, and again only on Refresh: a
phone that rebooted while the dialog was open went offline, dropped off, and
never came back to the list by itself.  It now lists the devices the window
knows at once and follows the window's device tracker; the device the user
picked stays picked while it is away.  No test here runs adb: the scans and
the tracker are fakes.
"""
import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QObject, pyqtSignal  # noqa: E402

from turboadb.devices import Device  # noqa: E402
from turboadb.gui import connect_dialog  # noqa: E402

from test_connect_dialog_share import _Scan  # noqa: E402
from test_main_window_actions import make_window  # noqa: E402,F401  (a fixture)


class _Feed(QObject):
    """The main window's device list, as its tracker changes it."""
    changed = pyqtSignal(list)


def _phone(serial="V2318", state="device"):
    return Device(serial, state, model="V2318")


def _rows(dlg):
    return [dlg.usb_list.item(i).text() for i in range(dlg.usb_list.count())]


def _picked(dlg):
    item = dlg.usb_list.currentItem()
    return item.data(connect_dialog.Qt.UserRole) if item is not None else None


@pytest.fixture
def scans(monkeypatch):
    _Scan.made = []
    monkeypatch.setattr(connect_dialog, "_ScanThread", _Scan)
    monkeypatch.setattr(connect_dialog, "park_thread", lambda thread: None)
    return _Scan.made


def test_the_usb_page_lists_the_devices_known_at_once(qapp, scans):
    feed = _Feed()
    dlg = connect_dialog.ConnectDialog(devices=[_phone()], device_feed=feed.changed)
    try:
        assert _rows(dlg) == ["V2318    V2318 [usb] · device"]  # before any scan answers
        assert _picked(dlg) == "V2318" and dlg.save_name.text() == "V2318"
        assert [scan.host for scan in scans] == [None]  # the scan still runs
    finally:
        dlg.reject()


def test_a_phone_that_reboots_while_the_dialog_is_open_comes_back(qapp, scans):
    """Issue: the phone rebooted with the dialog open and never came back to
    its list."""
    feed = _Feed()
    dlg = connect_dialog.ConnectDialog(devices=[_phone()], device_feed=feed.changed)
    try:
        scans[0].done.emit([_phone()])
        feed.changed.emit([_phone(state="offline")])  # it reboots
        assert _rows(dlg) == ["V2318    V2318 [usb] · offline"]
        feed.changed.emit([])  # gone
        assert _rows(dlg) == [] and dlg.usb_status.text() == "0 device(s)"
        feed.changed.emit([_phone()])  # back
        assert _rows(dlg) == ["V2318    V2318 [usb] · device"]
        assert _picked(dlg) == "V2318" and dlg.usb_status.text() == "1 device(s)"
        assert dlg.session()["serial"] == "V2318"
    finally:
        dlg.reject()


def test_a_picked_phone_stays_picked_while_it_reboots(qapp, scans):
    """Connect with nothing picked means the only device there is: while the
    picked phone was away that would have been another one."""
    feed = _Feed()
    lab = _phone("R58M123")
    dlg = connect_dialog.ConnectDialog(devices=[lab, _phone()], device_feed=feed.changed)
    try:
        dlg.usb_list.setCurrentRow(1)  # the user picks the vivo
        assert _picked(dlg) == "V2318"
        feed.changed.emit([lab])  # the vivo reboots
        assert _rows(dlg) == ["R58M123    V2318 [usb] · device", "V2318    not connected"]
        assert _picked(dlg) == "V2318" and dlg.session()["serial"] == "V2318"
        assert dlg.usb_status.text() == "1 device(s)"
        feed.changed.emit([lab, _phone()])  # back
        assert _rows(dlg)[1] == "V2318    V2318 [usb] · device" and _picked(dlg) == "V2318"
        dlg.usb_list.setCurrentRow(0)  # another pick: the vivo is just a device again
        feed.changed.emit([lab])
        assert _rows(dlg) == ["R58M123    V2318 [usb] · device"] and _picked(dlg) == "R58M123"
    finally:
        dlg.reject()


def test_a_scan_older_than_the_live_list_does_not_replace_it(qapp, scans):
    """A scan can take seconds; a change the tracker reported meanwhile is newer."""
    feed = _Feed()
    dlg = connect_dialog.ConnectDialog(devices=[], device_feed=feed.changed)
    try:
        feed.changed.emit([_phone()])  # plugged in while the first scan runs
        scans[0].done.emit([])  # what that scan saw
        assert _rows(dlg) == ["V2318    V2318 [usb] · device"]
        assert dlg.usb_status.text() == "1 device(s)"
        dlg._scan_usb()  # Refresh: a scan with nothing newer is shown
        assert _rows(dlg) == ["V2318    V2318 [usb] · device"]  # kept while it runs
        scans[1].done.emit([_phone(), _phone("R58M123")])
        assert len(_rows(dlg)) == 2 and dlg.usb_status.text() == "2 device(s)"
    finally:
        dlg.reject()


def test_a_closed_dialog_stops_following(qapp, scans):
    feed = _Feed()
    dlg = connect_dialog.ConnectDialog(devices=[_phone()], device_feed=feed.changed)
    dlg.reject()
    feed.changed.emit([])  # nothing calls into the closed dialog
    assert _rows(dlg) == ["V2318    V2318 [usb] · device"]
    dlg.deleteLater()


def test_the_window_hands_its_device_changes_to_the_dialog(qapp, scans, make_window,
                                                           monkeypatch):
    win = make_window()
    seen = []
    win.devices_changed.connect(seen.append)
    win._on_devices([_phone()])
    win._on_devices([_phone()])  # the same list: nothing to tell
    assert [[d.serial for d in devs] for devs in seen] == [["V2318"]]
    shown = []

    def run(dlg):
        shown.append(_rows(dlg))  # at once
        win._on_devices([_phone(state="offline")])  # it reboots while the dialog is open
        win._on_devices([])
        win._on_devices([])  # (an empty list is believed the second time)
        shown.append(_rows(dlg))
        win._on_devices([_phone()])
        shown.append(_rows(dlg))
        return connect_dialog.ConnectDialog.Rejected

    monkeypatch.setattr(connect_dialog.ConnectDialog, "exec_", run)
    win.open_connect()
    assert shown == [["V2318    V2318 [usb] · device"], [], ["V2318    V2318 [usb] · device"]]
