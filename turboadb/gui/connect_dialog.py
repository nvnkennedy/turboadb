"""One clear Connect dialog for all three ways to reach a device:

  • USB device         — pick from the devices plugged into THIS PC
  • Network device     — a device reachable by IP (Wi-Fi / Ethernet head unit)
  • Remote PC's ADB    — a device plugged into ANOTHER machine; we list and drive
                         the devices on that machine's adb server

Host fields remember recent machines, scanning runs off the UI thread, and the
target is saved by default (auto-named) so you can close and reopen it from the
sidebar with a double-click."""

from __future__ import annotations

import logging
import os

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QComboBox,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QDialogButtonBox,
    QWidget,
    QCheckBox,
    QFrame,
)

from ..config import format_host_port

# The one shared endpoint parser: a host typed WITH a port ("10.0.0.5:5037") is
# split here so it is never doubled downstream, and a bare IPv6 literal
# ("fe80::1") is never mistaken for a host:port pair.
from ..config import parse_host_port as _split_host_port
from ..scrcpy import TUNNEL_PORT_FIREWALL_RANGE
from . import settings as settings_mod
from .adb_path import gui_adb_path
from .icons import icon
from .qtutil import disconnect_signals, park_thread, thread_running

_log = logging.getLogger(__name__)

# The three ways to reach a device, colour-coded the same everywhere
# (Connect and the saved-target dialog): (icon, tone).
TRANSPORT_ICONS = (("usb", "green"), ("wifi", "blue"), ("server", "purple"))

# Device-list rows: a phone tinted by its adb state.
_STATE_TONES = {"device": "green", "unauthorized": "amber", "offline": "red"}


def _dialog_footer(parent_layout):
    """A full-width #dialogFooter row; returns its layout (buttons go right)."""
    footer = QWidget()
    footer.setObjectName("dialogFooter")
    footer.setAttribute(Qt.WA_StyledBackground, True)
    row = QHBoxLayout(footer)
    row.setContentsMargins(16, 10, 16, 10)
    row.setSpacing(8)
    parent_layout.addWidget(footer)
    return row


def _form_layout(parent=None):
    form = QFormLayout(parent) if parent is not None else QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    form.setHorizontalSpacing(12)
    form.setVerticalSpacing(8)
    return form


