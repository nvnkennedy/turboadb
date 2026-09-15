"""Tabbed multi-device main window: ribbon toolbar, sidebar with saved targets +
LIVE `adb devices` and a quick-connect filter, per-device tabs (Shell / Logcat /
Files / Apps + Screen controls), and a color-coded log dock."""

from __future__ import annotations

import os
import re
import time

from PyQt5.QtCore import Qt, QSize, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QIcon, QKeySequence
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QListWidget, QListWidgetItem, QPushButton,
                             QDockWidget, QLabel, QLineEdit, QMessageBox,
                             QToolBar, QAction, QStatusBar, QShortcut, QToolButton,
                             QApplication, QMenu, QSizePolicy, QProgressDialog,
                             QInputDialog)

from ..results import OperationResult
from ..scrcpy import TUNNEL_PORT_FIREWALL_RANGE
from . import icons, theme
from .log_panel import LogPanel
from .sessions import SessionStore
from .session_dialog import SessionDialog
from .settings_dialog import SettingsDialog
from .device_tab import DeviceTab
from . import settings as settings_mod
from .adb_path import gui_adb_path
from .qtutil import AnimatedTabWidget, FunctionThread, park_thread, thread_running

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

# The GUI's local adb daemon.  The device tracker speaks its socket protocol.
_LOCAL_ADB_HOST = "127.0.0.1"
_LOCAL_ADB_PORT = 5037


class _AdbInitThread(QThread):
    """Proactively ensure local ADB server is running and tools environment is configured on app startup."""
    ready = pyqtSignal(bool, str)

    def __init__(self, adb_path: str | None = None, parent=None):
        super().__init__(parent)
        self.adb_path = adb_path

    def run(self):
        try:
            from ..tools import find_adb, ensure_adb_server, last_adb_server_error
            # ``adb_path`` is gui_adb_path(): the Settings override or the
            # managed copy.  Only when neither exists yet (first run, before
            # the tools download) does find_adb fall back to discovery.
            adb = find_adb(self.adb_path)
            # ensure_adb_server performs the one locked health probe itself.
            # Checking here and then checking again inside that function added
            # another network timeout before a cold daemon could even launch.
            ok = ensure_adb_server(adb, timeout=2.5)
            if ok:
                self.ready.emit(
                    True, f"ADB server active ({_LOCAL_ADB_HOST}:{_LOCAL_ADB_PORT})"
                )
            else:
                detail = last_adb_server_error()
                self.ready.emit(
                    False,
                    "ADB server did not start"
                    + (f": {detail}" if detail else
                       " — another ADB version may own port 5037 or the configured adb path may be invalid"),
                )
        except Exception as exc:
            self.ready.emit(False, f"ADB initialization note: {exc}")


class _DevicesPoll(QThread):
    result = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, adb_path: str | None = None, *, socket_only: bool = False, parent=None):
        super().__init__(parent)
        self.adb_path = adb_path
        self.socket_only = socket_only

    def run(self):
        try:
            if self.socket_only:
                # A warm ADB daemon can answer this in a few milliseconds.  Do
                # not start a daemon or invoke adb.exe here: the locked startup
                # workers own that work, so this can never create a competing
                # start race.
                from ..devices import _list_devices_socket
                devices = _list_devices_socket(timeout=0.075)
                if devices is not None:
                    self.result.emit(devices)
                return
            from ..devices import list_devices
            # A GUI refresh must never sit behind a long command-line fallback.
            # The socket tracker supplies near-instant changes after this first
            # bounded reconciliation scan.
            self.result.emit(list_devices(adb_path=self.adb_path, timeout=3.0, strict=True))
        except Exception as exc:
            import logging
            logging.getLogger("turboadb").debug("Device poll exception: %s", exc)
            self.failed.emit(str(exc) or type(exc).__name__)


class _DeviceTracker(QThread):
    """Event-driven device tracker using ADB server's 'host:track-devices-l' stream.

    Pushes device updates in < 1ms when a phone is plugged/unplugged, completely
    eliminating the 2-second polling delay.
    """
    result = pyqtSignal(list)

    def __init__(self, adb_path: str | None = None, parent=None):
        super().__init__(parent)
        self.adb_path = adb_path
        self._stopped = False
        self._sock = None

    def stop(self):
        self._stopped = True
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def run(self):
        import socket
        import time
        from ..devices import _parse_line
        from ..tools import _recv_exact

        def _backoff():
            # A reset socket fails every recv immediately.  Always leave the
            # broken connection and delay before reconnecting so it cannot spin.
            for _ in range(10):
                if self._stopped:
                    return
                time.sleep(0.1)

        while not self._stopped:
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock = s
                s.settimeout(2.0)
                s.connect((_LOCAL_ADB_HOST, _LOCAL_ADB_PORT))
                req = b"host:track-devices-l"
                s.sendall(f"{len(req):04x}".encode("ascii") + req)
                status = _recv_exact(s, 4)
                if status != b"OKAY":
                    _backoff()
                    continue

                s.settimeout(1.0)
                while not self._stopped:
                    raw_len = _recv_exact(s, 4, retry_timeouts=True)
                    if raw_len is None:
                        break
                    try:
                        length = int(raw_len, 16)
                    except ValueError:
                        break

                    data = _recv_exact(s, length, retry_timeouts=True)
                    if data is None:
                        break

                    text = data.decode("utf-8", errors="replace")
                    devices = []
                    for line in text.splitlines():
                        dev = _parse_line(line)
                        if dev is not None:
                            devices.append(dev)
                    if not self._stopped:
                        self.result.emit(devices)
            except (OSError, TimeoutError, ValueError):
                pass
            except Exception:
                # A tracker must never die silently because a malformed update
                # or platform-specific socket edge case escaped the expected
                # protocol errors.  The finally/backoff path reconnects safely.
                pass
            finally:
                self._sock = None
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
            if not self._stopped:
                _backoff()


def _adb_restart_worker(adb_path):
    """A worker whose ``done`` carries an ``[OK]``/``[ERROR]`` log line.

    It restarts with the exact executable configured for this GUI: a default
    handler could kill a daemon of one adb version and start a different one,
    a common cause of slow launches and disappearing devices on Windows.
    """

    def work():
        try:
            from ..core import ADBHandler
            from ..config import ADBConfig
            from ..tools import is_adb_server_alive

            res = ADBHandler(ADBConfig(adb_path=adb_path)).restart_server(safe=True)
            if isinstance(res, OperationResult) and not res.success:
                detail = str(res.error or "").strip().replace("\n", " ")[:300]
                return f"[ERROR] ADB server did not restart{': ' + detail if detail else ''}"
            if not is_adb_server_alive(timeout=0.5):
                return "[ERROR] ADB server did not restart: nothing answers on port 5037"
            return "[OK] ADB server restarted"
        except Exception as exc:
            return f"[ERROR] restart adb server: {exc}"

    return FunctionThread(work)


def _discover_worker(adb_path):
    """Find Android 11+ Wireless-debugging devices on the LAN (adb mdns)."""

    def work():
        try:
            from ..devices import mdns_devices
            return mdns_devices(adb_path=adb_path)
        except Exception:
            return []

    return FunctionThread(work)


class _BroadcastThread(QThread):
    """Run one adb shell command on EVERY connected device, off the UI thread."""
    line = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(self, serials, command, adb_path=None):
        super().__init__()
        self.serials, self.command = serials, command
        self.adb_path = adb_path

    def run(self):
        from ..core import ADBHandler
        from ..config import ADBConfig
        for s in self.serials:
            try:
                h = ADBHandler(ADBConfig(serial=s, adb_path=self.adb_path), safe=True)
                outcome = h.shell(self.command, safe=True)
                if isinstance(outcome, OperationResult):
                    if not outcome.success:
                        self.line.emit(f"[ERROR] {s}: {outcome.error}")
                        continue
                    outcome = outcome.value
                res = outcome
                head = (res.text or res.stderr.strip() or "(no output)")
                head = head.splitlines()[0][:200] if head else "(no output)"
                tag = "OK" if res.ok else f"exit {res.exit_code}"
                self.line.emit(f"[{'OK' if res.ok else 'WARNING'}] {s}: "
                               f"{tag} · {head}")
            except Exception as exc:
                self.line.emit(f"[ERROR] {s}: {exc}")
        self.done.emit()


class _ToolsDownloadThread(QThread):
    progress = pyqtSignal(int)
    stage = pyqtSignal(str)
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
                                  on_progress=self.progress.emit,
                                  on_stage=self.stage.emit)
        except Exception as exc:
            res = {"adb": None, "scrcpy": None, "errors": {"download": str(exc)}}
        self.done.emit(res or {})


class _ShareThread(QThread):
    """Start a shared adb server (so other machines can drive THIS PC's devices),
    open the firewall, and optionally install the login auto-start."""
    msg = pyqtSignal(str)

    def __init__(self, install_startup=False, adb_path=None):
        super().__init__()
        self.install_startup = install_startup
        self.adb_path = adb_path

    def run(self):
        try:
            from ..devices import (start_shared_server, open_firewall,
                                   install_startup)
            self.msg.emit("[INFO] Restarting the local ADB server to expose it on the LAN; "
                          "active device sessions may reconnect briefly.")
            self.msg.emit("[OK] " + start_shared_server(adb_path=self.adb_path))
            self.msg.emit("[INFO] " + open_firewall((5037, TUNNEL_PORT_FIREWALL_RANGE)))
            if self.install_startup:
                path = install_startup()
                self.msg.emit(f"[OK] Auto-start installed (runs at every login): "
                              f"{path}")
            self.msg.emit("[OK] Other machines can now connect via TurboADB → "
                          "Remote using this PC's IP/hostname.")
        except Exception as exc:
            self.msg.emit(f"[ERROR] share devices: {exc}")


class _StopShareThread(QThread):
    """Undo device sharing on THIS PC: remove both auto-start vectors (login
    launcher + SYSTEM scheduled task) and return the adb server to local-only."""
    msg = pyqtSignal(str)

    def __init__(self, adb_path=None):
        super().__init__()
        self.adb_path = adb_path

    def run(self):
        try:
            from ..devices import (uninstall_startup, uninstall_serve_task,
                                   stop_shared_server)
            removed = []
            if uninstall_startup():
                removed.append("login auto-start launcher")
            if uninstall_serve_task():
                removed.append("SYSTEM startup task")
            self.msg.emit("[OK] removed auto-start: " + (", ".join(removed)
                          if removed else "none was installed"))
            self.msg.emit("[OK] " + stop_shared_server(adb_path=self.adb_path))
            self.msg.emit("[OK] This PC no longer shares its devices and will not "
                          "start the shared server automatically.")
        except Exception as exc:
            self.msg.emit(f"[ERROR] stop sharing: {exc}")


