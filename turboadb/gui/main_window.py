"""Tabbed multi-device main window: ribbon toolbar, sidebar with saved targets +
LIVE `adb devices` and a quick-connect filter, per-device tabs (Shell / Logcat /
Files / Apps + Mirror), a Split/tile view, and a dark color-coded log dock."""

from __future__ import annotations

import os

from PyQt5.QtCore import Qt, QSize, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QIcon, QKeySequence
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QGridLayout, QListWidget, QListWidgetItem, QPushButton,
                             QTabWidget, QDockWidget, QLabel, QLineEdit, QMessageBox,
                             QToolBar, QAction, QStatusBar, QShortcut, QToolButton,
                             QApplication, QMenu, QSizePolicy, QProgressDialog,
                             QInputDialog, QSplitter)

from . import theme
from .log_panel import LogPanel
from .sessions import SessionStore
from .session_dialog import SessionDialog
from .settings_dialog import SettingsDialog
from .device_tab import DeviceTab

def _find_icon():
    """Locate icon.ico in both a normal install and the frozen one-file exe
    (PyInstaller extracts bundled data under sys._MEIPASS)."""
    import sys
    here = os.path.dirname(os.path.dirname(__file__))     # …/turboadb
    bases = [here, os.path.join(here, "..")]
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        bases = [os.path.join(mei, "turboadb"), mei] + bases
    for b in bases:
        p = os.path.join(b, "assets", "icon.ico")
        if os.path.exists(p):
            return os.path.abspath(p)
    return os.path.join(here, "assets", "icon.ico")


ICON_PATH = _find_icon()


class _DevicesPoll(QThread):
    result = pyqtSignal(list)

    def run(self):
        try:
            from ..devices import list_devices
            self.result.emit(list_devices())
        except Exception:
            self.result.emit([])


class _AdbServerThread(QThread):
    done = pyqtSignal(str)

    def run(self):
        try:
            from ..core import ADBHandler
            from ..config import ADBConfig
            h = ADBHandler(ADBConfig())
            h._run_global(["kill-server"], check=False, timeout=15)
            h._run_global(["start-server"], check=False, timeout=30)
            self.done.emit("[OK] ADB server restarted")
        except Exception as exc:
            self.done.emit(f"[ERROR] restart adb server: {exc}")


class _ToolsDownloadThread(QThread):
    progress = pyqtSignal(int)
    done = pyqtSignal(dict)

    def __init__(self, mode="fetch", force=False):
        super().__init__()
        self.mode, self.force = mode, force

    def run(self):
        try:
            from ..toolsdl import fetch_tools, ensure_tools, upgrade_tools
            if self.mode == "ensure":
                res = ensure_tools(on_progress=self.progress.emit)
            elif self.mode == "upgrade":
                res = upgrade_tools(on_progress=self.progress.emit)
            else:
                res = fetch_tools(adb=True, scrcpy=True, force=self.force,
                                  on_progress=self.progress.emit)
        except Exception as exc:
            res = {"adb": None, "scrcpy": None, "errors": {"download": str(exc)}}
        self.done.emit(res or {})


class _ShareThread(QThread):
    """Start a shared adb server (so other machines can drive THIS PC's devices),
    open the firewall, and optionally install the login auto-start."""
    msg = pyqtSignal(str)

    def __init__(self, install_startup=False):
        super().__init__()
        self.install_startup = install_startup

    def run(self):
        try:
            from ..devices import (start_shared_server, open_firewall,
                                   install_startup)
            self.msg.emit("[OK] " + start_shared_server())
            self.msg.emit("[INFO] " + open_firewall((5037, 27184)))
            if self.install_startup:
                path = install_startup()
                self.msg.emit(f"[OK] Auto-start installed (runs at every login): "
                              f"{path}")
            self.msg.emit("[OK] Other machines can now connect via TurboADB → "
                          "Remote using this PC's IP/hostname.")
        except Exception as exc:
            self.msg.emit(f"[ERROR] share devices: {exc}")


class _DeployThread(QThread):
    """Deploy + start `turboadb serve` on remote hosts over WinRM, streaming
    per-host status back to the log."""
    status = pyqtSignal(str)

    def __init__(self, hosts, user, pw, port, update):
        super().__init__()
        self.hosts, self.user, self.pw = hosts, user, pw
        self.port, self.update = port, update

    def run(self):
        try:
            from ..remote_deploy import deploy_serve
            deploy_serve(self.hosts, self.user, self.pw, update=self.update,
                         port=self.port, on_status=self.status.emit)
            self.status.emit("[OK] Remote deploy finished.")
        except Exception as exc:
            self.status.emit(f"[ERROR] remote deploy: {exc}")


class _ShortcutThread(QThread):
    """Create the Desktop + Start-menu shortcuts if missing (off the UI thread,
    since the first run spawns PowerShell)."""
    done = pyqtSignal(dict)

    def run(self):
        try:
            from ..cli import ensure_shortcuts
            # force: refresh every launch so the icon is ALWAYS present (it was
            # intermittently missing before), pointing at the right launcher
            self.done.emit(ensure_shortcuts(force=True) or {})
        except Exception:
            self.done.emit({})


class _AppUpdateCheckThread(QThread):
    """Ask PyPI (off the UI thread) whether a newer TurboADB exists."""
    result = pyqtSignal(str)               # latest version if newer, else ""

    def run(self):
        try:
            from ..update import check
            self.result.emit(check() or "")
        except Exception:
            self.result.emit("")