def _list_card(list_widget):
    """Frame a device list in a #card so it reads as a box, not floating text."""
    card = QFrame()
    card.setObjectName("card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(6, 6, 6, 6)
    lay.addWidget(list_widget)
    return card


def _muted(text):
    label = QLabel(text)
    label.setObjectName("mutedHint")
    label.setWordWrap(True)
    return label


def _endpoint_name(text, port, default_port=None):
    """The endpoint a session built from *text* and the *port* box uses (a port
    typed after the host wins, as in ``_build_session``), written as saved
    targets name it; without the port when it is *default_port*.  "" when no
    host is entered."""
    host, port = _split_host_port(text, port)
    if not host:
        return ""
    if port == default_port:
        return host
    try:
        return format_host_port(host, port)
    except ValueError:  # a typed port out of range: the connect says so
        return (text or "").strip()


class _ScanThread(QThread):
    done = pyqtSignal(list)
    fail = pyqtSignal(str)

    def __init__(self, server_host=None, server_port=5037, adb_path=None):
        super().__init__()
        self.server_host, self.server_port = server_host, server_port
        self.adb_path = adb_path

    def run(self):
        try:
            from ..devices import list_devices

            devs = list_devices(
                adb_path=self.adb_path,
                server_host=self.server_host,
                server_port=self.server_port,
            )
            self.done.emit(devs)
        except Exception as exc:
            self.fail.emit(str(exc))


class _ServeThread(QThread):
    done = pyqtSignal(str)
    fail = pyqtSignal(str)

    def __init__(self, port=5037, install_login=False, adb_path=None):
        super().__init__()
        self.port, self.install_login = port, install_login
        self.adb_path = adb_path
        # whether the local adb server (the device tabs') answers afterwards
        self.server_up = False

    def run(self):
        try:
            from ..devices import start_shared_server, install_startup, open_firewall
            from ..tools import local_adb_port

            msg = "Restarting the local ADB server; active sessions may reconnect briefly.  ·  "
            msg += start_shared_server(port=self.port, adb_path=self.adb_path)
            # The engine's own firewall spelling of the scrcpy tunnel ports, so
            # the rule can never drift from the ports scrcpy actually uses; the
            # adb port is the one the server listens on (ANDROID_ADB_SERVER_PORT
            # moves the default, and start_shared_server follows it).
            msg += "  ·  " + open_firewall(
                (local_adb_port(self.port), TUNNEL_PORT_FIREWALL_RANGE))
            if self.install_login:
                # the launcher runs the adb this server was started with
                path = install_startup(port=self.port, adb_path=self.adb_path)
                msg += f"  ·  auto-starts at login ({path})"
            self.done.emit(msg)
        except Exception as exc:
            self.fail.emit(str(exc))
        finally:
            try:
                from ..tools import is_adb_server_alive

                self.server_up = is_adb_server_alive()
            except Exception:
                self.server_up = False


class ConnectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to a device")
        self.resize(560, 520)
        self._scan = None
        # Scans asked for while one runs: one per page, keyed by its status
        # line, run in turn (see _start_scan).
        self._pending_scans = {}
        self._serve = None
        self._closing = False
        self._result = None  # the target chosen on Connect (computed once)
        self._adb_path = gui_adb_path()
        self._name_locked = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        lay = QVBoxLayout()
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(12)
        root.addLayout(lay, 1)

        top = _form_layout()
        self.mode = QComboBox()
        for (glyph, tone), label in zip(
            TRANSPORT_ICONS,
            (
                "USB — device plugged into this PC",
                "Network — device reachable by IP (Wi-Fi/Ethernet)",
                "Remote — device on ANOTHER PC's adb server",
            ),
        ):
            self.mode.addItem(icon(glyph, tone), label)
        self.mode.currentIndexChanged.connect(self._on_mode)
        top.addRow("How is it connected?", self.mode)
        lay.addLayout(top)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._usb_page())
        self.stack.addWidget(self._net_page())
        self.stack.addWidget(self._remote_page())
        lay.addWidget(self.stack, 1)

        save = QHBoxLayout()
        save.setSpacing(8)
        self.save_chk = QCheckBox("Save this target")
        self.save_chk.setChecked(True)
        self.save_chk.setToolTip(
            "Saved targets appear in the sidebar — double-click to reconnect any time."
        )
        self.save_name = QLineEdit()
        self.save_name.setPlaceholderText("name (auto)")
        self.save_name.textEdited.connect(lambda *_: setattr(self, "_name_locked", True))
        save.addWidget(self.save_chk)
        save.addWidget(QLabel("as"))
        save.addWidget(self.save_name, 1)
        lay.addLayout(save)

        footer = _dialog_footer(root)
        footer.addStretch(1)
        btns = QDialogButtonBox()
        self.connect_btn = btns.addButton("Connect", QDialogButtonBox.AcceptRole)
        self.connect_btn.setProperty("role", "ok")
        self.connect_btn.setIcon(icon("plug", "on-accent"))
        self.connect_btn.setDefault(True)
        cancel = btns.addButton(QDialogButtonBox.Cancel)
        cancel.setProperty("role", "ghost")
        cancel.setIcon(icon("x"))
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        footer.addWidget(btns)

        self._on_mode(0)
        self._scan_usb()

    # ---- pages ----
    def _usb_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(QLabel("Devices plugged into this PC"))
        self.usb_list = QListWidget()
        self.usb_list.currentItemChanged.connect(lambda *_: self._autoname())
        self.usb_list.itemDoubleClicked.connect(lambda _: self._accept())
        v.addWidget(_list_card(self.usb_list), 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        r = QPushButton("Refresh")
        r.setProperty("role", "ghost")
        r.setIcon(icon("refresh", "green"))
        r.clicked.connect(self._scan_usb)
        self.usb_status = QLabel("")
        row.addWidget(r)
        row.addWidget(self.usb_status, 1)
        v.addLayout(row)
        v.addWidget(
            _muted("No device? Enable USB debugging on the phone. Pick none to use the only device.")
        )
        return w

    def _net_page(self):
        w = QWidget()
        f = _form_layout(w)
        self.net_host = QComboBox()
        self.net_host.setEditable(True)
        self.net_host.addItems(settings_mod.get("recent_network_hosts") or [])
        self.net_host.setCurrentText("")
        self.net_host.lineEdit().setPlaceholderText("192.168.1.50  or  my-headunit.local")
        self.net_host.editTextChanged.connect(lambda *_: self._autoname())
        self.net_port = QSpinBox()
        self.net_port.setRange(1, 65535)
        self.net_port.setValue(5555)
        self.net_port.valueChanged.connect(lambda *_: self._autoname())
        f.addRow("Device IP / hostname", self.net_host)
        f.addRow("Port", self.net_port)
        f.addRow(
            "",
            _muted(
                "Enable wireless first: on the device (over USB once)\n"
                "run  adb tcpip 5555  — or use Android 11+ Wireless debugging."
            ),
        )
        return w

    def _remote_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        f = _form_layout()
        self.rem_host = QComboBox()
        self.rem_host.setEditable(True)
        self.rem_host.addItems(settings_mod.get("recent_remote_hosts") or [])
        self.rem_host.setCurrentText("")
        self.rem_host.lineEdit().setPlaceholderText("192.168.1.20  or  rdp-pc.corp.local")
        self.rem_host.editTextChanged.connect(lambda *_: self._autoname())
        self.rem_port = QSpinBox()
        self.rem_port.setRange(1, 65535)
        self.rem_port.setValue(5037)
        self.rem_port.valueChanged.connect(lambda *_: self._autoname())
        f.addRow("That PC's IP / hostname", self.rem_host)
        f.addRow("adb server port", self.rem_port)
        v.addLayout(f)
        v.addWidget(
            _muted("Hostnames work — they're resolved automatically for the mirror tunnel.")
        )

        # If you're sitting at (or RDP'd into) the PC that has the device, this
        # starts its shared adb server for you — no more typing the nodaemon
        # command. Tick "at login" to make it permanent.
        srv = QHBoxLayout()
        srv.setSpacing(8)
        self.btn_serve = QPushButton("Start shared server on THIS PC")
        self.btn_serve.setIcon(icon("server", "purple"))
        self.btn_serve.setToolTip(
            "Run this ON the machine that has the device. "
            "It exposes that PC's adb server to the network "
            "so you can reach it from here. Replaces the "
            "manual 'adb -a nodaemon server start'."
        )
        self.btn_serve.clicked.connect(self._start_shared)
        self.chk_login = QCheckBox("at login")
        self.chk_login.setToolTip(
            "Also start it automatically every Windows login, so it never has to be done again."
        )
        srv.addWidget(self.btn_serve)
        srv.addWidget(self.chk_login)
        if os.name != "nt":
            # The login auto-start is a launcher in the Windows Startup folder:
            # elsewhere it could only fail, after the server had started.
            self.chk_login.hide()
        srv.addStretch(1)
        v.addLayout(srv)

        row = QHBoxLayout()
        row.setSpacing(8)
        scan = QPushButton("Scan devices there")
        scan.setIcon(icon("search", "accent"))
        scan.clicked.connect(self._scan_remote)
        self.rem_status = QLabel("")
        row.addWidget(scan)
        row.addWidget(self.rem_status, 1)
        v.addLayout(row)
        self.rem_list = QListWidget()
        self.rem_list.currentItemChanged.connect(lambda *_: self._autoname())
        self.rem_list.itemDoubleClicked.connect(lambda _: self._accept())
        v.addWidget(_list_card(self.rem_list), 1)
        return w

    # ---- mode ----
    def _on_mode(self, idx):
        self.stack.setCurrentIndex(idx)
        if idx == 2 and self.rem_host.currentText().strip():
            self._scan_remote()  # auto-scan a known remote host
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
            # the endpoint it connects to: "10.0.0.7:5556" was saved as
            # "10.0.0.7:5556:5555" while the connection went to 5556
            name = _endpoint_name(self.net_host.currentText(), self.net_port.value())
        else:
            it = self.rem_list.currentItem()
            server = _endpoint_name(self.rem_host.currentText(), self.rem_port.value(), 5037)
            if it and server:
                name = f"{it.data(Qt.UserRole)} @ {server}"
        self.save_name.setText(name)

    # ---- shared server ----
    def _confirm_share(self, port) -> bool:
        from ..tools import local_adb_port

        restarts = local_adb_port(port) == local_adb_port()
        text = (
            "Share the devices plugged into THIS PC with other machines?\n\n"
            f"An adb server on port {port} starts listening on the network"
            + (" (the local one restarts, so open device tabs reconnect)" if restarts else "")
            + f", and TCP {port} + {TUNNEL_PORT_FIREWALL_RANGE} open in the Windows firewall "
            "for Domain and Private networks (that needs Administrator)."
            + (" It starts again at every Windows login." if self.chk_login.isChecked() else "")
            + "\n\nThe adb server has no password: anyone who can reach this PC on "
            "that port can control its devices. Share only on networks you trust."
        )
        return QMessageBox.question(
            self, "Share this PC's devices", text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) == QMessageBox.Yes

    def _start_shared(self):
        if thread_running(self._serve):
            return
        window = self.parent()
        busy = getattr(window, "server_busy", None)
        if callable(busy) and busy():
            self.rem_status.setText(
                "The adb server is busy (a restart, a share or a tools download) — "
                "try again once it has finished.")
            return
        port = self.rem_port.value()
        # the main window's Share asks first as well; this one restarts the
        # adb server and exposes the devices just the same
        if not self._confirm_share(port):
            return
        self.btn_serve.setEnabled(False)
        self.rem_status.setText("starting shared adb server here…")
        thread = _ServeThread(port, self.chk_login.isChecked(), self._adb_path)
        self._serve = thread
        thread.done.connect(self._served)
        thread.fail.connect(self._serve_failed)
        thread.finished.connect(lambda t=thread: self._clear_serve_thread(t))
        park_thread(thread)
        # The window runs it as its own Share (MainWindow.run_share_task): its
        # poll pauses, and so do the device tabs' shells and logcats when the
        # port is theirs.  It gives them back on `finished`, which outlives
        # this dialog (closing it disconnects only done/fail).
        run = getattr(window, "run_share_task", None)
        if callable(run):
            run(thread, port)
        else:
            thread.start()

    def _clear_serve_thread(self, thread) -> None:
        if self._serve is thread:
            self._serve = None

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
        self.usb_status.setText("scanning…")
        self.usb_list.clear()
        self._start_scan(None, 5037, self._fill_usb, self.usb_status)

    def _scan_remote(self):
        host, port = _split_host_port(self.rem_host.currentText(), self.rem_port.value())
        if not host:
            self.rem_status.setText("enter the PC's IP first")
            return
        if port != self.rem_port.value():
            self.rem_port.setValue(port)  # reflect a typed-in :port
            self.rem_host.setCurrentText(host)
        self.rem_status.setText("scanning…")
        self.rem_list.clear()
        self._start_scan(host, port, self._fill_remote, self.rem_status)

    def _start_scan(self, host, port, on_done, status_label):
        request = (host, port, on_done, status_label)
        if thread_running(self._scan):
            # One scan runs at a time.  Each page keeps its newest request and
            # they run in turn: with one request for both, a USB Refresh
            # replaced the Remote page's queued scan, and that page stayed at
            # "scanning…" forever.
            self._pending_scans[status_label] = request
            return
        self._launch_scan(*request)

    def _launch_scan(self, host, port, on_done, status_label):
        scan = _ScanThread(host, port, self._adb_path)
        self._scan = scan
        scan.done.connect(
            lambda devs, t=scan: self._finish_scan(t, on_done, status_label, devs, None)
        )
        scan.fail.connect(
            lambda message, t=scan: self._finish_scan(t, on_done, status_label, None, message)
        )
        park_thread(scan)  # survive the dialog closing mid-scan
        scan.start()

    def _finish_scan(self, scan, on_done, status_label, devices, error):
        if scan is not self._scan:
            return
        self._scan = None
        if not self._closing:
            if error is None:
                on_done(devices)
                status_label.setText(f"{len(devices)} device(s)")
            else:
                status_label.setText(error)
        if self._pending_scans and not self._closing:
            # The page that asked first goes next.  Its line says "scanning…"
            # again: a scan of that same page may have just shown its result.
            pending = self._pending_scans.pop(next(iter(self._pending_scans)))
            pending[3].setText("scanning…")
            self._launch_scan(*pending)

    def _release_threads(self):
        """Keep background QThreads alive after this short-lived dialog closes,
        but never let them call back into it."""
        self._closing = True
        self._pending_scans.clear()
        for thread in (self._scan, self._serve):
            if thread is None:
                continue
            disconnect_signals(thread)
            park_thread(thread)

    def closeEvent(self, event):
        self._release_threads()
        super().closeEvent(event)

    def done(self, result):
        # accept()/reject() (Connect, Cancel, Esc) never run closeEvent, so the
        # thread cleanup lives here.
        self._release_threads()
        super().done(result)
        if self.parent() is not None:
            # exec_() callers read session() synchronously right after exec_
            # returns; the deferred delete only runs back in the outer event
            # loop, so the dialog is no longer leaked on every use.
            self.deleteLater()

    def _fill(self, widget, devs):
        widget.clear()
        for d in devs:
            tone = _STATE_TONES.get(str(d.state), "dim")
            it = QListWidgetItem(icon("smartphone", tone), f"{d.serial}    {d.label} · {d.state}")
            it.setData(Qt.UserRole, d.serial)
            widget.addItem(it)
        if devs:
            widget.setCurrentRow(0)

    def _fill_usb(self, devs):
        self._fill(self.usb_list, devs)
        self._autoname()

    def _fill_remote(self, devs):
        self._fill(self.rem_list, devs)
        self._autoname()

    # ---- result ----
    def _accept(self):
        result = self._build_session()
        if result is None:
            return
        # Remember the host once, on Connect — session() is also called by the
        # main window afterwards and must not write settings a second time.
        try:
            if result["type"] == "network":
                settings_mod.add_recent("recent_network_hosts", result["host"])
            elif result["type"] == "remote":
                settings_mod.add_recent("recent_remote_hosts", result["adb_host"])
        except (OSError, ValueError) as exc:
            # Only the list of recent hosts is lost (a settings file another
            # program keeps locked, or one that can't be read): the connection
            # goes ahead.
            _log.warning("could not remember the host in the recent list: %s", exc)
        self._result = result
        self.accept()

    def session(self):
        """The chosen target (``None`` if the form is incomplete).  Side-effect
        free: after Connect it returns the result computed at that moment."""
        if self._result is not None:
            return dict(self._result)
        return self._build_session()

    def _build_session(self):
        m = self.mode.currentIndex()
        name = self.save_name.text().strip() if self.save_chk.isChecked() else ""
        if m == 0:
            it = self.usb_list.currentItem()
            return {
                "name": name,
                "type": "usb",
                "serial": (it.data(Qt.UserRole) if it else "") or "",
            }
        if m == 1:
            host, port = _split_host_port(self.net_host.currentText(), self.net_port.value())
            if not host:
                self.net_host.setFocus()
                return None
            return {"name": name, "type": "network", "host": host, "port": port}
        host, port = _split_host_port(self.rem_host.currentText(), self.rem_port.value())
        it = self.rem_list.currentItem()
        if not host or it is None:
            self.rem_status.setText("enter the IP and Scan, then pick a device")
            return None
        return {
            "name": name,
            "type": "remote",
            "adb_host": host,
            "adb_port": port,
            "serial": it.data(Qt.UserRole) or "",
        }