class _DeployThread(QThread):
    """Deploy + start `turboadb serve` on remote hosts over WinRM, streaming
    per-host status back to the log."""
    status = pyqtSignal(str)

    def __init__(self, hosts, user, pw, port, update, use_ssl=False):
        super().__init__()
        self.hosts, self.user, self.pw = hosts, user, pw
        self.port, self.update, self.use_ssl = port, update, use_ssl

    def run(self):
        try:
            from ..remote_deploy import deploy_serve
            deploy_serve(self.hosts, self.user, self.pw, update=self.update,
                         port=self.port, use_ssl=self.use_ssl,
                         winrm_port=5986 if self.use_ssl else 5985,
                         on_status=self.status.emit)
            self.status.emit("[OK] Remote deploy finished.")
        except Exception as exc:
            self.status.emit(f"[ERROR] remote deploy: {exc}")


def _shortcut_worker(force=False):
    """Create the Desktop + Start-menu shortcuts (``done`` carries a dict).

    Off the UI thread, since the first run spawns PowerShell.  Launch only
    repairs missing shortcuts: rewriting shell links on every launch adds
    PowerShell work and can trigger endpoint protection, so only an explicit
    refresh from the menu forces it.
    """

    def work():
        try:
            from ..cli import ensure_shortcuts
            return ensure_shortcuts(force=force) or {}
        except Exception:
            return {}

    return FunctionThread(work)


