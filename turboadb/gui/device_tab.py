"""A per-device tab: connects in the background, then exposes an interactive
Shell, a live Logcat viewer, a file browser, and an app manager — plus quick
Mirror (scrcpy), Screenshot, and Reboot actions in a header bar."""

from __future__ import annotations

import os
import shlex

from PyQt5.QtCore import QThread, pyqtSignal, Qt, QEvent, QSize, QTimer
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QFileDialog, QMenu, QToolButton,
    QMessageBox, QSplitter, QStackedWidget, QDialog,
    QDialogButtonBox, QComboBox, QFormLayout, QFrame, QSizePolicy
)

from ..config import ADBConfig
from ..core import ADBHandler
from ..results import OperationResult
from . import settings as settings_mod
from . import theme
from .terminal import ReaderThread
from .console import AnsiConsole
from .logcat_view import LogcatPanel
from .file_browser import FileBrowser
from .apps_panel import AppsPanel
from .controls_panel import ControlsPanel
from .mirror_panel import MirrorPanel
from .camera_widget import CameraPanel
from .phone_panel import PhonePanel
from .adb_path import gui_adb_path
from . import icons
from .qtutil import AnimatedTabWidget, FunctionThread, close_jobs, run_job
from .sessions import normalize_session


import re
import unicodedata

from ..results import strip_ansi


# The only pages that can take part in a session workspace.  They remain the
# same persistent widgets used by the normal tab view; split mode never starts
# a second shell, logcat stream, file browser, or mirror.
_SPLIT_VIEW_META = {
    "shell": ("⌨", "Terminal"),
    "controls": ("🎛", "Device Control"),
    "files": ("📁", "Files"),
    "logcat": ("📜", "Logcat"),
}

# Section tab label -> (icon name, colour tone). Each section keeps one colour
# everywhere it appears.
_SECTION_ICONS = {
    "Terminal": ("terminal", "teal"),
    "Logcat": ("logcat", "amber"),
    "Files": ("folder", "blue"),
    "Device Control": ("screen-control", "purple"),
    "Apps": ("apps", "orange"),
    "Webcam": ("video", "red"),
    "Phone": ("phone", "green"),
    "IVI Displays": ("car", "pink"),
}


def _char_width(ch: str) -> int:
    if ch in ("✔", "✓", "⚡", "💻", "📱", "📦", "🛑", "🔄", "➤", "▸", "►", "📁", "📅", "🕒"):
        return 2
    w = unicodedata.east_asian_width(ch)
    return 2 if w in ("W", "F") else 1


def _str_width(s: str) -> int:
    """Calculate monospaced terminal display column width of a string.

    Treats East Asian Wide ('W') and Fullwidth ('F') characters as 2 columns,
    ignoring ANSI escape sequences.
    """
    clean = strip_ansi(s)
    return sum(_char_width(c) for c in clean)


def _render_mobaxterm_banner(
    header_title: str,
    header_sub: str,
    session_title: str,
    items: list[tuple[str, str, bool]],
    min_width: int = 61,
    cwd: str = "/",
    include_prompt_bar: bool = False,
) -> str:
    """Render the original framed terminal welcome banner.

    ``include_prompt_bar`` is off for normal sessions: the real PowerShell/CMD
    prompt, or the concise synthetic ADB prompt, already shows the path without
    duplicating it in a large date-and-time bar.
    """
    max_lbl_len = max(len(lbl) for lbl, _, _ in items) if items else 0
    formatted_items = []
    for lbl, val, has_check in items:
        pad_lbl = lbl.ljust(max_lbl_len)
        chk = "\x1b[1;92m✔\x1b[0m" if has_check else " "
        val_str = f" \x1b[37m{val}\x1b[0m" if val else ""
        formatted_items.append(f"\x1b[37m{pad_lbl} :  {chk}{val_str}")

    all_content = [header_title, header_sub, f" ➤ {session_title}"] + [f"   {it}" for it in formatted_items]
    max_c = max(_str_width(x) for x in all_content)
    width = max(min_width, max_c + 4)
    inner_w = width - 2

    top = "\x1b[37m┌" + ("─" * inner_w) + "┐\x1b[0m"
    bot = "\x1b[37m└" + ("─" * inner_w) + "┘\x1b[0m"
    blank = "\x1b[37m│" + (" " * inner_w) + "│\x1b[0m"

    def center(s):
        sw = _str_width(s)
        pad = max(0, inner_w - sw)
        left = pad // 2
        right = pad - left
        return "\x1b[37m│\x1b[0m" + (" " * left) + s + (" " * right) + "\x1b[37m│\x1b[0m"

    def left_row(s, indent=1):
        sw = _str_width(s)
        rem = max(0, inner_w - indent - sw)
        return "\x1b[37m│\x1b[0m" + (" " * indent) + s + (" " * rem) + "\x1b[37m│\x1b[0m"

    lines = [top, center(header_title), center(header_sub), blank]
    lines.append(left_row(f"➤ {session_title}", indent=1))
    lines.extend(left_row(it, indent=3) for it in formatted_items)
    lines.append(bot)
    banner = "\n" + "\n".join(lines) + "\n\n"
    if not include_prompt_bar:
        return banner

    import datetime
    now = datetime.datetime.now()
    d_s = now.strftime("%m-%d")
    t_s = now.strftime("%H:%M")
    return banner + (
        f"\x1b[30;46m 📅 {d_s} "
        f"\x1b[36;42m▶"
        f"\x1b[30;42m 🕒 {t_s} "
        f"\x1b[32;43m▶"
        f"\x1b[30;43m 📁 {cwd or '/'} "
        f"\x1b[33;49m▶\x1b[0m "
    )


def _boxed_banner(lines) -> str:
    """Short welcome lines inside a thin frame, set apart from terminal output.

    Every terminal (Android, PowerShell, CMD) opens with this two-line box.
    """
    width = max((_str_width(line) for line in lines), default=0)
    edge = "\x1b[90m"
    reset = "\x1b[0m"
    rows = [
        f"{edge}│{reset} {line}{' ' * (width - _str_width(line))} {edge}│{reset}"
        for line in lines
    ]
    top = f"{edge}┌{'─' * (width + 2)}┐{reset}"
    bottom = f"{edge}└{'─' * (width + 2)}┘{reset}"
    return "\n" + "\n".join([top, *rows, bottom]) + "\n\n"


def _render_box_banner(title: str, lines: list[str], min_width: int = 74) -> str:
    """Render a clean, fully enclosed ASCII box banner with ANSI styling."""
    all_content = [title] + [f"  {l}" for l in lines]
    max_c = max(_str_width(x) for x in all_content)
    width = max(min_width, max_c + 4)
    inner_w = width - 2

    top = "\x1b[90m┌" + ("─" * inner_w) + "┐\x1b[0m"
    bot = "\x1b[90m└" + ("─" * inner_w) + "┘\x1b[0m"
    blank = "\x1b[90m│" + (" " * inner_w) + "│\x1b[0m"

    def center(s):
        sw = _str_width(s)
        pad = max(0, inner_w - sw)
        l = pad // 2
        r = pad - l
        return "\x1b[90m│\x1b[0m" + (" " * l) + s + (" " * r) + "\x1b[90m│\x1b[0m"

    def left_row(s, indent=2):
        sw = _str_width(s)
        rem = max(0, inner_w - indent - sw)
        return "\x1b[90m│\x1b[0m" + (" " * indent) + s + (" " * rem) + "\x1b[90m│\x1b[0m"

    body = [center(title), blank] + [left_row(l, indent=2) for l in lines]
    return "\n" + "\n".join([top] + body + [bot]) + "\n"


def _session_banner(header_sub: str, session_title: str, items, cwd: str = "/") -> str:
    """The TurboADB welcome banner shared by the Android and local terminals."""
    from .. import __version__

    header_title = f"\x1b[1;92m•  TurboADB Professional v{__version__}  •\x1b[0m"
    return _render_mobaxterm_banner(header_title, header_sub, session_title, items, cwd=cwd)


# PowerShell and Command Prompt advertise the same local capabilities.
_LOCAL_BANNER_ITEMS = [
    ("Platform-tools", "", True),
    ("Local-terminal", "(ANSI cooked mode is enabled)", True),
]


def config_from_session(s: dict) -> ADBConfig:
    s = normalize_session(s)
    adb_path = gui_adb_path()
    st = settings_mod.load()
    scrcpy_path = st.get("scrcpy_path") or None
    if s.get("type") == "network":
        return ADBConfig(
            host=s.get("host", ""),
            port=int(s.get("port", 5555)),
            adb_path=adb_path,
            scrcpy_path=scrcpy_path,
        )
    if s.get("type") == "remote":
        return ADBConfig(
            serial=s.get("serial") or None,
            adb_server_host=s.get("adb_host", ""),
            adb_server_port=int(s.get("adb_port", 5037)),
            adb_path=adb_path,
            scrcpy_path=scrcpy_path,
        )
    return ADBConfig(
        serial=s.get("serial") or None,
        adb_path=adb_path,
        scrcpy_path=scrcpy_path,
    )


class _ConnectThread(QThread):
    ok = pyqtSignal(object, object)
    fail = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, cfg, *, fetch_identity: bool = True):
        super().__init__()
        self.cfg = cfg
        self.fetch_identity = bool(fetch_identity)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            if self._cancelled:
                return
            h = ADBHandler(
                self.cfg,
                safe=True,
                log_callback=lambda m: None if self._cancelled else self.log.emit(m),
            )
            res = h.connect()
            if self._cancelled:
                try:
                    h.disconnect()
                except Exception:
                    pass
                return
            if isinstance(res, OperationResult) and not res.success:
                self.fail.emit(str(res.error))
                return
            quick = None
            if self.fetch_identity:
                # Fields for a friendly first banner. A terminal-only duplicate
                # needs no device profile, so it skips this round-trip. The
                # probe is quiet: it used to run with a 0.45 s limit and log an
                # ERROR, so a phone that was a little slow right after connecting
                # raised a red error popup and a banner without device details.
                try:
                    ident = h.quick_identity(timeout=1.5)
                except Exception:
                    ident = {}
                if ident:
                    quick = dict(ident, _quick_identity=True)
            self.ok.emit(h, quick)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _DeviceInfoThread(QThread):
    """Fetch nonessential build identity after the device tab is usable."""

    done = pyqtSignal(object)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler

    def run(self):
        # Raw mode inside our own try: the details are optional, so a failure
        # is reported to the tab (as an [INFO] line) instead of the engine's
        # ERROR log, which the GUI turns into a red error popup.
        try:
            info = self.handler.device_info(safe=False)
            self.done.emit(OperationResult(True, "device_info", value=info))
        except Exception as exc:
            self.done.emit(OperationResult(False, "device_info", error=exc))


_ActionThread = FunctionThread


class _CompletionThread(QThread):
    """Run an expensive Android completion query without blocking a key press."""

    ready = pyqtSignal(str, object, object)

    def __init__(self, line, fn):
        super().__init__()
        self.line = line
        self.fn = fn

    def run(self):
        try:
            newline, options = self.fn(self.line)
        except Exception:
            newline, options = None, []
        self.ready.emit(self.line, newline, options)


class _ReconnectThread(QThread):
    """Wait for a device to become fully shell-ready after a transport reset."""
    done = pyqtSignal(bool)

    def __init__(self, handler, timeout=180, *, initial_delay=3.0, poll_interval=2.0):
        import threading

        super().__init__()
        self.handler = handler
        self.timeout = timeout
        self.initial_delay = initial_delay
        self.poll_interval = poll_interval
        self._cancelled = threading.Event()

    def cancel(self):
        """Stop waiting; a closed tab must not keep re-connecting its device."""
        self._cancelled.set()

    def run(self):
        import time
        from ..results import OperationResult

        # A reboot genuinely needs time to disappear.  An ADB-server restart
        # does not: it only needs the new server to re-enumerate USB, which is
        # normally available within a few hundred milliseconds.
        if self.initial_delay and self._cancelled.wait(self.initial_delay):
            return
        deadline = time.time() + self.timeout
        host = getattr(getattr(self.handler, "config", None), "host", None)
        port = getattr(getattr(self.handler, "config", None), "port", 5555)

        while time.time() < deadline and not self._cancelled.is_set():
            try:
                if host:
                    try:
                        self.handler.connect_tcp(host, port, safe=True)
                    except Exception:
                        pass

                if self.handler.get_state() == "device":
                    res = self.handler.shell("echo 1", timeout=3, safe=True)
                    if isinstance(res, OperationResult) and res.success and res.value.ok:
                        time.sleep(0.5)
                        self.done.emit(True)
                        return
            except Exception:
                pass
            if self._cancelled.wait(self.poll_interval):
                return
        if not self._cancelled.is_set():
            self.done.emit(False)


class _PromptThread(QThread):
    """Fetch the shell's root state without blocking the UI.

    The prompt always shows the session's stable device name, so no device
    name query is made here.
    """
    ready = pyqtSignal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler

    def run(self):
        root = False
        try:
            res = self.handler.shell("id -u", timeout=3.0, safe=True)
            value = res.value if isinstance(res, OperationResult) else res
            if not isinstance(res, OperationResult) or res.success:
                root = bool(value is not None and value.ok and value.text.strip() == "0")
        except Exception:
            pass
        self.ready.emit(root)


