"""Dialog to create / edit a saved device target.

Three connection modes — only the fields for the chosen mode are enabled:
  * USB device          — pick a serial (or leave blank for the only device)
  * Network device      — a device reachable by IP (Wi-Fi / Ethernet head unit)
  * Remote ADB server   — a device plugged into ANOTHER machine; we talk to that
                          machine's adb server (adb -H host -P 5037), so you see
                          and drive the devices attached over there.
"""

from __future__ import annotations

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QLineEdit,
                             QSpinBox, QComboBox, QDialogButtonBox, QGroupBox,
                             QLabel, QHBoxLayout, QPushButton, QWidget)

from ..devices import list_devices

_MODES = ["USB device", "Network device (Wi-Fi / Ethernet)",
          "Remote ADB server (another PC)"]
_TYPE = {0: "usb", 1: "network", 2: "remote"}
_INDEX = {"usb": 0, "network": 1, "remote": 2}


class SessionDialog(QDialog):
    def __init__(self, parent=None, existing: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Device target")
        self.resize(440, 360)
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self.name = QLineEdit()
        self.mode = QComboBox(); self.mode.addItems(_MODES)
        self.mode.currentIndexChanged.connect(self._sync)
        form.addRow("Name", self.name)
        form.addRow("Connection", self.mode)
        lay.addLayout(form)

        # USB
        self.usb_box = QGroupBox("USB device")
        uf = QFormLayout(self.usb_box)
        row = QHBoxLayout()
        self.serial = QComboBox(); self.serial.setEditable(True)
        pick = QPushButton("Detect"); pick.setProperty("role", "ghost")
        pick.clicked.connect(self._detect)
        row.addWidget(self.serial, 1); row.addWidget(pick)
        uf.addRow("Serial", _wrap(row))
        lay.addWidget(self.usb_box)

        # Network device
        self.net_box = QGroupBox("Network device")
        nf = QFormLayout(self.net_box)
        self.host = QLineEdit(); self.host.setPlaceholderText("192.168.1.50")
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(5555)
        nf.addRow("Host / IP", self.host)
        nf.addRow("Port", self.port)
        lay.addWidget(self.net_box)

        # Remote adb server
        self.rem_box = QGroupBox("Remote ADB server")
        rf = QFormLayout(self.rem_box)
        self.srv_host = QLineEdit(); self.srv_host.setPlaceholderText("192.168.1.20")
        self.srv_port = QSpinBox(); self.srv_port.setRange(1, 65535); self.srv_port.setValue(5037)
        rowr = QHBoxLayout()
        self.rserial = QComboBox(); self.rserial.setEditable(True)
        self.rserial.setToolTip("Device serial on that machine (blank = only device)")
        rpick = QPushButton("List"); rpick.setProperty("role", "ghost")
        rpick.clicked.connect(self._detect_remote)
        rowr.addWidget(self.rserial, 1); rowr.addWidget(rpick)
        rf.addRow("Server host / IP", self.srv_host)
        rf.addRow("Server port", self.srv_port)
        rf.addRow("Device serial", _wrap(rowr))
        lay.addWidget(self.rem_box)

        lay.addWidget(QLabel("Remote server: on that machine run once →  "
                             "adb -a nodaemon server start"))

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        if existing:
            self._load(existing)
        self._sync()

    def _detect(self):
        try:
            self.serial.clear()
            for d in list_devices():
                self.serial.addItem(d.serial)
        except Exception:
            pass

    def _detect_remote(self):
        host = self.srv_host.text().strip()
        if not host:
            return
        try:
            self.rserial.clear()
            for d in list_devices(server_host=host, server_port=self.srv_port.value()):
                self.rserial.addItem(d.serial)
        except Exception as exc:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Remote ADB server", str(exc))

    def _sync(self, *_):
        m = self.mode.currentIndex()
        self.usb_box.setEnabled(m == 0)
        self.net_box.setEnabled(m == 1)
        self.rem_box.setEnabled(m == 2)

    def _load(self, s):
        self.name.setText(s.get("name", ""))
        self.mode.setCurrentIndex(_INDEX.get(s.get("type", "usb"), 0))
        self.serial.setEditText(s.get("serial", ""))
        self.host.setText(s.get("host", ""))
        self.port.setValue(int(s.get("port", 5555)))
        self.srv_host.setText(s.get("adb_host", ""))
        self.srv_port.setValue(int(s.get("adb_port", 5037)))
        self.rserial.setEditText(s.get("serial", "") if s.get("type") == "remote" else "")

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
        return out


def _wrap(layout):
    w = QWidget(); w.setLayout(layout)
    return w