def _update_check_worker(cached=False):
    """Ask PyPI whether a newer TurboADB exists; ``done`` carries the newer
    version or "".  *cached=True* uses the once-a-day cache (quiet launch check)."""

    def work():
        try:
            from ..update import check, check_cached
            return (check_cached() if cached else check()) or ""
        except Exception:
            return ""

    return FunctionThread(work)


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
    # Socket tracking is the primary, immediate notification channel.  This is
    # deliberately only a reconciliation fallback, never a second fast poller.
    _DEVICE_POLL_INTERVAL_MS = 15_000
    # A last-confirmed entry lets the sidebar stay useful while a cold Windows
    # adb daemon enumerates USB.  It is explicitly labelled as reconnecting and
    # is replaced as soon as the live socket reports the real state.
    _DEVICE_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
    _DEVICE_CACHE_HINT_MS = 1_500
    # At most one warning/error toast in this window; the rest still reach the
    # log and the status bar, so a burst never stacks up popups.
    _TOAST_INTERVAL_S = 4.0

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
        # TurboADB is a multi-tool workspace; start maximized so device
        # controls, file tables, and split panes have a usable first layout.
        self.setWindowState(self.windowState() | Qt.WindowMaximized)
        theme.apply_to_app(QApplication.instance(), settings_mod.get("theme"))

        self.store = SessionStore()
        self._last_toast = float("-inf")
        # A timed status message clears to nothing; put the connection summary back.
        self.statusBar().messageChanged.connect(
            lambda message: None if message else QTimer.singleShot(0, self._update_status)
        )
        self._poll = None
        self._live_devices = []
        self._last_device_sigs = None
        self._empty_device_count = 0
        self._tracker = None
        self._cached_devices = []
        self._build_menubar()
        self._build_ribbon()
        self._build_sidebar()
        self._build_center()
        self._build_log_dock()

        self.setStatusBar(QStatusBar())
        self._build_status_indicators()
        self._install_shortcuts()
        self.refresh_sessions()
        self._update_status()
        self._seen_serials = None
        # version is shown in the status bar only (not decorated elsewhere)
        self.log_panel.append("[OK] TurboADB ready")
        if self.store.load_error:
            self.log_panel.append(
                "[WARNING] Saved targets could not be loaded; the file was left untouched. "
                "Import a backup or repair sessions.json before saving new targets."
            )

        # A cold adb daemon can take several seconds for Windows USB
        # enumeration.  Show a clearly-labelled, last-confirmed device right
        # away (when one exists) instead of making the sidebar look empty until
        # the server replies.  This is never treated as a current live result.
        self._cached_devices = self._load_recent_devices()
        if self._cached_devices:
            self._show_cached_devices()
            # A cached entry is only a launch hint. Leaving it yellow while an
            # ADB failure is retried makes a broken startup look merely slow.
            QTimer.singleShot(self._DEVICE_CACHE_HINT_MS, self._expire_cached_hint)
        else:
            init_item = QListWidgetItem("Detecting devices…")
            init_item.setFlags(Qt.NoItemFlags)
            self.live_list.addItem(init_item)

        # Give an already-running daemon an immediate socket-only probe and
        # tracker subscription.  Neither can start/restart adb, so the locked
        # startup workers below remain the only daemon owners.  This makes warm
        # launches show real devices before a cold-start check has completed.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_devices)
        self._start_device_tracker()
        self._poll_devices(socket_only=True)
        self._begin_adb_startup()

        # if adb is missing, offer to download it once the window is up
        QTimer.singleShot(500, self._check_tools)
        # make sure the Desktop + Start-menu shortcuts exist (self-healing)
        QTimer.singleShot(1000, self._ensure_shortcuts)
        # quiet launch check (Settings → Startup, on by default): if PyPI has a
        # newer TurboADB it only logs a line — installing still happens ONLY via
        # the ribbon 🔄 Upgrade button, never automatically.
        if settings_mod.get("auto_update"):
            QTimer.singleShot(2500, self._quiet_update_check)

    # ---- menu bar (basic actions: sessions, save files, tools, help) ----
    # Menu glyph -> (vector icon, tone); anything unmapped keeps its glyph.
    _MENU_ICONS = {
        "➕": ("plus", "accent"), "🔌": ("plug", "green"), "💾": ("save", "teal"),
        "📝": ("save", "teal"), "🖥": ("monitor", "teal"), "📤": ("upload", "blue"),
        "📥": ("download", "blue"), "✖": ("x", "danger"), "📹": ("video", "red"),
        "🗜": ("columns", "dim"), "🌗": ("moon", "blue"), "📋": ("panel-bottom", "teal"),
        "📱": ("smartphone", "green"), "📸": ("camera", "blue"), "🔄": ("refresh", "amber"),
        "📶": ("wifi", "teal"), "🔗": ("link", "purple"), "📢": ("terminal", "orange"),
        "🛰": ("server", "purple"), "📡": ("upload", "purple"), "⬆": ("download", "green"),
        "⚙": ("settings", "text"), "❓": ("help", "blue"), "ℹ": ("info", "blue"),
        "☀": ("sun", "amber"), "🌙": ("moon", "blue"),
    }

    def _build_menubar(self):
        def ico(glyph, colour=None):
            if glyph in self._MENU_ICONS:
                return icons.icon(*self._MENU_ICONS[glyph])
            return theme.emoji_icon(glyph, colour)

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
        m_file.addAction(ico("📤"), "Export saved targets…", self.export_targets)
        m_file.addAction(ico("📥"), "Import saved targets…", self.import_targets)
        m_file.addSeparator()
        m_file.addAction(ico("✖", theme.DANGER), "Exit", self.close, "Ctrl+Q")

        m_view = mb.addMenu("&View")
        m_view.addAction(ico("📹"), "Open webcam (host camera)", self.open_webcam_tab)
        m_view.addAction(ico("🌗"), "Toggle dark / light theme", self.toggle_theme)
        m_view.addAction(ico("📋"), "Toggle log panel", self.toggle_log)

        m_theme = mb.addMenu("&Themes")
        self._theme_menu_actions = {}
        # Dark and light palettes are listed as two clearly separate groups.
        for light in (False, True):
            if light:
                m_theme.addSeparator()
            heading = m_theme.addAction("Light themes" if light else "Dark themes")
            heading.setEnabled(False)
            for key in theme.theme_names():
                if theme.is_light(key) != light:
                    continue
                action = m_theme.addAction(ico("☀" if light else "🌙"), theme.theme_label(key))
                action.setCheckable(True)
                action.triggered.connect(lambda _checked=False, name=key: self._apply_theme(name))
                self._theme_menu_actions[key] = action

        m_dev = mb.addMenu("&Device")
        m_dev.addAction(ico("📱"), "Mirror (scrcpy)", self._mirror_current)
        m_dev.addAction(ico("📸"), "Screenshot", self._shot_current)
        m_dev.addSeparator()
        m_dev.addAction(ico("🔄"), "Restart ADB server", self.restart_adb_server)
        m_dev.addAction(ico("📶"), "Discover Wi-Fi devices (Android 11+)…",
                        self.discover_wireless)
        m_dev.addAction(ico("🔗"), "Pair device (Android 11+)…", self.pair_device)
        m_dev.addSeparator()
        m_dev.addAction(ico("📢"), "Run a command on ALL devices…",
                        self.broadcast_command)
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
        if thread_running(getattr(self, "_sc", None)):
            self.log_panel.append("[INFO] Shortcut update is already running.")
            return
        # Creating shell links spawns PowerShell, so it never runs on the UI thread.
        self._sc = _shortcut_worker(force=True)
        self._sc.done.connect(self._on_shortcuts_refreshed)
        self._sc.start()

    def _on_shortcuts_refreshed(self, res):
        bad = [k for k, v in (res or {}).items() if not v]
        if bad:
            self.log_panel.append(f"[WARNING] Could not create: {', '.join(bad)}.")
        else:
            self.log_panel.append("[OK] Desktop + Start-menu shortcuts refreshed.")

    def export_targets(self):
        from PyQt5.QtWidgets import QFileDialog
        if not self.store.sessions:
            QMessageBox.information(self, "Export targets",
                                    "No saved targets to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export saved targets", "turboadb-targets.json",
            "JSON (*.json);;All files (*)")
        if not path:
            return
        try:
            n = self.store.export_to(path)
            self.log_panel.append(f"[OK] exported {n} target(s) → {path}")
        except Exception as exc:
            self.log_panel.append(f"[ERROR] export targets: {exc}")

    def import_targets(self):
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Import saved targets", "",
            "JSON (*.json);;All files (*)")
        if not path:
            return
        try:
            n = self.store.import_from(path)
            self.refresh_sessions()
            self.log_panel.append(f"[OK] imported {n} target(s) from {path}")
        except Exception as exc:
            self.log_panel.append(f"[ERROR] import targets: {exc}")
            QMessageBox.warning(self, "Import targets",
                                f"Couldn't import that file:\n\n{exc}")

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

    # ---- app bar: global actions only (device actions live in each device tab) ----
    def _build_ribbon(self):
        tb = QToolBar("TurboADB")
        tb.setObjectName("ribbon")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self._ribbon = tb
        self.addToolBar(tb)

        def menu_button(text, tooltip, role=None, glyph=None, tone=None):
            button = QToolButton()
            button.setText(text)
            button.setToolTip(tooltip)
            if glyph:
                button.setIcon(icons.icon(glyph, tone))
                button.setIconSize(QSize(18, 18))
                button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            else:
                button.setToolButtonStyle(Qt.ToolButtonTextOnly)
            button.setPopupMode(QToolButton.InstantPopup)
            if role:
                button.setProperty("role", role)
            tb.addWidget(button)
            return button

        # Connect: the unified Connect dialog (USB / Network / Remote PC) plus extras.
        dev_btn = menu_button(
            "Connect ▾", "Connect to a device or save a target", role="ok",
            glyph="plug", tone="on-accent",
        )
        dmenu = QMenu(dev_btn)
        dmenu.addAction("Connect to a device…", self.open_connect)
        dmenu.addAction("Save a target (without connecting)…", self.new_session)
        dmenu.addSeparator()
        dmenu.addAction("Discover Wi-Fi devices (Android 11+)…",
                        self.discover_wireless)
        dmenu.addAction("Pair device (Android 11+)…", self.pair_device)
        dmenu.addAction("Restart ADB server", self.restart_adb_server)
        dev_btn.setMenu(dmenu)
        self._dev_btn = dev_btn

        srv_btn = menu_button(
            "ADB server ▾",
            "Start 'turboadb serve' on a remote machine, or share this PC's devices.",
            glyph="server", tone="purple",
        )
        smenu = QMenu(srv_btn)
        smenu.addAction(theme.emoji_icon("📡"),
                        "Deploy to remote machine(s) (RDP / WinRM)…",
                        self.deploy_serve_remote)
        smenu.addAction(theme.emoji_icon("🛰"), "Share THIS PC's devices…",
                        self.share_devices)
        smenu.addAction(theme.emoji_icon("🛑", theme.DANGER),
                        "Stop sharing & remove auto-start", self.stop_sharing)
        smenu.addSeparator()
        smenu.addAction(theme.emoji_icon("🔄"), "Restart local ADB server",
                        self.restart_adb_server)
        srv_btn.setMenu(smenu)
        self._srv_btn = srv_btn

        # Device sections (Terminal, Logcat, Files, …) are the device tab's own
        # tabs; repeating them here stacked a second navigation row on top.
        self.btn_upgrade = menu_button(
            "Tools ▾", "Updates, tool downloads and utilities", glyph="wrench", tone="amber"
        )
        m_upg = QMenu(self.btn_upgrade)
        m_upg.addAction(icons.icon("refresh", "green"), "Check for updates…", self.upgrade_tools_gui)
        m_upg.addAction(
            icons.icon("download", "blue"),
            "Download and reinstall ADB and scrcpy…",
            lambda: self._run_tools(
                "fetch",
                "Downloading latest official ADB + Scrcpy from Google & GitHub…",
                force=True,
            ),
        )
        m_upg.addSeparator()
        m_upg.addAction(icons.icon("video", "red"), "Open host webcam", self.open_webcam_tab)
        m_upg.addAction(
            icons.icon("monitor", "teal"),
            "Create desktop and Start-menu shortcuts",
            self.make_shortcuts_now,
        )
        self.btn_upgrade.setMenu(m_upg)

        spacer = QWidget()
        spacer.setObjectName("barSpacer")
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        def icon_action(glyph, tone, tooltip, slot):
            action = QAction(icons.icon(glyph, tone), tooltip, self)
            action.setToolTip(tooltip)
            action.triggered.connect(slot)
            tb.addAction(action)
            return action

        # utility icons on the right: theme toggle (glyph = theme you switch TO),
        # log panel, settings, help.  Compact and Exit stay in the View/File menus.
        self.act_theme = QAction(self)
        self.act_theme.triggered.connect(self.toggle_theme)
        tb.addAction(self.act_theme)
        self._sync_theme_action()
        self.act_logs = icon_action("panel-bottom", "teal", "Show or hide the log panel", self.toggle_log)
        icon_action("settings", "text", "Settings", self.show_settings)
        icon_action("help", "blue", "Help and documentation", self._open_docs)

    # ---- sidebar ----
    def _build_sidebar(self):
        dock = QDockWidget("Devices", self)
        dock.setObjectName("sidebarDock")
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        # A fixed navigation panel, not a floatable/closable tool window.
        dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
        dock.setTitleBarWidget(QWidget())
        panel = QWidget()
        panel.setObjectName("sidebarPanel")
        panel.setMinimumWidth(240)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)
        title = QLabel("Devices")
        title.setObjectName("sidebarTitle")
        self._device_count = QLabel("0")
        self._device_count.setObjectName("countBadge")
        self._device_count.setToolTip("Devices currently attached")
        add = QToolButton()
        add.setObjectName("iconButton")
        add.setIcon(icons.icon("plus", "accent"))
        add.setToolTip("New target (Ctrl+N)")
        add.clicked.connect(self.new_session)
        collapse = QToolButton()
        collapse.setObjectName("iconButton")
        collapse.setIcon(icons.icon("chevron-left", "dim"))
        collapse.setToolTip("Hide the device list (Ctrl+B)")
        collapse.clicked.connect(lambda: self._toggle_devices(False))
        header.addWidget(title)
        header.addWidget(self._device_count)
        header.addStretch(1)
        header.addWidget(add)
        header.addWidget(collapse)
        lay.addLayout(header)
        from PyQt5.QtWidgets import QShortcut

        QShortcut(QKeySequence("Ctrl+B"), self, activated=lambda: self._toggle_devices())

        self.quick = QLineEdit()
        self.quick.setPlaceholderText("Search or quick connect…")
        self.quick.setToolTip("Filter saved targets; press Enter to open the first match")
        self.quick.textChanged.connect(self._filter_sessions)
        self.quick.returnPressed.connect(self._quick_enter)
        lay.addWidget(self.quick)

        from PyQt5.QtWidgets import QFrame

        def card(title, glyph, tone):
            """A boxed sidebar section: an underlined icon + title header over its list."""
            frame = QFrame()
            frame.setObjectName("sidebarCard")
            box = QVBoxLayout(frame)
            box.setContentsMargins(1, 1, 1, 6)
            box.setSpacing(4)
            header_row = QWidget()
            header_row.setObjectName("sidebarCardHeader")
            header_row.setAttribute(Qt.WA_StyledBackground, True)
            head = QHBoxLayout(header_row)
            head.setContentsMargins(10, 7, 10, 7)
            head.setSpacing(8)
            mark = QToolButton()
            mark.setObjectName("iconButton")
            mark.setIcon(icons.icon(glyph, tone))
            mark.setIconSize(QSize(16, 16))
            mark.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            mark.setFocusPolicy(Qt.NoFocus)
            head.addWidget(mark)
            head.addWidget(_section(title))
            head.addStretch(1)
            box.addWidget(header_row)
            return frame, box

        self._live_card, live_box = card("Connected", "phone-link", "green")
        self.live_list = QListWidget()
        self.live_list.setTextElideMode(Qt.ElideRight)
        self.live_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Sized to its rows so "Saved targets" starts right below instead of
        # after a large empty gap.
        self.live_list.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        live_model = self.live_list.model()
        live_model.rowsInserted.connect(lambda *_: self._fit_live_list())
        live_model.rowsRemoved.connect(lambda *_: self._fit_live_list())
        live_model.modelReset.connect(self._fit_live_list)
        # itemActivated already fires for both Enter and a double-click.  Wiring
        # itemDoubleClicked too opened the same device twice on one double-click.
        self.live_list.itemActivated.connect(self._open_live)
        live_box.addWidget(self.live_list)
        lay.addWidget(self._live_card)
        lay.addSpacing(6)

        self._saved_card, saved_box = card("Saved targets", "bookmark", "blue")
        self.session_list = QListWidget()
        self.session_list.itemDoubleClicked.connect(lambda _: self.open_selected())
        self.session_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._session_menu)
        saved_box.addWidget(self.session_list, 1)
        self._saved_empty = QLabel("No saved targets yet.\nUse + above to add a phone or head unit.")
        self._saved_empty.setObjectName("emptyState")
        self._saved_empty.setWordWrap(True)
        self._saved_empty.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        saved_box.addWidget(self._saved_empty, 1)
        lay.addWidget(self._saved_card, 1)

        dock.setWidget(panel)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self._sidebar_dock = dock

    def _build_status_indicators(self):
        """Persistent, compact status facts complement the contextual message.

        The centre text can change for the current tab; these three pills keep
        transport, discovery and application version visible at all times.
        """
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self._status_adb = QLabel("ADB: starting…")
        self._status_devices = QLabel("Devices: 0")
        self._status_version = QLabel(f"TurboADB v{self._version or '—'}")
        for label in (self._status_adb, self._status_devices, self._status_version):
            label.setObjectName("statusPill")
            bar.addPermanentWidget(label)

    def _set_adb_indicator(self, state: str):
        if hasattr(self, "_status_adb"):
            self._status_adb.setText(f"ADB: {state}")

    def _set_device_indicator(self, devices):
        if not hasattr(self, "_status_devices"):
            return
        online = sum(bool(getattr(device, "is_online", False)) for device in (devices or []))
        total = len(devices or [])
        if not total:
            self._status_devices.setText("Devices: 0")
        elif online == total:
            self._status_devices.setText(f"Devices: {online} online")
        else:
            self._status_devices.setText(f"Devices: {online}/{total} online")

    def _build_center(self):
        self.tabs = AnimatedTabWidget(transition_ms=145)
        self.tabs.setObjectName("mainTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        # don't truncate device-tab titles; scroll when there are many
        self.tabs.setElideMode(Qt.ElideNone)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_main_tab_changed)
        # right-click a tab for the standard close actions
        tb = self.tabs.tabBar()
        tb.setContextMenuPolicy(Qt.CustomContextMenu)
        tb.customContextMenuRequested.connect(self._tab_context_menu)
        plus = QToolButton()
        plus.setObjectName("iconButton")
        plus.setIcon(icons.icon("plus", "accent"))
        plus.setToolTip("Open another device")
        plus.clicked.connect(self.new_session)
        self.tabs.setCornerWidget(plus, Qt.TopRightCorner)
        # central STACK: a MobaXterm-style welcome page when nothing is open,
        # the device tabs otherwise (so the app never shows a blank rectangle).
        from PyQt5.QtWidgets import QStackedWidget
        from .welcome import WelcomeScreen
        self._center = QStackedWidget()
        self.welcome = WelcomeScreen(self)
        self._center.addWidget(self.welcome)      # index 0 — landing page
        self._center.addWidget(self.tabs)         # index 1 — tabs
        # A slim bar on the left edge, shown while the device list is hidden
        # (by hand or while a device screen is showing), reopens the list.
        self._sidebar_handle = _EdgeHandle()
        self._sidebar_handle.clicked.connect(lambda: self._toggle_devices(True))
        self._sidebar_handle.hide()
        central = QWidget()
        central_row = QHBoxLayout(central)
        central_row.setContentsMargins(0, 0, 0, 0)
        central_row.setSpacing(0)
        central_row.addWidget(self._sidebar_handle)
        central_row.addWidget(self._center, 1)
        self.setCentralWidget(central)
        self._update_center()

    def _on_main_tab_changed(self, index):
        """Max view belongs to the device tab on screen: restore the docks for
        every other tab first, then let the current one re-apply its own."""
        current = self.tabs.widget(index)
        for tab in self._device_tabs():
            if tab is not current:
                tab.set_workspace_active(False)
        if isinstance(current, DeviceTab):
            current.set_workspace_active(True)
        self._update_status()

    def _update_center(self):
        """Show the welcome landing page while no device tab is open, and the tab
        area as soon as one is."""
        stack = getattr(self, "_center", None)
        if stack is None:
            return
        stack.setCurrentWidget(self.welcome if self.tabs.count() == 0
                               else self.tabs)

    def _build_log_dock(self):
        self.log_panel = LogPanel()
        dock = QDockWidget("Log", self)
        dock.setWidget(self.log_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        dock.hide()
        self._log_dock = dock

    _LOG_LEVEL_RE = re.compile(
        r"^\s*\[(DEBUG|INFO|OK|SUCCESS|WARNING|WARN|STDERR|ERROR|CRITICAL|FATAL)\]\s*",
        re.IGNORECASE,
    )
    _LEVEL_ALIASES = {
        "SUCCESS": "OK", "WARN": "WARNING", "STDERR": "WARNING",
        "CRITICAL": "ERROR", "FATAL": "ERROR",
    }

    def _log(self, text: str):
        """Record *text* in the log panel and surface it outside the log.

        Every meaningful message updates the status bar. Unless the log panel
        is open with Silent ticked, actions also show a small activity toast
        (updated in place, never a stack), and errors show a large red toast
        at the bottom with an alert sound.
        """
        if not text:
            return
        self.log_panel.append(text)
        match = self._LOG_LEVEL_RE.match(text)
        level = match.group(1).upper() if match else "INFO"
        level = self._LEVEL_ALIASES.get(level, level)
        clean = (text[match.end():] if match else text).strip()
        # The raw adb command trace is DEBUG noise; it stays in the log only.
        if level == "DEBUG" or not clean or clean.startswith(("$ ", "-> ")):
            return
        prefix = {"ERROR": "Error: ", "WARNING": "Warning: "}.get(level, "")
        self.statusBar().showMessage(prefix + clean.splitlines()[0], 10000 if prefix else 6000)
        silent = bool(
            getattr(self.log_panel, "chk_silent", None) and self.log_panel.chk_silent.isChecked()
        )
        dock_open = bool(getattr(self, "_log_dock", None) and self._log_dock.isVisible())
        # "Silent" only mutes popups while the log itself is on screen; it used
        # to mute them always, so with the log closed nothing was ever shown.
        if dock_open and silent:
            return
        from . import fileutil

        self._last_toast = time.monotonic()
        if level == "ERROR":
            fileutil.error_toast(self, clean, action_text="Show log", action=self._show_log_dock)
        else:
            fileutil.activity_toast(
                self,
                clean,
                level={"WARNING": "warning", "OK": "ok"}.get(level, "info"),
                action_text="Show log" if level == "WARNING" else "",
                action=self._show_log_dock,
            )

    def _show_log_dock(self):
        """Reveal and focus the diagnostic dock from a notification action.

        Notification toasts are tool windows and may be clicked while the main
        window is behind another app or minimised.  Merely showing the dock in
        that state changes its visibility flag without giving the user anything
        to see, which made the toast's ``Show log`` action look inert.
        """
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        dock = self._log_dock
        dock.setVisible(True)
        dock.show()
        dock.raise_()
        self.log_panel.show()
        self.log_panel.view.setFocus(Qt.OtherFocusReason)
        # A dock can be tabified behind another bottom dock. Raising it again
        # on the next event-loop turn, after the parent has been restored,
        # makes the toast action deterministic.
        def finish_reveal():
            dock.show()
            dock.raise_()
            self.log_panel.view.setFocus(Qt.OtherFocusReason)

        QTimer.singleShot(0, finish_reveal)

    def _install_shortcuts(self):
        # Ctrl+N (New target) and F1 (Docs) live on the menu bar now, so they're
        # not duplicated here (avoids "ambiguous shortcut" warnings).
        for seq, slot in (("Ctrl+T", self.new_session),
                          ("Ctrl+W", self._close_current_tab),
                          ("Ctrl+Return", self.open_selected)):
            QShortcut(QKeySequence(seq), self, activated=slot)

    # ---- live devices ----
    def _start_poll_timer(self):
        if not self._timer.isActive():
            # The socket tracker publishes changes immediately.  Poll only as a
            # low-frequency reconciliation fallback rather than duplicating every
            # request and racing tracker updates.
            self._timer.start(self._DEVICE_POLL_INTERVAL_MS)

    def _set_live_message(self, text: str) -> None:
        """Show a meaningful non-device state without pretending it is empty."""
        self.live_list.clear()
        item = QListWidgetItem(text)
        item.setFlags(Qt.NoItemFlags)
        self.live_list.addItem(item)

    @staticmethod
    def _recent_devices_path() -> str:
        return os.path.join(os.path.expanduser("~"), ".turboadb", "recent-devices.json")

    def _load_recent_devices(self) -> list[dict]:
        """Return a small, fresh last-confirmed-device cache for launch UI."""
        import json
        import time

        try:
            with open(self._recent_devices_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            saved_at = float(data.get("saved_at", 0))
            devices = data.get("devices")
            if time.time() - saved_at > self._DEVICE_CACHE_MAX_AGE_SECONDS:
                return []
            if not isinstance(devices, list):
                return []
            return [
                {"serial": str(d["serial"]), "label": str(d.get("label") or "device")}
                for d in devices
                if isinstance(d, dict) and str(d.get("serial") or "").strip()
            ]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return []

    def _save_recent_devices(self, devices) -> None:
        """Persist only real online-device identities, never a transient empty scan."""
        import time

        records = [
            {"serial": d.serial, "label": d.label or d.model or "device"}
            for d in devices
            if getattr(d, "is_online", False) and getattr(d, "serial", "")
        ]
        if not records:
            return
        try:
            from .settings import _atomic_write_json

            _atomic_write_json(
                self._recent_devices_path(),
                {"saved_at": time.time(), "devices": records},
            )
        except Exception:
            # The cache is only a launch-time convenience; device detection
            # must still work when a profile directory is read-only.
            pass

    def _show_cached_devices(self) -> None:
        """Display last-confirmed devices while retaining honest live state."""
        self.live_list.clear()
        for dev in self._cached_devices:
            serial = dev["serial"]
            label = dev["label"]
            item = QListWidgetItem(f"🔄  {label}  ·  verifying…")
            # Do not let a stale item launch a second `adb start-server` while
            # the startup worker already owns the daemon.  It becomes a normal
            # live, connectable item as soon as ADB confirms it.
            item.setFlags(Qt.NoItemFlags)
            item.setToolTip(
                f"{label}\n{serial}\n\nThis device was confirmed on the previous session. "
                "TurboADB is verifying the current ADB connection."
            )
            self.live_list.addItem(item)

    def _expire_cached_hint(self) -> None:
        """Do not leave a stale-device hint masquerading as a live result."""
        if self._cached_devices and not self._last_device_sigs:
            self._set_live_message("🔄  Starting bundled ADB…")

    def _begin_adb_startup(self) -> None:
        """Start a cold local daemon once, without blocking the live tracker."""
        if thread_running(getattr(self, "_adb_init", None)):
            return
        # Do not replace a result which the tracker/socket probe may already
        # have delivered during a warm launch.  A cold daemon is still started
        # in the worker below, but the rest of the UI remains ready at once.
        if not self._last_device_sigs and not self._cached_devices:
            self._set_live_message("🔄  Connecting to ADB…")
        self._adb_init = _AdbInitThread(adb_path=gui_adb_path())
        self._adb_init.ready.connect(self._on_adb_ready)
        self._adb_init.start()

    def _start_device_tracker(self):
        if thread_running(getattr(self, "_tracker", None)):
            return
        self._tracker = _DeviceTracker(adb_path=gui_adb_path())
        self._tracker.result.connect(self._on_devices)
        self._tracker.start()

    def _on_adb_ready(self, ok: bool, msg: str):
        if ok:
            self._set_adb_indicator("ready")
            self.log_panel.append(f"[OK] {msg}")
            self._poll_devices()
            self._start_device_tracker()
            self._start_poll_timer()
        else:
            self._set_adb_indicator("unavailable")
            self.log_panel.append(f"[WARNING] {msg}")
            self._cached_devices = []
            self._set_live_message("⚠  ADB unavailable — see the log")

    def _server_task_running(self) -> bool:
        """True while a worker kills, restarts, re-shares or replaces the daemon."""
        return any(
            thread_running(getattr(self, name, None))
            for name in ("_as", "_share", "_unshare", "_dl")
        )

    def _poll_devices(self, *, socket_only: bool = False):
        if thread_running(self._poll):
            return
        if self._server_task_running():
            # `adb devices` would auto-start a daemon in the kill→start gap (or
            # re-lock adb.exe mid-replace).  A socket probe can't start one.
            socket_only = True
        self._poll = _DevicesPoll(adb_path=gui_adb_path(), socket_only=socket_only)
        self._poll.result.connect(self._on_devices)
        self._poll.failed.connect(self._on_device_poll_error)
        self._poll.start()

    def _on_device_poll_error(self, detail: str):
        """Keep a known device list stable, but never hide a real ADB failure."""
        self.log_panel.append(f"[WARNING] Device scan failed: {detail[:400]}")
        self._set_adb_indicator("scan issue")
        if not self._last_device_sigs:
            self._cached_devices = []
            self._set_live_message("⚠  Device scan failed — see the log")

    def _on_devices(self, devices):
        # Debounce: if devices is empty but we previously had devices, require 2 consecutive
        # empty reports before clearing the UI or logging disconnection. This avoids UI
        # flicker and false disconnect alarms during transient socket hiccups.
        if not devices:
            self._empty_device_count += 1
            if self._empty_device_count < 2 and self._last_device_sigs:
                self._set_device_indicator(self._live_devices)
                return
        else:
            self._empty_device_count = 0

        # Signature of current device list
        new_sigs = tuple((d.serial, d.state, d.label, d.is_online) for d in devices) if devices else ()
        if self._last_device_sigs is not None and new_sigs == self._last_device_sigs:
            # Device list has not changed at all! Do not clear live_list or rebuild items.
            self._set_device_indicator(devices)
            return
        self._last_device_sigs = new_sigs

        self._live_devices = devices
        self._set_device_indicator(devices)
        if devices:
            self._cached_devices = []
            self._save_recent_devices(devices)
        if hasattr(self, "welcome"):
            self.welcome.refresh_status(devices)
        if hasattr(self, "_device_count"):
            self._device_count.setText(str(len(devices)))
        self.live_list.clear()
        if not devices:
            it = QListWidgetItem("No devices connected")
            it.setFlags(Qt.NoItemFlags)
            self.live_list.addItem(it)
            if self._seen_serials:
                self.log_panel.append("[INFO] All devices disconnected")
                self._seen_serials = set()
            self._update_status()
            return

        current_serials = {d.serial for d in devices}
        if self._seen_serials is None or current_serials != self._seen_serials:
            for d in devices:
                if self._seen_serials is None or d.serial not in self._seen_serials:
                    self.log_panel.append(
                        f"[OK] Device found: {d.label or d.model or 'device'} · {d.serial} ({d.state})"
                    )
            self._seen_serials = current_serials

        tokens = theme.palette()
        for d in devices:
            # Keep the clickable label readable in a narrow sidebar.  The
            # serial is still one hover away and retained in UserRole for the
            # exact session connection; the status dot is the row icon.
            it = QListWidgetItem(f"{d.label or d.model or 'device'}  ·  {d.state}")
            it.setIcon(theme.emoji_icon("●", tokens["ok_text"] if d.is_online else tokens["warn_text"]))
            it.setData(Qt.UserRole, d.serial)
            it.setToolTip(f"{d.label or d.model or 'device'}\n{d.serial}\nState: {d.state}")
            self.live_list.addItem(it)
        self._update_status()

    def _open_live(self, item):
        serial = item.data(Qt.UserRole)
        if not serial:
            return
        live = next((d for d in self._live_devices if d.serial == serial), None)
        friendly = (
            getattr(live, "model", "")
            or getattr(live, "product", "")
            or getattr(live, "device", "")
            or serial
        )
        is_net = ":" in serial and serial.rpartition(":")[2].isdigit()
        s = {"name": friendly, "type": "network" if is_net else "usb",
             "serial": "" if is_net else serial,
             "host": serial.rpartition(":")[0] if is_net else "",
             "port": int(serial.rpartition(":")[2]) if is_net else 5555}
        self._open_session(s, friendly)

    # ---- saved sessions ----
    # Distinct glyphs per target type, muted to match the calm palettes; the
    # light-theme colours are deep enough to read on the stone sidebar.
    #   type -> (glyph, dark-theme colour, light-theme colour)
    _TYPE_ICON = {
        "usb": ("usb", "green"),         # local USB device
        "network": ("wifi", "blue"),     # device by IP
        "remote": ("server", "purple"),  # device on another PC
    }

    def refresh_sessions(self):
        self.session_list.clear()
        for s in self.store.sessions:
            t = s.get("type") or "usb"
            glyph, tone = self._TYPE_ICON.get(t, self._TYPE_ICON["usb"])
            if t == "network":
                tgt = f"{s.get('host')}:{s.get('port')}"
            elif t == "remote":
                tgt = (f"{s.get('adb_host')}:{s.get('adb_port')} → "
                       f"{s.get('serial') or 'only device'}")
            else:
                tgt = s.get("serial") or "only device"
            name = s.get("name") or tgt
            # auto-named targets ARE their address/serial — showing
            # "192.168.1.7:5555 · 192.168.1.7:5555" repeated the name twice
            label = f"  {name}" if name == tgt else f"  {name}   ·  {tgt}"
            it = QListWidgetItem(label)
            it.setIcon(icons.icon(glyph, tone))
            it.setData(Qt.UserRole, s.get("name"))
            self.session_list.addItem(it)
        if hasattr(self, "_saved_empty"):
            empty = not self.store.sessions
            self.session_list.setVisible(not empty)
            self._saved_empty.setVisible(empty)
        self._filter_sessions(self.quick.text())

    def _fit_live_list(self):
        """Height = its rows (1 to 6), so the sidebar has no dead gap."""
        rows = self.live_list.count()
        heights = [max(self.live_list.sizeHintForRow(i), 28) for i in range(min(rows, 6))]
        content = sum(heights) if heights else 28
        self.live_list.setFixedHeight(content + 2 * self.live_list.frameWidth() + 4)

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
        menu.addAction(icons.icon("plus", "accent"), "New target…", self.new_session)
        item = self.session_list.itemAt(pos)
        if item is not None:
            self.session_list.setCurrentItem(item)
            menu.addSeparator()
            menu.addAction(icons.icon("plug", "green"), "Open / Connect", self.open_selected)
            menu.addAction(icons.icon("edit", "blue"), "Edit…", self.edit_session)
            menu.addAction(icons.icon("copy", "blue"), "Duplicate", self._duplicate_session)
            menu.addSeparator()
            menu.addAction(icons.icon("trash", "danger"), "Delete", self.delete_session)
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
            dlg.mode.setCurrentIndex(1)
            host, _, port = prefill_host.rpartition(":")
            if port.isdigit():
                dlg.host.setText(host); dlg.port.setValue(int(port))
            else:
                dlg.host.setText(prefill_host)
        # SessionDialog validates the target (name included) before accepting,
        # and deletes itself once closed.
        if dlg.exec_() != dlg.Accepted:
            return
        s = dlg.result_session()
        self.store.save(s)
        self.refresh_sessions()
        self.log_panel.append(f"[OK] Saved target '{s['name']}'")

    def edit_session(self):
        name = self._selected_name()
        if not name:
            return
        dlg = SessionDialog(self, existing=self.store.get(name))
        if dlg.exec_() == dlg.Accepted:
            # result_session() carries previous_name, so a rename replaces the
            # old entry instead of leaving a copy behind.
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

    @staticmethod
    def _session_identity(session):
        """A stable device identity, independent of a user-editable tab name."""
        if not isinstance(session, dict):
            return None
        kind = (session.get("type") or "usb").lower()
        serial = str(session.get("serial") or "").strip()
        if kind == "remote":
            return (
                "remote",
                str(session.get("adb_host") or "").strip().strip("[]").lower(),
                str(session.get("adb_port") or 5037),
                serial,
            )
        host = str(session.get("host") or "").strip().strip("[]").lower()
        if kind == "network" or host:
            return ("network", host, str(session.get("port") or 5555))
        return ("usb", serial)

    def _resolve_identity(self, identity):
        """Map an 'only device' USB target (empty serial) to the one live USB
        device adb would pick, so it matches a tab opened from that device."""
        if identity != ("usb", ""):
            return identity
        online = [d for d in self._live_devices if getattr(d, "is_online", False)]
        if len(online) == 1:
            serial = str(getattr(online[0], "serial", "") or "")
            is_net = ":" in serial and serial.rpartition(":")[2].isdigit()
            if serial and not is_net:
                return ("usb", serial)
        return identity

    def _tab_identity(self, tab):
        identity = self._session_identity(getattr(tab, "session", None))
        if identity == ("usb", ""):
            serial = str(getattr(getattr(tab, "handler", None), "serial", "") or "")
            if serial:
                return ("usb", serial)
        return self._resolve_identity(identity)

    def _open_session(self, s, name):
        from .sessions import normalize_session

        try:
            # Unsaved targets from Connect intentionally have an empty name.
            # Give the transient tab its display label before applying the same
            # validation used for persistent session files.
            if isinstance(s, dict) and not str(s.get("name") or "").strip():
                s = dict(s)
                s["name"] = name or "device"
            s = normalize_session(s)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid target", str(exc))
            self._log(f"[ERROR] cannot open '{name}': {exc}")
            return
        identity = self._resolve_identity(self._session_identity(s))
        if identity is not None:
            for index in range(self.tabs.count()):
                tab = self.tabs.widget(index)
                # A terminal-only tab is intentionally an extra shell session,
                # not a replacement for the full device workspace.  It must
                # never prevent the user from opening Device Control later.
                if (
                    not getattr(tab, "_terminal_only", False)
                    and getattr(tab, "session", None) is not None
                    and self._tab_identity(tab) == identity
                ):
                    self._open_duplicate_device(s, name, index)
                    return
        self._add_device_tab(s, name)

    def _open_duplicate_device(self, session, name: str, existing_index: int) -> None:
        """Handle a second request for an open device without silently refusing
        the useful case: an independent trio of shell terminals."""
        action = settings_mod.get("duplicate_device_action") or "ask"
        if action == "ask":
            answer = QMessageBox.question(
                self,
                "Device already open",
                f"'{name}' already has a full Device Control tab.\n\n"
                "Open an additional terminal session with Android Shell, "
                "PowerShell and Command Prompt?\n\n"
                "You can remember this choice in Settings → Startup.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            action = "terminal" if answer == QMessageBox.Yes else "focus"
        if action == "terminal":
            self._add_device_tab(session, name, terminal_only=True)
            return
        self.tabs.setCurrentIndex(existing_index)
        self._update_center()
        self._log(f"'{name}' is already open; focused its Device Control tab.")

    def _add_device_tab(self, session, name: str, *, terminal_only: bool = False) -> None:
        """Create either the full workspace or a deliberately slim terminal
        session.  Both use independent interactive terminal processes."""
        w = DeviceTab(session, terminal_only=terminal_only)
        w.log.connect(self._log)
        w.screen_active.connect(self._on_screen_active)
        if terminal_only:
            w.title_changed.connect(
                lambda title, ww=w: self._set_tab_title(ww, f"{title} · Terminal")
            )
            label = f"{name} · Terminal"
            icon = icons.icon("terminal", "teal")
        else:
            w.title_changed.connect(lambda title, ww=w: self._set_tab_title(ww, title))
            label = name
            icon = icons.icon("smartphone", "green")
        idx = self.tabs.addTab(w, label)
        self.tabs.setTabIcon(idx, icon)
        self.tabs.setCurrentIndex(idx)
        self._update_center()
        self._log(
            f"Opening terminal session for '{name}'…"
            if terminal_only
            else f"Opening '{name}'…"
        )
        if hasattr(w, "start_connect"):
            w.start_connect()

    def open_webcam_tab(self):
        """Open the host Webcam as a standalone tab — available right away, with no
        device connected. Focuses the existing one if it's already open."""
        from .camera_widget import CameraPanel
        existing = getattr(self, "_webcam_tab", None)
        if existing is not None and self.tabs.indexOf(existing) >= 0:
            self.tabs.setCurrentWidget(existing)
            return
        cam = CameraPanel()
        cam.log.connect(self._log)
        self._webcam_tab = cam
        idx = self.tabs.addTab(cam, "Webcam")
        self.tabs.setTabIcon(idx, icons.icon("video", "red"))
        self.tabs.setCurrentIndex(idx)
        self._update_center()
        self.log_panel.append("Opened the host Webcam (no device needed).")

    def _set_tab_title(self, widget, title):
        idx = self.tabs.indexOf(widget)
        if idx >= 0:
            # emoji as the tab ICON + plain text, so the label never truncates
            self.tabs.setTabText(idx, title)
            self.tabs.setTabIcon(
                idx,
                icons.icon("terminal", "teal")
                if getattr(widget, "_terminal_only", False)
                else icons.icon("smartphone", "green"),
            )
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
            attached = len(getattr(self, "_live_devices", None) or [])
            if attached:
                # Devices are plugged in but none is open yet; "no device
                # connected" contradicted the sidebar's Connected list.
                self.statusBar().showMessage(
                    f"TurboADB {self._version} — {attached} device(s) attached; "
                    f"double-click one in the sidebar to open it")
            else:
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
        """All currently open device tabs."""
        return [self.tabs.widget(i) for i in range(self.tabs.count())
                if isinstance(self.tabs.widget(i), DeviceTab)]

    def _current_tab(self):
        w = self.tabs.currentWidget()
        return w if isinstance(w, DeviceTab) else None

    def _subtab(self, name):
        tab = self._current_tab()
        if tab is None:
            QMessageBox.information(self, "TurboADB", "Select an open device tab first.")
            return
        tab.show_subtab(name)

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
        try:
            s = dlg.session() if dlg.exec_() == dlg.Accepted else None
        finally:
            dlg.deleteLater()
        if not s:
            return
        name = s.get("name")
        if name:                              # a name was given -> also save it
            self.store.save(s)
            self.refresh_sessions()
        label = name or s.get("serial") or s.get("host") or s.get("adb_host") or "device"
        self._open_session(s, label)

    def discover_wireless(self):
        """Find Android 11+ Wireless-debugging devices on the LAN (adb mdns) and
        offer to connect to a 'connect'-ready one."""
        if thread_running(getattr(self, "_disc", None)):
            return
        self.log_panel.append("Scanning the LAN for Wireless-debugging devices "
                              "(adb mdns)…")
        self._disc = _discover_worker(gui_adb_path())
        self._disc.done.connect(self._on_discovered)
        self._disc.start()

    def _on_discovered(self, found):
        connectable = [d for d in found if d.get("service") == "connect"]
        pairing = [d for d in found if d.get("service") == "pairing"]
        for d in found:
            self.log_panel.append(f"[OK] found {d['service']}: {d['address']} "
                                  f"({d['name']})")
        if not found:
            self.log_panel.append("[INFO] no Wireless-debugging devices found. "
                                  "On the device: Settings → Developer options → "
                                  "Wireless debugging (same Wi-Fi as this PC).")
            QMessageBox.information(
                self, "Discover Wi-Fi devices",
                "No Android 11+ Wireless-debugging devices were found on the "
                "LAN.\n\nOn the device, turn on Settings → Developer options → "
                "Wireless debugging, and make sure it's on the same network as "
                "this PC. New devices usually need Pair (with a code) once first.")
            return
        if pairing and not connectable:
            self.log_panel.append("[INFO] found only PAIRING entries — use "
                                  "Pair device to enter the code shown on-screen.")
        for d in connectable:
            s = {"name": d["address"], "type": "network",
                 "host": d["host"], "port": d["port"]}
            self.store.save(s)
        if connectable:
            self.refresh_sessions()
            first = connectable[0]
            if QMessageBox.question(
                    self, "Discover Wi-Fi devices",
                    f"Found {len(connectable)} connectable device(s). Connect to "
                    f"{first['address']} now?\n\n(All were saved to the sidebar.)"
                    ) == QMessageBox.Yes:
                self._open_session({"name": first["address"], "type": "network",
                                    "host": first["host"], "port": first["port"]},
                                   first["address"])

    def broadcast_command(self):
        """Run a single adb shell command on EVERY connected device at once."""
        online = [d for d in self._live_devices if d.is_online]
        if not online:
            QMessageBox.information(self, "Run on all devices",
                                    "No connected devices. Plug in / connect "
                                    "some first.")
            return
        cmd, ok = QInputDialog.getText(
            self, "Run on all devices",
            f"adb shell command to run on all {len(online)} connected "
            f"device(s):", text="getprop ro.build.version.release")
        if not ok or not cmd.strip():
            return
        if getattr(self, "_bcast", None) and self._bcast.isRunning():
            self.log_panel.append("[WARNING] a broadcast is already running.")
            return
        self.log_panel.append(f"Running on {len(online)} device(s):  {cmd}")
        self._bcast = _BroadcastThread(
            [d.serial for d in online], cmd.strip(), gui_adb_path()
        )
        self._bcast.line.connect(self.log_panel.append)
        self._bcast.done.connect(
            lambda: self.log_panel.append("[OK] broadcast finished."))
        self._bcast.start()

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
        if not host:
            self.log_panel.append("[ERROR] pairing address must be host:port")
            return
        from ..core import ADBHandler
        from ..config import ADBConfig

        if thread_running(getattr(self, "_pair", None)):
            self.log_panel.append("[INFO] Pairing is already in progress.")
            return
        adb_path = gui_adb_path()
        pair_code = code.strip()

        def work():
            # `adb pair` can block for its full 30 s timeout on a wrong address.
            return ADBHandler(ADBConfig(adb_path=adb_path)).pair(
                host, int(port), pair_code, safe=True
            )

        self.log_panel.append(f"[INFO] Pairing with {host}:{port}…")
        self._pair = FunctionThread(work)
        self._pair.done.connect(self._on_paired)
        self._pair.fail.connect(lambda msg: self.log_panel.append(f"[ERROR] pair: {msg}"))
        self._pair.start()

    def _on_paired(self, res):
        if isinstance(res, OperationResult) and not res.success:
            self.log_panel.append(f"[ERROR] pair: {res.error}")
        else:
            val = res.value if isinstance(res, OperationResult) else res
            self.log_panel.append(f"[OK] {val}")
        self._poll_devices()

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
            f"It also opens the firewall ports (5037 + {TUNNEL_PORT_FIREWALL_RANGE}). "
            "Run it automatically "
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
        # pause the device poll: its `adb devices` would auto-start a plain
        # localhost server in the kill→bind gap and steal port 5037
        self._timer.stop()
        self._share = _ShareThread(
            install_startup=(clicked is b_start_login),
            adb_path=gui_adb_path(),
        )
        self._share.msg.connect(self.log_panel.append)
        self._share.finished.connect(
            lambda: (self._start_poll_timer(), self._poll_devices()))
        self._share.start()

    def stop_sharing(self):
        """Stop sharing this PC's devices and remove BOTH auto-start vectors (the
        login launcher and the SYSTEM startup task), so nothing runs on its own at
        boot/login anymore. Addresses 'turboadb keeps auto-starting locally'."""
        if getattr(self, "_unshare", None) and self._unshare.isRunning():
            self.log_panel.append("[WARNING] Stop-sharing is already running…")
            return
        if QMessageBox.question(
                self, "Stop sharing & remove auto-start",
                "Stop sharing this PC's devices and remove the auto-start "
                "(login launcher + SYSTEM startup task)?\n\n"
                "The adb server returns to local-only. Removing the SYSTEM task "
                "may need Administrator.",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.log_panel.append("Stopping device sharing and removing auto-start…")
        self._timer.stop()          # keep the poll out of the kill→restart gap
        self._unshare = _StopShareThread(adb_path=gui_adb_path())
        self._unshare.msg.connect(self.log_panel.append)
        self._unshare.finished.connect(
            lambda: (self._start_poll_timer(), self._poll_devices()))
        self._unshare.start()

    def deploy_serve_remote(self):
        """Install/start `turboadb serve` on remote Windows hosts FROM here, over
        WinRM — so you don't have to RDP into each one to enable device sharing."""
        if getattr(self, "_deploy", None) and self._deploy.isRunning():
            self.log_panel.append("[WARNING] A remote deploy is already running.")
            return
        from .deploy_dialog import DeployDialog
        dlg = DeployDialog(self)
        try:
            vals = dlg.values() if dlg.exec_() == dlg.Accepted else None
        finally:
            dlg.deleteLater()
        if vals is None:
            return
        if not vals["hosts"]:
            QMessageBox.warning(self, "Deploy", "Enter at least one host.")
            return
        if not vals["user"] or not vals["password"]:
            QMessageBox.warning(self, "Deploy",
                                "Enter the admin user and password.")
            return
        for h in reversed(vals["hosts"]):
            settings_mod.add_recent("recent_remote_hosts", h)
        n = len(vals["hosts"])
        self.log_panel.append(f"Deploying ‘serve’ to {n} host(s) over WinRM…")
        # a MODAL progress popup while the deploy runs — it takes a while
        # (pip update + server start per host) and used to happen invisibly in
        # the background, which made it look like the click did nothing
        self._dep_dlg = QProgressDialog(
            f"Deploying ‘serve’ to {n} host(s) over WinRM…\n"
            f"(updating turboadb + starting the shared adb server on each — "
            f"this can take a couple of minutes)", None, 0, 0, self)
        self._dep_dlg.setWindowTitle("TurboADB — remote deploy")
        self._dep_dlg.setWindowModality(Qt.WindowModal)
        self._dep_dlg.setMinimumDuration(0)
        self._dep_dlg.setMinimumWidth(460)
        self._dep_dlg.setAutoClose(False); self._dep_dlg.setAutoReset(False)
        self._dep_dlg.show()
        self._dep_results = []
        self._deploy = _DeployThread(vals["hosts"], vals["user"],
                                     vals["password"], vals["port"],
                                     vals["update"], vals.get("use_ssl", False))
        self._deploy.status.connect(self._on_deploy_status)
        self._deploy.finished.connect(self._on_deploy_finished)
        self._deploy.start()

    def _on_deploy_status(self, msg):
        self.log_panel.append(msg)
        if msg.startswith(("[OK]", "[ERROR]")):
            self._dep_results.append(msg)
        try:                                     # live line in the popup
            self._dep_dlg.setLabelText(
                re.sub(r"^\[(OK|ERROR|WARNING|INFO)\]\s*", "", msg)[:200])
        except Exception:
            pass

    def _on_deploy_finished(self):
        try:
            self._dep_dlg.close()
        except Exception:
            pass
        results = [r for r in getattr(self, "_dep_results", [])
                   if r != "[OK] Remote deploy finished."]
        ok = [r for r in results if r.startswith("[OK]")]
        bad = [r for r in results if r.startswith("[ERROR]")]
        lines = "\n".join("• " + re.sub(r"^\[(OK|ERROR)\]\s*", "", r)[:160]
                          for r in results) or "(no per-host output)"
        if bad:
            QMessageBox.warning(
                self, "Remote deploy finished (with errors)",
                f"{len(ok)} host(s) OK, {len(bad)} failed:\n\n{lines}")
        else:
            QMessageBox.information(
                self, "Remote deploy finished",
                f"All {len(ok)} host(s) deployed:\n\n{lines}\n\n"
                f"Connect to them via Connect → Remote.")
        self._poll_devices()

    def restart_adb_server(self):
        if thread_running(getattr(self, "_as", None)):
            self.log_panel.append("[INFO] ADB restart is already in progress.")
            return
        self.log_panel.append("Restarting ADB server… (fixes 'device not "
                              "visible' from adb version mismatches)")
        self._set_adb_indicator("restarting…")
        # Killing the shared daemon necessarily closes every adb shell.  Pause
        # their individual reconnect loops first; otherwise they hammer the old
        # serial while Windows is re-enumerating USB and fill the terminal with
        # misleading "device not found" errors.
        for tab in self._device_tabs():
            try:
                tab.prepare_for_adb_restart()
            except Exception:
                pass
        # Keep the fallback poll out of the kill→start gap; _poll_devices also
        # falls back to a socket-only probe while this worker runs.
        self._poll_timer_was_active = self._timer.isActive()
        self._timer.stop()
        # No Qt parent: a parented thread is destroyed with the window even when
        # closeEvent parks it, which aborts Qt if the restart is still running.
        self._as = _adb_restart_worker(gui_adb_path())
        self._as.done.connect(self._on_adb_restarted)
        self._as.start()

    def _on_adb_restarted(self, message: str) -> None:
        if getattr(self, "_poll_timer_was_active", False):
            self._start_poll_timer()
        self.log_panel.append(message)
        success = message.startswith("[OK]")
        self._set_adb_indicator("ready" if success else "restart failed")
        if success:
            self._poll_devices()
            self._start_device_tracker()
        for tab in self._device_tabs():
            try:
                tab.finish_adb_restart(success)
            except Exception:
                pass

    # ---- tool download (adb / scrcpy, auto on install/upgrade) ----
    def _check_tools(self):
        """On startup, auto-fetch the latest adb/scrcpy when enabled (default),
        or fall back to a one-off prompt if auto-fetch is disabled."""
        try:
            from ..tools import adb_available, scrcpy_available
            from ..toolsdl import auto_fetch_enabled, _pkg_version, _write_stamp
        except Exception:
            return
        if auto_fetch_enabled():
            # Only fetch when adb or scrcpy is actually MISSING
            need = not adb_available() or not scrcpy_available()
            if need:
                self._run_tools("ensure",
                                "Downloading platform-tools + scrcpy (one-time)…")
            else:
                try:
                    _write_stamp(_pkg_version())  # mark current; skip re-checking
                except Exception:
                    pass
            return
        if not adb_available():
            msg = ("adb (Android platform-tools) wasn't found.\n\n"
                   "Download platform-tools + scrcpy now into ~/.turboadb/tools?")
            if QMessageBox.question(self, "Download tools", msg) == QMessageBox.Yes:
                self._run_tools("fetch", "Downloading adb + scrcpy…")

    def upgrade_tools_gui(self):
        """Ribbon ‘Upgrade’: the ONE place updates happen. First check TurboADB
        itself on PyPI; if newer, self-update (which also refreshes adb + scrcpy)
        and restart. Otherwise just check/refresh adb + scrcpy."""
        if thread_running(getattr(self, "_upd_run", None)):
            self.log_panel.append("[WARNING] An update is already running.")
            return
        if thread_running(getattr(self, "_dl", None)) or thread_running(
            getattr(self, "_upd_chk", None)
        ):
            self.log_panel.append("[WARNING] A tool task is already running.")
            return
        from .. import update as _upd
        if not _upd.can_self_update():
            # standalone executable: can't pip-upgrade itself, so just do adb/scrcpy
            self.log_panel.append("Checking adb/scrcpy for updates…")
            self._upgrade_tools_only()
            return
        self.log_panel.append("Checking PyPI for a newer TurboADB…")
        self._upd_chk = _update_check_worker()
        self._upd_chk.done.connect(self._upgrade_after_appcheck)
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

    def _start_tools_task(self, mode, text, title, on_done, *, force=False,
                          show_stage=False) -> bool:
        """Run one adb/scrcpy download or upgrade with a progress dialog."""
        if thread_running(getattr(self, "_dl", None)):
            self.log_panel.append("[WARNING] A tool task is already running.")
            return False
        self._dlg = QProgressDialog(text, None, 0, 100, self)
        self._dlg.setWindowTitle(title)
        self._dlg.setAutoClose(True)
        self._dlg.setMinimumDuration(0)
        self._dlg.setValue(0)
        # Pause the fallback device poll: its `adb devices` can restart the adb
        # server and re-lock adb.exe in the middle of the replace.
        self._timer.stop()
        self._dl = _ToolsDownloadThread(mode=mode, force=force)
        self._dl.progress.connect(self._dlg.setValue)
        if show_stage:
            self._dl.stage.connect(
                lambda s: self._dlg.setLabelText(f"Downloading {s}…\n"
                                                 "Cached in ~/.turboadb/tools."))
        self._dl.done.connect(on_done)
        self._dl.start()
        return True

    def _upgrade_tools_only(self):
        if self._start_tools_task(
            "upgrade",
            "Checking for newer adb / scrcpy…\nDownloads only if an update is available.",
            "TurboADB — upgrade",
            self._upgrade_done,
        ):
            self.log_panel.append("Checking for adb/scrcpy updates…")

    def _upgrade_done(self, res):
        try:
            self._dlg.close()
        except Exception:
            pass
        self._start_poll_timer()              # resume the fallback device poll
        checks = res.get("checks") or {}
        for tool in ("adb", "scrcpy"):
            c = checks.get(tool) or {}
            if c:
                self.log_panel.append(
                    f"[OK] {tool}: installed {c.get('installed')} · "
                    f"latest {c.get('latest')}")
        unknown = res.get("unknown") or []
        if res.get("up_to_date"):
            self.log_panel.append("[OK] adb & scrcpy match latest upstream versions.")
            versions = " · ".join(
                f"{label} {(checks.get(tool) or {}).get('latest')}"
                for tool, label in (("adb", "ADB"), ("scrcpy", "Scrcpy"))
                if (checks.get(tool) or {}).get("latest")
            )
            ans = QMessageBox.question(
                self, "ADB & Scrcpy Up To Date",
                "ADB and Scrcpy match the latest upstream versions"
                + (f" ({versions})" if versions else "") + ".\n\n"
                "Would you like to force a clean re-download and re-install from Google & GitHub now?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if ans == QMessageBox.Yes:
                self._run_tools("fetch", "Force re-downloading ADB and Scrcpy from Google & GitHub…", force=True)
            return
        elif unknown and not res.get("updated") and not res.get("errors"):
            # a FAILED check used to be reported as "already up to date"
            msg = ("Couldn't check " + " / ".join(unknown) + " for updates "
                   "(no network, or the version source is rate-limiting). "
                   "Nothing was changed — try again in a while.")
            self.log_panel.append(f"[WARNING] {msg}")
            QMessageBox.warning(self, "Update check failed", msg)
        for tool, path in (res.get("updated") or {}).items():
            self.log_panel.append(f"[OK] updated {tool} → {path}")
        for tool, err in (res.get("errors") or {}).items():
            self.log_panel.append(f"[WARNING] {tool}: {err}")
        if res.get("updated"):
            QMessageBox.information(self, "Updated",
                                    "Updated: " + ", ".join(res["updated"].keys()))
            # a custom adb path overrides the managed download — say so
            custom = (os.environ.get("TURBOADB_ADB")
                      or (settings_mod.get("adb_path") or "").strip())
            if "adb" in res["updated"] and custom:
                self.log_panel.append(
                    f"[WARNING] a custom adb path is set ({custom}) and takes "
                    f"precedence over the freshly downloaded adb — clear it in "
                    f"Settings → Tools (or unset TURBOADB_ADB) to use the update.")
        self._begin_adb_startup()

    def _run_tools(self, mode, label, force=False):
        if self._start_tools_task(
            mode,
            label + "\nCached in ~/.turboadb/tools.",
            "TurboADB — tools",
            self._tools_done,
            force=force,
            show_stage=True,
        ):
            self.log_panel.append(label)

    def _tools_done(self, res):
        try:
            self._dlg.close()
        except Exception:
            pass
        self._start_poll_timer()             # resume the fallback device poll
        if res.get("note") == "already-ensured":
            self.log_panel.append(
                "[INFO] tools were already checked earlier in this session — "
                "use the 🔄 Upgrade button to force a fresh check.")
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
        self._begin_adb_startup()

    def _quiet_update_check(self):
        self._upd_quiet = _update_check_worker(cached=True)  # daily cache
        self._upd_quiet.done.connect(self._on_quiet_update)
        self._upd_quiet.start()

    def _on_quiet_update(self, latest):
        if latest:
            self.log_panel.append(
                f"[INFO] TurboADB {latest} is available (you have "
                f"{self._version}) — click the 🔄 Upgrade button to update.")

    def _ensure_shortcuts(self):
        if not settings_mod.get("make_shortcut_first_run"):
            return           # the user opted out of shortcut self-healing
        if thread_running(getattr(self, "_sc", None)):
            return
        self._sc = _shortcut_worker()
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
        for tool, err in (res.get("tools_errors") or {}).items():
            self.log_panel.append(f"[WARNING] {tool} refresh failed: {err}")
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

    def show_settings(self):
        dlg = SettingsDialog(self)
        # On OK the dialog itself persists only the keys that changed (merged
        # into the current file) and deletes itself afterwards.
        if dlg.exec_() != dlg.Accepted:
            return
        changes = dlg.changed_settings()
        name = settings_mod.get("theme")
        self._apply_theme(name, persist=False, announce=False)
        self.log_panel.append(f"[OK] Settings saved — theme: {theme.theme_label(name)}")
        if "adb_path" in changes:
            self._offer_adb_restart_for_new_binary()

    def _offer_adb_restart_for_new_binary(self):
        """A new adb path only takes over once that binary owns the daemon;
        until then two adb versions take turns on port 5037."""
        answer = QMessageBox.question(
            self, "Restart ADB server",
            "The adb executable changed.\n\n"
            "Restart the ADB server now so it runs the new binary? Open device "
            "shells reconnect automatically; reopen device tabs to use the new "
            "adb for their own commands too.\n\n"
            "(Two different adb versions sharing port 5037 make devices disconnect.)",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            self.restart_adb_server()
        else:
            self.log_panel.append(
                "[WARNING] adb path changed but the running ADB server still uses the "
                "previous binary — use Restart ADB server to switch.")

    def _apply_theme(self, name: str, *, persist: bool = True, announce: bool = True) -> None:
        """Apply any theme from ``theme.THEMES`` from any entry point (menu,
        toolbar, or Settings) and keep every chooser in sync."""
        if name not in theme.THEMES:
            name = "dark"
        if persist:
            settings_mod.set("theme", name)
        theme.apply_to_app(QApplication.instance(), name)
        # Terminal widgets have a small inline stylesheet for monospace
        # rendering; refresh it explicitly after a switch.
        from .console import AnsiConsole
        from .mirror_panel import MirrorPanel
        for console in self.findChildren(AnsiConsole):
            console.refresh_theme()
        # The screen options popover paints its own surface and headings.
        for mirror in self.findChildren(MirrorPanel):
            mirror.refresh_theme()
        self._sync_theme_action()
        self._sync_theme_menu()
        self.refresh_sessions()
        if announce:
            self.log_panel.append(f"[OK] Theme: {theme.theme_label(name)}")

    def _sync_theme_action(self):
        """Point the ribbon toggle at the theme you'll switch TO — the other half
        of the current dark/light pair (moon = go dark, sun = go light)."""
        if not hasattr(self, "act_theme"):
            return
        target = theme.counterpart(settings_mod.get("theme"))
        self.act_theme.setIcon(
            icons.icon("sun", "amber") if theme.is_light(target) else icons.icon("moon", "blue")
        )
        self.act_theme.setText(theme.theme_label(target))
        self.act_theme.setToolTip(f"Switch to the {theme.theme_label(target)} theme")
        self._sync_theme_menu()

    def _sync_theme_menu(self):
        if not hasattr(self, "_theme_menu_actions"):
            return
        current = settings_mod.get("theme")
        for key, action in self._theme_menu_actions.items():
            action.setChecked(key == current)

    def toggle_theme(self):
        """Switch to the other half of the current dark/light pair, live."""
        self._apply_theme(theme.counterpart(settings_mod.get("theme")))

    def _close_tab(self, index):
        w = self.tabs.widget(index)
        try:
            w.close_session()
        except Exception:
            pass
        self.tabs.removeTab(index)
        if w is not None and w is getattr(self, "_webcam_tab", None):
            # Forget the deleted panel so View -> Open webcam can reopen it.
            self._webcam_tab = None
        if w is not None:
            w.deleteLater()
        self._update_center()
        self._update_status()

    def _close_current_tab(self):
        i = self.tabs.currentIndex()
        if i >= 0:
            self._close_tab(i)

    def _tab_context_menu(self, pos):
        bar = self.tabs.tabBar()
        idx = bar.tabAt(pos)
        if idx < 0:
            return
        n = self.tabs.count()
        menu = QMenu(self)
        act_close = menu.addAction(icons.icon("x"), "Close tab")
        act_others = menu.addAction("Close other tabs")
        act_left = menu.addAction("Close tabs to the left")
        act_right = menu.addAction("Close tabs to the right")
        menu.addSeparator()
        act_all = menu.addAction(icons.icon("x", "danger"), "Close all tabs")
        act_others.setEnabled(n > 1)
        act_left.setEnabled(idx > 0)
        act_right.setEnabled(idx < n - 1)
        chosen = menu.exec_(bar.mapToGlobal(pos))
        if chosen is None:
            return
        # close by INDEX from the right so earlier indices stay valid
        if chosen is act_close:
            targets = [idx]
        elif chosen is act_others:
            targets = [i for i in range(n) if i != idx]
        elif chosen is act_left:
            targets = list(range(0, idx))
        elif chosen is act_right:
            targets = list(range(idx + 1, n))
        elif chosen is act_all:
            targets = list(range(n))
        else:
            return
        for i in sorted(targets, reverse=True):
            self._close_tab(i)

    # ---- device list visibility ----
    def _set_sidebar_visible(self, visible: bool) -> None:
        dock = getattr(self, "_sidebar_dock", None)
        if dock is not None:
            dock.setVisible(visible)
        handle = getattr(self, "_sidebar_handle", None)
        if handle is not None:
            handle.setVisible(not visible)

    def _toggle_devices(self, show=None) -> None:
        """Show, hide or (``None``) flip the device list; the user's own choice
        wins over the automatic hide."""
        dock = getattr(self, "_sidebar_dock", None)
        if show is None:
            # isHidden, not isVisible: the list's own state, even while the
            # window itself is minimised or not shown yet.
            show = dock is None or dock.isHidden()
        self._sidebar_auto_hidden = False
        self._set_sidebar_visible(bool(show))

    def _on_screen_active(self, active: bool) -> None:
        """Give a showing device screen the room; restore the list afterwards."""
        dock = getattr(self, "_sidebar_dock", None)
        if dock is None:
            return
        if active:
            if dock.isVisible():
                self._sidebar_auto_hidden = True
                self._set_sidebar_visible(False)
        elif getattr(self, "_sidebar_auto_hidden", False):
            self._sidebar_auto_hidden = False
            self._set_sidebar_visible(True)

    def _open_docs(self):
        import webbrowser
        webbrowser.open("https://nvnkennedy.github.io/turboadb/")

    def closeEvent(self, event):
        try:
            self._timer.stop()
        except Exception:
            pass
        if getattr(self, "_tracker", None):
            try:
                self._tracker.stop()
            except Exception:
                pass
        # park any still-running worker threads — Qt crashes if a QThread object
        # is destroyed (with this window) while its thread is alive
        for attr in ("_poll", "_tracker", "_adb_init", "_as", "_dl", "_share", "_unshare", "_deploy",
                     "_sc", "_upd_chk", "_upd_run", "_upd_quiet", "_disc",
                     "_bcast", "_pair"):
            park_thread(getattr(self, attr, None))
        for i in range(self.tabs.count()):
            try:
                self.tabs.widget(i).close_session()
            except Exception:
                pass
        super().closeEvent(event)


class _EdgeHandle(QWidget):
    """The slim left-edge bar with a › arrow at its top that reopens the
    hidden device list; a click anywhere on the bar works."""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebarHandle")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(26)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Show the device list (Ctrl+B)")
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 8, 0, 8)
        column.setSpacing(0)
        arrow = QToolButton()
        arrow.setObjectName("sidebarHandleArrow")
        arrow.setIcon(icons.icon("chevron-right", "accent", width=2.6))
        arrow.setIconSize(QSize(16, 16))
        arrow.setFixedSize(22, 34)
        arrow.setToolTip(self.toolTip())
        arrow.clicked.connect(self.clicked)
        column.addWidget(arrow, 0, Qt.AlignHCenter | Qt.AlignTop)
        column.addStretch(1)
        self.arrow = arrow

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def _section(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    # Theme styling belongs in the application stylesheet, not inline: an
    # inline colour was frozen at construction time and survived a theme switch.
    lbl.setObjectName("sidebarSection")
    return lbl
