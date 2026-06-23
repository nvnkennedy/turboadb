"""One clear Connect dialog for all three ways to reach a device:

  • USB device         — pick from the devices plugged into THIS PC
  • Network device     — a device reachable by IP (Wi-Fi / Ethernet head unit)
  • Remote PC's ADB    — a device plugged into ANOTHER machine; we list and drive
                         the devices on that machine's adb server

Host fields remember recent machines, scanning runs off the UI thread, and the
target is saved by default (auto-named) so you can close and reopen it from the
sidebar with a double-click."""

from __future__ import annotations

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                             QLabel, QComboBox, QLineEdit, QSpinBox, QPushButton,
                             QListWidget, QListWidgetItem, QStackedWidget,
                             QDialogButtonBox, QWidget, QCheckBox)

from . import settings as settings_mod


def _split_host_port(text, default_port):
    """Accept a host typed WITH a port ('10.0.0.5:5037') and return
    (host, port), so the port is never accidentally doubled downstream."""
    text = (text or "").strip()
    host, _, p = text.rpartition(":")
    if host and p.isdigit():
        return host, int(p)
    return text, default_port


class _ScanThread(QThread):
    done = pyqtSignal(list)
    fail = pyqtSignal(str)

    def __init__(self, server_host=None, server_port=5037):
        super().__init__()
        self.server_host, self.server_port = server_host, server_port

    def run(self):
        try:
            from ..devices import list_devices
            devs = list_devices(server_host=self.server_host,
                                server_port=self.server_port)
            self.done.emit(devs)
        except Exception as exc:
            self.fail.emit(str(exc))


class _ServeThread(QThread):
    done = pyqtSignal(str)
    fail = pyqtSignal(str)

    def __init__(self, port=5037, install_login=False):
        super().__init__()
        self.port, self.install_login = port, install_login

    def run(self):
        try:
            from ..devices import (start_shared_server, install_startup,
                                   open_firewall)
            msg = start_shared_server(port=self.port)
            msg += "  ·  " + open_firewall((self.port, 27184))
            if self.install_login:
                path = install_startup(port=self.port)
                msg += f"  ·  auto-starts at login ({path})"
            self.done.emit(msg)
        except Exception as exc:
            self.fail.emit(str(exc))


class ConnectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to a device")
        self.resize(560, 520)
        self._scan = None
        self._name_locked = False
        lay = QVBoxLayout(self)

        top = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItems(["USB — device plugged into this PC",
                            "Network — device reachable by IP (Wi-Fi/Ethernet)",
                            "Remote — device on ANOTHER PC's adb server"])
        self.mode.currentIndexChanged.connect(self._on_mode)
        top.addRow("How is it connected?", self.mode)
        lay.addLayout(top)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._usb_page())
        self.stack.addWidget(self._net_page())
        self.stack.addWidget(self._remote_page())
        lay.addWidget(self.stack, 1)

        save = QHBoxLayout()
        self.save_chk = QCheckBox("Save this target")
        self.save_chk.setChecked(True)
        self.save_chk.setToolTip("Saved targets appear in the sidebar — "
                                 "double-click to reconnect any time.")
        self.save_name = QLineEdit()
        self.save_name.setPlaceholderText("name (auto)")
        self.save_name.textEdited.connect(lambda *_: setattr(self, "_name_locked", True))
        save.addWidget(self.save_chk)
        save.addWidget(QLabel("as")); save.addWidget(self.save_name, 1)
        lay.addLayout(save)

        btns = QDialogButtonBox()
        self.connect_btn = btns.addButton("Connect", QDialogButtonBox.AcceptRole)
        btns.addButton(QDialogButtonBox.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._on_mode(0)
        self._scan_usb()

    # ---- pages ----
    def _usb_page(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.addWidget(QLabel("Devices plugged into this PC:"))
        self.usb_list = QListWidget()
        self.usb_list.currentItemChanged.connect(lambda *_: self._autoname())
        self.usb_list.itemDoubleClicked.connect(lambda _: self._accept())
        v.addWidget(self.usb_list, 1)
        row = QHBoxLayout()
        r = QPushButton("Refresh"); r.setProperty("role", "ghost")
        r.clicked.connect(self._scan_usb)
        self.usb_status = QLabel("")
        row.addWidget(r); row.addWidget(self.usb_status, 1)
        v.addLayout(row)
        v.addWidget(QLabel("No device? Enable USB debugging on the phone. "
                           "Pick none to use the only device."))
        return w

    def _net_page(self):
        w = QWidget(); f = QFormLayout(w)
        self.net_host = QComboBox(); self.net_host.setEditable(True)
        self.net_host.addItems(settings_mod.get("recent_network_hosts") or [])
        self.net_host.setCurrentText("")
        self.net_host.lineEdit().setPlaceholderText("192.168.1.50  or  my-headunit.local")
        self.net_host.editTextChanged.connect(lambda *_: self._autoname())
        self.net_port = QSpinBox(); self.net_port.setRange(1, 65535); self.net_port.setValue(5555)
        f.addRow("Device IP / hostname", self.net_host)
        f.addRow("Port", self.net_port)
        f.addRow(QLabel("Enable wireless first: on the device (over USB once)\n"
                        "run  adb tcpip 5555  — or use Android 11+ Wireless debugging."))
        return w

    def _remote_page(self):
        w = QWidget(); v = QVBoxLayout(w)
        f = QFormLayout()
        self.rem_host = QComboBox(); self.rem_host.setEditable(True)
        self.rem_host.addItems(settings_mod.get("recent_remote_hosts") or [])
        self.rem_host.setCurrentText("")
        self.rem_host.lineEdit().setPlaceholderText("192.168.1.20  or  rdp-pc.corp.local")
        self.rem_host.editTextChanged.connect(lambda *_: self._autoname())
        self.rem_port = QSpinBox(); self.rem_port.setRange(1, 65535); self.rem_port.setValue(5037)
        f.addRow("That PC's IP / hostname", self.rem_host)
        f.addRow("adb server port", self.rem_port)
        v.addLayout(f)
        v.addWidget(QLabel("Hostnames work — they're resolved automatically for "
                           "the mirror tunnel."))

        # If you're sitting at (or RDP'd into) the PC that has the device, this
        # starts its shared adb server for you — no more typing the nodaemon
        # command. Tick "at login" to make it permanent.
        srv = QHBoxLayout()
        self.btn_serve = QPushButton("Start shared server on THIS PC")
        self.btn_serve.setProperty("role", "ghost")
        self.btn_serve.setToolTip("Run this ON the machine that has the device. "
                                  "It exposes that PC's adb server to the network "
                                  "so you can reach it from here. Replaces the "
                                  "manual 'adb -a nodaemon server start'.")
        self.btn_serve.clicked.connect(self._start_shared)
        self.chk_login = QCheckBox("at login")
        self.chk_login.setToolTip("Also start it automatically every Windows "
                                  "login, so it never has to be done again.")
        srv.addWidget(self.btn_serve); srv.addWidget(self.chk_login); srv.addStretch(1)
        v.addLayout(srv)

        row = QHBoxLayout()
        scan = QPushButton("Scan devices there"); scan.setProperty("role", "ok")
        scan.clicked.connect(self._scan_remote)
        self.rem_status = QLabel("")
        row.addWidget(scan); row.addWidget(self.rem_status, 1)
        v.addLayout(row)
        self.rem_list = QListWidget()
        self.rem_list.currentItemChanged.connect(lambda *_: self._autoname())
        self.rem_list.itemDoubleClicked.connect(lambda _: self._accept())
        v.addWidget(self.rem_list, 1)
        return w

    # ---- mode ----
    def _on_mode(self, idx):
        self.stack.setCurrentIndex(idx)
        if idx == 2 and self.rem_host.currentText().strip():
            self._scan_remote()                      # auto-scan a known remote host
        self._autoname()

    # ---- auto name ----
    def _autoname(self):
        if self._name_locked:
            return
        m = self.mode.currentIndex()
        name = ""
        if m == 0:
            it = self.usb_list.currentItem()
            name = it.data(Qt.UserRole) if it else ""
        elif m == 1:
            h = self.net_host.currentText().strip()
            name = f"{h}:{self.net_port.value()}" if h else ""
        else:
            it = self.rem_list.currentItem()
            h = self.rem_host.currentText().strip()
            if it and h:
                name = f"{it.data(Qt.UserRole)} @ {h}"
        self.save_name.setText(name)

    # ---- shared server ----
    def _start_shared(self):
        self.btn_serve.setEnabled(False)
        self.rem_status.setText("starting shared adb server here…")
        self._serve = _ServeThread(self.rem_port.value(),
                                   self.chk_login.isChecked())
        self._serve.done.connect(self._served)
        self._serve.fail.connect(self._serve_failed)
        self._serve.start()

    def _served(self, msg):
        self.btn_serve.setEnabled(True)
        self.rem_status.setText(msg)
        # if we just exposed THIS PC, point the host field at it and scan
        if not self.rem_host.currentText().strip():
            self.rem_host.setCurrentText("127.0.0.1")
        self._scan_remote()

    def _serve_failed(self, msg):
        self.btn_serve.setEnabled(True)
        self.rem_status.setText("couldn't start server: " + msg)

    # ---- scanning ----
    def _scan_usb(self):
        self.usb_status.setText("scanning…"); self.usb_list.clear()
        self._start_scan(None, 5037, self._fill_usb, self.usb_status)

    def _scan_remote(self):
        host, port = _split_host_port(self.rem_host.currentText(),
                                      self.rem_port.value())
        if not host:
            self.rem_status.setText("enter the PC's IP first"); return
        if port != self.rem_port.value():
            self.rem_port.setValue(port)          # reflect a typed-in :port
            self.rem_host.setCurrentText(host)
        self.rem_status.setText("scanning…"); self.rem_list.clear()
        self._start_scan(host, port, self._fill_remote, self.rem_status)

    def _start_scan(self, host, port, on_done, status_label):
        if self._scan and self._scan.isRunning():
            return
        self._scan = _ScanThread(host, port)
        self._scan.done.connect(lambda devs: (on_done(devs),
                                              status_label.setText(
                                                  f"{len(devs)} device(s)")))
        self._scan.fail.connect(lambda m: status_label.setText(m))
        self._scan.start()

    def _fill(self, widget, devs):
        widget.clear()
        for d in devs:
            it = QListWidgetItem(f"{d.serial}    {d.label} · {d.state}")
            it.setData(Qt.UserRole, d.serial)
            widget.addItem(it)
        if devs:
            widget.setCurrentRow(0)

    def _fill_usb(self, devs):
        self._fill(self.usb_list, devs); self._autoname()

    def _fill_remote(self, devs):
        self._fill(self.rem_list, devs); self._autoname()

    # ---- result ----
    def _accept(self):
        if self.session() is None:
            return
        self.accept()

    def session(self):
        m = self.mode.currentIndex()
        name = self.save_name.text().strip() if self.save_chk.isChecked() else ""
        if m == 0:
            it = self.usb_list.currentItem()
            return {"name": name, "type": "usb",
                    "serial": (it.data(Qt.UserRole) if it else "") or ""}
        if m == 1:
            host, port = _split_host_port(self.net_host.currentText(),
                                         self.net_port.value())
            if not host:
                self.net_host.setFocus(); return None
            settings_mod.add_recent("recent_network_hosts", host)
            return {"name": name, "type": "network", "host": host, "port": port}
        host, port = _split_host_port(self.rem_host.currentText(),
                                      self.rem_port.value())
        it = self.rem_list.currentItem()
        if not host or it is None:
            self.rem_status.setText("enter the IP and Scan, then pick a device")
            return None
        settings_mod.add_recent("recent_remote_hosts", host)
        return {"name": name, "type": "remote", "adb_host": host,
                "adb_port": port, "serial": it.data(Qt.UserRole) or ""}
