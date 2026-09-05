"""A per-device tab: connects in the background, then exposes an interactive
Shell, a live Logcat viewer, a file browser, and an app manager — plus quick
Mirror (scrcpy), Screenshot, and Reboot actions in a header bar."""

from __future__ import annotations

import os
import shlex

from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QTabWidget, QFileDialog, QMenu, QToolButton,
    QMessageBox
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


import re
import unicodedata

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


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
    clean = ANSI_RE.sub("", s)
    return sum(_char_width(c) for c in clean)


def _render_mobaxterm_banner(
    header_title: str,
    header_sub: str,
    session_title: str,
    items: list[tuple[str, str, bool]],
    min_width: int = 61,
    cwd: str = "/",
) -> str:
    """Render a MobaXterm-style professional terminal welcome banner.

    Exact 1:1 visual match to MobaXterm:
      - Solid unbroken white/light-gray top and bottom borders
      - Centered title in bright green and subtitle in yellow
      - Blank separation line
      - '➤ Session to ...' with target highlighted in magenta and OS in red
      - Aligned sub-items with white labels, colons at column 19, and green checkmarks (✔)
      - Exact column width calculated across all lines for clean box closure
    """
    max_lbl_len = max(len(lbl) for lbl, _, _ in items) if items else 0
    formatted_items = []
    for lbl, val, has_check in items:
        pad_lbl = lbl.ljust(max_lbl_len)
        chk = "\x1b[1;92m\u2714\x1b[0m" if has_check else " "
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
        l = pad // 2
        r = pad - l
        return "\x1b[37m│\x1b[0m" + (" " * l) + s + (" " * r) + "\x1b[37m│\x1b[0m"

    def left_row(s, indent=1):
        sw = _str_width(s)
        rem = max(0, inner_w - indent - sw)
        return "\x1b[37m│\x1b[0m" + (" " * indent) + s + (" " * rem) + "\x1b[37m│\x1b[0m"

    l1 = center(header_title)
    l2 = center(header_sub)
    sess_line = left_row(f"➤ {session_title}", indent=1)
    sub_lines = [left_row(it, indent=3) for it in formatted_items]

    lines = [top, l1, l2, blank, sess_line] + sub_lines + [bot]

    import datetime
    now = datetime.datetime.now()
    d_s = now.strftime("%m-%d")
    t_s = now.strftime("%H:%M")
    clean_cwd = cwd or "/"
    arrow = "\u25b6"
    prompt_bar = (
        f"\x1b[30;46m 📅 {d_s} "
        f"\x1b[36;42m{arrow}"
        f"\x1b[30;42m 🕒 {t_s} "
        f"\x1b[32;43m{arrow}"
        f"\x1b[30;43m 📁 {clean_cwd} "
        f"\x1b[33;49m{arrow}\x1b[0m "
    )

    return "\n" + "\n".join(lines) + "\n\n" + prompt_bar


def _render_box_banner(title: str, lines: list[str], min_width: int = 74) -> str:
    """Render a clean, fully enclosed ASCII box banner with ANSI styling."""
    all_content = [title] + [f"  {l}" for l in lines]
    max_c = max(_str_width(x) for x in all_content)
    width = max(min_width, max_c + 4)
    inner_w = width - 2

    top = f"\x1b[90m┌" + ("─" * inner_w) + "┐\x1b[0m"
    bot = f"\x1b[90m└" + ("─" * inner_w) + "┘\x1b[0m"
    blank = f"\x1b[90m│" + (" " * inner_w) + "│\x1b[0m"

    def center(s):
        sw = _str_width(s)
        pad = max(0, inner_w - sw)
        l = pad // 2
        r = pad - l
        return f"\x1b[90m│\x1b[0m" + (" " * l) + s + (" " * r) + "\x1b[90m│\x1b[0m"

    def left_row(s, indent=2):
        sw = _str_width(s)
        rem = max(0, inner_w - indent - sw)
        return f"\x1b[90m│\x1b[0m" + (" " * indent) + s + (" " * rem) + "\x1b[90m│\x1b[0m"

    body = [center(title), blank] + [left_row(l, indent=2) for l in lines]
    return "\n" + "\n".join([top] + body + [bot]) + "\n"


def config_from_session(s: dict) -> ADBConfig:
    st = settings_mod.load()
    adb_path = st.get("adb_path") or None
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

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        try:
            h = ADBHandler(
                self.cfg,
                safe=True,
                log_callback=lambda m: self.log.emit(m),
            )
            res = h.connect()
            if isinstance(res, OperationResult) and not res.success:
                self.fail.emit(str(res.error))
                return
            info = h.device_info(safe=True)
            self.ok.emit(h, info)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _ActionThread(QThread):
    done = pyqtSignal(str)
    fail = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(str(self.fn()))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _ReconnectThread(QThread):
    """Wait for the device to come back and be fully shell-ready after a reboot."""
    done = pyqtSignal(bool)

    def __init__(self, handler, timeout=180):
        super().__init__()
        self.handler = handler
        self.timeout = timeout

    def run(self):
        import time
        from ..results import OperationResult

        time.sleep(3)  # let it actually go down first
        deadline = time.time() + self.timeout
        host = getattr(getattr(self.handler, "config", None), "host", None)
        port = getattr(getattr(self.handler, "config", None), "port", 5555)

        while time.time() < deadline:
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
            time.sleep(2.0)
        self.done.emit(False)