class _TerminalWidgetBase(QWidget):
    """Toolbar, font zoom, async Android completion and ADB restart shared by
    the Android shell and the local PowerShell / Command Prompt terminals."""

    log = pyqtSignal(str)

    _BIN_DIRS = (
        "/system/bin",
        "/system/xbin",
        "/vendor/bin",
        "/apex/com.android.runtime/bin",
        "/apex/com.android.art/bin",
    )

    def __init__(self, handler=None, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.session = None
        self.reader = None
        self._closing = False
        self._completion_thread = None

    def _build_toolbar(self, layout, *, restart_tip, restart_slot, info_text):
        """Add the action row. Call :meth:`_attach_terminal` once ``term`` exists."""
        from .flowlayout import ToolbarFlowLayout

        toolbar = QWidget()
        toolbar.setObjectName("pageToolbar")
        # Wraps instead of widening the window when the tab is narrow.
        row = ToolbarFlowLayout(toolbar, hspacing=4, vspacing=4)
        row.setContentsMargins(10, 6, 10, 6)
        stop = QPushButton("Stop")
        stop.setProperty("role", "danger")
        stop.setToolTip("Stop the active command and open a fresh shell (Ctrl+C)")
        stop.clicked.connect(self.interrupt)

        clr = QPushButton("Clear")
        clr.setProperty("role", "ghost")

        paste = QPushButton("Paste")
        paste.setProperty("role", "ghost")
        paste.clicked.connect(lambda: self.term.paste_clipboard())

        copy = QPushButton("Copy")
        copy.setProperty("role", "ghost")
        copy.clicked.connect(lambda: self.term.copy())

        save = QPushButton("Save…")
        save.setProperty("role", "ghost")
        save.clicked.connect(lambda: self.term._save_output())

        latest = QPushButton("Follow output")
        latest.setProperty("role", "ghost")
        latest.setObjectName("latestFollowButton")
        latest.setCheckable(True)
        latest.setToolTip("Keep this terminal pinned to its latest output (click again to stop)")
        self.btn_latest = latest

        font_down = QPushButton("A−")
        font_down.setProperty("role", "ghost")
        font_down.setObjectName("terminalFontButton")
        font_down.setToolTip("Decrease terminal text size (Ctrl+− or Ctrl + mouse wheel)")
        font_down.clicked.connect(lambda: self._change_font_size(-1))
        self.btn_font_down = font_down

        font_up = QPushButton("A+")
        font_up.setProperty("role", "ghost")
        font_up.setObjectName("terminalFontButton")
        font_up.setToolTip("Increase terminal text size (Ctrl++ or Ctrl + mouse wheel)")
        font_up.clicked.connect(lambda: self._change_font_size(1))
        self.btn_font_up = font_up

        font_size = QLabel()
        font_size.setObjectName("terminalFontSize")
        font_size.setToolTip("Current terminal font size. Ctrl + mouse wheel also changes it.")
        self.lbl_font_size = font_size

        restart_shell = QPushButton("Restart shell")
        restart_shell.setProperty("role", "ghost")
        restart_shell.setToolTip(restart_tip)
        restart_shell.clicked.connect(restart_slot)

        restart_adb = QPushButton("Restart ADB")
        restart_adb.setProperty("role", "ghost")
        restart_adb.setToolTip("Restart the shared ADB server and refresh devices")
        restart_adb.clicked.connect(self.restart_adb)

        def separator():
            line = QFrame()
            line.setObjectName("barSeparator")
            line.setFrameShape(QFrame.VLine)
            line.setFixedSize(1, 22)
            return line

        # Groups: command control | clipboard and output | view | recovery.
        stop.setIcon(icons.icon("stop", "on-danger"))
        for button, glyph, tone in (
            (copy, "copy", "blue"),
            (paste, "paste", "blue"),
            (save, "save", "teal"),
            (clr, "eraser", "amber"),
            (latest, "arrow-down", "accent"),
            (restart_shell, "refresh", "green"),
            (restart_adb, "server", "purple"),
        ):
            button.setIcon(icons.icon(glyph, tone))

        # ShellPanel fills this with its Android / PowerShell / CMD switcher, so
        # the shell choice and the actions share one row above the terminal.
        self._switcher_row = QHBoxLayout()
        self._switcher_row.setSpacing(2)
        row.addLayout(self._switcher_row)
        for widget in (stop, separator(), copy, paste, save, clr, separator(),
                       latest, font_down, font_size, font_up, separator()):
            row.addWidget(widget)
        row.addWidget(restart_shell)
        row.addWidget(restart_adb)
        # The usage hint lives in the tooltip: as a label it wrapped onto a row
        # of its own and took height away from the terminal.
        toolbar.setToolTip(info_text)
        layout.addWidget(toolbar)
        self._btn_clear = clr

    def _attach_terminal(self, layout):
        """Wire the toolbar to ``self.term`` and add the terminal below it."""
        self._sync_font_size()
        # A zoom in any terminal resizes every terminal; keep this label true.
        self.term.font_size_changed.connect(lambda _size: self._sync_font_size())
        self._btn_clear.clicked.connect(self.term.clear)
        self.btn_latest.toggled.connect(self.term.set_follow_latest)
        layout.addWidget(self.term, 1)

    def _sync_font_size(self):
        self.lbl_font_size.setText(f"{self.term.font_size()} pt")

    def _change_font_size(self, delta):
        self.term.bump_font(delta)
        self._sync_font_size()

    def _start_async_completion(self, line, query):
        """Schedule an Android completion query and return to Qt's event loop."""
        from .qtutil import thread_running

        if thread_running(self._completion_thread):
            return None, []
        thread = _CompletionThread(line, query)
        self._completion_thread = thread
        thread.ready.connect(self._apply_async_completion)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: self._clear_completion_thread(t))
        thread.start()
        return None, []

    def _clear_completion_thread(self, thread) -> None:
        if self._completion_thread is thread:
            self._completion_thread = None

    def _apply_async_completion(self, line, newline, options) -> None:
        if not self._closing:
            self.term.apply_completion_result(line, newline, options)

    def _park_completion_thread(self) -> None:
        from .qtutil import park_thread, thread_running

        completion = self._completion_thread
        if thread_running(completion):
            try:
                completion.ready.disconnect(self._apply_async_completion)
            except Exception:
                pass
            park_thread(completion)

    def _query_android_completion(self, line: str, cwd: str):
        """Complete Android commands (first word) or paths relative to *cwd*."""
        handler = self.handler
        if not line or not line.strip() or handler is None:
            return None, []
        match = re.search(r"(\S*)$", line)
        token = match.group(1) if match else ""
        if not token:
            return None, []
        quote = lambda value: "'" + value.replace("'", "'\\''") + "'"
        head = line[: len(line) - len(token)]
        first_word = " " not in line.strip()
        try:
            if first_word:
                globs = " ".join(f"{path}/{quote(token)}*" for path in self._BIN_DIRS)
                result = handler.shell(f"ls -d {globs} 2>/dev/null", timeout=1.5, safe=True)
                if not isinstance(result, OperationResult) or not result.success:
                    return None, []
                names = sorted({
                    os.path.basename(path.rstrip("\r"))
                    for path in result.value.text.split()
                    if path.strip()
                })
                if not names:
                    return None, []
                if len(names) == 1:
                    return head + names[0] + " ", []
                prefix = os.path.commonprefix(names)
                return (head + prefix if len(prefix) > len(token) else None), names

            result = handler.shell(
                f"cd {quote(cwd)} 2>/dev/null; "
                f"ls -dp {quote(token)}* 2>/dev/null",
                timeout=1.5,
                safe=True,
            )
            if not isinstance(result, OperationResult) or not result.success:
                return None, []
            entries = [entry.rstrip("\r") for entry in result.value.text.split("\n") if entry.strip()]
            if not entries:
                return None, []
            if len(entries) == 1:
                return head + entries[0], []
            prefix = os.path.commonprefix(entries)
            return (head + prefix if len(prefix) > len(token) else None), entries
        except Exception:
            return None, []

    def restart_adb(self):
        """Ask the top-level window to restart its one shared ADB daemon."""
        restart = getattr(self.window(), "restart_adb_server", None)
        if callable(restart):
            self._announce_adb_restart()
            restart()
        else:
            self.log.emit("[WARNING] ADB restart is only available from a TurboADB window.")

    def _announce_adb_restart(self) -> None:
        """Hook for a terminal that echoes the restart before it happens."""