class _AppUpgradeThread(QThread):
    """pip-upgrade TurboADB + refresh adb/scrcpy, reporting progress."""
    progress = pyqtSignal(str)
    done = pyqtSignal(dict)

    def run(self):
        try:
            from ..update import run_upgrade
            res = run_upgrade(notify=self.progress.emit)
        except Exception as exc:
            res = {"ok": False, "error": str(exc)}
        self.done.emit(res or {})


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        try:
            from .. import __version__ as _ver
        except Exception:
            _ver = ""
        self._version = _ver
        self.setWindowTitle("TurboADB")
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(1240, 800)
        from . import settings as settings_mod
        QApplication.instance().setStyleSheet(theme.stylesheet(settings_mod.get("theme")))

        self.store = SessionStore()
        self._tiled = False
        self._poll = None
        self._live_devices = []
        self._build_menubar()
        self._build_ribbon()
        self._build_sidebar()
        self._build_center()
        self._build_log_dock()

        self.setStatusBar(QStatusBar())
        self._install_shortcuts()
        self.refresh_sessions()
        self._update_status()
        self.log_panel.append(f"[OK] TurboADB {self._version} ready")

        # live adb-devices auto-refresh
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_devices)
        self._timer.start(3000)
        self._poll_devices()

        # if adb is missing, offer to download it once the window is up
        QTimer.singleShot(500, self._check_tools)
        # make sure the Desktop + Start-menu shortcuts exist (self-healing)
        QTimer.singleShot(1000, self._ensure_shortcuts)
        # NOTE: no automatic update check at launch — updates happen only when the
        # user clicks the ribbon "Upgrade" button (checks TurboADB + adb + scrcpy).

    # ---- menu bar (basic actions: sessions, save files, tools, help) ----
    def _build_menubar(self):
        ico = theme.emoji_icon
        mb = self.menuBar()

        m_file = mb.addMenu("&File")
        m_file.addAction(ico("➕"), "New target…", self.new_session, "Ctrl+N")
        m_file.addAction(ico("🔌"), "Connect a device…", self.open_connect)
        m_file.addSeparator()
        m_file.addAction(ico("💾"), "Save active output…",
                         self.save_active_output, "Ctrl+S")
        m_file.addAction(ico("📝"), "Save log…", self.save_log)
        m_file.addSeparator()
        m_file.addAction(ico("🖥"), "Create desktop + Start-menu shortcuts",
                         self.make_shortcuts_now)
        m_file.addSeparator()
        m_file.addAction(ico("✖", theme.DANGER), "Exit", self.close, "Ctrl+Q")

        m_view = mb.addMenu("&View")
        m_view.addAction(ico("📋"), "Toggle log panel", self.toggle_log)
        m_view.addAction(ico("🔲"), "Toggle split / tabbed view", self.toggle_split)

        m_dev = mb.addMenu("&Device")
        m_dev.addAction(ico("📱"), "Mirror (scrcpy)", self._mirror_current)
        m_dev.addAction(ico("📸"), "Screenshot", self._shot_current)
        m_dev.addSeparator()
        m_dev.addAction(ico("🔄"), "Restart ADB server", self.restart_adb_server)
        m_dev.addAction(ico("🔗"), "Pair device (Android 11+)…", self.pair_device)
        m_dev.addAction(ico("🛰"), "Share this PC's devices over the network…",
                        self.share_devices)
        m_dev.addAction(ico("📡"), "Deploy ‘serve’ to remote machines (WinRM)…",
                        self.deploy_serve_remote)

        m_tools = mb.addMenu("&Tools")
        m_tools.addAction(ico("⬆"), "Check for updates / Upgrade",
                          self.upgrade_tools_gui)
        m_tools.addAction(ico("⚙"), "Settings…", self.show_settings)

        m_help = mb.addMenu("&Help")
        m_help.addAction(ico("❓"), "Documentation", self._open_docs, "F1")
        m_help.addAction(ico("ℹ"), "About TurboADB", self._about)

    def make_shortcuts_now(self):
        from ..cli import ensure_shortcuts
        res = ensure_shortcuts(force=True)
        bad = [k for k, v in (res or {}).items() if not v]
        if bad:
            self.log_panel.append(f"[WARNING] Could not create: {', '.join(bad)}.")
        else:
            self.log_panel.append("[OK] Desktop + Start-menu shortcuts refreshed.")

    def save_active_output(self):
        t = self._current_tab()
        if not t:
            QMessageBox.information(self, "Save output", "Open a device first.")
            return
        t.save_active_output()

    def save_log(self):
        self.log_panel._save()

    def _about(self):
        QMessageBox.about(
            self, "About TurboADB",
            f"<b>TurboADB {self._version}</b><br><br>"
            "Android ADB + scrcpy device toolkit for automotive/embedded "
            "(Android Automotive / IVI) &amp; general Android.<br><br>"
            "<a href='https://pypi.org/project/turboadb/'>"
            "pypi.org/project/turboadb</a>")

    # ---- ribbon toolbar ----
    def _build_ribbon(self):
        tb = QToolBar("Ribbon")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tb.setIconSize(QSize(22, 22))            # compact, so more fits when narrow
        # when items don't fit, Qt shows a ">>" overflow — Exit is always reachable
        self.addToolBar(tb)

        # Connect: one click opens the unified Connect dialog (USB / Network /
        # Remote PC). The arrow has the few advanced extras.
        dev_btn = QToolButton()
        dev_btn.setText("Connect")
        dev_btn.setIcon(theme.emoji_icon("➕"))
        dev_btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        dev_btn.setIconSize(QSize(26, 26))
        dev_btn.setPopupMode(QToolButton.InstantPopup)   # whole button = menu
        dev_btn.clicked.connect(self.open_connect)
        dmenu = QMenu(dev_btn)
        dmenu.addAction("Connect to a device…", self.open_connect)
        dmenu.addAction("Save a target (without connecting)…", self.new_session)
        dmenu.addSeparator()
        dmenu.addAction("Pair device (Android 11+)…", self.pair_device)
        dmenu.addAction("Restart ADB server", self.restart_adb_server)
        dev_btn.setMenu(dmenu)
        tb.addWidget(dev_btn)

        # ADB Server: click = deploy/start `serve` on a remote machine (enter its
        # RDP host + admin creds); the arrow has share-this-PC and restart.
        srv_btn = QToolButton()
        srv_btn.setText("ADB Server")
        srv_btn.setIcon(theme.emoji_icon("📡"))
        srv_btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        srv_btn.setIconSize(QSize(22, 22))
        srv_btn.setPopupMode(QToolButton.MenuButtonPopup)   # click=deploy, ▾=menu
        srv_btn.setToolTip("Start 'turboadb serve' on a remote machine (enter its "
                           "host + admin login), or share this PC's devices.")
        srv_btn.clicked.connect(self.deploy_serve_remote)
        smenu = QMenu(srv_btn)
        smenu.addAction(theme.emoji_icon("📡"),
                        "Deploy to remote machine(s) (RDP / WinRM)…",
                        self.deploy_serve_remote)
        smenu.addAction(theme.emoji_icon("🛰"), "Share THIS PC's devices…",
                        self.share_devices)
        smenu.addSeparator()
        smenu.addAction(theme.emoji_icon("🔄"), "Restart local ADB server",
                        self.restart_adb_server)
        srv_btn.setMenu(smenu)
        tb.addWidget(srv_btn)

        items = [
            ("🖥", "Shell", lambda: self._subtab("shell")),
            ("📜", "Logcat", lambda: self._subtab("logcat")),
            ("📁", "Files", lambda: self._subtab("files")),
            ("📦", "Apps", lambda: self._subtab("apps")),
            ("🎛", "Controls", lambda: self._subtab("controls")),
            ("📱", "Scrcpy", self._mirror_current),
            ("📸", "Screenshot", self._shot_current),
            ("🔲", "Split", self.toggle_split),
            ("📋", "Logs", self.toggle_log),
            ("🔄", "Upgrade", self.upgrade_tools_gui),
            ("⚙", "Settings", self.show_settings),
            ("❓", "Help", self._open_docs),
        ]
        for emoji, label, slot in items:
            act = QAction(theme.emoji_icon(emoji), label, self)
            act.triggered.connect(slot)
            tb.addAction(act)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        exit_act = QAction(theme.emoji_icon("✖", theme.DANGER), "Exit", self)
        exit_act.triggered.connect(self.close)
        tb.addAction(exit_act)
        ew = tb.widgetForAction(exit_act)        # red text only (no red fill)
        if ew is not None:
            ew.setStyleSheet("QToolButton{color:%s;}" % theme.DANGER)

    # ---- sidebar ----
    def _build_sidebar(self):
        dock = QDockWidget("Devices", self)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        panel = QWidget()
        lay = QVBoxLayout(panel)
        self.quick = QLineEdit()
        self.quick.setPlaceholderText("Quick connect…  (filter / Enter to open)")
        self.quick.textChanged.connect(self._filter_sessions)
        self.quick.returnPressed.connect(self._quick_enter)
        lay.addWidget(self.quick)

        lay.addWidget(_section("Saved targets"))
        self.session_list = QListWidget()
        self.session_list.itemDoubleClicked.connect(lambda _: self.open_selected())
        self.session_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._session_menu)
        lay.addWidget(self.session_list, 2)

        lay.addWidget(_section("Connected now (live)"))
        self.live_list = QListWidget()
        self.live_list.itemDoubleClicked.connect(self._open_live)
        lay.addWidget(self.live_list, 1)

        newt = QPushButton("➕  New target"); newt.setProperty("role", "ok")
        newt.clicked.connect(self.new_session)
        lay.addWidget(newt)

        dock.setWidget(panel)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self._sidebar_dock = dock

    def _build_center(self):
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        # don't truncate device-tab titles; scroll when there are many
        self.tabs.setElideMode(Qt.ElideNone)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(lambda *_: self._update_status())
        plus = QToolButton(); plus.setText("  +  "); plus.setToolTip("New device")
        plus.clicked.connect(self.new_session)
        self.tabs.setCornerWidget(plus, Qt.TopRightCorner)
        self.setCentralWidget(self.tabs)

    def _build_log_dock(self):
        self.log_panel = LogPanel()
        dock = QDockWidget("Log", self)
        dock.setWidget(self.log_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self._log_dock = dock

    def _install_shortcuts(self):
        # Ctrl+N (New target) and F1 (Docs) live on the menu bar now, so they're
        # not duplicated here (avoids "ambiguous shortcut" warnings).
        for seq, slot in (("Ctrl+T", self.new_session),
                          ("Ctrl+W", self._close_current_tab),
                          ("Ctrl+Return", self.open_selected)):
            QShortcut(QKeySequence(seq), self, activated=slot)

    # ---- live devices ----
    def _poll_devices(self):
        if self._poll and self._poll.isRunning():
            return
        self._poll = _DevicesPoll()
        self._poll.result.connect(self._on_devices)
        self._poll.start()

    def _on_devices(self, devices):
        self._live_devices = devices
        self.live_list.clear()
        if not devices:
            it = QListWidgetItem("— none connected —")
            it.setFlags(Qt.NoItemFlags)
            self.live_list.addItem(it)
            return
        for d in devices:
            dot = "🟢" if d.is_online else "🟠"
            it = QListWidgetItem(f"{dot}  {d.label}  ·  {d.serial}  ({d.state})")
            it.setData(Qt.UserRole, d.serial)
            self.live_list.addItem(it)

    def _open_live(self, item):
        serial = item.data(Qt.UserRole)
        if not serial:
            return
        is_net = ":" in serial and serial.rpartition(":")[2].isdigit()
        s = {"name": serial, "type": "network" if is_net else "usb",
             "serial": "" if is_net else serial,
             "host": serial.rpartition(":")[0] if is_net else "",
             "port": int(serial.rpartition(":")[2]) if is_net else 5555}
        self._open_session(s, serial)

    # ---- saved sessions ----
    # distinct coloured glyph per target type — with DARKER variants for light
    # mode (the bright greens/cyans were nearly invisible on a light list)
    #   type -> (glyph, dark-theme colour, light-theme colour)
    _TYPE_ICON = {
        "usb":     ("🔌", "#5be39a", "#1f9d5b"),   # green  — local USB device
        "network": ("🌐", "#48d6e8", "#0e7d92"),   # cyan   — device by IP
        "remote":  ("🖧", "#c79bff", "#7b3fd1"),   # purple — device on another PC
    }

    def refresh_sessions(self):
        from . import settings as settings_mod
        light = settings_mod.get("theme") == "light"
        self.session_list.clear()
        for s in self.store.sessions:
            t = s.get("type") or "usb"
            glyph, dark_c, light_c = self._TYPE_ICON.get(t, self._TYPE_ICON["usb"])
            colour = light_c if light else dark_c
            if t == "network":
                tgt = f"{s.get('host')}:{s.get('port')}"
            elif t == "remote":
                tgt = (f"{s.get('adb_host')}:{s.get('adb_port')} → "
                       f"{s.get('serial') or 'only device'}")
            else:
                tgt = s.get("serial") or "only device"
            it = QListWidgetItem(f"  {s.get('name')}   ·  {tgt}")
            it.setIcon(theme.emoji_icon(glyph, colour))
            it.setData(Qt.UserRole, s.get("name"))
            self.session_list.addItem(it)
        self._filter_sessions(self.quick.text())

    def _filter_sessions(self, text):
        text = (text or "").lower()
        for i in range(self.session_list.count()):
            it = self.session_list.item(i)
            it.setHidden(bool(text) and text not in it.text().lower())

    def _quick_enter(self):
        for i in range(self.session_list.count()):
            it = self.session_list.item(i)
            if not it.isHidden():
                self.session_list.setCurrentItem(it)
                self.open_selected()
                return
        host = self.quick.text().strip()
        if host:
            # treat free text as a host[:port] network target
            self.new_session(prefill_host=host)

    def _session_menu(self, pos):
        menu = QMenu(self)
        menu.addAction(theme.emoji_icon("➕"), "New target…", self.new_session)
        item = self.session_list.itemAt(pos)
        if item is not None:
            self.session_list.setCurrentItem(item)
            menu.addSeparator()
            menu.addAction(theme.emoji_icon("▶"), "Open / Connect", self.open_selected)
            menu.addAction(theme.emoji_icon("✏"), "Edit…", self.edit_session)
            menu.addAction(theme.emoji_icon("📄"), "Duplicate", self._duplicate_session)
            menu.addSeparator()
            menu.addAction(theme.emoji_icon("🗑"), "Delete", self.delete_session)
        menu.exec_(self.session_list.viewport().mapToGlobal(pos))

    def _selected_name(self):
        it = self.session_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _duplicate_session(self):
        name = self._selected_name()
        s = dict(self.store.get(name) or {}) if name else {}
        if not s:
            return
        s["name"] = s.get("name", "device") + " (copy)"
        self.store.save(s)
        self.refresh_sessions()
        self.log_panel.append(f"[OK] Duplicated '{name}'")

    def new_session(self, *_, prefill_host=None):
        dlg = SessionDialog(self)
        if prefill_host:
            dlg.network.setChecked(True)
            host, _, port = prefill_host.rpartition(":")
            if port.isdigit():
                dlg.host.setText(host); dlg.port.setValue(int(port))
            else:
                dlg.host.setText(prefill_host)
        if dlg.exec_() == dlg.Accepted:
            s = dlg.result_session()
            if not s["name"]:
                QMessageBox.warning(self, "Target", "Give the target a name.")
                return
            self.store.save(s)
            self.refresh_sessions()
            self.log_panel.append(f"[OK] Saved target '{s['name']}'")

    def edit_session(self):
        name = self._selected_name()
        if not name:
            return
        dlg = SessionDialog(self, existing=self.store.get(name))
        if dlg.exec_() == dlg.Accepted:
            self.store.save(dlg.result_session())
            self.refresh_sessions()

    def delete_session(self):
        name = self._selected_name()
        if name and QMessageBox.question(self, "Delete",
                                         f"Delete '{name}'?") == QMessageBox.Yes:
            self.store.delete(name)
            self.refresh_sessions()

    def open_selected(self):
        name = self._selected_name()
        if not name:
            QMessageBox.information(self, "Connect", "Select a target first.")
            return
        s = self.store.get(name)
        self._open_session(s, name)

    def _open_session(self, s, name):
        if self._tiled:
            self.toggle_split()
        w = DeviceTab(s)
        w.log.connect(self.log_panel.append)
        w.title_changed.connect(lambda title, ww=w: self._set_tab_title(ww, title))
        idx = self.tabs.addTab(w, name)
        self.tabs.setTabIcon(idx, theme.emoji_icon("📱"))
        self.tabs.setCurrentIndex(idx)
        self.log_panel.append(f"Opening '{name}'…")

    def _set_tab_title(self, widget, title):
        idx = self.tabs.indexOf(widget)
        if idx >= 0:
            # emoji as the tab ICON + plain text, so the label never truncates
            self.tabs.setTabText(idx, title)
            self.tabs.setTabIcon(idx, theme.emoji_icon("📱"))
        self._update_status()

    def _update_status(self):
        """Reflect the real connection state in the status bar (instead of always
        showing the idle 'ready' message)."""
        try:
            online = [t for t in self._device_tabs()
                      if getattr(t, "handler", None) is not None]
        except Exception:
            online = []
        if not online:
            self.statusBar().showMessage(
                f"TurboADB {self._version} — no device connected; click Connect "
                f"to add one")
            return
        name = ""
        cur = self.tabs.currentWidget()
        if isinstance(cur, DeviceTab) and getattr(cur, "handler", None):
            i = self.tabs.indexOf(cur)
            name = self.tabs.tabText(i).replace("📱", "").strip()
        msg = f"● Connected — {name}" if name else f"● {len(online)} device(s) connected"
        if len(online) > 1:
            msg += f"    ·    {len(online)} devices open"
        self.statusBar().showMessage(msg)

    # ---- ribbon helpers acting on the open device(s) ----
    def _device_tabs(self):
        """All open DeviceTabs — works in tabbed AND split view."""
        if self._tiled:
            return [w for w, _t in getattr(self, "_tiled_items", [])
                    if isinstance(w, DeviceTab)]
        return [self.tabs.widget(i) for i in range(self.tabs.count())
                if isinstance(self.tabs.widget(i), DeviceTab)]

    def _current_tab(self):
        if not self._tiled:
            w = self.tabs.currentWidget()
            if isinstance(w, DeviceTab):
                return w
        tabs = self._device_tabs()
        return tabs[0] if tabs else None

    def _subtab(self, name):
        tabs = self._device_tabs()
        if not tabs:
            QMessageBox.information(self, "TurboADB", "Open a device first.")
            return
        # switch every open device to that sub-tab (handy in split view)
        for t in tabs:
            t.show_subtab(name)

    def _mirror_current(self):
        t = self._current_tab()
        if t:
            t.mirror()
        else:
            QMessageBox.information(self, "Scrcpy", "Open a device first.")

    def _shot_current(self):
        t = self._current_tab()
        if t:
            t.screenshot()
        else:
            QMessageBox.information(self, "Screenshot", "Open a device first.")

    # ---- one unified Connect dialog (USB / Network / Remote PC) ----
    def open_connect(self, *_):
        from .connect_dialog import ConnectDialog
        dlg = ConnectDialog(self)
        if dlg.exec_() != dlg.Accepted:
            return
        s = dlg.session()
        if not s:
            return
        name = s.get("name")
        if name:                              # a name was given -> also save it
            self.store.save(s)
            self.refresh_sessions()
        label = name or s.get("serial") or s.get("host") or s.get("adb_host") or "device"
        self._open_session(s, label)

    def pair_device(self):
        addr, ok = QInputDialog.getText(
            self, "Pair device (Android 11+)",
            "Pairing address shown on the device (host:pairing_port):")
        if not ok or not addr.strip():
            return
        code, ok = QInputDialog.getText(self, "Pair device (Android 11+)",
                                        "6-digit pairing code shown on the device:")
        if not ok or not code.strip():
            return
        host, _, port = addr.strip().rpartition(":")
        if not port.isdigit():
            self.log_panel.append("[ERROR] pairing address must be host:port")
            return
        try:
            from ..core import ADBHandler
            from ..config import ADBConfig
            from ..results import OperationResult
            h = ADBHandler(ADBConfig())
            res = h.pair(host, int(port), code.strip(), safe=True)
            if isinstance(res, OperationResult) and not res.success:
                self.log_panel.append(f"[ERROR] pair: {res.error}")
            else:
                val = res.value if isinstance(res, OperationResult) else res
                self.log_panel.append(f"[OK] {val}")
            self._poll_devices()
        except Exception as exc:
            self.log_panel.append(f"[ERROR] pair: {exc}")

    def share_devices(self):
        """Turn THIS machine into the remote adb host (what `turboadb serve`
        does): start a shared adb server + open the firewall so other PCs can
        drive the devices plugged in here — handy for RDP / lab setups."""
        if getattr(self, "_share", None) and self._share.isRunning():
            self.log_panel.append("[WARNING] Share is already starting…")
            return
        box = QMessageBox(self)
        box.setWindowTitle("Share devices over the network")
        box.setIcon(QMessageBox.Question)
        box.setText(
            "Start a shared adb server so OTHER machines can use the devices "
            "plugged into THIS PC (TurboADB → Remote, or `adb -H <this-pc>`)?\n\n"
            "It also opens the firewall ports (5037 + 27184). Run it automatically "
            "at every login too?\n\n"
            "Tip: opening the firewall needs Administrator — if it can't, run "
            "TurboADB as Administrator once.")
        b_start_login = box.addButton("Start + run at login",
                                      QMessageBox.AcceptRole)
        b_once = box.addButton("Start once", QMessageBox.YesRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec_()
        clicked = box.clickedButton()
        if clicked not in (b_start_login, b_once):
            return
        self.log_panel.append("Starting shared adb server (network device "
                              "sharing)…")
        self._share = _ShareThread(install_startup=(clicked is b_start_login))
        self._share.msg.connect(self.log_panel.append)
        self._share.finished.connect(self._poll_devices)
        self._share.start()

    def deploy_serve_remote(self):
        """Install/start `turboadb serve` on remote Windows hosts FROM here, over
        WinRM — so you don't have to RDP into each one to enable device sharing."""
        if getattr(self, "_deploy", None) and self._deploy.isRunning():
            self.log_panel.append("[WARNING] A remote deploy is already running.")
            return
        from .deploy_dialog import DeployDialog
        dlg = DeployDialog(self)
        if dlg.exec_() != dlg.Accepted:
            return
        vals = dlg.values()
        if not vals["hosts"]:
            QMessageBox.warning(self, "Deploy", "Enter at least one host.")
            return
        if not vals["user"] or not vals["password"]:
            QMessageBox.warning(self, "Deploy",
                                "Enter the admin user and password.")
            return
        from . import settings as settings_mod
        for h in reversed(vals["hosts"]):
            settings_mod.add_recent("recent_remote_hosts", h)
        self.log_panel.append(
            f"Deploying ‘serve’ to {len(vals['hosts'])} host(s) over WinRM…")
        self._deploy = _DeployThread(vals["hosts"], vals["user"],
                                     vals["password"], vals["port"],
                                     vals["update"])
        self._deploy.status.connect(self.log_panel.append)
        self._deploy.start()

    def restart_adb_server(self):
        self.log_panel.append("Restarting ADB server… (fixes 'device not "
                              "visible' from adb version mismatches)")
        self._as = _AdbServerThread()
        self._as.done.connect(lambda m: (self.log_panel.append(m),
                                         self._poll_devices()))
        self._as.start()

    # ---- tool download (adb / scrcpy, auto on install/upgrade) ----
    def _check_tools(self):
        """On startup, auto-fetch the latest adb/scrcpy when enabled (default),
        or fall back to a one-off prompt if auto-fetch is disabled."""
        try:
            from ..tools import adb_available
            from ..toolsdl import (auto_fetch_enabled, managed_adb, _read_stamp,
                                   _pkg_version)
        except Exception:
            return
        if auto_fetch_enabled():
            # Only fetch when adb is actually MISSING — don't hit the network to
            # re-check tools on every TurboADB version bump (that added a slow
            # round-trip at every launch after an upgrade). Use the ribbon
            # “Upgrade” button to refresh adb/scrcpy on demand.
            need = (managed_adb() is None or not adb_available())
            if need:
                self._run_tools("ensure",
                                "Downloading platform-tools + scrcpy (one-time)…")
            else:
                from ..toolsdl import _write_stamp, _pkg_version as _pv
                try:
                    _write_stamp(_pv())          # mark current; skip re-checking
                except Exception:
                    pass
            return
        if not adb_available():
            msg = ("adb (Android platform-tools) wasn't found.\n\n"
                   "Download platform-tools + scrcpy now into ~/.turboadb/tools?")
            if QMessageBox.question(self, "Download tools", msg) == QMessageBox.Yes:
                self._run_tools("fetch", "Downloading adb + scrcpy…")

    def download_tools(self, force=False):
        """Manual download — refreshes to latest."""
        self._run_tools("fetch", "Downloading the latest adb + scrcpy…", force=True)

    def upgrade_tools_gui(self):
        """Ribbon ‘Upgrade’: the ONE place updates happen. First check TurboADB
        itself on PyPI; if newer, self-update (which also refreshes adb + scrcpy)
        and restart. Otherwise just check/refresh adb + scrcpy."""
        if getattr(self, "_upd_run", None) and self._upd_run.isRunning():
            self.log_panel.append("[WARNING] An update is already running.")
            return
        if getattr(self, "_dl", None) and self._dl.isRunning():
            self.log_panel.append("[WARNING] A tool task is already running.")
            return
        from .. import update as _upd
        if not _upd.can_self_update():
            # bundled exe: can't pip-upgrade itself, so just do adb/scrcpy
            self.log_panel.append("Checking adb/scrcpy for updates…")
            self._upgrade_tools_only()
            return
        self.log_panel.append("Checking PyPI for a newer TurboADB…")
        self._upd_chk = _AppUpdateCheckThread()
        self._upd_chk.result.connect(self._upgrade_after_appcheck)
        self._upd_chk.start()

    def _upgrade_after_appcheck(self, latest):
        from .. import update as _upd
        if latest and _upd.is_newer(latest):
            self._do_self_update(latest)        # also updates adb/scrcpy, then restarts
        else:
            self.log_panel.append(
                f"[OK] TurboADB {self._version} is the latest — now checking "
                f"adb/scrcpy…")
            self._upgrade_tools_only()

    def _upgrade_tools_only(self):
        if getattr(self, "_dl", None) and self._dl.isRunning():
            self.log_panel.append("[WARNING] A tool task is already running.")
            return
        self._dlg = QProgressDialog("Checking for newer adb / scrcpy…\n"
                                    "Downloads only if an update is available.",
                                    None, 0, 100, self)
        self._dlg.setWindowTitle("TurboADB — upgrade")
        self._dlg.setAutoClose(True); self._dlg.setMinimumDuration(0)
        self._dlg.setValue(0)
        self.log_panel.append("Checking for adb/scrcpy updates…")
        self._dl = _ToolsDownloadThread(mode="upgrade")
        self._dl.progress.connect(self._dlg.setValue)
        self._dl.done.connect(self._upgrade_done)
        self._dl.start()

    def _upgrade_done(self, res):
        try:
            self._dlg.close()
        except Exception:
            pass
        checks = res.get("checks") or {}
        for tool in ("adb", "scrcpy"):
            c = checks.get(tool) or {}
            if c:
                self.log_panel.append(
                    f"[OK] {tool}: installed {c.get('installed')} · "
                    f"latest {c.get('latest')}")
        if res.get("up_to_date"):
            self.log_panel.append("[OK] adb & scrcpy are already up to date.")
            QMessageBox.information(self, "Up to date",
                                    "adb and scrcpy are already the latest version.")
        for tool, path in (res.get("updated") or {}).items():
            self.log_panel.append(f"[OK] updated {tool} → {path}")
        for tool, err in (res.get("errors") or {}).items():
            self.log_panel.append(f"[WARNING] {tool}: {err}")
        if res.get("updated"):
            QMessageBox.information(self, "Updated",
                                    "Updated: " + ", ".join(res["updated"].keys()))
        self._poll_devices()

    def _run_tools(self, mode, label, force=False):
        if getattr(self, "_dl", None) and self._dl.isRunning():
            self.log_panel.append("[WARNING] A tool download is already running.")
            return
        self._dlg = QProgressDialog(label + "\nCached in ~/.turboadb/tools.",
                                    None, 0, 100, self)
        self._dlg.setWindowTitle("TurboADB — tools")
        self._dlg.setAutoClose(True)
        self._dlg.setMinimumDuration(0)
        self._dlg.setValue(0)
        self.log_panel.append(label)
        self._dl = _ToolsDownloadThread(mode=mode, force=force)
        self._dl.progress.connect(self._dlg.setValue)
        self._dl.done.connect(self._tools_done)
        self._dl.start()

    def _tools_done(self, res):
        try:
            self._dlg.close()
        except Exception:
            pass
        if res.get("adb"):
            self.log_panel.append(f"[OK] adb ready: {res['adb']}")
        if res.get("scrcpy"):
            self.log_panel.append(f"[OK] scrcpy ready: {res['scrcpy']}")
        for tool, err in (res.get("errors") or {}).items():
            self.log_panel.append(f"[WARNING] {tool}: {err}")
        if res.get("note") == "up-to-date":
            self.log_panel.append("[OK] adb / scrcpy already up to date")
        elif not res.get("adb") and (res.get("errors")):
            QMessageBox.warning(self, "Download failed",
                                "Could not download the tools. Check your network, "
                                "or install them manually (see Help).")
        self._poll_devices()

    def _ensure_shortcuts(self):
        self._sc = _ShortcutThread()
        self._sc.done.connect(self._on_shortcuts)
        self._sc.start()

    def _on_shortcuts(self, res):
        made = [k for k, v in (res or {}).items() if v]
        failed = [k for k, v in (res or {}).items() if not v]
        if made:
            self.log_panel.append(
                f"[OK] Added TurboADB shortcut to your {', '.join(made)}.")
        if failed:
            self.log_panel.append(
                f"[WARNING] Could not create the {', '.join(failed)} shortcut "
                f"(try: turboadb shortcut).")

    # ---- TurboADB self-update (pip), triggered only by the Upgrade button ----
    def _do_self_update(self, latest):
        if getattr(self, "_upd_run", None) and self._upd_run.isRunning():
            return
        msg = (f"A newer TurboADB is available:\n\n    {self._version}  →  {latest}"
               "\n\nUpdate now? TurboADB will pip-install the new version "
               "(with the latest adb & scrcpy) and restart.")
        if QMessageBox.question(self, "Update available", msg,
                                QMessageBox.Ok | QMessageBox.Cancel,
                                QMessageBox.Ok) != QMessageBox.Ok:
            return
        self._upd_dlg = QProgressDialog("Updating TurboADB…", None, 0, 0, self)
        self._upd_dlg.setWindowTitle("TurboADB — updating")
        self._upd_dlg.setMinimumDuration(0)
        self._upd_dlg.setAutoClose(False); self._upd_dlg.setAutoReset(False)
        self._upd_dlg.show()
        self.log_panel.append(f"Updating TurboADB {self._version} -> {latest}…")
        self._upd_run = _AppUpgradeThread()
        self._upd_run.progress.connect(
            lambda m: (self._upd_dlg.setLabelText(m), self.log_panel.append(m)))
        self._upd_run.done.connect(self._on_self_update_done)
        self._upd_run.start()

    def _on_self_update_done(self, res):
        try:
            self._upd_dlg.close()
        except Exception:
            pass
        from .. import update as _upd
        if not res.get("ok"):
            err = res.get("error") or "unknown error"
            self.log_panel.append(f"[ERROR] update failed: {err}")
            QMessageBox.warning(
                self, "Update failed",
                "Could not update automatically:\n\n"
                f"{err}\n\nUpdate manually with:\n    pip install --upgrade turboadb")
            return
        bits = []
        if res.get("adb"):
            bits.append(f"adb {res['adb']}")
        if res.get("scrcpy"):
            bits.append(f"scrcpy {res['scrcpy']}")
        tools = ("  ·  " + ", ".join(bits)) if bits else ""
        self.log_panel.append(
            f"[OK] Updated to TurboADB {res.get('new')}{tools}. Restarting…")
        relaunched = _upd.relaunch()
        QMessageBox.information(
            self, "Updated",
            f"Updated to TurboADB {res.get('new')}.\n"
            + (f"Latest tools: {', '.join(bits)}.\n" if bits else "")
            + ("\nTurboADB will now restart." if relaunched
               else "\nReopen TurboADB to use the new version."))
        if relaunched:
            QApplication.instance().quit()

    def toggle_log(self):
        show = not self._log_dock.isVisible()
        self._log_dock.setVisible(show)
        if show:                                 # bring it to front when restoring
            self._log_dock.raise_()

    def toggle_split(self):
        if not self._tiled:
            if self.tabs.count() < 1:
                QMessageBox.information(self, "Split", "Open a device or two first, "
                                       "then Split to view them side by side.")
                return
            self._tiled_items = []
            while self.tabs.count():
                self._tiled_items.append((self.tabs.widget(0), self.tabs.tabText(0)))
                self.tabs.removeTab(0)
            n = len(self._tiled_items)
            cols = 1 if n == 1 else 2
            outer = QSplitter(Qt.Vertical)        # rows (resizable)
            row = None
            for i, (w, title) in enumerate(self._tiled_items):
                if i % cols == 0:
                    row = QSplitter(Qt.Horizontal)   # columns (resizable)
                    outer.addWidget(row)
                cell = QWidget()
                v = QVBoxLayout(cell); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(2)
                cap = QLabel("  " + title)
                cap.setStyleSheet(f"background:{theme.THEMES['dark']['ribbon']};"
                                  f"color:{theme.ACCENT};padding:5px 8px;"
                                  f"border-radius:5px;font-weight:700;")
                v.addWidget(cap)
                v.addWidget(w, 1)
                cell.setMinimumSize(320, 240)
                row.addWidget(cell)
            self.tabs.setParent(None)
            self.setCentralWidget(outer)
            outer.show()
            # removeTab() leaves the page hidden — re-show each device widget now
            # that it lives inside the (visible) splitter, so panes aren't blank
            for w, _title in self._tiled_items:
                w.setVisible(True)
            self._tiled = True
            self.statusBar().showMessage(f"Split view — {n} device(s); drag the "
                                         f"dividers to resize. Click Split again for tabs.")
            self.log_panel.append(f"[OK] Split view: {n} device(s). Drag dividers "
                                  f"to resize; click Split again for tabs.")
        else:
            for (w, title) in getattr(self, "_tiled_items", []):
                self.tabs.addTab(w, title)
            self.setCentralWidget(self.tabs)
            self._tiled = False
            self.statusBar().showMessage("Tabbed view")
            self.log_panel.append("[OK] Back to tabbed view.")

    def show_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec_() == dlg.Accepted:
            from . import settings as settings_mod
            cfg = dlg.result_settings()
            settings_mod.save(cfg)
            QApplication.instance().setStyleSheet(theme.stylesheet(cfg["theme"]))
            self.log_panel.append(f"[OK] Settings saved — theme: {cfg['theme']}")

    def _close_tab(self, index):
        w = self.tabs.widget(index)
        try:
            w.close_session()
        except Exception:
            pass
        self.tabs.removeTab(index)
        self._update_status()

    def _close_current_tab(self):
        i = self.tabs.currentIndex()
        if i >= 0:
            self._close_tab(i)

    def _open_docs(self):
        import webbrowser
        webbrowser.open("https://pypi.org/project/turboadb/")

    def closeEvent(self, event):
        try:
            self._timer.stop()
        except Exception:
            pass
        for i in range(self.tabs.count()):
            try:
                self.tabs.widget(i).close_session()
            except Exception:
                pass
        super().closeEvent(event)


def _section(text: str) -> QLabel:
    from . import settings as settings_mod
    col = theme.accent_text(settings_mod.get("theme"))
    lbl = QLabel(text.upper())
    # bolder + bigger than before so it's clearly legible (it was nearly invisible
    # in light mode); the deep-teal accent now has strong contrast on both themes
    lbl.setStyleSheet(f"color:{col}; font-weight:800; font-size:9.5pt; "
                      f"padding:7px 2px 2px 2px; letter-spacing:1px;")
    return lbl
