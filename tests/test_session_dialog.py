"""The saved-target dialog while Detect or List looks for devices: what OK
saves, and how the scan shows it is running.

Headless (Qt offscreen); the scan is a fake that answers when told to."""
from __future__ import annotations

import types

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QObject, pyqtSignal  # noqa: E402


@pytest.fixture
def dialog(qapp, monkeypatch):
    from turboadb.gui import session_dialog as sd

    scans = []

    class Scan(QObject):
        done = pyqtSignal(list)
        fail = pyqtSignal(str)
        finished = pyqtSignal()

        def __init__(self, host, port, adb_path):
            super().__init__()
            self.host, self.port = host, port
            scans.append(self)

        def start(self):
            pass

        def isRunning(self):
            return True

        def isFinished(self):
            return False

    monkeypatch.setattr(sd, "_ScanThread", Scan)
    monkeypatch.setattr(sd, "park_thread", lambda thread: None)
    shown = []
    monkeypatch.setattr(sd.QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a)))
    dlg = sd.SessionDialog()
    dlg.scans, dlg.shown = scans, shown
    dlg.name.setText("Head unit")
    try:
        yield dlg
    finally:
        dlg.deleteLater()


def test_ok_during_a_scan_saves_the_serial_being_edited(dialog):
    """"scanning…" was written into the serial box, and OK saved it as the
    serial: connecting then failed with "device not found"."""
    dialog.serial.setEditText("R58M123")
    dialog._detect()
    assert dialog.result_session()["serial"] == "R58M123"
    assert not dialog.btn_detect.isEnabled() and dialog.btn_detect.text() == "Scanning…"
    dialog._detect()  # a second click while it runs starts nothing
    assert len(dialog.scans) == 1
    dialog.scans[0].done.emit([types.SimpleNamespace(serial="A1"),
                               types.SimpleNamespace(serial="B2")])
    assert dialog.btn_detect.isEnabled() and dialog.btn_detect.text() == "Detect"
    assert [dialog.serial.itemText(i) for i in range(dialog.serial.count())] == ["A1", "B2"]


def test_an_empty_box_shows_the_scan_as_its_placeholder(dialog):
    dialog.mode.setCurrentIndex(2)
    dialog.srv_host.setText("192.168.1.20")
    dialog._detect_remote()
    line = dialog.rserial.lineEdit()
    assert dialog.rserial.currentText() == "" and line.placeholderText() == "scanning…"
    assert dialog.result_session()["serial"] == ""  # blank: the only device over there
    dialog.scans[0].fail.emit("connection refused")
    assert line.placeholderText() == "" and dialog.btn_list.isEnabled()
    assert dialog.shown  # a failed List says why


def test_a_scan_that_finds_nothing_puts_the_serial_back(dialog):
    dialog.serial.setEditText("R58M123")
    dialog._detect()
    dialog.scans[0].done.emit([])
    assert dialog.serial.currentText() == "R58M123"
    assert dialog.serial.lineEdit().placeholderText() == ""