class _PromptThread(QThread):
    """Fetch real device name and root state without blocking the UI."""
    ready = pyqtSignal(str, bool)

    def __init__(self, handler, device_name, info=None):
        super().__init__()
        self.handler = handler
        self.device_name = device_name
        self._info = info or {}

    def run(self):
        host = self._info.get("device") or self._info.get("model") or self.device_name
        root = False
        if not host:
            try:
                r = self.handler.shell("getprop ro.product.device", safe=False, timeout=3.0)
                if r.ok and r.text.strip():
                    host = r.text.strip()
            except Exception:
                pass
        try:
            r = self.handler.shell("id -u", safe=False, timeout=3.0)
            root = r.ok and r.text.strip() == "0"
        except Exception:
            pass
        self.ready.emit(host or "android", root)


class _LocalShellWidget(QWidget):
    """Dedicated interactive terminal tab for local PowerShell or CMD."""
    log = pyqtSignal(str)

    def __init__(self, shell_type: str = "powershell", serial: str = None, parent=None):
        super().__init__(parent)
        self.shell_type = shell_type.lower()
        self.serial = serial
        self.session = None
        self.reader = None
        self._closing = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        row = QHBoxLayout()
        stop = QPushButton(" Stop")
        stop.setProperty("role", "danger")
        stop.setIcon(theme.emoji_icon("⏹"))
        stop.setToolTip("Stop / interrupt running command (Ctrl+C)")
        stop.clicked.connect(self.interrupt)

        clr = QPushButton(" Clear")
        clr.setProperty("role", "ghost")
        clr.setIcon(theme.emoji_icon("🧹"))

        paste = QPushButton(" Paste")
        paste.setProperty("role", "ghost")
        paste.setIcon(theme.emoji_icon("📥"))
        paste.clicked.connect(lambda: self.term.paste_clipboard())

        copy = QPushButton(" Copy")
        copy.setProperty("role", "ghost")
        copy.setIcon(theme.emoji_icon("📋"))
        copy.clicked.connect(lambda: self.term.copy())

        save = QPushButton(" Save…")
        save.setProperty("role", "ghost")
        save.setIcon(theme.emoji_icon("💾"))
        save.clicked.connect(lambda: self.term._save_output())

        reopen = QPushButton(" Restart")
        reopen.setProperty("role", "ghost")
        reopen.setIcon(theme.emoji_icon("🔄"))
        reopen.clicked.connect(self.reopen)

        row.addWidget(stop)
        row.addWidget(copy)
        row.addWidget(paste)
        row.addWidget(save)
        row.addWidget(clr)
        row.addWidget(reopen)
        row.addStretch(1)

        lbl = "⚡ PowerShell" if self.shell_type == "powershell" else "💻 Command Prompt"
        row.addWidget(QLabel(f"{lbl} • ANDROID_SERIAL={serial or 'auto'}"))
        lay.addLayout(row)

        self._shell_cwd = os.path.expanduser("~")

        self.term = AnsiConsole(send_fn=self._send)
        self.term.set_emulate_prompt(False)
        self.term.set_completion_fn(self._local_complete)
        self.term.set_prompt_provider_fn(self._get_prompt)
        self.term.set_interrupt_fn(self.interrupt)
        clr.clicked.connect(self.term.clear)
        lay.addWidget(self.term, 1)

        self._started = False
        self._in_adb_shell = False
        self._strip_startup_banner = True

    def _get_prompt(self) -> str:
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

    @classmethod
    def _get_path_commands(cls) -> set[str]:
        import time as _t
        now = _t.time()
        if cls._PATH_CACHE and (now - cls._PATH_CACHE_TIME) < 30.0:
            return cls._PATH_CACHE
        cmds = set()
        pathext = tuple(e.lower() for e in os.environ.get("PATHEXT", ".exe;.bat;.cmd;.ps1").split(";") if e)
        for p in os.environ.get("PATH", "").split(os.pathsep):
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

        # 1. Command completion (completing the first token)
        if len(tokens) == 1 and not trailing_space:
            prefix = tokens[0].lower()
            matches = {c for c in builtins if c.lower().startswith(prefix)}

            # Look in cached PATH
            path_cmds = self._get_path_commands()
            for cmd_name in path_cmds:
                if cmd_name.startswith(prefix):
                    matches.add(cmd_name)

            # Look in current working directory
            if os.path.isdir(self._shell_cwd):
                try:
                    pathext = tuple(e.lower() for e in os.environ.get("PATHEXT", ".exe;.bat;.cmd;.ps1").split(";") if e)
                    with os.scandir(self._shell_cwd) as it:
                        for entry in it:
                            if entry.name.lower().startswith(prefix):
                                base, ext = os.path.splitext(entry.name)
                                if ext.lower() in pathext:
                                    matches.add(base)
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
            return None, formatted_matches
        return None, formatted_matches

    def ensure_started(self):
        if not self._started:
            self._started = True
            self._start_session()

    def focus_terminal(self):
        self.ensure_started()
        self.term.setFocus(Qt.OtherFocusReason)

    def _start_session(self):
        from .local_terminal import LocalShellSession

        try:
            self.session = LocalShellSession(self.shell_type, serial=self.serial, cwd=self._shell_cwd)
            from .. import __version__
            header_title = f"\x1b[1;92m•  TurboADB Professional v{__version__}  •\x1b[0m"
            if self.shell_type == "powershell":
                header_sub = "\x1b[93m(PowerShell terminal, ADB tools and environment)\x1b[0m"
                session_title = "\x1b[37mLocal session to \x1b[1;35mPowerShell\x1b[0m  \x1b[37m(\x1b[91m@Windows\x1b[37m)\x1b[0m"
                items = [
                    ("Platform-tools", "", True),
                    ("Local-terminal", "(ANSI cooked mode is enabled)", True),
                ]
            else:
                header_sub = "\x1b[93m(Command Prompt terminal, ADB tools and environment)\x1b[0m"
                session_title = "\x1b[37mLocal session to \x1b[1;35mCommand Prompt\x1b[0m  \x1b[37m(\x1b[91m@Windows\x1b[37m)\x1b[0m"
                items = [
                    ("Platform-tools", "", True),
                    ("Local-terminal", "(ANSI cooked mode is enabled)", True),
                ]
            banner = _render_mobaxterm_banner(header_title, header_sub, session_title, items, cwd=self._shell_cwd)
            self.term.banner(banner)
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
            self.term.feed(data)

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
            if parts and parts[0].lower() in ("cd", "chdir"):
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
            elif parts and parts[0].lower() == "set-location" and len(parts) > 1:
                target = parts[1].strip().strip('"\'')
                new_cwd = os.path.normpath(os.path.join(self._shell_cwd, target))
                if os.path.isdir(new_cwd):
                    self._shell_cwd = new_cwd

            # Track entry and exit from interactive adb shell
            if line.lower() in ("exit", "exit 0", "logout"):
                self._in_adb_shell = False

            tokens = line.split()
            if len(tokens) == 2 and tokens[0].lower() == "adb" and tokens[1].lower() == "shell":
                self._in_adb_shell = True
                data = b"adb shell -t -t\r\n"
                if hasattr(self.term, "_pending_echo"):
                    self.term._pending_echo = "adb shell -t -t"
            elif len(tokens) == 4 and tokens[0].lower() == "adb" and tokens[1].lower() in ("-s", "-t") and tokens[3].lower() == "shell":
                self._in_adb_shell = True
                data = f"adb {tokens[1]} {tokens[2]} shell -t -t\r\n".encode("utf-8")
                if hasattr(self.term, "_pending_echo"):
                    self.term._pending_echo = f"adb {tokens[1]} {tokens[2]} shell -t -t"

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
        except Exception:
            pass

        self.session.send(data)

    def interrupt(self):
        if self.session and self.session.running:
            self.session.send(b"\x03")

    def reopen(self):
        self.close_panel()
        self.term.clear()
        self._closing = False
        self._started = False
        self.ensure_started()

    def close_panel(self):
        self._closing = True
        if self.reader:
            try:
                self.reader.closed.disconnect(self._on_closed)
            except Exception:
                pass
        if self.session:
            self.session.close()
            self.session = None
        if self.reader:
            self.reader.stop()
            self.reader.wait(300)
            self.reader = None
        try:
            self.term.close_archive()
        except Exception:
            pass