class _LocalShellWidget(_TerminalWidgetBase):
    """Dedicated interactive terminal tab for local PowerShell or CMD."""
    adb_reboot_requested = pyqtSignal(object)

    def __init__(self, shell_type: str = "powershell", serial: str = None, handler=None, parent=None):
        # The same handler backs the device tab, so completion requests use the
        # same managed ADB binary/server as the active interactive `adb shell`.
        super().__init__(handler, parent)
        self.shell_type = shell_type.lower()
        self.serial = serial

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        lbl = "PowerShell" if self.shell_type == "powershell" else "Command Prompt"
        self._build_toolbar(
            lay,
            restart_tip="Close and reopen this local shell",
            restart_slot=self.reopen,
            info_text=f"{lbl} • ANDROID_SERIAL={serial or 'auto'}",
        )

        self._shell_cwd = os.path.expanduser("~")

        self.term = AnsiConsole(send_fn=self._send)
        self.term.set_emulate_prompt(False)
        self.term.set_completion_fn(self._local_complete)
        self.term.set_prompt_provider_fn(self._get_prompt)
        self.term.set_interrupt_fn(self.interrupt)
        self._attach_terminal(lay)

        self._started = False
        self._in_adb_shell = False
        self._adb_shell_cwd = "/"
        self._strip_startup_banner = True
        self._prompt_tail = ""

    def _get_prompt(self) -> str:
        if self._in_adb_shell:
            host = self.serial or "android"
            return f"{host}:{self._adb_shell_cwd} $ "
        if self.shell_type == "powershell":
            return f"PS {self._shell_cwd}> "
        return f"{self._shell_cwd}>"

    _ADB_SUBCOMMANDS = (
        "devices", "shell", "push", "pull", "install", "uninstall",
        "logcat", "reboot", "connect", "disconnect", "forward", "reverse",
        "root", "unroot", "remount", "kill-server", "start-server", "version",
        "bugreport", "tcpip", "wait-for-device", "pair",
    )
    _GIT_SUBCOMMANDS = (
        "status", "commit", "push", "pull", "checkout", "branch", "clone",
        "diff", "log", "stash", "add", "fetch", "merge", "rebase", "reset",
        "remote", "tag", "show", "init",
    )
    _SCRCPY_SUBCOMMANDS = (
        "--max-size", "--bit-rate", "--record", "--stay-awake",
        "--turn-screen-off", "--display", "--list-displays", "--select-usb",
        "--tcpip", "--audio", "--no-audio", "--camera-id", "--window-title",
    )
    _CMD_BUILTINS = (
        "dir", "cd", "cls", "copy", "del", "move", "ren", "type", "echo",
        "set", "help", "exit", "mkdir", "rmdir", "where", "systeminfo",
        "tasklist", "taskkill", "netstat", "ipconfig", "ping", "tracert",
        "reg", "powershell", "cmd", "adb", "scrcpy", "fastboot", "git",
        "python", "pip",
    )
    _PS_BUILTINS = (
        "Get-ChildItem", "Set-Location", "Get-Process", "Get-Service",
        "Clear-Host", "Get-Content", "Get-Help", "Get-Command", "ls", "cd",
        "cat", "ps", "pwd", "clear", "echo", "cp", "mv", "rm", "mkdir",
        "adb", "scrcpy", "fastboot", "git", "python", "pip",
    )

    _PATH_CACHE: set[str] = set()
    _PATH_CACHE_TIME: float = 0.0
    _PATH_CACHE_KEY: str = ""

    @classmethod
    def _get_path_commands(cls, env=None) -> set[str]:
        """Command names on PATH, from the shell's own environment when given.

        TurboADB's process PATH is not the terminal's (PyInstaller/PyQt5 add
        private folders; the terminal adds ADB and registry entries)."""
        import time as _t
        source = os.environ if env is None else env
        path = next((v for k, v in source.items() if k.upper() == "PATH"), "")
        pathext_value = next((v for k, v in source.items() if k.upper() == "PATHEXT"), "")
        pathext_value = pathext_value or ".exe;.bat;.cmd;.ps1"
        key = path + "|" + pathext_value
        now = _t.time()
        if cls._PATH_CACHE and cls._PATH_CACHE_KEY == key and (now - cls._PATH_CACHE_TIME) < 30.0:
            return cls._PATH_CACHE
        cls._PATH_CACHE_KEY = key
        cmds = set()
        pathext = tuple(e.lower() for e in pathext_value.split(";") if e)
        for p in path.split(os.pathsep):
            p_clean = p.strip().strip('"')
            if not p_clean or not os.path.isdir(p_clean):
                continue
            try:
                with os.scandir(p_clean) as it:
                    for entry in it:
                        try:
                            base, ext = os.path.splitext(entry.name)
                            if ext.lower() in pathext or not ext:
                                cmds.add(base.lower())
                        except Exception:
                            pass
            except Exception:
                pass
        cls._PATH_CACHE = cmds
        cls._PATH_CACHE_TIME = now
        return cmds

    def _local_complete(self, line: str):
        builtins = self._PS_BUILTINS if self.shell_type == "powershell" else self._CMD_BUILTINS
        if not line or not line.strip():
            # If line is empty or whitespace, show common default commands
            return None, sorted(builtins)

        trailing_space = line.endswith(" ")
        tokens = line.split()
        if not tokens:
            return None, sorted(builtins)

        first = tokens[0].lower()

        # 1. Command completion (completing the first token).  A token that is
        # already a path (``.\tool``, ``..\bin\x``, ``C:\...``) is completed as
        # a path below, so ``.\to<Tab>`` finds ``.\tool.exe``.
        first_tok = tokens[0]
        is_path_token = (
            "\\" in first_tok or "/" in first_tok or first_tok.startswith(".")
            or (len(first_tok) >= 2 and first_tok[1] == ":")
        )
        if len(tokens) == 1 and not trailing_space and not is_path_token:
            prefix = first_tok.lower()
            matches = {c for c in builtins if c.lower().startswith(prefix)}

            # Look in the shell's own PATH (cached)
            path_cmds = self._get_path_commands(getattr(self.session, "env", None))
            for cmd_name in path_cmds:
                if cmd_name.startswith(prefix):
                    matches.add(cmd_name)

            # Look in current working directory
            if os.path.isdir(self._shell_cwd):
                try:
                    pathext = tuple(e.lower() for e in os.environ.get("PATHEXT", ".exe;.bat;.cmd;.ps1").split(";") if e)
                    if self.shell_type == "powershell":
                        pathext += (".ps1",)
                    with os.scandir(self._shell_cwd) as it:
                        for entry in it:
                            if entry.name.lower().startswith(prefix) and entry.is_file():
                                base, ext = os.path.splitext(entry.name)
                                if ext.lower() in pathext:
                                    # PowerShell never runs a bare name from the
                                    # current folder ("...does exist in the current
                                    # location"); offer the runnable .\ form there.
                                    matches.add(
                                        ".\\" + entry.name if self.shell_type == "powershell" else base
                                    )
                except Exception:
                    pass

            sorted_matches = sorted(matches, key=lambda x: (len(x), x.lower()))
            if len(sorted_matches) == 1:
                return sorted_matches[0] + " ", []
            elif sorted_matches:
                c_pref = os.path.commonprefix(sorted_matches)
                if len(c_pref) > len(prefix):
                    return c_pref, sorted_matches
            return None, sorted_matches

        # 2. Subcommand completion for adb
        if first == "adb":
            if (len(tokens) == 1 and trailing_space) or (len(tokens) == 2 and not trailing_space):
                pref = "" if trailing_space else tokens[1].lower()
                matches = [c for c in self._ADB_SUBCOMMANDS if c.startswith(pref)]
                if len(matches) == 1:
                    return f"adb {matches[0]} ", []
                elif matches:
                    c_pref = os.path.commonprefix(matches)
                    if len(c_pref) > len(pref):
                        return f"adb {c_pref}", matches
                return None, matches

        # 3. Subcommand completion for git
        if first == "git":
            if (len(tokens) == 1 and trailing_space) or (len(tokens) == 2 and not trailing_space):
                pref = "" if trailing_space else tokens[1].lower()
                matches = [c for c in self._GIT_SUBCOMMANDS if c.startswith(pref)]
                if len(matches) == 1:
                    return f"git {matches[0]} ", []
                elif matches:
                    c_pref = os.path.commonprefix(matches)
                    if len(c_pref) > len(pref):
                        return f"git {c_pref}", matches
                return None, matches

        # 4. Subcommand completion for scrcpy
        if first == "scrcpy" and (trailing_space or tokens[-1].startswith("-")):
            pref = "" if trailing_space else tokens[-1].lower()
            matches = [c for c in self._SCRCPY_SUBCOMMANDS if c.startswith(pref)]
            if len(matches) == 1:
                idx = line.rfind(tokens[-1]) if not trailing_space else len(line)
                return line[:idx] + matches[0] + " ", []
            elif matches:
                c_pref = os.path.commonprefix(matches)
                if len(c_pref) > len(pref):
                    idx = line.rfind(tokens[-1]) if not trailing_space else len(line)
                    return line[:idx] + c_pref, matches
            return None, matches

        # 5. Directory / file path completion
        dirs_only = first in ("cd", "chdir", "pushd", "set-location")
        if trailing_space:
            prefix = ""
        else:
            raw_last = tokens[-1]
            prefix = raw_last.strip('"\'')

        # ``/name`` is an Android/Unix-style path, not an absolute Windows
        # path. Treat it as relative in a local PowerShell/CMD session so Tab
        # never suggests `/name` and causes PowerShell to look for `C:\\name`.
        normalized_line = line
        if os.name == "nt" and prefix.startswith("/") and not prefix.startswith("//"):
            prefix = prefix.lstrip("/")
            if not trailing_space:
                normalized_line = line[:line.rfind(tokens[-1])] + prefix

        if os.path.isabs(prefix):
            search_dir = os.path.dirname(prefix) or prefix
            base = os.path.basename(prefix)
        else:
            rel_dir = os.path.dirname(prefix)
            search_dir = os.path.join(self._shell_cwd, rel_dir) if rel_dir else self._shell_cwd
            base = os.path.basename(prefix)

        real_dir = os.path.abspath(search_dir)
        if not os.path.exists(real_dir) or not os.path.isdir(real_dir):
            return None, []

        matches = []
        try:
            with os.scandir(real_dir) as it:
                for entry in it:
                    if dirs_only and not entry.is_dir():
                        continue
                    if entry.name.lower().startswith(base.lower()):
                        suffix = "\\" if entry.is_dir() else ""
                        matches.append(entry.name + suffix)
        except Exception:
            return None, []

        if not matches:
            return None, []

        matches.sort(key=lambda s: (not s.endswith("\\"), s.lower()))

        def _format_cand(m: str) -> str:
            if os.path.isabs(prefix):
                full = os.path.join(os.path.dirname(prefix), m)
            else:
                rel_dir = os.path.dirname(prefix)
                full = os.path.join(rel_dir, m) if rel_dir else m
            if " " in full or any(c in full for c in "&()"):
                clean = full.rstrip("\\")
                return f'"{clean}"'
            return full

        formatted_matches = [_format_cand(m) for m in matches]

        if len(matches) == 1:
            completed = formatted_matches[0]
            if not trailing_space:
                last_token = tokens[-1]
                idx = line.rfind(last_token)
                new_line = line[:idx] + completed
            else:
                new_line = line + completed
            return new_line, []
        elif matches:
            c_pref = os.path.commonprefix(matches)
            if len(c_pref) > len(base):
                if os.path.isabs(prefix):
                    completed = os.path.join(os.path.dirname(prefix), c_pref)
                else:
                    rel_dir = os.path.dirname(prefix)
                    completed = os.path.join(rel_dir, c_pref) if rel_dir else c_pref
                if " " in completed or any(c in completed for c in "&()"):
                    completed = f'"{completed.rstrip(chr(92))}"'
                if not trailing_space:
                    last_token = tokens[-1]
                    idx = line.rfind(last_token)
                    new_line = line[:idx] + completed
                else:
                    new_line = line + completed
                return new_line, formatted_matches
            # Even if the names have no longer common prefix, remove an
            # accidental Unix slash before presenting the choices. Otherwise
            # Enter would send `cd /name` to PowerShell as `C:\\name`.
            return (normalized_line if normalized_line != line else None), formatted_matches
        return None, formatted_matches

    def _adb_shell_complete(self, line: str):
        """Schedule Android completion and immediately return to Qt's event loop."""
        return self._start_async_completion(line, self._query_adb_shell_complete)

    def _query_adb_shell_complete(self, line: str):
        """Complete commands and paths while CMD/PowerShell hosts `adb shell`.

        The previous implementation disabled completion here to avoid using
        Windows paths for Android paths.  That made Tab appear broken exactly
        when it was most useful.  A small parallel ADB query is safe while the
        interactive shell is open and returns Android-native candidates.
        """
        return self._query_android_completion(line, self._adb_shell_cwd)

    def _track_adb_shell_directory(self, line: str) -> None:
        """Keep completion rooted at the last requested Android directory."""
        import posixpath

        parts = line.strip().split(maxsplit=1)
        if not parts or parts[0] != "cd":
            return
        if len(parts) == 1:
            self._adb_shell_cwd = "/"
        else:
            target = parts[1].strip().strip("'\"")
            if target and target not in ("-", "~"):
                self._adb_shell_cwd = posixpath.normpath(
                    target if target.startswith("/") else posixpath.join(self._adb_shell_cwd, target)
                )
        self.term._cwd = self._adb_shell_cwd

    def reset_adb_shell_context(self) -> None:
        """Return completion/prompt bookkeeping to the local host shell.

        A device reboot terminates an interactive ``adb shell`` subprocess.
        Without this reset the visible PowerShell/CMD prompt was local again,
        but Tab completion still treated subsequent commands as Android input.
        """
        self._in_adb_shell = False
        self._adb_shell_cwd = "/"
        self.term._cwd = self._shell_cwd
        self.term.set_completion_fn(self._local_complete)

    def _local_adb_reboot_mode(self, tokens: list[str]):
        """Return the requested reboot mode for this terminal's device, if any."""
        if not tokens or os.path.basename(tokens[0]).lower() not in ("adb", "adb.exe"):
            return None
        index = 1
        selected_serial = None
        while index < len(tokens) and tokens[index].startswith("-"):
            flag = tokens[index].lower()
            if flag in ("-s", "-t", "-h", "-p"):
                if index + 1 >= len(tokens):
                    return None
                if flag == "-s":
                    selected_serial = tokens[index + 1]
                index += 2
            else:
                index += 1
        if index >= len(tokens) or tokens[index].lower() != "reboot":
            return None
        if selected_serial and self.serial and selected_serial != self.serial:
            return None
        return tokens[index + 1].lower() if index + 1 < len(tokens) else ""

    def ensure_started(self):
        if not self._started:
            self._started = True
            self._start_session()

    def focus_terminal(self):
        self.ensure_started()
        self.term.setFocus(Qt.OtherFocusReason)

    def _start_session(self, *, show_banner=True):
        from .local_terminal import LocalShellSession

        try:
            self.session = LocalShellSession(self.shell_type, serial=self.serial, cwd=self._shell_cwd)
            if show_banner:
                from .. import __version__

                name = "PowerShell" if self.shell_type == "powershell" else "Command Prompt"
                target = f"  ·  ANDROID_SERIAL={self.serial}" if self.serial else ""
                # Two framed lines, like the Android terminal's banner.
                self.term.banner(
                    _boxed_banner(
                        [
                            f"\x1b[1;92m● {name}\x1b[0m \x1b[37mon this PC\x1b[0m  "
                            f"\x1b[90mTurboADB {__version__}\x1b[0m",
                            f"\x1b[37mTurboADB's adb is first on PATH{target}\x1b[0m",
                        ]
                    )
                )
        except Exception as exc:
            self.term.feed(f"\n[Could not start local {self.shell_type}: {exc}]\n".encode("utf-8"))
            return

        sess = self.session

        def read_fn():
            if not sess.running:
                data = sess.read(4096)
                return data or None
            return sess.read(4096)

        self.reader = ReaderThread(read_fn, decode=False)
        rd = self.reader
        self.reader.data.connect(lambda d: self._feed_from(rd, d))
        self.reader.closed.connect(self._on_closed)
        self.reader.start()
        self.term.set_alive(True)

    def _feed_from(self, reader, data):
        if reader is self.reader:
            if getattr(self, "_strip_startup_banner", False) and self.shell_type == "cmd":
                self._strip_startup_banner = False
                try:
                    text = data.decode("utf-8", errors="replace")
                    clean = re.sub(
                        r"^Microsoft Windows \[Version [^\]]+\]\r?\n(?:\(c\)[^\n]*\r?\n+)*\s*",
                        "",
                        text,
                    )
                    data = clean.encode("utf-8")
                except Exception:
                    pass
            self._track_prompt_cwd(data)
            self.term.feed(data)

    _PS_PROMPT_TAIL = re.compile(r"(?:^|[\r\n])PS ([^\r\n>]+)> ?$")
    _CMD_PROMPT_TAIL = re.compile(r"(?:^|[\r\n])([A-Za-z]:\\[^\r\n<>|*?\"]*)>$")

    def _track_prompt_cwd(self, data) -> None:
        """Follow the folder the shell reports in its own prompt.

        Parsing typed ``cd`` lines misses ``cd $env:TEMP``, ``pushd``,
        ``Set-Location ~\\x`` and profile functions, which left Tab completion
        (and ``.\\tool.exe`` suggestions) looking in the wrong folder and made
        Stop reopen the shell somewhere else."""
        if self._in_adb_shell:
            self._prompt_tail = ""
            return
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        tail = (getattr(self, "_prompt_tail", "") + data)[-1024:]
        self._prompt_tail = tail
        pattern = self._PS_PROMPT_TAIL if self.shell_type == "powershell" else self._CMD_PROMPT_TAIL
        match = pattern.search(tail)
        if match and os.path.isdir(match.group(1)):
            self._shell_cwd = match.group(1)

    def _on_closed(self):
        if not self._closing:
            self.term.set_alive(False)
            self.term.feed(b"\r\n[Process terminated]\r\n")

    def _send(self, data: bytes):
        self.ensure_started()
        if not (self.session and self.session.running):
            return

        # Track directory changes and handle shell conveniences
        try:
            line = data.decode("utf-8", "replace").strip()
            parts = line.split(maxsplit=1)
            # Once `adb shell` is interactive, its commands and paths belong
            # to Android. Never apply Windows path completion to them.
            was_in_adb_shell = self._in_adb_shell
            if was_in_adb_shell:
                self._track_adb_shell_directory(line)
            if not was_in_adb_shell and parts and parts[0].lower() in ("cd", "chdir"):
                if len(parts) > 1:
                    target = parts[1].strip().strip('"\'')
                    if target.lower().startswith("/d "):
                        target = target[3:].strip().strip('"\'')
                    if target == "~":
                        new_cwd = os.path.expanduser("~")
                    else:
                        new_cwd = os.path.normpath(os.path.join(self._shell_cwd, target))
                    if os.path.isdir(new_cwd):
                        self._shell_cwd = new_cwd
                elif self.shell_type == "powershell":
                    self._shell_cwd = os.path.expanduser("~")
            elif (not was_in_adb_shell and parts
                  and parts[0].lower() == "set-location" and len(parts) > 1):
                target = parts[1].strip().strip('"\'')
                new_cwd = os.path.normpath(os.path.join(self._shell_cwd, target))
                if os.path.isdir(new_cwd):
                    self._shell_cwd = new_cwd

            # Track entry and exit from interactive adb shell
            if line.lower() in ("exit", "exit 0", "logout"):
                self._in_adb_shell = False
                self.term.set_completion_fn(self._local_complete)

            tokens = line.split()
            local_reboot_mode = (
                self._local_adb_reboot_mode(tokens) if not was_in_adb_shell else None
            )
            if len(tokens) == 2 and tokens[0].lower() == "adb" and tokens[1].lower() == "shell":
                self._in_adb_shell = True
                self._adb_shell_cwd = "/"
                self.term._cwd = self._adb_shell_cwd
                self.term.set_completion_fn(self._adb_shell_complete)
                data = b"adb shell -t -t\r\n"
                if hasattr(self.term, "_pending_echo"):
                    self.term._pending_echo = "adb shell -t -t"
            elif len(tokens) == 4 and tokens[0].lower() == "adb" and tokens[1].lower() in ("-s", "-t") and tokens[3].lower() == "shell":
                self._in_adb_shell = True
                self._adb_shell_cwd = "/"
                self.term._cwd = self._adb_shell_cwd
                self.term.set_completion_fn(self._adb_shell_complete)
                data = f"adb {tokens[1]} {tokens[2]} shell -t -t\r\n".encode("utf-8")
                if hasattr(self.term, "_pending_echo"):
                    self.term._pending_echo = f"adb {tokens[1]} {tokens[2]} shell -t -t"

            if not getattr(self, "_in_adb_shell", False):
                rewritten = self._interactive_rewrite(line)
                if rewritten is not None:
                    if hasattr(self.term, "_pending_echo"):
                        self.term._pending_echo = rewritten
                    data = (rewritten + "\r\n").encode("utf-8")

            # In CMD when at top-level prompt, provide ls -> dir convenience
            if self.shell_type == "cmd" and not getattr(self, "_in_adb_shell", False):
                if line.lower() == "ls":
                    if hasattr(self.term, "_pending_echo"):
                        self.term._pending_echo = "dir"
                    data = b"dir\r\n"
                elif line.lower().startswith("ls "):
                    rest = line[3:].strip()
                    if rest in ("-la", "-al", "-l", "-a"):
                        if hasattr(self.term, "_pending_echo"):
                            self.term._pending_echo = "dir /a" if "a" in rest else "dir"
                        data = b"dir /a\r\n" if "a" in rest else b"dir\r\n"
                    else:
                        if hasattr(self.term, "_pending_echo"):
                            self.term._pending_echo = f"dir {rest}"
                        data = f"dir {rest}\r\n".encode("utf-8")
                elif line.lower() == "clear":
                    if hasattr(self.term, "_pending_echo"):
                        self.term._pending_echo = "cls"
                    data = b"cls\r\n"
            if local_reboot_mode is not None:
                # The command itself is still sent to PowerShell/CMD below.
                # This only tells DeviceTab to preserve input and reconnect the
                # Android stream after an explicit local `adb reboot`.
                self.adb_reboot_requested.emit(local_reboot_mode or None)
        except Exception:
            pass

        self.session.send(data)

    # Interpreters that show their prompt only on a real console. Their input
    # here is a pipe, so a bare `python` or `node` waited silently and looked
    # hung; -i forces the interactive prompt and -u keeps output unbuffered.
    _INTERACTIVE_FLAGS = {"python": "-i -u", "python3": "-i -u", "py": "-i -u", "node": "-i"}
    # Flags after which the interpreter runs something instead of a REPL.
    _NON_REPL_FLAGS = {"-c", "-m", "-v", "-V", "--version", "-h", "--help", "-e", "-p", "--eval",
                       "--print", "-i"}

    def _interactive_rewrite(self, line):
        """The command to send instead of *line*, or None to send it as typed.

        * PowerShell: ``where`` is the ``Where-Object`` alias, so ``where
          python`` printed nothing and a bare ``where`` waited for pipeline
          input. A line that starts with ``where`` and is not part of a
          pipeline or script block runs ``where.exe`` like in CMD.
        * A bare ``python`` / ``py`` / ``node`` (optionally with interpreter
          flags, but no script) starts its interactive prompt.
        """
        stripped = (line or "").strip()
        if not stripped:
            return None
        tokens = stripped.split()
        first = tokens[0].lower()
        if (
            self.shell_type == "powershell"
            and first == "where"
            and "|" not in stripped
            and "{" not in stripped
        ):
            return "where.exe" + stripped[len(tokens[0]):]
        name = first[:-4] if first.endswith(".exe") else first
        flags = self._INTERACTIVE_FLAGS.get(name)
        if flags is None:
            return None
        args = iter(tokens[1:])
        for arg in args:
            if not arg.startswith("-") or arg in self._NON_REPL_FLAGS:
                return None  # a script, -c/-m code, or an explicit mode: run as typed
            if arg in ("-X", "-W"):
                next(args, None)  # python's option value (e.g. -X utf8), not a script
        return f"{stripped} {flags}"

    def _stop_session(self, *, interrupt=False):
        """End only this local process; the terminal widget remains reusable."""
        reader, session = self.reader, self.session
        self.reader = None
        self.session = None
        if reader:
            try:
                reader.closed.disconnect(self._on_closed)
            except Exception:
                pass
            reader.stop()
        if session:
            try:
                if interrupt:
                    session.interrupt()
                else:
                    session.close()
            except Exception:
                pass
        if reader:
            if not reader.wait(500):
                # A Windows console pipe can take longer than the child process
                # to report EOF. Keep the QThread alive until it unwinds instead
                # of destroying it with the terminal widget.
                from .qtutil import park_thread

                park_thread(reader)

    def interrupt(self):
        """Hard-stop a local command instead of sending an inert Ctrl+C byte."""
        if self._closing or not (self.session and self.session.running):
            return
        self._stop_session(interrupt=True)
        self.reset_adb_shell_context()
        self._started = True
        self.term._echo("\n^C  — stopped; fresh local shell ready\n", theme.ECHO_ERROR)
        self._start_session(show_banner=False)
        self.term.set_alive(True)
        self.term.setFocus(Qt.OtherFocusReason)

    def reopen(self):
        self._stop_session()
        self.term.clear()
        self._closing = False
        self.reset_adb_shell_context()
        self._started = False
        self.ensure_started()

    def _announce_adb_restart(self) -> None:
        self.term._echo("\nRestarting shared ADB server…\n", theme.ECHO_WARN)

    def close_panel(self):
        self._closing = True
        self._park_completion_thread()
        self._stop_session()
        try:
            self.term.close_archive()
        except Exception:
            pass


