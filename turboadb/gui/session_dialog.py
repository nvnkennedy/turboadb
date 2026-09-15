"""Dialog to create / edit a saved device target.

Three connection modes — only the fields for the chosen mode are enabled:
  * USB device          — pick a serial (or leave blank for the only device)
  * Network device      — a device reachable by IP (Wi-Fi / Ethernet head unit)
  * Remote ADB server   — a device plugged into ANOTHER machine; we talk to that
                          machine's adb server (adb -H host -P 5037), so you see
                          and drive the devices attached over there.
"""

from __future__ import annotations

from PyQt5.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QComboBox,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QHBoxLayout,
    QPushButton,
    QWidget,
    QMessageBox,
)

from PyQt5.QtCore import Qt

from .connect_dialog import TRANSPORT_ICONS, _ScanThread, _dialog_footer, _form_layout, _muted
from .icons import icon
from .qtutil import disconnect_signals, park_thread, thread_running
from .adb_path import gui_adb_path
from .sessions import SessionStore, normalize_session

_MODES = ["USB device", "Network device (Wi-Fi / Ethernet)", "Remote ADB server (another PC)"]
_TYPE = {0: "usb", 1: "network", 2: "remote"}
_INDEX = {"usb": 0, "network": 1, "remote": 2}


class SessionDialog(QDialog):
    def __init__(self, parent=None, existing: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Device target")
        self.resize(480, 420)
        self._adb_path = gui_adb_path()
        self._scan = None
        # The name the target had when the dialog opened (None for a new one),
        # so an edit that changes the name renames instead of copying.
        self._original_name = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        lay = QVBoxLayout()
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(12)
        root.addLayout(lay, 1)

        form = _form_layout()
        self.name = QLineEdit()
        self.mode = QComboBox()
        for (glyph, tone), label in zip(TRANSPORT_ICONS, _MODES):
            self.mode.addItem(icon(glyph, tone), label)
        self.mode.currentIndexChanged.connect(self._sync)
        form.addRow("Name", self.name)
        form.addRow("Connection", self.mode)
        lay.addLayout(form)

        # USB
        self.usb_box = QGroupBox("USB device")
        uf = self._box_form(self.usb_box)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.serial = QComboBox()
        self.serial.setEditable(True)
        pick = QPushButton("Detect")
        pick.setProperty("role", "ghost")
        pick.setIcon(icon("search", "accent"))
        pick.clicked.connect(self._detect)
        row.addWidget(self.serial, 1)
        row.addWidget(pick)
        uf.addRow("Serial", row)
        lay.addWidget(self.usb_box)

        # Network device
        self.net_box = QGroupBox("Network device")
        nf = self._box_form(self.net_box)
        self.host = QLineEdit()
        self.host.setPlaceholderText("192.168.1.50")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(5555)
        nf.addRow("Host / IP", self.host)
        nf.addRow("Port", self.port)
        lay.addWidget(self.net_box)

        # Remote adb server
        self.rem_box = QGroupBox("Remote ADB server")
        rf = self._box_form(self.rem_box)
        self.srv_host = QLineEdit()
        self.srv_host.setPlaceholderText("192.168.1.20")
        self.srv_port = QSpinBox()
        self.srv_port.setRange(1, 65535)
        self.srv_port.setValue(5037)
        rowr = QHBoxLayout()
        rowr.setSpacing(8)
        self.rserial = QComboBox()
        self.rserial.setEditable(True)
        self.rserial.setToolTip("Device serial on that machine (blank = only device)")
        rpick = QPushButton("List")
        rpick.setProperty("role", "ghost")
        rpick.setIcon(icon("search", "accent"))
        rpick.clicked.connect(self._detect_remote)
        rowr.addWidget(self.rserial, 1)
        rowr.addWidget(rpick)
        rf.addRow("Server host / IP", self.srv_host)
        rf.addRow("Server port", self.srv_port)
        rf.addRow("Device serial", rowr)
        lay.addWidget(self.rem_box)

        lay.addWidget(
            _muted("Remote server: on that machine run once →  adb -a nodaemon server start")
        )
        lay.addStretch(1)

        footer = _dialog_footer(root)
        footer.addStretch(1)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setProperty("role", "ok")
        btns.button(QDialogButtonBox.Ok).setIcon(icon("check", "on-accent"))
        btns.button(QDialogButtonBox.Cancel).setProperty("role", "ghost")
        btns.button(QDialogButtonBox.Cancel).setIcon(icon("x"))
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        footer.addWidget(btns)

        if existing:
            self._load(existing)
        self._sync()

    @staticmethod
    def _box_form(box):
        """A form inside a group-box card, label column right-aligned."""
        form = QFormLayout(box)
        form.setContentsMargins(10, 8, 10, 10)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        return form

    def _detect(self):
        self._scan_into(self.serial, None, 5037, quiet=True)

    def _detect_remote(self):
        host = self.srv_host.text().strip()
        if not host:
            return
        self._scan_into(self.rserial, host, self.srv_port.value(), quiet=False)

    def _scan_into(self, combo, host, port, *, quiet):
        """List devices OFF the UI thread — a remote scan blocks for up to the
        adb timeout (15 s), which used to freeze this modal dialog."""
        if thread_running(self._scan):
            return
        prev = combo.currentText().strip()
        combo.clear()
        combo.setEditText("scanning…")
        self._scan = _ScanThread(host, port, self._adb_path)

        def done(devs):
            combo.clear()
            for d in devs:
                combo.addItem(d.serial)
            if not devs:
                combo.setEditText(prev)

        def fail(msg):
            combo.clear()
            combo.setEditText(prev)
            if not quiet:
                QMessageBox.warning(self, "Remote ADB server", msg)

        self._scan.done.connect(done)
        self._scan.fail.connect(fail)
        self._scan.finished.connect(
            lambda t=self._scan: self._clear_scan_thread(t)
        )
        self._scan.start()
        park_thread(self._scan)  # survive the dialog closing mid-scan

    def _clear_scan_thread(self, thread):
        if self._scan is thread:
            self._scan = None

    def _sync(self, *_):
        m = self.mode.currentIndex()
        self.usb_box.setEnabled(m == 0)
        self.net_box.setEnabled(m == 1)
        self.rem_box.setEnabled(m == 2)

    def _load(self, s):
        s = normalize_session(s)
        self._original_name = s["name"]
        self.name.setText(s["name"])
        self.mode.setCurrentIndex(_INDEX[s["type"]])
        self.serial.setEditText(s.get("serial", ""))
        self.host.setText(s.get("host", ""))
        self.port.setValue(s.get("port", 5555))
        self.srv_host.setText(s.get("adb_host", ""))
        self.srv_port.setValue(s.get("adb_port", 5037))
        self.rserial.setEditText(s.get("serial", "") if s["type"] == "remote" else "")

    def _release_scan(self):
        """Detach a running scan so it can never call back into this dialog."""
        scan, self._scan = self._scan, None
        if scan is not None:
            disconnect_signals(scan)
            park_thread(scan)

    def closeEvent(self, event):
        self._release_scan()
        super().closeEvent(event)

    def accept(self):
        """Validate before closing: a bad target used to close the dialog and
        then raise ``ValueError`` from ``normalize_session`` in the caller."""
        session = self.result_session()
        try:
            normalized = normalize_session(session)
        except ValueError as exc:
            QMessageBox.warning(self, "Device target", f"Please fix the target: {exc}.")
            return
        name = normalized["name"]
        if name != self._original_name:
            try:
                taken = name in SessionStore().names()
            except Exception:  # an unreadable store must not block saving
                taken = False
            if taken and QMessageBox.question(
                self,
                "Device target",
                f"A saved target named '{name}' already exists. Replace it?",
            ) != QMessageBox.Yes:
                return
        super().accept()

    def done(self, result):
        # accept()/reject() never run closeEvent, so the scan cleanup lives here.
        self._release_scan()
        super().done(result)
        if self.parent() is not None:
            # exec_() callers read result_session() synchronously right after
            # exec_ returns; the deferred delete only runs once control is back
            # in the outer event loop, so the dialog no longer leaks per use.
            self.deleteLater()

    def result_session(self) -> dict:
        t = _TYPE[self.mode.currentIndex()]
        out = {"name": self.name.text().strip(), "type": t}
        if t == "usb":
            out["serial"] = self.serial.currentText().strip()
        elif t == "network":
            out["host"] = self.host.text().strip()
            out["port"] = self.port.value()
        else:
            out["adb_host"] = self.srv_host.text().strip()
            out["adb_port"] = self.srv_port.value()
            out["serial"] = self.rserial.currentText().strip()
        if self._original_name and self._original_name != out["name"]:
            # SessionStore.save() uses this to rename rather than copy.
            out["previous_name"] = self._original_name
        return out


def _wrap(layout):
    w = QWidget()
    w.setLayout(layout)
    return w