class _AndroidShellWidget(QWidget):
    """A native interactive ``adb shell`` with local prompt emulation and auto-completion."""
    log = pyqtSignal(str)
    disconnected = pyqtSignal()

    _BIN_DIRS = (
        "/system/bin",
        "/system/xbin",
        "/vendor/bin",
        "/apex/com.android.runtime/bin",
        "/apex/com.android.art/bin",
    )
    _BC, _BT, _BL, _BV, _BD, _BR = (
        "\x1b[36m",
        "\x1b[96m",
        "\x1b[97m",
        "\x1b[92m",
        "\x1b[90m",
        "\x1b[0m",
    )

    def __init__(self, handler, device_name="", info=None, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.device_name = device_name or (handler.serial or "android")
        self._info = info or {}
        self.session = None
        self.reader = None
        self._pt = None
        self._closing = False
        self._banner_shown = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        stop = QPushButton(" Stop")
        stop.setProperty("role", "danger")
        stop.setIcon(theme.emoji_icon("⏹"))
        stop.setToolTip("Stop running command (Ctrl+C / SIGINT)")
        stop.clicked.connect(self.interrupt)

        clr = QPushButton(" Clear")
        clr.setProperty("role", "ghost")
        clr.setIcon(theme.emoji_icon("🧹"))

        paste = QPushButton(" Paste")
        paste.setProperty("role", "ghost")
        paste.setIcon(theme.emoji_icon("📥"))
        paste.clicked.connect(lambda: self.term.paste_clipboard())

        copy = QPushButton(" Copy")
        copy.setProperty("role", "ghost")
        copy.setIcon(theme.emoji_icon("📋"))
        copy.clicked.connect(lambda: self.term.copy())

        save = QPushButton(" Save…")
        save.setProperty("role", "ghost")
        save.setIcon(theme.emoji_icon("💾"))
        save.clicked.connect(lambda: self.term._save_output())

        row.addWidget(stop)
        row.addWidget(copy)
        row.addWidget(paste)
        row.addWidget(save)
        row.addWidget(clr)
        row.addStretch(1)
        row.addWidget(QLabel("Tab completes • right-click Copy/Paste"))
        lay.addLayout(row)

        self.term = AnsiConsole(send_fn=self._send)
        self.term.set_completion_fn(self._complete)
        self.term.set_interrupt_fn(self.interrupt)
        clr.clicked.connect(self.term.clear)
        lay.addWidget(self.term, 1)
        self._open()

    def _complete(self, line):
        import re

        m = re.search(r"(\S*)$", line)
        token = m.group(1) if m else ""
        if not token or not self.handler:
            return None, []
        dq = lambda s: "'" + s.replace("'", "'\\''") + "'"
        head = line[: len(line) - len(token)]
        first_word = " " not in line.strip()

        try:
            if first_word:
                globs = " ".join(f"{d}/{dq(token)}*" for d in self._BIN_DIRS)
                res = self.handler.shell(f"ls -d {globs} 2>/dev/null", timeout=1.5, safe=True)
                if not isinstance(res, OperationResult) or not res.success:
                    return None, []
                cmd_res = res.value
                names = sorted({
                    os.path.basename(p.rstrip("\r"))
                    for p in cmd_res.text.split()
                    if p.strip()
                })
                if not names:
                    return None, []
                if len(names) == 1:
                    return head + names[0] + " ", []
                prefix = os.path.commonprefix(names)
                return (head + prefix if len(prefix) > len(token) else None), names

            cwd = getattr(self.term, "_cwd", "/")
            res = self.handler.shell(
                f"cd {dq(cwd)} 2>/dev/null; ls -dp {dq(token)}* 2>/dev/null",
                timeout=1.5,
                safe=True,
            )
            if not isinstance(res, OperationResult) or not res.success:
                return None, []
            cmd_res = res.value
            entries = [e.rstrip("\r") for e in cmd_res.text.split("\n") if e.strip()]
            if not entries:
                return None, []
            if len(entries) == 1:
                return head + entries[0], []
            prefix = os.path.commonprefix(entries)
            if len(prefix) > len(token):
                return head + prefix, entries
            return None, entries
        except Exception:
            return None, []

    def _open(self):
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
        self.term.setFocus()

        self.term.set_prompt(self.device_name, root=False)
        if not self._banner_shown and self.term._alive:
            self._banner_shown = True
            try:
                self.term.banner(self._welcome_banner())
            except Exception:
                pass
        if self.term._alive:
            self.term.show_prompt()
        self._pt = _PromptThread(self.handler, self.device_name, info=self._info)
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
        auto = " · Automotive IVI" if d.get("automotive") else ""

        andro_str = f"Android {andro}" + (f" · SDK {sdk}" if sdk else "") if andro else "Android"

        from .. import __version__
        header_title = f"\x1b[1;92m•  TurboADB Professional v{__version__}  •\x1b[0m"
        header_sub = "\x1b[93m(ADB client, Screen mirror and device tools)\x1b[0m"
        session_title = f"\x1b[37mADB session to \x1b[1;35m{model} [{via}]\x1b[0m  \x1b[37m(\x1b[91m@{andro_str}\x1b[37m)\x1b[0m"
        items = [
            ("File-browser", "", True),
            ("Screen-mirror", "(remote display is forwarded)", True),
        ]
        return _render_mobaxterm_banner(header_title, header_sub, session_title, items, cwd="/")

    def _feed_from(self, reader, data):
        if reader is self.reader:
            self.term.feed(data)

    def _on_reader_closed(self):
        if self._closing:
            return
        self.term.set_alive(False)
        self.disconnected.emit()

    def reconnect(self):
        if self.reader:
            try:
                self.reader.closed.disconnect(self._on_reader_closed)
            except Exception:
                pass
        if self.session:
            self.session.close()
            self.session = None
        if self.reader:
            self.reader.stop()
            self.reader.wait(300)
            self.reader = None
        self.term.feed(b"\n")
        self._open()
        self.term.set_alive(True)
        self.focus_terminal()

    def _on_prompt(self, host, is_root):
        self.term.set_prompt(host, is_root)

    def _send(self, data: bytes):
        if self.session and self.session.running:
            self.session.send(data)

    def focus_terminal(self):
        try:
            self.term.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass

    def _reap_device_streamers(self):
        h = self.handler
        if h is None:
            return
        import threading

        def work():
            try:
                h.shell(
                    "pkill -f logcat 2>/dev/null; "
                    "pkill logcat 2>/dev/null; "
                    "killall -9 logcat 2>/dev/null; "
                    "killall logcat 2>/dev/null; "
                    "pkill -f 'top -' 2>/dev/null; true",
                    timeout=8,
                    safe=True,
                )
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

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

        if self.reader:
            try:
                self.reader.closed.disconnect(self._on_reader_closed)
            except Exception:
                pass
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass
        if self.reader:
            self.reader.stop()
            self.reader.wait(800)
            self.reader = None
        self.session = None

        self._reap_device_streamers()
        self.term._echo("\n^C  — stopped\n", "#ff7a6e")
        self.term._last_feed = 0.0
        self.term._cwd = cwd
        self._open()
        self.term.set_alive(True)

        if cwd and cwd not in ("", "/") and self.session:
            try:
                self.session.send(("cd " + shlex.quote(cwd) + "\n").encode("utf-8"))
            except Exception:
                pass

    def close_panel(self):
        self._closing = True
        if self.session:
            self.session.close()
        if self.reader:
            self.reader.stop()
            self.reader.wait(700)
        if self._pt:
            self._pt.wait(700)
        try:
            self.term.close_archive()
        except Exception:
            pass


class ShellPanel(QWidget):
    """Container holding persistent sub-tabs for Android Shell, PowerShell, and CMD."""
    log = pyqtSignal(str)
    disconnected = pyqtSignal()

    def __init__(self, handler, device_name="", info=None, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.device_name = device_name
        self._info = info

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.subtabs = QTabWidget()
        self.subtabs.setDocumentMode(True)

        self.android_widget = _AndroidShellWidget(handler, device_name=device_name, info=info)
        self.android_widget.log.connect(self.log)
        self.android_widget.disconnected.connect(self.disconnected)
        self.subtabs.addTab(self.android_widget, "📱 Android Shell")

        serial = getattr(handler, "serial", None)
        self.ps_widget = _LocalShellWidget("powershell", serial=serial)
        self.ps_widget.log.connect(self.log)
        self.subtabs.addTab(self.ps_widget, "⚡ PowerShell")

        self.cmd_widget = _LocalShellWidget("cmd", serial=serial)
        self.cmd_widget.log.connect(self.log)
        self.subtabs.addTab(self.cmd_widget, "💻 Command Prompt")
        self.subtabs.currentChanged.connect(lambda *_: self.focus_terminal())

        lay.addWidget(self.subtabs, 1)

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

    def reconnect(self):
        self.android_widget.reconnect()

    def interrupt(self):
        curr = self.subtabs.currentWidget()
        if hasattr(curr, "interrupt"):
            curr.interrupt()

    def close_panel(self):
        self.android_widget.close_panel()
        self.ps_widget.close_panel()
        self.cmd_widget.close_panel()


class DeviceTab(QWidget):
    log = pyqtSignal(str)
    title_changed = pyqtSignal(str)

    def __init__(self, session: dict, parent=None):
        super().__init__(parent)
        self.session = session
        self.handler = None
        self._threads = []
        self._scrcpy = []
        self._automotive = False
        self._reconnecting = False

        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.status = QLabel("Connecting…")

        self.btn_mirror = QToolButton()
        self.btn_mirror.setText(" Mirror")
        self.btn_mirror.setIcon(theme.emoji_icon("📱"))
        self.btn_mirror.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_mirror.setProperty("role", "ok")
        self.btn_mirror.setPopupMode(QToolButton.InstantPopup)
        mmenu = QMenu(self.btn_mirror)
        mmenu.addAction("Mirror (separate window)", lambda: self.mirror())
        mmenu.addAction("Embed in this tab", lambda: self.mirror(embed=True))
        mmenu.addAction("Mirror a specific display…", self.mirror_choose_display)
        mmenu.addAction(
            "Mirror (compatibility mode — for IVI/automotive)",
            lambda: self.mirror(compat=True),
        )
        self.btn_mirror.setMenu(mmenu)

        self.btn_shot = QToolButton()
        self.btn_shot.setText(" Screenshot")
        self.btn_shot.setIcon(theme.emoji_icon("📸"))
        self.btn_shot.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_shot.setProperty("role", "ghost")
        self.btn_shot.clicked.connect(self.screenshot)

        self.btn_health = QToolButton()
        self.btn_health.setText(" Health")
        self.btn_health.setIcon(theme.emoji_icon("❤"))
        self.btn_health.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_health.setProperty("role", "ghost")
        self.btn_health.setToolTip("Battery, temperature, memory, CPU and uptime in one snapshot.")
        self.btn_health.clicked.connect(self.show_health)

        self.btn_adv = QToolButton()
        self.btn_adv.setText(" Root / Mount")
        self.btn_adv.setIcon(theme.emoji_icon("🔧"))
        self.btn_adv.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_adv.setProperty("role", "ghost")
        self.btn_adv.setPopupMode(QToolButton.InstantPopup)
        amenu = QMenu(self.btn_adv)
        amenu.addAction("adb root", lambda: self._op("root", lambda h: h.root(safe=False)))
        amenu.addAction("adb unroot", lambda: self._op("unroot", lambda h: h.unroot(safe=False)))
        amenu.addSeparator()
        amenu.addAction("adb remount (rw)", lambda: self._op("remount", lambda h: h.remount(safe=False)))
        amenu.addAction("mount -o remount,rw /", lambda: self._op("mount rw", lambda h: h.mount_rw(safe=False)))
        amenu.addSeparator()
        amenu.addAction("adb disable-verity  (sync + reboot)", lambda: self._verity(False))
        amenu.addAction("adb enable-verity  (sync + reboot)", lambda: self._verity(True))
        amenu.addSeparator()
        amenu.addAction("Go wireless (USB → Wi-Fi)", self.go_wireless)
        amenu.addAction("Capture bugreport…", self.capture_bugreport)
        self.btn_adv.setMenu(amenu)

        self.btn_reboot = QToolButton()
        self.btn_reboot.setText(" Reboot")
        self.btn_reboot.setIcon(theme.emoji_icon("🔁"))
        self.btn_reboot.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_reboot.setProperty("role", "ghost")
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

        for w in (
            self.status,
            self.btn_mirror,
            self.btn_shot,
            self.btn_health,
            self.btn_adv,
            self.btn_reboot,
        ):
            bar.addWidget(w)
        bar.setStretch(0, 1)
        lay.addLayout(bar)

        self.inner = QTabWidget()
        self.inner.setElideMode(Qt.ElideNone)
        self.inner.setUsesScrollButtons(True)
        tb = self.inner.tabBar()
        if tb is not None:
            tb.setExpanding(False)
        self.inner.currentChanged.connect(self._on_subtab_changed)
        lay.addWidget(self.inner, 1)
        self._enable_actions(False)

        cfg = config_from_session(session)
        self._ct = _ConnectThread(cfg)
        self._ct.ok.connect(self._on_connected)
        self._ct.fail.connect(self._on_fail)
        self._ct.log.connect(self.log)
        self._started_connect = False
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(0, self._auto_start_connect)

    def start_connect(self):
        if hasattr(self, "_ct") and not self._ct.isRunning() and not self._started_connect:
            self._started_connect = True
            self._ct.start()

    def _auto_start_connect(self):
        if not self._started_connect:
            self.start_connect()

    def _track_thread(self, t):
        self._threads.append(t)
        t.finished.connect(t.deleteLater)
        t.finished.connect(self._on_thread_finished)

    def _on_thread_finished(self):
        t = self.sender()
        if t in self._threads:
            self._threads.remove(t)

    def _enable_actions(self, on):
        for w in (
            self.btn_mirror,
            self.btn_shot,
            self.btn_health,
            self.btn_adv,
            self.btn_reboot,
        ):
            w.setEnabled(on)

    def _on_shell_lost(self):
        try:
            if self.handler and self.handler.get_state() == "device":
                self.log.emit("[INFO] Shell connection reset; restarting shell…")
                if hasattr(self, "shell"):
                    self.shell.reconnect()
                return
        except Exception:
            pass
        self._wait_and_reconnect()

    def _wait_and_reconnect(self):
        if self._reconnecting or not self.handler:
            return
        self._reconnecting = True
        self.status.setText("Reconnecting… waiting for the device to come back")
        self.log.emit("[WARNING] device went away (reboot/unplug) — waiting for it to come back…")
        self._enable_actions(False)
        self._rc = _ReconnectThread(self.handler)
        self._rc.done.connect(self._on_reconnected)
        self._rc.start()

    def _on_reconnected(self, ok):
        self._reconnecting = False
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
            self.log.emit(
                "[ERROR] device did not return to 'device' state (timed out). "
                "If it's in recovery/bootloader this is expected; otherwise replug and use Connect."
            )

    def _op(self, label, fn):
        if not self.handler:
            return
        self.log.emit(f"{label}…")
        t = _ActionThread(lambda: fn(self.handler))
        t.done.connect(lambda r: self.log.emit(f"[OK] {label}: {r}"))
        t.fail.connect(lambda m: self.log.emit(f"[ERROR] {label}: {m}"))
        self._track_thread(t)
        t.start()

    def _verity(self, enable):
        if not self.handler:
            return
        label = "enable-verity" if enable else "disable-verity"

        def work(h):
            out = (h.enable_verity if enable else h.disable_verity)(safe=False)
            try:
                h.shell("sync", safe=False)
            except Exception:
                pass
            return out

        self.log.emit(f"{label}…")
        t = _ActionThread(lambda: work(self.handler))
        t.done.connect(lambda r: self._after_verity(label, r))
        t.fail.connect(lambda m: self.log.emit(f"[ERROR] {label}: {m}"))
        self._track_thread(t)
        t.start()

    def _after_verity(self, label, result):
        self.log.emit(f"[OK] {label} (+ sync): {result}")
        if (
            QMessageBox.question(
                self,
                "Reboot required",
                f"{label} done and filesystem synced.\n\nA reboot is required for it to take effect. Reboot now?",
            )
            == QMessageBox.Yes
        ):
            self._op("reboot", lambda h: h.reboot(safe=False) or "rebooting")

    def _on_connected(self, handler, info=None):
        self.handler = handler
        self.status.setText(f"Connected — {handler.serial or self.session.get('name')}")
        self.log.emit(f"[OK] {self.session.get('name')}: connected")
        self._enable_actions(True)

        if info is None:
            info = handler.device_info(safe=True)

        dev_name = ""
        binfo = {}
        d = None
        if isinstance(info, OperationResult) and info.success:
            d = info.value
        elif isinstance(info, dict):
            d = info

        if isinstance(d, dict):
            binfo = dict(d)
            self._automotive = bool(d.get("automotive"))
            auto = " · AUTOMOTIVE" if self._automotive else ""
            self.log.emit(
                f"[OK] {d.get('manufacturer')} {d.get('model')} · "
                f"Android {d.get('android_version')} (SDK {d.get('sdk')}) · {d.get('abi')}{auto}"
            )
            if self._automotive:
                self.btn_mirror.setText("📱 Mirror (IVI) ▾")
            name = d.get("model") or d.get("device") or d.get("name") or handler.serial or ""
            if name:
                self.title_changed.emit(name)
            dev_name = d.get("device") or d.get("model") or ""

        self.shell = ShellPanel(handler, device_name=dev_name, info=binfo)
        self.shell.log.connect(self.log)
        self.shell.disconnected.connect(self._on_shell_lost)
        self.logcat = LogcatPanel(handler)
        self.logcat.log.connect(self.log)
        self.files = FileBrowser(handler, start="/sdcard")
        self.files.log.connect(self.log)
        self.apps = AppsPanel(handler, automotive=self._automotive)
        self.apps.log.connect(self.log)
        self.combo_view = self._build_control_view(handler)
        self.webcam = CameraPanel()
        self.webcam.log.connect(self.log)

        self._add_subtab(self.shell, "🖥", "Terminal")
        self._add_subtab(self.logcat, "📜", "Logcat")
        self._add_subtab(self.files, "📁", "Files")
        self._add_subtab(self.combo_view, "📱", "Mirror & Controls")
        self._add_subtab(self.apps, "📦", "Apps")
        self._add_subtab(self.webcam, "📹", "Webcam")

        self._subtabs = {
            "shell": self.shell,
            "terminal": self.shell,
            "logcat": self.logcat,
            "files": self.files,
            "mirror": self.combo_view,
            "controls": self.combo_view,
            "apps": self.apps,
            "webcam": self.webcam,
        }

    def _add_subtab(self, widget, emoji, label):
        idx = self.inner.addTab(widget, label)
        self.inner.setTabIcon(idx, theme.emoji_icon(emoji))
        return idx

    def toggle_device_split(self):
        """Toggle side-by-side view (Terminal on left, Mirror & Controls on right)."""
        if getattr(self, "_inner_split_active", False):
            if hasattr(self, "_split_container") and self._split_container is not None:
                self.layout().removeWidget(self._split_container)
                self.shell.setParent(self.inner)
                self.combo_view.setParent(self.inner)
                if self.inner.indexOf(self.shell) == -1:
                    self.inner.insertTab(0, self.shell, theme.emoji_icon("🖥"), "Terminal")
                if self.inner.indexOf(self.combo_view) == -1:
                    self.inner.insertTab(min(3, self.inner.count()), self.combo_view, theme.emoji_icon("📱"), "Mirror & Controls")
                self._split_container.deleteLater()
                self._split_container = None
            self._inner_split_active = False
            self.inner.show()
            return False
        else:
            if not hasattr(self, "shell") or not hasattr(self, "combo_view"):
                return False
            sh_idx = self.inner.indexOf(self.shell)
            if sh_idx != -1:
                self.inner.removeTab(sh_idx)
            cv_idx = self.inner.indexOf(self.combo_view)
            if cv_idx != -1:
                self.inner.removeTab(cv_idx)
            self.inner.hide()
            from PyQt5.QtWidgets import QSplitter

            splitter = QSplitter(Qt.Horizontal)
            splitter.addWidget(self.shell)
            splitter.addWidget(self.combo_view)
            w = max(400, self.width())
            splitter.setSizes([w // 2, w // 2])
            self.layout().addWidget(splitter, 1)
            self._split_container = splitter
            self._inner_split_active = True
            return True

    def _build_control_view(self, handler):
        from PyQt5.QtWidgets import QSplitter, QFrame

        col = theme.THEMES.get(settings_mod.get("theme"), theme.THEMES["dark"])
        atext = theme.accent_text(settings_mod.get("theme"))

        def card(emoji, title, inner):
            frame = QFrame()
            frame.setObjectName("cvCard")
            frame.setStyleSheet(
                f"QFrame#cvCard {{ background: {col['panel']};"
                f" border: 1px solid {col['border']}; border-radius: 8px; }}"
            )
            fv = QVBoxLayout(frame)
            fv.setContentsMargins(0, 0, 0, 0)
            fv.setSpacing(0)
            if title:
                cap = QLabel(f"  {emoji}  {title}")
                cap.setStyleSheet(
                    f"background: {col['ribbon']}; color: {atext};"
                    f" padding: 5px 8px; font-weight: 600; font-size: 8.5pt;"
                    f" border-top-left-radius: 8px; border-top-right-radius: 8px;"
                    f" border-bottom: 1px solid {col['border']};"
                )
                fv.addWidget(cap)
            body = QWidget()
            bv = QVBoxLayout(body)
            bv.setContentsMargins(
                0 if not title else 4,
                0 if not title else 4,
                0 if not title else 4,
                0 if not title else 4,
            )
            bv.addWidget(inner)
            fv.addWidget(body, 1)
            return frame

        self.mirror_tab = MirrorPanel(
            handler,
            self.session,
            automotive=self._automotive,
            prefer_embed=True,
        )
        self.mirror_tab.log.connect(self.log)
        self.controls = ControlsPanel(handler, compact=True)
        self.controls.log.connect(self.log)
        m_card = card("", "", self.mirror_tab)
        m_card.setMinimumWidth(340)
        c_card = card("🎛", "Device Controls", self.controls)
        c_card.setMinimumWidth(280)

        btn_tog = QPushButton("🎛 Controls")
        btn_tog.setProperty("role", "ghost")
        btn_tog.setToolTip("Show / hide the device controls dock")
        btn_tog.clicked.connect(lambda: c_card.setVisible(not c_card.isVisible()))
        if hasattr(self.mirror_tab, "btn_opts") and self.mirror_tab.btn_opts.parentWidget():
            p_lay = self.mirror_tab.btn_opts.parentWidget().layout()
            if p_lay:
                p_lay.addWidget(btn_tog)

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
            self.inner.setCurrentWidget(w)

    def _on_subtab_changed(self, *_):
        w = self.inner.currentWidget()
        sh = getattr(self, "shell", None)
        if sh is not None and w is sh:
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, sh.focus_terminal)

        mt = getattr(self, "mirror_tab", None)
        cv = getattr(self, "combo_view", None)
        if mt and hasattr(mt, "act_max"):
            if w is cv:
                mt._resume_max()
            else:
                mt._suspend_max()

    def mirror(self, display_id=None, compat=False, embed=None):
        if not self.handler:
            return
        if hasattr(self, "combo_view"):
            self.inner.setCurrentWidget(self.combo_view)
        elif hasattr(self, "mirror_tab"):
            self.inner.setCurrentWidget(self.mirror_tab)
        if hasattr(self, "mirror_tab") and self.mirror_tab:
            self.mirror_tab.start(display_id=display_id, compat=compat, embed=embed)

    def mirror_choose_display(self):
        if not self.handler:
            return
        try:
            if hasattr(self, "combo_view") and self.combo_view:
                self.inner.setCurrentWidget(self.combo_view)
            elif hasattr(self, "mirror_tab") and self.mirror_tab:
                self.inner.setCurrentWidget(self.mirror_tab)
        except Exception:
            pass
        if hasattr(self, "mirror_tab") and self.mirror_tab:
            self.mirror_tab._show_display_manager()

    def screenshot(self):
        if not self.handler:
            return
        import time as _t
        from .fileutil import download_path

        default = download_path("screenshot-" + _t.strftime("%Y%m%d-%H%M%S") + ".png")
        path, _ = QFileDialog.getSaveFileName(self, "Save screenshot", default, "PNG (*.png)")
        if not path:
            return
        t = _ActionThread(lambda: self.handler.screenshot(path, safe=False))
        t.done.connect(lambda p: self.log.emit(f"[OK] screenshot saved: {p}"))
        t.fail.connect(lambda m: self.log.emit("[ERROR] screenshot: " + m))
        self._track_thread(t)
        t.start()

    def show_health(self):
        if not self.handler:
            return
        self.log.emit("reading device health…")
        t = _ActionThread(lambda: self.handler.health_text(safe=False))
        t.done.connect(self._show_health_dialog)
        t.fail.connect(lambda m: self.log.emit("[ERROR] health: " + m))
        self._track_thread(t)
        t.start()

    def _show_health_dialog(self, text):
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QPlainTextEdit, QDialogButtonBox
        from PyQt5.QtGui import QFont

        dlg = QDialog(self)
        dlg.setWindowTitle("Device health")
        dlg.resize(420, 240)
        v = QVBoxLayout(dlg)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        view.setFont(QFont("Consolas", 10))
        v.addWidget(view)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept)
        v.addWidget(bb)
        dlg.exec_()

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
        t = _ActionThread(lambda: self.handler.go_wireless(safe=False))
        t.done.connect(
            lambda s: self.log.emit(
                f"[OK] now reachable wirelessly at {s} — the USB cable can be "
                f"unplugged. Save it from Connect → Network to reconnect later."
            )
        )
        t.fail.connect(lambda m: self.log.emit("[ERROR] go wireless: " + m))
        self._track_thread(t)
        t.start()

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
        t = _ActionThread(lambda: self.handler.bugreport(path, safe=False))
        t.done.connect(lambda p: self.log.emit(f"[OK] bugreport saved: {p}"))
        t.fail.connect(lambda m: self.log.emit("[ERROR] bugreport: " + m))
        self._track_thread(t)
        t.start()

    def reboot(self, mode):
        if not self.handler:
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
        t = _ActionThread(lambda: self.handler.reboot(mode, safe=False))
        t.done.connect(lambda _: self.log.emit(f"[OK] rebooting to {label}…"))
        t.fail.connect(lambda m: self.log.emit("[ERROR] reboot: " + m))
        self._track_thread(t)
        t.start()

    def close_session(self):
        from .qtutil import park_thread

        for attr in ("_ct", "_rc"):
            t = getattr(self, attr, None)
            if t is not None:
                for sig in ("ok", "fail", "log", "done"):
                    try:
                        getattr(t, sig).disconnect()
                    except Exception:
                        pass
                park_thread(t)
        for t in list(self._threads):
            for sig in ("done", "fail"):
                try:
                    getattr(t, sig).disconnect()
                except Exception:
                    pass
            park_thread(t)
        self._threads.clear()

        if getattr(self, "_inner_split_active", False) and hasattr(self, "_split_container") and self._split_container is not None:
            try:
                self.layout().removeWidget(self._split_container)
                if hasattr(self, "shell"):
                    self.shell.setParent(self)
                if hasattr(self, "combo_view"):
                    self.combo_view.setParent(self)
                self._split_container.deleteLater()
                self._split_container = None
            except Exception:
                pass
        for attr in (
            "shell", "logcat", "files", "apps", "controls",
            "mirror_tab", "webcam",
        ):
            p = getattr(self, attr, None)
            if p is not None:
                try:
                    p.close_panel()
                except Exception:
                    pass
        for s in self._scrcpy:
            try:
                s.stop()
            except Exception:
                pass
        if self.handler:
            try:
                self.handler.disconnect()
            except Exception:
                pass