class _AndroidShellWidget(_TerminalWidgetBase):
    """A native interactive ``adb shell`` with local prompt emulation and auto-completion."""
    disconnected = pyqtSignal()

    def __init__(self, handler, device_name="", info=None, parent=None):
        super().__init__(handler, parent)
        self.setObjectName("terminalPanel")
        self.device_name = device_name or (handler.serial or "android")
        self._info = info or {}
        self._pt = None
        self._banner_shown = False
        self._banner_pending = False
        self._banner_waited = False
        self._adb_restart_paused = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._build_toolbar(
            lay,
            restart_tip="Close and reopen the Android shell",
            restart_slot=self.restart_shell,
            info_text="Tab completes • right-click Copy/Paste",
        )

        self.term = AnsiConsole(send_fn=self._send)
        self.term.set_completion_fn(self._complete)
        self.term.set_interrupt_fn(self.interrupt)
        self._attach_terminal(lay)
        self._open()

    def _complete(self, line):
        """Schedule an ADB completion query instead of blocking the Tab key."""
        return self._start_async_completion(line, self._query_complete)

    def _query_complete(self, line):
        return self._query_android_completion(line, getattr(self.term, "_cwd", "/"))

    def _open(self, *, focus=True):
        if not self.handler:
            return
        res = self.handler.open_shell(tty=False)
        self.session = res.value if isinstance(res, OperationResult) else res
        if self.session is None:
            self.term.feed(b"\n[could not open adb shell]\n")
            return

        sess = self.session

        def read_fn():
            if not sess.running:
                data = sess.read(65536)
                return data or None
            return sess.read(65536)

        self.reader = ReaderThread(read_fn, decode=False)
        rd = self.reader
        self.reader.data.connect(lambda d: self._feed_from(rd, d))
        self.reader.closed.connect(self._on_reader_closed)
        self.reader.start()
        if focus:
            self.term.setFocus(Qt.OtherFocusReason)

        self.term.set_prompt(self.device_name, root=False)
        if not self._banner_shown and self.term._alive and not (
            self._info.get("kind") or self._banner_waited
        ):
            # Give the device-type probe a moment so the banner shows the full
            # identity (type, CPU, display); never wait longer than 2.5 s.
            self._banner_pending = True
            QTimer.singleShot(2500, self._flush_pending_banner)
        else:
            self._show_banner_and_prompt()
        self._start_prompt_probe()

    def _show_banner_and_prompt(self):
        self._banner_pending = False
        if not self._banner_shown and self.term._alive:
            self._banner_shown = True
            try:
                self.term.banner(self._welcome_banner())
            except Exception:
                pass
        if self.term._alive:
            self.term.show_prompt()

    def _flush_pending_banner(self):
        if self._banner_pending and not self._closing:
            self._banner_waited = True
            self._show_banner_and_prompt()

    def update_identity(self, details):
        """Full device details arrived; show the waiting banner with them."""
        self._info = dict(details or {})
        if self._banner_pending and not self._closing:
            self._show_banner_and_prompt()

    def _start_prompt_probe(self):
        """Ask the device for root state; a stale probe is detached and parked."""
        from .qtutil import park_thread, thread_running

        previous = self._pt
        if thread_running(previous):
            # Stop/reconnect reopen the shell while the last probe may still be
            # waiting on adb.  Replacing the reference would destroy a running
            # QThread, and its answer describes the previous shell anyway.
            try:
                previous.ready.disconnect(self._on_prompt)
            except Exception:
                pass
            park_thread(previous)
        self._pt = _PromptThread(self.handler)
        self._pt.ready.connect(self._on_prompt)
        self._pt.start()

    def _welcome_banner(self) -> str:
        d = self._info
        cfg = getattr(self.handler, "config", None)
        if cfg is not None and getattr(cfg, "adb_server_host", None):
            via = f"remote adb {cfg.adb_server_host}:{getattr(cfg, 'adb_server_port', 5037)}"
        elif cfg is not None and getattr(cfg, "host", None):
            via = f"network {cfg.host}:{getattr(cfg, 'port', 5555)}"
        else:
            via = "USB"

        model = (
            ((d.get("manufacturer") or d.get("brand") or "") + " " +
             (d.get("model") or self.device_name or "device")).strip()
        )
        andro = d.get("android_version")
        sdk = d.get("sdk")
        abi = d.get("abi")
        serial = self.handler.serial or d.get("serial") or ""
        kind_label = d.get("kind_label") or ("Automotive head unit" if d.get("automotive") else "")

        andro_str = f"Android {andro}" + (f" · SDK {sdk}" if sdk else "") if andro else "Android"

        from .. import __version__

        # Two lines only: who/where, then the device identity in one row.
        first = (
            f"\x1b[1;92m● Connected\x1b[0m \x1b[37mto \x1b[1;35m{model}\x1b[0m "
            f"\x1b[37m[{via}]\x1b[0m  \x1b[90mTurboADB {__version__}\x1b[0m"
        )
        facts = [kind_label, andro_str if andro else "", abi, serial]
        second = "\x1b[37m" + "  ·  ".join(part for part in facts if part) + "\x1b[0m"
        return _boxed_banner([first, second])

    def _feed_from(self, reader, data):
        if reader is self.reader:
            self.term.feed(data)

    def _on_reader_closed(self):
        if self._closing or self._adb_restart_paused:
            return
        self.term.set_alive(False)
        self.disconnected.emit()

    def _close_stream(self, wait_ms=300):
        """Close this widget's adb shell and its reader thread.

        The reader is parked if it doesn't stop within *wait_ms*, so dropping
        the reference can never destroy a still-running QThread.
        """
        reader, self.reader = self.reader, None
        session, self.session = self.session, None
        if reader is not None:
            try:
                reader.closed.disconnect(self._on_reader_closed)
            except Exception:
                pass
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        if reader is not None:
            reader.stop()
            if not reader.wait(wait_ms):
                from .qtutil import park_thread

                park_thread(reader)

    def reconnect(self, *, focus=True):
        self._adb_restart_paused = False
        self._close_stream()
        self.term.feed(b"\n")
        self._open(focus=focus)
        self.term.set_alive(True)
        if focus:
            self.focus_terminal()

    def pause_for_device_reboot(self):
        """Close only the Android stream while the device is rebooting.

        Local CMD/PowerShell terminals stay alive, so their host-shell input
        remains usable when the interactive ``adb shell`` subprocess exits.
        """
        self._pause_stream("Device rebooting")

    def pause_for_adb_restart(self):
        """Close this transport quietly while the shared daemon is replaced."""
        self._pause_stream("ADB server restarting")

    def _pause_stream(self, reason):
        if self._closing:
            return
        self._adb_restart_paused = True
        self._close_stream()
        self.term.set_alive(False, show_disconnect_notice=False)
        self.term._echo(
            f"\n  ↻  {reason} — this shell will reconnect automatically.\n\n",
            theme.ECHO_WARN,
        )

    def _on_prompt(self, is_root):
        # The prompt's label is the same stable identity used in the banner;
        # only the root marker comes from the device.
        if not self._closing:
            self.term.set_prompt(self.device_name, is_root)

    def _send(self, data: bytes):
        if self.session and self.session.running:
            self.session.send(data)

    def focus_terminal(self):
        try:
            self.term.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass

    def interrupt(self):
        if self._closing or not self.handler:
            return
        cwd = getattr(self.term, "_cwd", "/")
        try:
            self.term._inq.clear()
            self.term._inq_len = 0
            self.term._drain.stop()
        except Exception:
            pass

        # Closing this adb shell ends its own device-side process group (the
        # command being stopped).  Never kill processes device-wide: that also
        # killed the Logcat tab and any other tool's logcat/top.
        self._close_stream(wait_ms=800)

        self.term._echo("\n^C  — stopped\n", theme.ECHO_ERROR)
        self.term._last_feed = 0.0
        self.term._cwd = cwd
        self._open()
        self.term.set_alive(True)

        if cwd and cwd not in ("", "/") and self.session:
            try:
                self.session.send(("cd " + shlex.quote(cwd) + "\n").encode("utf-8"))
            except Exception:
                pass

    def restart_shell(self):
        if self._closing:
            return
        self.term._echo("\nRestarting Android shell…\n", theme.ECHO_WARN)
        self.interrupt()

    def close_panel(self):
        from .qtutil import park_thread, thread_running

        self._closing = True
        self._adb_restart_paused = True
        self._park_completion_thread()
        self._close_stream(wait_ms=700)
        prompt, self._pt = self._pt, None
        if thread_running(prompt):
            # Never block closing on a slow `id -u`; keep the worker alive instead.
            try:
                prompt.ready.disconnect(self._on_prompt)
            except Exception:
                pass
            park_thread(prompt)
        try:
            self.term.close_archive()
        except Exception:
            pass


class ShellPanel(QWidget):
    """Container holding persistent sub-tabs for Android Shell, PowerShell, and CMD."""
    log = pyqtSignal(str)
    disconnected = pyqtSignal()
    adb_reboot_requested = pyqtSignal(object)

    def __init__(self, handler, device_name="", info=None, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.device_name = device_name
        self._info = info

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.subtabs = AnimatedTabWidget(transition_ms=125)
        self.subtabs.setObjectName("terminalTabs")
        self.subtabs.setDocumentMode(True)
        self.subtabs.tabBar().setDrawBase(False)

        self.android_widget = _AndroidShellWidget(handler, device_name=device_name, info=info)
        self.android_widget.log.connect(self.log)
        self.android_widget.disconnected.connect(self.disconnected)
        self.subtabs.addTab(self.android_widget, "Android shell")

        serial = getattr(handler, "serial", None)
        self.ps_widget = _LocalShellWidget("powershell", serial=serial, handler=handler)
        self.ps_widget.log.connect(self.log)
        self.ps_widget.adb_reboot_requested.connect(self.adb_reboot_requested)
        self.subtabs.addTab(self.ps_widget, "PowerShell")

        self.cmd_widget = _LocalShellWidget("cmd", serial=serial, handler=handler)
        self.cmd_widget.log.connect(self.log)
        self.cmd_widget.adb_reboot_requested.connect(self.adb_reboot_requested)
        self.subtabs.addTab(self.cmd_widget, "Command Prompt")
        self.subtabs.currentChanged.connect(self._on_terminal_changed)
        self._install_switchers()

        lay.addWidget(self.subtabs, 1)

    _SHELLS = (
        ("smartphone", "green", "Android"),
        ("terminal", "blue", "PowerShell"),
        ("terminal", "amber", "CMD"),
    )

    def _install_switchers(self):
        """Replace the separate shell tab row with a segmented switcher at the
        start of every shell's toolbar (the tab bar keeps the page order)."""
        self.subtabs.tabBar().hide()
        self._switch_groups = []
        for page in (self.android_widget, self.ps_widget, self.cmd_widget):
            group = []
            for index, (glyph, tone, label) in enumerate(self._SHELLS):
                button = QToolButton()
                button.setObjectName("shellSwitch")
                button.setCheckable(True)
                button.setText(label)
                button.setIcon(icons.icon(glyph, tone))
                button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
                button.setToolTip(f"Switch to {self.subtabs.tabText(index)}")
                button.clicked.connect(lambda _=False, i=index: self._switch_shell(i))
                page._switcher_row.addWidget(button)
                group.append(button)
            line = QFrame()
            line.setObjectName("barSeparator")
            line.setFrameShape(QFrame.VLine)
            line.setFixedWidth(1)
            page._switcher_row.addWidget(line)
            self._switch_groups.append(group)
        self.subtabs.currentChanged.connect(self._sync_switchers)
        self._sync_switchers(self.subtabs.currentIndex())

    def _switch_shell(self, index):
        self.subtabs.setCurrentIndex(index)
        self._sync_switchers(index)

    def _sync_switchers(self, index):
        for group in self._switch_groups:
            for position, button in enumerate(group):
                button.setChecked(position == index)

    @property
    def term(self):
        curr = self.subtabs.currentWidget()
        return getattr(curr, "term", self.android_widget.term)

    def focus_terminal(self):
        curr = self.subtabs.currentWidget()
        if hasattr(curr, "focus_terminal"):
            curr.focus_terminal()
        elif hasattr(curr, "term"):
            curr.term.setFocus(Qt.OtherFocusReason)

    def _on_terminal_changed(self, _index):
        """Return an opted-in terminal to its newest line after tab changes."""
        # A programmatic tab change while the whole terminal page is hidden
        # must not launch a local PowerShell/CMD process. DeviceTab calls
        # ``focus_terminal`` when the page is actually shown.
        if self.isVisible():
            self.focus_terminal()
        term = getattr(self.subtabs.currentWidget(), "term", None)
        if term is not None and getattr(term, "_follow_latest", False):
            QTimer.singleShot(0, term._pin_to_latest_if_following)

    def reconnect(self):
        """Reconnect Android without stealing focus from CMD/PowerShell."""
        active = self.subtabs.currentWidget()
        self.android_widget.reconnect(focus=False)
        if active is not None and self.subtabs.indexOf(active) >= 0:
            self.subtabs.setCurrentWidget(active)
        # Only restore keyboard focus when this panel is actually on screen.
        # A reconnect occurring while the user is in Files/Apps must not move
        # their cursor into a hidden terminal widget.
        if self.isVisible():
            self.focus_terminal()

    def pause_for_adb_restart(self):
        self.android_widget.pause_for_adb_restart()

    def pause_for_device_reboot(self):
        self.android_widget.pause_for_device_reboot()
        self.ps_widget.reset_adb_shell_context()
        self.cmd_widget.reset_adb_shell_context()

    def interrupt(self):
        curr = self.subtabs.currentWidget()
        if hasattr(curr, "interrupt"):
            curr.interrupt()

    def close_panel(self):
        self.android_widget.close_panel()
        self.ps_widget.close_panel()
        self.cmd_widget.close_panel()


class _StatePill(QLabel):
    """Connection state badge in the device header.

    Callers keep setting full sentences ("Connected — Pixel 7"); the pill shows
    the short state before " — ", keeps the sentence as its tooltip, and picks
    an ok / warn / error tint from the wording.
    """

    _ERROR_WORDS = ("fail", "timed out", "didn't come back", "lost", "error")
    _WARN_WORDS = ("connecting", "reconnecting", "rebooting", "rebooted", "restarting", "waiting")

    def __init__(self, text=""):
        super().__init__()
        self.setObjectName("statePill")
        self.setText(text)

    def setText(self, text):
        text = str(text or "")
        lowered = text.lower()
        if any(word in lowered for word in self._ERROR_WORDS):
            state = "error"
        elif any(word in lowered for word in self._WARN_WORDS):
            state = "warn"
        elif lowered.startswith("connected"):
            state = "ok"
        else:
            state = ""
        super().setText(text.split(" — ", 1)[0])
        self.setToolTip(text)
        self._full_text = text
        if self.property("state") != state:
            self.setProperty("state", state)
            self.style().unpolish(self)
            self.style().polish(self)

    def full_text(self):
        return self._full_text


class DeviceTab(QWidget):
    log = pyqtSignal(str)
    title_changed = pyqtSignal(str)
    # Relays Device Control's screen session state (True while video shows).
    screen_active = pyqtSignal(bool)

    def __init__(self, session: dict, parent=None, *, terminal_only: bool = False):
        super().__init__(parent)
        self.session = normalize_session(session)
        self._terminal_only = bool(terminal_only)
        self.handler = None
        self._threads = []
        self._rc = None
        self._automotive = False
        self._reconnecting = False
        self._reboot_in_progress = False
        self._adb_restart_in_progress = False
        self._info_thread = None
        self._device_dispatcher = None
        self._session_closed = False
        self._subtab_meta = {}
        self._split_keys = ()
        self._split_panes = {}
        self._split_root = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Device actions sit at the right end of the section tab row, so there
        # is no separate header row. The identity (name, state, Android, CPU,
        # serial, device type) is in the terminal's welcome banner and in the
        # window tab title; these hidden objects keep the state for the code
        # and the status bar.
        actions = QWidget()
        actions.setObjectName("deviceActions")
        self._device_actions = actions
        bar = QHBoxLayout(actions)
        bar.setContentsMargins(0, 2, 8, 2)
        bar.setSpacing(6)
        self.title_label = QLabel(self.session.get("name") or "Device", self)
        self.title_label.hide()
        self.status = _StatePill("Connecting…")
        self.status.setParent(self)
        self.status.hide()
        chips = QWidget(self)
        chips.hide()
        self._chip_row = QHBoxLayout(chips)
        self._chip_row.addStretch(1)
        self._set_chips(self._session_chips())
        self.title_changed.connect(self.title_label.setText)

        def header_button(text, tooltip="", role=None, glyph=None, tone=None):
            button = QToolButton()
            button.setText(text)
            button.setToolTip(tooltip)
            if glyph:
                button.setIcon(icons.icon(glyph, tone))
                button.setIconSize(QSize(18, 18))
                button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            else:
                button.setToolButtonStyle(Qt.ToolButtonTextOnly)
            if role:
                button.setProperty("role", role)
            return button

        self.btn_mirror = header_button(
            "Screen ▾", "Show the device screen", role="ok", glyph="monitor", tone="on-accent"
        )
        self.btn_mirror.setPopupMode(QToolButton.InstantPopup)
        mmenu = QMenu(self.btn_mirror)
        # This action must override the remembered embedded-view preference.
        # Passing ``None`` made it silently honour that preference, so an item
        # labelled “separate window” could still open inside Device Control.
        mmenu.addAction("Open screen in a separate window", lambda: self.mirror(embed=False))
        mmenu.addAction("Show screen in this tab", lambda: self.mirror(embed=True))
        mmenu.addAction("Choose a device display…", self.mirror_choose_display)
        mmenu.addAction(
            "Open screen in compatibility mode (IVI / automotive)",
            lambda: self.mirror(compat=True, embed=False),
        )
        self.btn_mirror.setMenu(mmenu)

        # Less frequent device actions share one overflow menu so the header
        # stays on a single row at any window width.
        self.btn_more = header_button(
            "More ▾", "Split view, device health, build details, root and mount", glyph="more"
        )
        self.btn_more.setPopupMode(QToolButton.InstantPopup)
        more_menu = QMenu(self.btn_more)
        split_menu = more_menu.addMenu(icons.icon("columns", "purple"), "Split view")
        split_menu.setToolTip(
            "Show the same session in two or four panes without opening duplicate tools"
        )
        self._split_menu = split_menu
        split_menu.addAction(
            "Two panes — side by side…",
            lambda: self._configure_split(2, Qt.Horizontal),
        )
        split_menu.addAction(
            "Two panes — top and bottom…",
            lambda: self._configure_split(2, Qt.Vertical),
        )
        split_menu.addAction("Four panes — grid…", lambda: self._configure_split(4))
        split_menu.addAction("Evenly resize active panes", self._even_split_sizes)
        split_menu.addSeparator()
        split_menu.addAction("Return to tabs", self._leave_split)

        self.btn_restore_split = header_button(
            "Return to tabs", "Leave split view and return to the normal tabs",
            glyph="columns", tone="purple",
        )
        self.btn_restore_split.clicked.connect(self._leave_split)
        self.btn_restore_split.hide()

        self.btn_shot = header_button(
            "Screenshot", "Save a screenshot of the device screen", glyph="camera", tone="blue"
        )
        self.btn_shot.clicked.connect(self.screenshot)

        more_menu.addSeparator()
        health = more_menu.addAction(icons.icon("heart", "red"), "Device health…", self.show_health)
        health.setToolTip("Battery, temperature, memory, CPU and uptime in one snapshot.")
        build = more_menu.addAction(icons.icon("chip", "blue"), "Build details…", self.show_build)
        build.setToolTip("Build identity, software version, kernel, and Android properties")
        more_menu.addSeparator()
        amenu = more_menu.addMenu(icons.icon("shield", "amber"), "Root and mount")
        amenu.addAction("adb root", lambda: self._op("root", lambda h: h.root(safe=True)))
        amenu.addAction("adb unroot", lambda: self._op("unroot", lambda h: h.unroot(safe=True)))
        amenu.addSeparator()
        amenu.addAction("adb remount (rw)", lambda: self._op("remount", lambda h: h.remount(safe=True)))
        amenu.addAction("mount -o remount,rw /", lambda: self._op("mount rw", lambda h: h.mount_rw(safe=True)))
        amenu.addSeparator()
        amenu.addAction("adb disable-verity  (sync + reboot)", lambda: self._verity(False))
        amenu.addAction("adb enable-verity  (sync + reboot)", lambda: self._verity(True))
        more_menu.addAction(icons.icon("wifi", "teal"), "Go wireless (USB → Wi-Fi)", self.go_wireless)
        more_menu.addAction(icons.icon("bug", "orange"), "Capture bugreport…", self.capture_bugreport)
        self.btn_more.setMenu(more_menu)

        self.btn_reboot = header_button("Reboot ▾", "Reboot the device", glyph="refresh", tone="amber")
        self.btn_reboot.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.btn_reboot)
        for label, mode in (
            ("System", None),
            ("Recovery", "recovery"),
            ("Bootloader", "bootloader"),
            ("Sideload", "sideload"),
        ):
            menu.addAction(label, lambda *_, m=mode: self.reboot(m))
        self.btn_reboot.setMenu(menu)

        # Shown only when the device is not shell-ready: a failed connect, a
        # reconnect timeout, or a reboot into recovery/bootloader.
        self.btn_reconnect = header_button(
            "Reconnect", "Try to connect to this device again", role="ok",
            glyph="plug", tone="on-accent",
        )
        self.btn_reconnect.clicked.connect(self.reconnect_device)
        self.btn_reconnect.hide()

        for w in (
            self.btn_reconnect,
            self.btn_restore_split,
            self.btn_mirror,
            self.btn_shot,
            self.btn_reboot,
            self.btn_more,
        ):
            bar.addWidget(w, 0, Qt.AlignVCenter)
        if self._terminal_only:
            self.status.setText("Connecting terminal session…")
            for action in (
                self.btn_mirror,
                self.btn_restore_split,
                self.btn_shot,
                self.btn_reboot,
                self.btn_more,
            ):
                action.hide()

        self.inner = AnimatedTabWidget(transition_ms=145)
        self.inner.setObjectName("deviceTabs")
        self.inner.setDocumentMode(True)
        self.inner.setElideMode(Qt.ElideNone)
        self.inner.setUsesScrollButtons(True)
        tb = self.inner.tabBar()
        if tb is not None:
            tb.setExpanding(False)
            tb.setDrawBase(False)
        self.inner.setCornerWidget(actions, Qt.TopRightCorner)
        self.inner.currentChanged.connect(self._on_subtab_changed)
        self._content_stack = QStackedWidget()
        self._content_stack.addWidget(self.inner)
        self._split_workspace = QWidget()
        self._split_workspace.setObjectName("splitWorkspace")
        self._split_layout = QVBoxLayout(self._split_workspace)
        self._split_layout.setContentsMargins(8, 8, 8, 8)
        self._split_layout.setSpacing(0)
        self._content_stack.addWidget(self._split_workspace)
        lay.addWidget(self._content_stack, 1)
        self._enable_actions(False)

        self._ct = None
        self._new_connect_thread()
        QTimer.singleShot(0, self._auto_start_connect)

    def _new_connect_thread(self):
        """Create the connect worker, detaching (and parking) a previous one."""
        from .qtutil import park_thread, thread_running

        previous = self._ct
        if previous is not None:
            for sig in ("ok", "fail", "log"):
                try:
                    getattr(previous, sig).disconnect()
                except Exception:
                    pass
            if thread_running(previous):
                previous.cancel()
                park_thread(previous)
        cfg = config_from_session(self.session)
        self._ct = _ConnectThread(cfg, fetch_identity=not self._terminal_only)
        self._ct.ok.connect(self._on_connected)
        self._ct.fail.connect(self._on_fail)
        self._ct.log.connect(self.log)
        self._started_connect = False

    def start_connect(self):
        from .qtutil import thread_running

        if (
            not self._session_closed
            and self._ct is not None
            and not thread_running(self._ct)
            and not self._started_connect
        ):
            self._started_connect = True
            self._ct.start()

    def _auto_start_connect(self):
        if not self._session_closed and not self._started_connect:
            self.start_connect()

    def reconnect_device(self):
        """Retry after a failed connect, a reconnect timeout, or a recovery reboot."""
        if self._session_closed or self._reconnecting:
            return
        self.btn_reconnect.hide()
        if self.handler is None:
            self.status.setText("Connecting…")
            self._new_connect_thread()
            self.start_connect()
            return
        self._reboot_in_progress = False
        self._wait_and_reconnect(manual=True)

    def _offer_reconnect(self):
        """Leave a way out when the device is not shell-ready."""
        self.btn_reconnect.show()
        if self.handler is not None:
            # Recovery / bootloader still accept `adb reboot`, the way back.
            self.btn_reboot.setEnabled(True)

    @staticmethod
    def _release_handler(handler):
        """Disconnect a handler this tab will not use, off the UI thread."""
        if handler is None:
            return
        from .qtutil import park_thread

        worker = _ActionThread(handler.disconnect)
        park_thread(worker)
        worker.start()

    def _enable_actions(self, on):
        for w in (self.btn_mirror, self.btn_shot, self.btn_reboot, self.btn_more):
            w.setEnabled(on)
        if on:
            self.btn_reconnect.hide()

    def _session_chips(self):
        """Transport chips known before connecting: serial or host:port, remote server."""
        session = self.session
        chips = []
        target = session.get("serial") or ""
        if not target and session.get("host"):
            target = f"{session.get('host')}:{session.get('port') or 5555}"
        if target:
            chips.append(target)
        server = session.get("adb_host")
        if server:
            chips.append(f"via {server}:{session.get('adb_port') or 5037}")
        return chips

    def _set_chips(self, texts):
        """Replace the header's info chips (kept on one row, before the stretch)."""
        while self._chip_row.count() > 1:
            item = self._chip_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, text in enumerate(t for t in texts if t):
            chip = QLabel(str(text))
            chip.setObjectName("deviceChip")
            self._chip_row.insertWidget(index, chip)

    def _on_shell_lost(self):
        if self._reboot_in_progress:
            # The reboot flow deliberately closed the Android stream and owns
            # the one reconnect attempt. Do not race it from ReaderThread.
            return
        if self._adb_restart_in_progress:
            # MainWindow will start exactly one recovery attempt after the
            # replacement daemon is confirmed.  Retrying here races USB
            # enumeration and produces repeated "device not found" output.
            return
        if self._session_closed or not self.handler:
            return
        import time

        # A transport that accepts the shell and drops it at once (unauthorized
        # or restricted IVI adbd) must not spin reconnect -> closed -> reconnect.
        now = time.monotonic()
        resets = [t for t in getattr(self, "_shell_resets", ()) if now - t < 30.0]
        if len(resets) >= 3:
            self._shell_resets = []
            self._wait_and_reconnect()
            return
        self._shell_resets = resets + [now]
        # ``get-state`` can block for seconds exactly when the device vanished,
        # so it never runs on the UI thread.
        handler = self.handler
        run_job(
            self._threads,
            handler.get_state,
            lambda state, h=handler: self._on_shell_lost_state(h, state),
            lambda _msg, h=handler: self._on_shell_lost_state(h, None),
        )

    def _on_shell_lost_state(self, handler, state):
        if (
            self._session_closed
            or handler is not self.handler
            or self._reboot_in_progress
            or self._adb_restart_in_progress
        ):
            return
        if state == "device" and hasattr(self, "shell"):
            self.log.emit("[INFO] Shell connection reset; restarting shell…")
            try:
                self.shell.reconnect()
            except Exception as exc:
                self.log.emit(f"[ERROR] shell reconnect: {exc}")
            return
        self._wait_and_reconnect()

    def _wait_and_reconnect(self, *, adb_restart=False, manual=False):
        from .qtutil import park_thread, thread_running

        if self._reconnecting or not self.handler or self._session_closed:
            return
        self._reconnecting = True
        self.btn_reconnect.hide()
        self.status.setText("Reconnecting… waiting for the device to come back")
        if manual:
            self.log.emit("[INFO] Reconnecting to the device…")
        elif adb_restart:
            self.log.emit("[INFO] ADB restarted — waiting for the USB transport to reappear…")
        else:
            self.log.emit("[WARNING] device went away (reboot/unplug) — waiting for it to come back…")
        self._enable_actions(False)
        previous = self._rc
        if thread_running(previous):
            # ``done`` is emitted just before run() returns; dropping the only
            # reference to a still-running QThread aborts Qt.
            park_thread(previous)
        quick = adb_restart or manual
        self._rc = _ReconnectThread(
            self.handler,
            timeout=25 if adb_restart else (60 if manual else 180),
            initial_delay=0.1 if quick else 3.0,
            poll_interval=0.5 if quick else 2.0,
        )
        self._rc.done.connect(self._on_reconnected)
        self._rc.start()

    def prepare_for_adb_restart(self):
        """Suspend retrying shell streams before their shared daemon is killed."""
        if not self.handler:
            return
        self._adb_restart_in_progress = True
        self.status.setText("Restarting local ADB server…")
        self._enable_actions(False)
        try:
            self.shell.pause_for_adb_restart()
        except Exception:
            pass

    def finish_adb_restart(self, success: bool):
        """Recover a paused shell once MainWindow has restarted the daemon."""
        if not self._adb_restart_in_progress:
            return
        self._adb_restart_in_progress = False
        if success:
            self._wait_and_reconnect(adb_restart=True)
            return
        self.status.setText("ADB restart failed — try Restart ADB again")
        self._enable_actions(True)

    def _on_reconnected(self, ok):
        self._reconnecting = False
        self._reboot_in_progress = False
        if self._session_closed:
            return
        if ok:
            name = self.handler.serial or self.session.get("name")
            self.status.setText(f"Connected — {name}")
            self._enable_actions(True)
            self.log.emit("[OK] device back online — reconnected")
            try:
                self.shell.reconnect()
            except Exception as exc:
                self.log.emit(f"[ERROR] shell reconnect: {exc}")
        else:
            self.status.setText("Device didn't come back (timed out)")
            self._offer_reconnect()
            self.log.emit(
                "[ERROR] device did not return to 'device' state (timed out). "
                "If it's in recovery/bootloader this is expected; otherwise replug and click Reconnect."
            )

    def _run_action(self, label, fn, on_ok, *, on_error=None):
        """Run one engine call off the UI thread and report its outcome.

        *fn* calls the engine with ``safe=True``.  A failed ``OperationResult``
        and a raised exception both go to *on_error* (default: an
        ``[ERROR] label: …`` log line); success passes the unwrapped value to
        *on_ok*.
        """

        def report(message):
            if on_error is not None:
                on_error(message)
            else:
                self.log.emit(f"[ERROR] {label}: {message}")

        def finished(result):
            error = self._action_error(result)
            if error:
                report(error)
            else:
                on_ok(self._action_value(result))

        run_job(self._threads, fn, finished, report)

    def _op(self, label, fn):
        if not self.handler:
            return
        handler = self.handler
        self.log.emit(f"{label}…")
        self._run_action(
            label,
            lambda: fn(handler),
            lambda value: self.log.emit(f"[OK] {label}: {value}"),
        )

    def _verity(self, enable):
        if not self.handler:
            return
        handler = self.handler
        label = "enable-verity" if enable else "disable-verity"

        def work():
            out = (handler.enable_verity if enable else handler.disable_verity)(safe=True)
            handler.shell("sync", safe=True)
            return out

        self.log.emit(f"{label}…")
        self._run_action(label, work, lambda value: self._after_verity(label, value))

    def _after_verity(self, label, value):
        self.log.emit(f"[OK] {label} (+ sync): {value}")
        if (
            QMessageBox.question(
                self,
                "Reboot required",
                f"{label} done and filesystem synced.\n\nA reboot is required for it to take effect. Reboot now?",
            )
            == QMessageBox.Yes
        ):
            self._start_reboot(None, confirm=False)

    def _on_connected(self, handler, info=None):
        if self._session_closed or self.handler is not None:
            # A queued result can arrive after the tab closed (or after a
            # retry already connected).  Never build panels for it; release
            # its transport instead.
            self._release_handler(handler)
            return
        self.handler = handler
        self.btn_reconnect.hide()
        quick = dict(info) if isinstance(info, dict) and info.get("_quick_identity") else {}
        quick.pop("_quick_identity", None)
        friendly = " ".join(
            bit for bit in (quick.get("manufacturer"), quick.get("model")) if bit
        ).strip()
        dev_name = friendly or self.session.get("name") or handler.serial or "android"
        self.status.setText(f"Connected — {dev_name}")
        self.title_label.setText(dev_name)
        self.log.emit(f"[OK] {self.session.get('name')}: connected")
        self._enable_actions(True)

        # Do not block the usable terminal on a complete `getprop` dump.  Some
        # vendor/IVI images take seconds to serve it right after USB comes up.
        # The quick identity above supplies a friendly first banner; remaining
        # details continue loading in the background.
        self.shell = ShellPanel(handler, device_name=dev_name, info=quick)
        self.shell.log.connect(self.log)
        self.shell.disconnected.connect(self._on_shell_lost)
        self.shell.adb_reboot_requested.connect(self._on_local_adb_reboot)
        for terminal in (
            self.shell.android_widget.term,
            self.shell.ps_widget.term,
            self.shell.cmd_widget.term,
        ):
            terminal.installEventFilter(self)
        if self._terminal_only:
            # No identity probe runs for a terminal-only tab: show the banner now.
            self.shell.android_widget.update_identity(quick)
            self._add_subtab(self.shell, "⌨", "Terminal")
            self._subtabs = {"shell": self.shell, "terminal": self.shell}
            if friendly:
                self.title_changed.emit(friendly)
            return
        self.logcat = LogcatPanel(handler)
        self.logcat.log.connect(self.log)
        self.files = FileBrowser(handler, start="/sdcard")
        self.files.log.connect(self.log)
        self.apps = AppsPanel(handler, automotive=self._automotive)
        self.apps.log.connect(self.log)
        self.combo_view = self._build_control_view(handler)
        self.webcam = CameraPanel()
        self.webcam.log.connect(self.log)
        # Loads call history and messages only when the tab is first shown.
        self.phone = PhonePanel(handler)
        self.phone.log.connect(self.log)

        self._add_subtab(self.shell, "⌨", "Terminal")
        self._add_subtab(self.logcat, "📜", "Logcat")
        self._add_subtab(self.files, "📁", "Files")
        self._add_subtab(self.combo_view, "🎛", "Device Control")
        self._add_subtab(self.apps, "📦", "Apps")
        self._add_subtab(self.phone, "📞", "Phone")
        self._add_subtab(self.webcam, "📹", "Webcam")

        self._subtabs = {
            "shell": self.shell,
            "terminal": self.shell,
            "logcat": self.logcat,
            "files": self.files,
            "mirror": self.combo_view,
            "controls": self.combo_view,
            "apps": self.apps,
            "phone": self.phone,
            "webcam": self.webcam,
        }

        if info is not None and not quick:
            # Retain the direct-call path used by tests and integrations, but
            # only after all widgets exist so it cannot delay first paint.
            self._on_device_info(handler, info)
            return

        if friendly:
            self.title_changed.emit(friendly)

        self._info_thread = _DeviceInfoThread(handler)
        self._info_thread.done.connect(
            lambda result, h=handler: self._on_device_info(h, result)
        )
        self._info_thread.finished.connect(self._info_thread.deleteLater)
        self._info_thread.start()

    def _on_device_info(self, handler, info) -> None:
        """Apply optional build identity without affecting transport readiness."""
        if handler is not self.handler:
            return

        if isinstance(info, OperationResult):
            if not info.success:
                self.log.emit(f"[INFO] Device details are still unavailable: {info.error}")
                return
            details = info.value
        else:
            details = info
        if not isinstance(details, dict):
            return

        details = dict(details)
        self._automotive = bool(details.get("automotive"))
        kind_label = details.get("kind_label") or ("Automotive" if self._automotive else "")
        kind_reason = details.get("kind_reason")
        self.log.emit(
            f"[OK] {details.get('manufacturer')} {details.get('model')} · "
            f"Android {details.get('android_version')} (SDK {details.get('sdk')}) · "
            f"{details.get('abi')}"
            + (f" · {kind_label}" if kind_label else "")
            + (f" (detected from {kind_reason})" if kind_reason else "")
        )
        version = details.get("android_version")
        self._set_chips(
            [
                f"Android {version}" if version else "",
                details.get("abi") or "",
                "Automotive" if self._automotive else "",
            ]
            + self._session_chips()
        )
        apps = getattr(self, "apps", None)
        if apps is not None:
            apps.set_automotive_default(self._automotive)
        mirror = getattr(self, "mirror_tab", None)
        if mirror is not None:
            mirror.set_automotive_default(self._automotive)
        if self._automotive:
            self._add_ivi_tab()

        # Keep the tab, welcome banner and shell prompt on the identity used at
        # opening time.  Hardware details remain visible in the activity log,
        # but do not rename the same live session underneath the user.
        if mirror is not None and details.get("display_size"):
            setter = getattr(mirror, "set_default_display_size", None)
            if callable(setter):
                setter(details["display_size"])
        if hasattr(self, "shell"):
            android = getattr(self.shell, "android_widget", None)
            if android is not None:
                update = getattr(android, "update_identity", None)
                if callable(update):
                    update(details)
                else:
                    android._info = details

    def _add_ivi_tab(self) -> None:
        """Expose automotive multi-display tools as a first-class tab once the
        device identity confirms it is an IVI/head unit."""
        if getattr(self, "_ivi_hub", None) is not None:
            return
        mirror = getattr(self, "mirror_tab", None)
        if mirror is None:
            return
        hub = QWidget()
        layout = QVBoxLayout(hub)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        title = QLabel("IVI displays")
        title.setObjectName("iviPreviewTitle")
        layout.addWidget(title)
        description = QLabel(
            "This automotive device can expose a cluster, centre stack, passenger "
            "screen and other independent displays. Open the wall to see all detected "
            "displays together and control, maximise, capture or record each one."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        open_wall = QPushButton("▦ Open IVI display wall")
        open_wall.setProperty("role", "ok")
        open_wall.setToolTip("Show all detected IVI displays with per-display actions")
        open_wall.clicked.connect(mirror.open_ivi_view)
        layout.addWidget(open_wall)
        scan = QPushButton("↻ Refresh detected displays")
        scan.setProperty("role", "ghost")
        scan.clicked.connect(mirror.refresh_displays)
        layout.addWidget(scan)
        note = QLabel("Display discovery runs in the background and never starts mirroring by itself.")
        note.setObjectName("settingsHint")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        self._ivi_hub = hub
        self._add_subtab(hub, "▦", "IVI Displays")
        self._subtabs["ivi"] = hub

    def _add_subtab(self, widget, emoji, label):
        # The glyph is kept for split-view pane titles; the tab itself shows the
        # section's colour-coded vector icon.
        self._subtab_meta[widget] = (emoji, label)
        idx = self.inner.indexOf(widget)
        if idx < 0:
            idx = self.inner.addTab(widget, label)
        else:
            self.inner.setTabText(idx, label)
        glyph, tone = _SECTION_ICONS.get(label, ("apps", None))
        self.inner.setTabIcon(idx, icons.icon(glyph, tone))
        return idx

    def _split_choices(self):
        """Return persistent workspace pages available for the current device."""
        tabs = getattr(self, "_subtabs", {})
        return [
            (key, icon, label)
            for key, (icon, label) in _SPLIT_VIEW_META.items()
            if tabs.get(key) is not None
        ]

    def _configure_split(self, count, orientation=None):
        """Choose two or four distinct existing pages for a split workspace."""
        choices = self._split_choices()
        if len(choices) < count:
            QMessageBox.information(
                self,
                "Split view",
                "Connect a full device session before arranging a split view.",
            )
            return

        names = "Four-pane grid" if count == 4 else (
            "Two panes — side by side" if orientation == Qt.Horizontal
            else "Two panes — top and bottom"
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("Split view")
        dlg.setMinimumWidth(360)
        outer = QVBoxLayout(dlg)
        hint = QLabel(
            f"{names}\nChoose distinct views. The panes share this one device session."
        )
        hint.setWordWrap(True)
        hint.setObjectName("settingsHint")
        outer.addWidget(hint)
        form = QFormLayout()
        combos = []
        positions = ("Left", "Right") if count == 2 and orientation == Qt.Horizontal else (
            ("Top", "Bottom") if count == 2 else ("Top left", "Top right", "Bottom left", "Bottom right")
        )
        for index, position in enumerate(positions):
            combo = QComboBox()
            for key, icon, label in choices:
                combo.addItem(f"{icon}  {label}", key)
            combo.setCurrentIndex(index % len(choices))
            form.addRow(f"{position}:", combo)
            combos.append(combo)
        outer.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        outer.addWidget(buttons)
        if dlg.exec_() != QDialog.Accepted:
            return
        keys = [combo.currentData() for combo in combos]
        if len(set(keys)) != len(keys):
            QMessageBox.warning(
                self,
                "Split view",
                "Choose a different view for every pane. A page cannot be shown twice in one session.",
            )
            return
        self._activate_split(keys, orientation)

    def _make_split_pane(self, key, widget):
        icon, label = _SPLIT_VIEW_META[key]
        pane = QFrame()
        pane.setObjectName("splitPane")
        # A File browser has a large preferred size; split mode must still
        # start balanced and let the user choose where the space goes.
        pane.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        title = QLabel(f"  {icon}  {label}")
        title.setObjectName("splitPaneTitle")
        layout.addWidget(title)
        layout.addWidget(widget, 1)
        # QTabWidget.removeTab() deliberately hides its page.  Reparenting it
        # into the splitter does not undo that flag, which produced labelled
        # but empty panes.  Make the existing page visible again; no view or
        # worker is recreated.
        widget.show()
        return pane

    def _even_split_sizes(self):
        """Reset each active splitter to equal usable space, recursively."""
        root = self._split_root
        if root is None:
            return

        def even(splitter):
            count = splitter.count()
            if count < 1:
                return
            available = (
                splitter.width() if splitter.orientation() == Qt.Horizontal
                else splitter.height()
            )
            splitter.setSizes([max(1, available // count)] * count)
            for index in range(count):
                child = splitter.widget(index)
                if isinstance(child, QSplitter):
                    even(child)

        even(root)

    def _activate_split(self, keys, orientation=None):
        """Move selected persistent pages into a two-pane or four-pane layout."""
        if not self.handler:
            return
        tabs = getattr(self, "_subtabs", {})
        if any(key not in _SPLIT_VIEW_META or tabs.get(key) is None for key in keys):
            return
        if len(keys) not in (2, 4) or len(set(keys)) != len(keys):
            return
        if self._split_keys:
            self._leave_split()

        panes = {}
        for key in keys:
            widget = tabs[key]
            idx = self.inner.indexOf(widget)
            if idx >= 0:
                self.inner.removeTab(idx)
            panes[key] = self._make_split_pane(key, widget)

        if len(keys) == 2:
            root = QSplitter(orientation or Qt.Horizontal)
            for key in keys:
                root.addWidget(panes[key])
            root.setStretchFactor(0, 1)
            root.setStretchFactor(1, 1)
        else:
            root = QSplitter(Qt.Vertical)
            top = QSplitter(Qt.Horizontal)
            bottom = QSplitter(Qt.Horizontal)
            for key in keys[:2]:
                top.addWidget(panes[key])
            for key in keys[2:]:
                bottom.addWidget(panes[key])
            for row in (top, bottom):
                row.setChildrenCollapsible(False)
                row.setHandleWidth(8)
                row.setStretchFactor(0, 1)
                row.setStretchFactor(1, 1)
            root.addWidget(top)
            root.addWidget(bottom)
            root.setStretchFactor(0, 1)
            root.setStretchFactor(1, 1)

        root.setObjectName("sessionSplitter")
        root.setChildrenCollapsible(False)
        root.setHandleWidth(8)
        self._split_layout.addWidget(root, 1)
        self._split_root = root
        self._split_keys = tuple(keys)
        self._split_panes = panes
        self._content_stack.setCurrentWidget(self._split_workspace)
        for key in keys:
            # Explicitly showing after the stack switches also covers pages
            # that were hidden while a different normal tab was active.
            tabs[key].show()
        self._split_menu.setTitle(f"Split view ({len(keys)} panes)")
        self.btn_restore_split.show()
        # The tab row (and its corner actions) is hidden in split view; keep
        # the actions, including Return to tabs, above the panes.
        if self._split_layout.indexOf(self._device_actions) < 0:
            self._split_layout.insertWidget(0, self._device_actions, 0, Qt.AlignRight)
        self._device_actions.show()
        # QSplitter starts from child size hints, which makes a File browser or
        # Terminal consume the workspace.  Equalise after the live layout has
        # dimensions; users can then drag the wider handles as usual.
        QTimer.singleShot(0, self._even_split_sizes)
        QTimer.singleShot(120, self._even_split_sizes)

        self.set_workspace_active(True)
        self._schedule_embed_check()

    def _schedule_embed_check(self):
        """Split view moves Device Control between parents; make sure the
        embedded native scrcpy window is still attached and sized."""
        mirror = getattr(self, "mirror_tab", None)
        if mirror is not None and hasattr(mirror, "ensure_embedded"):
            QTimer.singleShot(0, mirror.ensure_embedded)

    def _restore_tab_order(self):
        """Restore the normal, predictable page order after a split is closed."""
        tabs = getattr(self, "_subtabs", {})
        desired = []
        for key in ("shell", "logcat", "files", "controls", "apps", "phone", "webcam", "ivi"):
            widget = tabs.get(key)
            if widget is not None and widget not in desired:
                desired.append(widget)
        for widget in desired:
            idx = self.inner.indexOf(widget)
            if idx >= 0:
                self.inner.removeTab(idx)
        for widget in desired:
            icon, label = self._subtab_meta.get(widget, ("", "Page"))
            self._add_subtab(widget, icon, label)

    def _leave_split(self, *_args, show_name=None):
        """Put every moved page back in the normal tab strip without recreation."""
        if not self._split_keys:
            return
        tabs = getattr(self, "_subtabs", {})
        for key in self._split_keys:
            widget = tabs.get(key)
            if widget is not None:
                widget.setParent(self.inner)
        root = self._split_root
        if root is not None:
            self._split_layout.removeWidget(root)
            root.setParent(None)
            root.deleteLater()
        self._split_root = None
        self._split_keys = ()
        self._split_panes = {}
        self._restore_tab_order()
        self._content_stack.setCurrentWidget(self.inner)
        self._split_menu.setTitle("Split view")
        self.btn_restore_split.hide()
        if self._split_layout.indexOf(self._device_actions) >= 0:
            self._split_layout.removeWidget(self._device_actions)
        self.inner.setCornerWidget(self._device_actions, Qt.TopRightCorner)
        self._device_actions.show()
        if show_name:
            target = tabs.get(show_name)
            if target is not None:
                self.inner.setCurrentWidget(target)
        self._schedule_embed_check()

    def _focus_split_view(self, key):
        """Apply the same focus policy as a normal tab when its pane is requested."""
        if key == "shell" and hasattr(self, "shell"):
            QTimer.singleShot(0, self.shell.focus_terminal)
        elif key == "controls":
            mirror = getattr(self, "mirror_tab", None)
            if mirror is not None and hasattr(mirror, "_resume_max"):
                mirror._resume_max()

    def _build_control_view(self, handler):
        from .device_commands import DeviceCommandDispatcher

        if self._device_dispatcher is None:
            self._device_dispatcher = DeviceCommandDispatcher()

        def card(title, inner, object_name):
            # One surface per region: the page's own widgets sit directly on
            # it instead of inside a second nested frame.
            frame = QFrame()
            frame.setObjectName(object_name)
            fv = QVBoxLayout(frame)
            fv.setContentsMargins(1, 1, 1, 1)
            fv.setSpacing(0)
            if title:
                cap = QLabel(title)
                cap.setObjectName("cardTitle")
                fv.addWidget(cap)
            fv.addWidget(inner, 1)
            return frame

        self.mirror_tab = MirrorPanel(
            handler,
            self.session,
            automotive=self._automotive,
            # Keep the screen in Device Control by default. Users can still
            # choose the dedicated native scrcpy window from Screen when a
            # device or remote desktop session does not embed cleanly.
            prefer_embed=True,
            dispatcher=self._device_dispatcher,
        )
        self.mirror_tab.log.connect(self.log)
        self.mirror_tab.video_active_changed.connect(self.screen_active)
        self.controls = ControlsPanel(
            handler,
            compact=True,
            on_reboot=lambda: self.reboot(None),
            dispatcher=self._device_dispatcher,
        )
        self.controls.log.connect(self.log)
        m_card = card("", self.mirror_tab, "mirrorView")
        m_card.setMinimumWidth(340)
        c_card = card("Device controls", self.controls, "sidePanel")
        c_card.setMinimumWidth(280)

        btn_tog = QPushButton("Hide controls")
        btn_tog.setProperty("role", "ghost")
        btn_tog.setIcon(icons.icon("sliders", "purple"))
        btn_tog.setToolTip("Show or hide the device-controls panel")

        def toggle_device_controls():
            visible = c_card.isVisible()
            c_card.setVisible(not visible)
            btn_tog.setText("Show controls" if visible else "Hide controls")
            self.mirror_tab._fit()

        btn_tog.clicked.connect(toggle_device_controls)
        self._device_controls_card = c_card
        self._device_controls_toggle = btn_tog
        self.mirror_tab.add_toolbar_widget(btn_tog)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(6)
        split.addWidget(m_card)
        split.addWidget(c_card)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([700, 320])
        split.setChildrenCollapsible(False)

        outer = QWidget()
        ov = QVBoxLayout(outer)
        ov.setContentsMargins(8, 8, 8, 8)
        ov.addWidget(split)
        return outer

    def _on_fail(self, msg):
        if self._session_closed:
            return
        # Let Reconnect (or a later start_connect) try again.
        self._started_connect = False
        self.btn_reconnect.show()
        self.status.setText("Connect failed")
        self.log.emit(f"[ERROR] {self.session.get('name')}: {msg}")
        QMessageBox.warning(self, "Connect failed", msg)

    def save_active_output(self):
        if not self.handler:
            QMessageBox.information(self, "Save output", "Connect a device first.")
            return
        w = self.inner.currentWidget()
        if isinstance(w, ShellPanel):
            w.term._save_output()
        elif isinstance(w, LogcatPanel):
            w._save()
        else:
            QMessageBox.information(
                self,
                "Save output",
                "Switch to the Shell or Logcat tab, then Save to write its full output to a file.",
            )

    def show_subtab(self, name: str):
        w = getattr(self, "_subtabs", {}).get(name)
        if self.handler and w is not None:
            # Ribbon shortcuts continue to work while split mode is active.  A
            # selected pane is focused in place; selecting another page returns
            # to the familiar single-tab view instead of silently opening a
            # duplicate page or ADB connection.
            if self._split_keys:
                canonical = next(
                    (
                        key for key in _SPLIT_VIEW_META
                        if getattr(self, "_subtabs", {}).get(key) is w
                    ),
                    None,
                )
                if canonical in self._split_keys:
                    self._focus_split_view(canonical)
                    return
                self._leave_split(show_name=name)
            self.inner.setCurrentWidget(w)

    def _on_subtab_changed(self, *_):
        w = self.inner.currentWidget()
        sh = getattr(self, "shell", None)
        if sh is not None and w is sh:
            QTimer.singleShot(0, sh.focus_terminal)
        self.set_workspace_active(self.isVisible())

    def set_workspace_active(self, active: bool) -> None:
        """Apply Device Control's max view only while it is actually on screen.

        MainWindow calls this when the top-level tab changes, so docks hidden
        by one device's max view are restored for every other tab.
        """
        mirror = getattr(self, "mirror_tab", None)
        if mirror is None or not hasattr(mirror, "act_max"):
            return
        if self._split_keys:
            controls_shown = "controls" in self._split_keys
        else:
            controls_shown = self.inner.currentWidget() is getattr(self, "combo_view", None)
        if active and controls_shown:
            mirror._resume_max()
        else:
            mirror._suspend_max()
            mirror.yield_keyboard()

    def eventFilter(self, watched, event):
        """Let any terminal take focus away from an embedded screen immediately."""
        if event.type() == QEvent.FocusIn:
            shell = getattr(self, "shell", None)
            if shell is not None and watched in (
                shell.android_widget.term,
                shell.ps_widget.term,
                shell.cmd_widget.term,
            ):
                mirror = getattr(self, "mirror_tab", None)
                if mirror is not None:
                    mirror.yield_keyboard()
        return super().eventFilter(watched, event)

    def mirror(self, display_id="__use_combo__", compat=None, embed=None):
        """Start the screen.  Unspecified options follow Device Control: the
        selected display, the IVI compatibility default and the embed choice."""
        if not self.handler:
            return
        if hasattr(self, "combo_view"):
            self.show_subtab("controls")
        if hasattr(self, "mirror_tab") and self.mirror_tab:
            self.mirror_tab.start(display_id=display_id, compat=compat, embed=embed)

    def mirror_choose_display(self):
        if not self.handler:
            return
        try:
            if hasattr(self, "combo_view") and self.combo_view:
                self.show_subtab("controls")
        except Exception:
            pass
        if hasattr(self, "mirror_tab") and self.mirror_tab:
            self.mirror_tab._show_display_manager()

    def screenshot(self):
        if not self.handler:
            return
        mirror = getattr(self, "mirror_tab", None)
        if mirror is not None and hasattr(mirror, "_take_screenshot"):
            # One capture flow for the header, ribbon and Device Control: it
            # honours the selected display and offers to open the saved file.
            mirror._take_screenshot()
            return
        # Terminal-only tabs have no Device Control panel.
        import time as _t
        from .fileutil import download_path

        default = download_path("screenshot-" + _t.strftime("%Y%m%d-%H%M%S") + ".png")
        path, _ = QFileDialog.getSaveFileName(self, "Save screenshot", default, "PNG (*.png)")
        if not path:
            return
        handler = self.handler
        self._run_action(
            "screenshot",
            lambda: handler.screenshot(path, safe=True),
            lambda p: self.log.emit(f"[OK] screenshot saved: {p}"),
        )

    def show_health(self):
        if not self.handler:
            return
        handler = self.handler
        self.log.emit("reading device health…")
        self._run_action("health", lambda: handler.health_report(safe=True), self._show_health_dialog)

    def show_build(self):
        if not self.handler:
            return
        handler = self.handler
        self.log.emit("reading build report…")
        self._run_action(
            "build report", lambda: handler.build_report(safe=True), self._show_build_dialog
        )

    def _show_health_dialog(self, text):
        from .report_dialog import show_report_dialog

        show_report_dialog(
            self,
            "Device health report",
            text,
            "health-report",
            "Includes a readable summary and the raw battery, memory, CPU, and uptime dumps.",
        )

    def _show_build_dialog(self, text):
        from .report_dialog import show_report_dialog

        show_report_dialog(
            self,
            "Device build report",
            text,
            "build-report",
            "The summary is suitable for viewing or PNG export; the complete Android property dump is kept separately.",
        )

    def go_wireless(self):
        if not self.handler:
            return
        if (
            QMessageBox.question(
                self,
                "Go wireless",
                "Switch this device from USB to Wi-Fi?\n\nTurboADB will read the "
                "device's IP, run 'adb tcpip', and connect to it — afterwards you "
                "can unplug the cable. The device must be on the same network as "
                "this PC.",
            )
            != QMessageBox.Yes
        ):
            return
        self.log.emit("switching device to wireless (USB → Wi-Fi)…")
        handler = self.handler
        self._run_action(
            "go wireless",
            lambda: handler.go_wireless(safe=True),
            lambda s: self.log.emit(
                f"[OK] now reachable wirelessly at {s} — the USB cable can be "
                f"unplugged. Save it from Connect → Network to reconnect later."
            ),
        )

    def capture_bugreport(self):
        if not self.handler:
            return
        import time as _t
        from .fileutil import download_path

        default = download_path(_t.strftime("bugreport-%Y%m%d-%H%M%S.zip"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save bugreport", default, "Zip (*.zip);;All files (*)"
        )
        if not path:
            return
        self.log.emit("capturing bugreport (this takes a few minutes)…")
        handler = self.handler
        self._run_action(
            "bugreport",
            lambda: handler.bugreport(path, safe=True),
            lambda p: self.log.emit(f"[OK] bugreport saved: {p}"),
        )

    @staticmethod
    def _action_value(result):
        return result.value if isinstance(result, OperationResult) else result

    @staticmethod
    def _action_error(result) -> str | None:
        """Return a useful failure message for a worker result, if any."""
        if isinstance(result, OperationResult):
            if not result.success:
                return str(result.error or "operation failed")
            result = result.value
        if result is False:
            return "ADB reported that the command did not succeed"
        if hasattr(result, "ok") and not result.ok:
            return str(getattr(result, "stderr", "") or "ADB command failed")
        return None

    def reboot(self, mode=None):
        """Confirm a reboot, then delegate to the one recovery-aware flow."""
        if not self.handler or self._reboot_in_progress:
            return
        label = mode or "system"
        if mode in ("bootloader", "sideload"):
            warn = (
                f"Reboot to {label.upper()}?\n\n"
                f"On Android Automotive / IVI head units this is risky: many "
                f"have no on-screen {label} UI and no hardware buttons, so the "
                f"unit can get STUCK with no easy way back. Only continue if you "
                f"know this device exposes {label} and how to recover it."
            )
            if self._automotive:
                warn = "⚠ AUTOMOTIVE DEVICE\n\n" + warn
            if (
                QMessageBox.warning(
                    self,
                    f"Reboot to {label} — risky",
                    warn,
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
        elif (
            QMessageBox.question(self, "Reboot", f"Reboot device to {label}?")
            != QMessageBox.Yes
        ):
            return
        self._start_reboot(mode, confirm=False)

    def _start_reboot(self, mode=None, *, confirm=False):
        """Send reboot once and recover the same terminal the user was using."""
        if not self.handler or self._reboot_in_progress:
            return
        if confirm:
            self.reboot(mode)
            return
        label = mode or "system"
        self._reboot_in_progress = True
        self.status.setText(f"Rebooting to {label}…")
        self._enable_actions(False)
        self.log.emit(f"[INFO] rebooting to {label}…")
        handler = self.handler
        self._run_action(
            "reboot",
            lambda: handler.reboot(mode, safe=True),
            lambda _value, m=mode: self._on_reboot_sent(m),
            on_error=self._on_reboot_failed,
        )

    def _on_reboot_failed(self, message: str) -> None:
        self._reboot_in_progress = False
        self.status.setText("Connected — reboot was not sent")
        self._enable_actions(True)
        self.log.emit("[ERROR] reboot: " + message)

    def _on_reboot_sent(self, mode) -> None:
        label = mode or "system"
        self.log.emit(f"[OK] reboot command accepted — entering {label}…")
        self._begin_reboot_recovery(mode)

    def _on_local_adb_reboot(self, mode) -> None:
        """Recover when a user explicitly types ``adb reboot`` in a local tab."""
        if not self.handler or self._reboot_in_progress:
            return
        label = mode or "system"
        self._reboot_in_progress = True
        self.status.setText(f"Rebooting to {label}…")
        self._enable_actions(False)
        self.log.emit(
            f"[INFO] local terminal sent adb reboot{(' ' + str(mode)) if mode else ''}; "
            "preparing shell recovery…"
        )
        self._begin_reboot_recovery(mode)

    def _begin_reboot_recovery(self, mode) -> None:
        """Suspend the Android stream and recover normal system reboots once."""
        label = mode or "system"
        try:
            self.shell.pause_for_device_reboot()
        except Exception as exc:
            self.log.emit(f"[WARNING] could not pause Android shell cleanly: {exc}")

        if mode is None:
            # Give adbd time to actually leave before considering the device
            # ready. This avoids reconnecting to the old pre-reboot transport.
            self._wait_and_reconnect()
            return

        # Recovery / bootloader / sideload intentionally do not return an ADB
        # shell immediately. Keeping actions disabled is safer than a false
        # "connected" state; the user can reconnect once normal Android booted.
        self._reboot_in_progress = False
        self.status.setText(f"Rebooted to {label} — reconnect after Android is ready")
        self._offer_reconnect()
        self.log.emit(
            f"[INFO] {label} does not provide a normal Android shell; "
            "click Reconnect after booting Android normally."
        )

    def close_session(self):
        from .qtutil import park_thread, thread_running

        if self._session_closed:
            return
        self._session_closed = True
        pending_workers = []

        # Keep ownership simple while the persistent child panels stop their
        # workers.  This does not recreate anything; it only detaches split
        # wrappers before their real pages are closed below.
        self._leave_split()

        # These workers may have already finished and deleted themselves
        # (finished -> deleteLater), so every Qt call is guarded; one stale
        # wrapper must not abort the rest of the teardown.
        for attr in ("_ct", "_rc", "_info_thread"):
            t = getattr(self, attr, None)
            if t is None:
                continue
            try:
                if hasattr(t, "cancel"):
                    t.cancel()
            except RuntimeError:
                pass
            for sig in ("ok", "fail", "log", "done"):
                try:
                    getattr(t, sig).disconnect()
                except Exception:
                    pass
            # Park only live workers: a never-started thread (e.g. a connect
            # the tab closed before) never emits ``finished`` and would stay
            # referenced forever.
            if thread_running(t):
                park_thread(t)
                pending_workers.append(t)
        # Action jobs: collect the live ones, then detach and park them all.
        pending_workers.extend(t for t in self._threads if thread_running(t))
        close_jobs(self._threads)

        for attr in (
            "shell", "logcat", "files", "apps", "controls",
            "mirror_tab", "webcam", "phone",
        ):
            p = getattr(self, attr, None)
            if p is not None:
                try:
                    p.close_panel()
                except Exception:
                    pass
        mirror = getattr(self, "mirror_tab", None)
        if mirror is not None and hasattr(mirror, "shutdown_threads"):
            try:
                pending_workers.extend(mirror.shutdown_threads())
            except RuntimeError:
                pass
        if self._device_dispatcher is not None:
            self._device_dispatcher.stop()
            if thread_running(self._device_dispatcher):
                park_thread(self._device_dispatcher)
                pending_workers.append(self._device_dispatcher)
        if self.handler:
            handler = self.handler
            pending_workers = list(dict.fromkeys(pending_workers))
            if pending_workers:
                def disconnect_after_workers():
                    for worker in pending_workers:
                        try:
                            worker.wait()
                        except RuntimeError:
                            pass
                    return handler.disconnect()

                disconnect_thread = _ActionThread(disconnect_after_workers)
                park_thread(disconnect_thread)
                disconnect_thread.start()
            else:
                try:
                    handler.disconnect()
                except Exception:
                    pass

    def closeEvent(self, event):
        self.close_session()
        super().closeEvent(event)
