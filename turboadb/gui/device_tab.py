"""A per-device tab: connects in the background, then exposes an interactive
Shell, a live Logcat viewer, a file browser, and an app manager — plus quick
Mirror (scrcpy), Screenshot, and Reboot actions in a header bar."""

from __future__ import annotations

import os
import queue
import shlex
import threading
import time

from PyQt5.QtCore import QThread, pyqtSignal, Qt, QEvent, QSize, QTimer
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QFileDialog, QMenu, QToolButton,
    QMessageBox, QSplitter, QStackedWidget, QDialog,
    QDialogButtonBox, QComboBox, QFormLayout, QFrame, QSizePolicy, QTabBar
)

from ..config import ADBConfig
from ..core import ADBHandler
from ..results import OperationResult
from . import device_access
from . import fileutil
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
from .device_access import refusal_line
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


def _files_tab_number(key: str) -> int:
    """``"files-3"`` -> 3: extra Files tabs sort by their number."""
    try:
        return int(str(key).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


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
    "Displays": ("screen-control", "pink"),
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
    all_content = [title] + [f"  {line}" for line in lines]
    max_c = max(_str_width(x) for x in all_content)
    width = max(min_width, max_c + 4)
    inner_w = width - 2

    top = "\x1b[90m┌" + ("─" * inner_w) + "┐\x1b[0m"
    bot = "\x1b[90m└" + ("─" * inner_w) + "┘\x1b[0m"
    blank = "\x1b[90m│" + (" " * inner_w) + "│\x1b[0m"

    def center(s):
        sw = _str_width(s)
        pad = max(0, inner_w - sw)
        left = pad // 2
        right = pad - left
        return "\x1b[90m│\x1b[0m" + (" " * left) + s + (" " * right) + "\x1b[90m│\x1b[0m"

    def left_row(s, indent=2):
        sw = _str_width(s)
        rem = max(0, inner_w - indent - sw)
        return "\x1b[90m│\x1b[0m" + (" " * indent) + s + (" " * rem) + "\x1b[90m│\x1b[0m"

    body = [center(title), blank] + [left_row(line, indent=2) for line in lines]
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


class _GateClosed(RuntimeError):
    """The device tab closed while a background command waited for a slot."""


class _AdbGate:
    """At most *slots* one-shot adb commands of one device tab run at once.

    A background job still gets its own worker thread, but it waits here for a
    slot before starting adb, so a burst of refreshes queues up instead of
    forking several adb.exe processes at the same moment.  Closing the tab
    wakes every waiting job with :class:`_GateClosed`: a job that had not
    started yet never starts adb for a tab that is gone.  Persistent streams
    (the shell, logcat, transfers) don't take a slot.
    """

    def __init__(self, slots: int = 2):
        self._cond = threading.Condition()
        self._free = max(1, int(slots))
        self._closed = False

    def acquire(self) -> None:
        with self._cond:
            while self._free <= 0 and not self._closed:
                self._cond.wait()
            if self._closed:
                raise _GateClosed("the device tab was closed")
            self._free -= 1

    def release(self) -> None:
        with self._cond:
            self._free += 1
            self._cond.notify()

    def wrap(self, fn):
        """*fn*, run inside a slot (for ``run_job`` workers)."""

        def gated(*args, **kwargs):
            self.acquire()
            try:
                return fn(*args, **kwargs)
            finally:
                self.release()

        return gated

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


class _ConnectThread(QThread):
    ok = pyqtSignal(object, object)
    fail = pyqtSignal(str)
    log = pyqtSignal(str)  # this worker's own progress and outcome lines
    # The handler's engine diagnostics ("[DEBUG] $ adb …", "… failed: …" from
    # ADBHandler._guard): log panel only, never a notification — the panel
    # that ran a failing command reports it itself.
    trace = pyqtSignal(str)
    # (handler, probe result): the rest of the connect-time probe, after ``ok``
    details = pyqtSignal(object, object)

    IDENTITY_TIMEOUT = 1.5

    def __init__(self, cfg, *, fetch_identity: bool = True, gate=None):
        super().__init__()
        self.cfg = cfg
        self.fetch_identity = bool(fetch_identity)
        self.gate = gate
        self._probe = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        probe = self._probe
        if probe is not None:
            probe.close()  # never leave the probe's adb shell behind a closed tab

    def _make_handler(self):
        """The tab's handler. Its engine log lines go to ``trace`` (the log
        panel), never ``log`` (notifications)."""
        return ADBHandler(
            self.cfg,
            safe=True,
            log_callback=lambda m: None if self._cancelled else self.trace.emit(m),
        )

    def run(self):
        try:
            if self._cancelled:
                return
            h = self._make_handler()
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
            if not self.fetch_identity:
                # A terminal-only duplicate needs no device profile.
                self.ok.emit(h, None)
                return
            self._probe_and_emit(h)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")

    def _probe_and_emit(self, handler):
        """One adb shell for identity, prompt, device kind and displays.

        ``ok`` goes out as soon as the identity sections are in (at most
        ``IDENTITY_TIMEOUT`` s, as the old quick-identity call), so the tab,
        its title and banner never wait for the slower feature list; the rest
        follows as ``details`` from the same process.  The probe is quiet: a
        phone that is slow right after connecting gets a plain banner, not an
        error popup.
        """
        probe = _DeviceProbe(handler)
        self._probe = probe
        emitted = []

        def identity(payload):
            if not self._cancelled:
                emitted.append(True)
                self.ok.emit(handler, dict(payload, _probe_pending=True))

        if self._cancelled:  # cancel() ran before the probe existed
            probe.close()
        try:
            result = probe.run(gate=self.gate, on_identity=identity,
                               identity_timeout=self.IDENTITY_TIMEOUT)
        except Exception as exc:
            # The device is connected: a probe failure must never reach
            # run()'s handler and turn into "Connect failed".
            probe.close()
            if not emitted:
                identity({})
            result = {"info": None, "prompt": None, "displays": None,
                      "error": f"{type(exc).__name__}: {exc}"}
        if self._cancelled:
            if not emitted:
                try:
                    handler.disconnect()
                except Exception:
                    pass
            return
        self.details.emit(handler, result)


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


class _ProbeThread(QThread):
    """Run the connect-time :class:`_DeviceProbe` for a tab that was handed a
    handler directly (not through its own connect worker)."""

    details = pyqtSignal(object)

    def __init__(self, handler, gate=None):
        super().__init__()
        self.probe = _DeviceProbe(handler)
        self.gate = gate
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self.probe.close()

    def run(self):
        result = self.probe.run(gate=self.gate)
        if not self._cancelled:
            self.details.emit(result)


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
    """Ask the device shell who and where it is, without blocking the UI.

    The prompt copies the device's own ``user@host`` (``root@adelegg``,
    ``shell@V2318``): the user from ``id -un`` and the host the way Android's
    shell sets ``HOSTNAME``, falling back to ``ro.product.device``. The
    friendly model name is for the banner, not the prompt.
    """
    ready = pyqtSignal(bool, str)

    COMMAND = 'echo "$(id -u)|$(id -un 2>/dev/null)|${HOSTNAME:-$(getprop ro.product.device)}"'

    def __init__(self, handler):
        super().__init__()
        self.handler = handler

    @staticmethod
    def parse(text):
        """``"0|root|adelegg"`` -> ``(True, "root@adelegg")``; ``(root, "")`` if unknown."""
        lines = (text or "").strip().splitlines()
        parts = [part.strip() for part in lines[-1].split("|")] if lines else []
        if len(parts) != 3 or not parts[0].isdigit():
            return False, ""
        uid, user, host = parts
        root = uid == "0"
        if not host:
            return root, ""
        return root, f"{user or ('root' if root else 'shell')}@{host}"

    def run(self):
        root, identity = False, ""
        try:
            res = self.handler.shell(self.COMMAND, timeout=3.0, safe=True)
            value = res.value if isinstance(res, OperationResult) else res
            ok = not isinstance(res, OperationResult) or res.success
            if ok and value is not None and value.ok:
                root, identity = self.parse(value.text)
        except Exception:
            pass
        self.ready.emit(root, identity)


class _DeviceProbe:
    """Everything a new device tab asks the device, in ONE ``adb shell``.

    Opening a tab used to start five short adb processes beside the
    interactive shell (quick identity, the prompt's ``user@host``, a full
    ``getprop`` dump, the device-kind script and a display scan), three of
    them at the same moment.  This script prints the same facts as delimited
    sections of one stream: the prompt and build properties come first (the
    tab title, banner and prompt need nothing else), then the feature list,
    ``wm size`` and the display list.  Parsing reuses the engine's own
    ``classify_device`` and ``parse_display_info``.

    Plain Python (no Qt): :meth:`run` blocks, so call it from a worker.
    """

    MARK = "__turboadb_probe_{}__"
    SECTIONS = ("prompt", "props", "kind", "displays", "end")
    # The first six are quick_identity's fields, in its order.
    PROPS = (
        "ro.product.manufacturer",
        "ro.product.model",
        "ro.build.version.release",
        "ro.build.version.sdk",
        "ro.product.device",
        "ro.product.cpu.abi",
        "ro.product.brand",
        "ro.product.name",
        "ro.build.display.id",
        "ro.serialno",
        "ro.build.characteristics",
    )
    IDENTITY_KEYS = ("manufacturer", "model", "android_version", "sdk", "device", "abi")
    # device_info's getprop (20 s) and device_kind (20 s) limits, as one budget.
    TIMEOUT = 25.0
    _PROP_RE = re.compile(r"\[(.+?)\]:\s*\[(.*)\]")

    def __init__(self, handler):
        self.handler = handler
        self.sections = {}
        self.error = ""
        self._marks = {self.MARK.format(name): name for name in self.SECTIONS}
        self._current = None
        self._lines = queue.Queue()
        self._lock = threading.Lock()
        self._proc = None
        self._eof = False
        self._closed = False

    @classmethod
    def script(cls) -> str:
        mark = cls.MARK.format
        return "; ".join((
            f"echo {mark('prompt')}",
            _PromptThread.COMMAND,
            f"echo {mark('props')}",
            f'for p in {" ".join(cls.PROPS)}; do echo "[$p]: [$(getprop $p)]"; done',
            f"echo {mark('kind')}",
            ADBHandler._KIND_SCRIPT,
            f"echo {mark('displays')}",
            # list_displays(method="adb"): dumpsys only when cmd lists nothing
            'd="$(cmd display get-displays 2>/dev/null)"; case "$d" in '
            '*DisplayInfo*) echo "$d" ;; *) dumpsys display 2>/dev/null ;; esac',
            f"echo {mark('end')}",
        ))

    # ---- the process ----
    def run(self, gate=None, on_identity=None, identity_timeout=1.5) -> dict:
        """Run the probe and return :meth:`result`.

        *on_identity(payload)* is called exactly once: when the prompt and
        property sections are complete, after *identity_timeout* seconds, or
        at once when the probe can't run (so a caller never waits on it).
        """
        slot = started = False
        try:
            if gate is not None:
                gate.acquire()
                slot = True
            started = self.start()
            if started:
                self.read_until("kind", identity_timeout)
        except _GateClosed as exc:
            self.error = str(exc)
        try:
            if on_identity is not None:
                on_identity(self.identity_payload())
            if started:
                self.read_until("end", self.TIMEOUT)
        finally:
            self.close(wait=2.0)
            if slot:
                gate.release()
        return self.result()

    def start(self) -> bool:
        with self._lock:
            if self._closed:
                self._eof = True
                return False
        try:
            proc = self.handler.popen(["shell", self.script()])
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._eof = True
            return False
        with self._lock:
            self._proc = proc
            closed = self._closed
        if closed:
            self._kill(proc)
            self._eof = True
            return False
        threading.Thread(
            target=self._pump, args=(proc,), name="turboadb-device-probe", daemon=True
        ).start()
        return True

    def _pump(self, proc) -> None:
        tail = b""
        try:
            read = getattr(proc.stdout, "read1", None) or proc.stdout.read
            while True:
                chunk = read(65536)
                if not chunk:
                    break
                lines = (tail + chunk).split(b"\n")
                tail = lines.pop()
                for line in lines:
                    self._lines.put(line)
        except (OSError, ValueError, AttributeError):
            pass  # pipe closed by close()
        if tail:
            self._lines.put(tail)
        self._lines.put(None)

    def read_until(self, section, timeout) -> bool:
        """Consume output until *section* has started; False at EOF, on
        :meth:`close` or after *timeout* seconds."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while section not in self.sections:
            if self._eof or self._closed:
                return False
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            try:
                raw = self._lines.get(timeout=min(left, 0.2))
            except queue.Empty:
                continue
            if raw is None:
                self._eof = True
                return False
            line = raw.decode("utf-8", "replace").rstrip("\r")
            name = self._marks.get(line.strip())
            if name is not None:
                self._current = name
                self.sections[name] = []
            elif self._current is not None:
                self.sections[self._current].append(line)
        return True

    def close(self, wait: float = 0.0) -> None:
        """End the probe's adb process (idempotent, any thread)."""
        with self._lock:
            self._closed = True
            proc = self._proc
        if proc is None:
            return
        if wait:
            try:
                proc.wait(timeout=wait)
            except Exception:
                pass
        self._kill(proc)

    @staticmethod
    def _kill(proc) -> None:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass

    # ---- parsing ----
    def props(self) -> dict:
        found = {}
        for line in self.sections.get("props", ()):
            match = self._PROP_RE.match(line.strip())
            if match:
                found[match.group(1)] = match.group(2)
        return found

    def prompt(self):
        """``(root, "user@host")`` once the prompt section is complete, else None."""
        if "props" not in self.sections:
            return None
        return _PromptThread.parse("\n".join(self.sections["prompt"]))

    def quick_identity(self) -> dict:
        """``ADBHandler.quick_identity()``'s dict, or {} before the properties are in."""
        if "kind" not in self.sections:
            return {}
        props = self.props()
        ident = {key: props.get(prop, "") for key, prop in zip(self.IDENTITY_KEYS, self.PROPS)}
        return ident if (ident["manufacturer"] or ident["model"]) else {}

    def identity_payload(self) -> dict:
        """What ``_ConnectThread.ok`` carries: quick identity and the prompt."""
        quick = self.quick_identity()
        payload = dict(quick, _quick_identity=True) if quick else {}
        prompt = self.prompt()
        if prompt is not None:
            payload["_prompt"] = prompt
        return payload

    def device_info(self):
        """``ADBHandler.device_info()``'s dict, or None when no properties came back."""
        if "kind" not in self.sections:
            return None
        props = self.props()
        if not any(props.get(p) for p in self.PROPS[:4]):
            return None
        characteristics = props.get("ro.build.characteristics", "")
        info = {
            "serial": getattr(self.handler, "serial", None) or props.get("ro.serialno", ""),
            "model": props.get("ro.product.model", ""),
            "brand": props.get("ro.product.brand", ""),
            "name": props.get("ro.product.name", ""),
            "device": props.get("ro.product.device", ""),
            "manufacturer": props.get("ro.product.manufacturer", ""),
            "android_version": props.get("ro.build.version.release", ""),
            "sdk": props.get("ro.build.version.sdk", ""),
            "build_id": props.get("ro.build.display.id", ""),
            "abi": props.get("ro.product.cpu.abi", ""),
            "characteristics": characteristics,
            "automotive": "automotive" in characteristics,
        }
        if "displays" in self.sections:  # the feature list / wm size section is complete
            kind = ADBHandler.classify_device("\n".join(self.sections["kind"]))
            ADBHandler.merge_kind(info, kind)
        return info

    def displays(self):
        """``list_displays(method="adb")``'s list, or None when the scan didn't finish."""
        if "end" not in self.sections:
            return None
        return ADBHandler.parse_display_info("\n".join(self.sections["displays"]))

    def result(self) -> dict:
        info = self.device_info()
        error = ""
        if info is None:
            error = self.error or (
                "the device did not answer in time" if not self._eof
                else "the device returned no properties"
            )
        return {
            "info": info,
            "prompt": self.prompt(),
            "displays": self.displays(),
            "error": error,
        }


class _TerminalWidgetBase(QWidget):
    """Toolbar, font zoom, async Android completion and ADB restart shared by
    the Android shell and the local PowerShell / Command Prompt terminals."""

    log = pyqtSignal(str)
    # an output line where the device refused a change (at most once a minute)
    access_refused = pyqtSignal(str)
    ACCESS_NOTICE_S = 60.0

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
        self._access_notice_at = 0.0

    def _notice_refusal(self, data) -> None:
        """Emit :attr:`access_refused` for a line where the device refused access."""
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        line = refusal_line(text)
        now = time.monotonic()
        if line and now - self._access_notice_at >= self.ACCESS_NOTICE_S:
            self._access_notice_at = now
            self.access_refused.emit(line)

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
    # "root" / "unroot" typed here for this device: adbd restarts
    adbd_restart_requested = pyqtSignal(str)

    # how long after the device is back an adb shell may still be ending
    ADB_RESUME_WAIT_S = 30.0

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
        self._adb_shell_command = ""  # the adb shell command as sent, to reopen it
        # the adb shell printed its prompt or an adb error, so a local prompt
        # after that means it ended (not an earlier prompt still arriving)
        self._adb_answered = False
        self._adb_reentry_cwd = None  # device folder to cd into once reopened
        self._adb_interrupt_pending = False  # Ctrl+C sent, device prompt not back yet
        self._adb_interrupt_heard = False
        self._adb_interrupt_timer = QTimer(self)
        self._adb_interrupt_timer.setSingleShot(True)
        self._adb_interrupt_timer.timeout.connect(self._on_adb_interrupt_timeout)
        # (command, device folder) of an adb shell to reopen once adbd is back
        self._adb_resume = None
        self._adb_resume_until = 0.0  # set when the device is back; monotonic
        self._strip_startup_banner = False  # set for each new CMD session
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
        self._adb_shell_command = ""
        self._adb_answered = False
        self._adb_reentry_cwd = None
        self._clear_adb_interrupt()
        self.term._cwd = self._shell_cwd
        self.term.set_completion_fn(self._local_complete)

    def _enter_adb_shell(self, command: str) -> bytes:
        """Follow an interactive adb shell started here; returns the line to send."""
        self._in_adb_shell = True
        self._adb_shell_command = command
        self._adb_shell_cwd = "/"
        self._adb_answered = False
        self._adb_reentry_cwd = None
        self._prompt_tail = ""
        self._clear_adb_interrupt()
        self.term._cwd = self._adb_shell_cwd
        self.term.set_completion_fn(self._adb_shell_complete)
        return (command + "\r\n").encode("utf-8")

    def _local_adb_command(self, tokens: list[str]):
        """``(subcommand, arguments)`` of an adb command line aimed at this
        terminal's device (``adb -s OTHER …`` is not), else None."""
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
        if index >= len(tokens):
            return None
        if selected_serial and self.serial and selected_serial != self.serial:
            return None
        return tokens[index].lower(), tokens[index + 1:]

    def _local_adb_reboot_mode(self, tokens: list[str]):
        """Return the requested reboot mode for this terminal's device, if any."""
        command = self._local_adb_command(tokens)
        if command is None or command[0] != "reboot":
            return None
        return command[1][0].lower() if command[1] else ""

    def _local_adbd_restart_verb(self, tokens: list[str]):
        """``"root"`` / ``"unroot"`` when the line restarts this device's adbd."""
        command = self._local_adb_command(tokens)
        if command is None or command[0] not in ("root", "unroot") or command[1]:
            return None
        return command[0]

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
            # CMD's copyright banner, also after Stop reopens the shell
            self._strip_startup_banner = True
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
            if self._adb_interrupt_pending:
                self._adb_interrupt_heard = True
            if self._in_adb_shell:
                self._notice_refusal(data)
            self._track_prompt_cwd(data)
            self.term.feed(data)

    _PS_PROMPT_TAIL = re.compile(r"(?:^|[\r\n])PS ([^\r\n>]+)> ?$")
    _CMD_PROMPT_TAIL = re.compile(r"(?:^|[\r\n])([A-Za-z]:\\[^\r\n<>|*?\"]*)>$")
    # an Android shell prompt ("PD2318:/sdcard $ ", "/ # ") ending the output
    _DEVICE_PROMPT_TAIL = re.compile(r"[:/][^\r\n]* [$#] ?$")
    _ADB_ERROR_LINE = re.compile(r"(?:^|[\r\n])(?:adb(?:\.exe)?|error): ")

    def _track_prompt_cwd(self, data) -> None:
        """Follow the folder the shell reports in its own prompt.

        Parsing typed ``cd`` lines misses ``cd $env:TEMP``, ``pushd``,
        ``Set-Location ~\\x`` and profile functions, which left Tab completion
        (and ``.\\tool.exe`` suggestions) looking in the wrong folder and made
        Stop reopen the shell somewhere else."""
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        tail = (getattr(self, "_prompt_tail", "") + data)[-1024:]
        self._prompt_tail = tail
        if self._in_adb_shell:
            self._track_adb_shell_output(tail)
            return
        match = self._local_prompt_match(tail)
        if match:
            self._shell_cwd = match.group(1)

    def _local_prompt_match(self, tail: str):
        pattern = self._PS_PROMPT_TAIL if self.shell_type == "powershell" else self._CMD_PROMPT_TAIL
        match = pattern.search(tail)
        return match if match and os.path.isdir(match.group(1)) else None

    def _track_adb_shell_output(self, tail: str) -> None:
        """Follow an adb shell started here from its output.

        The device prompt settles a pending Ctrl+C and, after a reopen, moves
        back to the device folder. PowerShell's or CMD's own prompt after the
        adb shell answered means it ended by itself (``exit`` in a script, the
        device unplugged), so Tab completion and Stop act locally again."""
        text = strip_ansi(tail)
        if self._DEVICE_PROMPT_TAIL.search(text):
            self._adb_answered = True
            self._clear_adb_interrupt()
            cwd, self._adb_reentry_cwd = self._adb_reentry_cwd, None
            if cwd and self.session and self.session.running:
                self.session.send(("cd " + shlex.quote(cwd) + "\r\n").encode("utf-8"))
            return
        if self._ADB_ERROR_LINE.search(text):
            self._adb_answered = True
        if not self._adb_answered:
            return
        match = self._local_prompt_match(tail)
        if match:
            self.reset_adb_shell_context()
            self._shell_cwd = match.group(1)
            self.term._cwd = self._shell_cwd
            if self._adb_resume is not None and time.monotonic() < self._adb_resume_until:
                self._resume_adb_shell_now()  # adbd restarted and the device is back

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
            # typing takes over: an adb shell waiting to reopen stays closed
            self._adb_resume = None
            # Once `adb shell` is interactive, its commands and paths belong
            # to Android. Never apply Windows path completion to them.
            was_in_adb_shell = self._in_adb_shell
            if was_in_adb_shell:
                self._track_adb_shell_directory(line)
                # input after a Ctrl+C: the next Stop sends Ctrl+C again
                self._clear_adb_interrupt()
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
            if was_in_adb_shell and line.lower() in ("exit", "exit 0", "logout"):
                self.reset_adb_shell_context()

            tokens = line.split()
            local_reboot_mode = (
                self._local_adb_reboot_mode(tokens) if not was_in_adb_shell else None
            )
            adbd_restart = (
                self._local_adbd_restart_verb(tokens) if not was_in_adb_shell else None
            )
            adb_shell = None
            if len(tokens) == 2 and tokens[0].lower() == "adb" and tokens[1].lower() == "shell":
                adb_shell = "adb shell -t -t"
            elif len(tokens) == 4 and tokens[0].lower() == "adb" and tokens[1].lower() in ("-s", "-t") and tokens[3].lower() == "shell":
                adb_shell = f"adb {tokens[1]} {tokens[2]} shell -t -t"
            if adb_shell is not None:
                data = self._enter_adb_shell(adb_shell)
                if hasattr(self.term, "_pending_echo"):
                    self.term._pending_echo = adb_shell

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
            adbd_restart = None

        self.session.send(data)
        if adbd_restart:
            # After the command is on its way: the device tab holds the other
            # terminals and brings them back once adbd has restarted.
            self.adbd_restart_requested.emit(adbd_restart)

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

    # how long the adb shell gets to answer a Ctrl+C before it is reopened
    ADB_INTERRUPT_WAIT_MS = 3000

    def interrupt(self):
        """Stop the running command.

        Inside an adb shell started here, Ctrl+C goes to the device: the shell
        runs on a device terminal (``-t -t``), so only the device command stops
        and the adb shell stays. If the adb shell doesn't answer, or Stop is
        pressed again before its prompt is back, the same adb shell is reopened
        in the same device folder. A local command is hard-stopped, since over
        pipes a Ctrl+C byte is only input."""
        if self._closing or not (self.session and self.session.running):
            return
        if self._in_adb_shell:
            if self._adb_interrupt_pending:
                self._reopen_adb_shell()
            else:
                self._adb_interrupt_pending = True
                self._adb_interrupt_heard = False
                self.session.send(b"\x03")
                self._adb_interrupt_timer.start(self.ADB_INTERRUPT_WAIT_MS)
                self.term.setFocus(Qt.OtherFocusReason)
            return
        self._stop_session(interrupt=True)
        self.reset_adb_shell_context()
        self._started = True
        self.term._echo("\n^C  — stopped; fresh local shell ready\n", theme.ECHO_ERROR)
        self._start_session(show_banner=False)
        self.term.set_alive(True)
        self.term.setFocus(Qt.OtherFocusReason)

    def _clear_adb_interrupt(self) -> None:
        self._adb_interrupt_pending = False
        self._adb_interrupt_heard = False
        self._adb_interrupt_timer.stop()

    def _on_adb_interrupt_timeout(self) -> None:
        if self._closing or not (self._in_adb_shell and self._adb_interrupt_pending):
            return
        if not self._adb_interrupt_heard:
            self._reopen_adb_shell()  # not even an echo: adb itself is stuck

    def _reopen_adb_shell(self) -> None:
        """End a stuck adb shell and start it again in its device folder."""
        command, cwd = self._adb_shell_command, self._adb_shell_cwd
        self._stop_session(interrupt=True)
        self._clear_adb_interrupt()
        self._started = True
        if command:
            self.term._echo(f"\n^C  — stopped; reopening adb shell in {cwd}\n", theme.ECHO_ERROR)
        else:
            self.reset_adb_shell_context()
            self.term._echo("\n^C  — stopped; fresh local shell ready\n", theme.ECHO_ERROR)
        self._start_session(show_banner=False)
        if not (self.session and self.session.running):
            self.reset_adb_shell_context()  # _start_session has shown why
            return
        if command:
            self._send_adb_shell(command, cwd)
        self.term.set_alive(True)
        self.term.setFocus(Qt.OtherFocusReason)

    def _send_adb_shell(self, command: str, cwd: str) -> None:
        """Start *command* (an adb shell) in this session and go back to *cwd*
        on the device once its prompt shows."""
        line = self._enter_adb_shell(command)
        self._adb_shell_cwd = self.term._cwd = cwd
        self._adb_reentry_cwd = cwd if cwd != "/" else None
        self.session.send(line)

    def hold_adb_shell(self) -> None:
        """adbd is about to restart (adb root / unroot, a reboot): remember an
        adb shell started here, so :meth:`resume_adb_shell` can reopen it."""
        if self._in_adb_shell and self._adb_shell_command:
            self._adb_resume = (self._adb_shell_command, self._adb_shell_cwd)
            self._adb_resume_until = 0.0

    def resume_adb_shell(self) -> None:
        """The device is back: reopen the remembered adb shell in its folder.

        At once when this terminal is at its own prompt already; otherwise as
        soon as that prompt shows (the adb shell may still be ending), for a
        short while only. If adbd did not actually restart, the adb shell is
        still running and nothing is typed into it."""
        if self._adb_resume is None or self._closing:
            return
        if not (self.session and self.session.running):
            self._adb_resume = None
            return
        if self._in_adb_shell:
            self._adb_resume_until = time.monotonic() + self.ADB_RESUME_WAIT_S
            return
        self._resume_adb_shell_now()

    def _resume_adb_shell_now(self) -> None:
        (command, cwd), self._adb_resume = self._adb_resume, None
        if not (self.session and self.session.running):
            return
        self.term._echo(f"\n↻  device back — reopening adb shell in {cwd}\n", theme.ECHO_WARN)
        self._send_adb_shell(command, cwd)

    def reopen(self):
        self._stop_session()
        self.term.clear()
        self._closing = False
        self._adb_resume = None
        self.reset_adb_shell_context()
        self._started = False
        self.ensure_started()

    def _announce_adb_restart(self) -> None:
        self.term._echo("\nRestarting shared ADB server…\n", theme.ECHO_WARN)

    def close_panel(self):
        self._closing = True
        self._adb_interrupt_timer.stop()
        self._park_completion_thread()
        self._stop_session()
        try:
            self.term.close_archive()
        except Exception:
            pass


class _AndroidShellWidget(_TerminalWidgetBase):
    """A native interactive ``adb shell`` with local prompt emulation and auto-completion."""
    disconnected = pyqtSignal()

    def __init__(self, handler, device_name="", info=None, parent=None, *, autostart=True):
        """*autostart=False* opens the ``adb shell`` only when the widget is
        first shown (or :meth:`ensure_started` is called): it is a persistent
        adb process, and a tab whose Terminal is never on screen needs none."""
        super().__init__(handler, parent)
        self.setObjectName("terminalPanel")
        self.device_name = device_name or (handler.serial or "android")
        self._info = info or {}
        self._pt = None
        self._started = False
        self._banner_shown = False
        self._banner_pending = False
        self._banner_waited = False
        self._adb_restart_paused = False
        # "unknown": the next shell open asks the device for user@host;
        # "pending": the tab's connect-time probe will deliver it;
        # "known": delivered, so reopening after Stop asks nothing.
        self._prompt_state = "unknown"
        self._prompt_root = False

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
        if autostart:
            self.ensure_started()

    def ensure_started(self):
        """Open the interactive shell the first time the terminal is needed."""
        if not self._started and not self._closing and self.handler:
            self._open()

    def showEvent(self, event):
        super().showEvent(event)
        self.ensure_started()

    def _complete(self, line):
        """Schedule an ADB completion query instead of blocking the Tab key."""
        return self._start_async_completion(line, self._query_complete)

    def _query_complete(self, line):
        return self._query_android_completion(line, getattr(self.term, "_cwd", "/"))

    def _open(self, *, focus=True):
        if not self.handler:
            return
        self._started = True
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

        self.term.set_prompt(self._prompt_identity(self._prompt_root), root=self._prompt_root)
        if not self._banner_shown and self.term._alive and not (
            self._info.get("kind") or self._banner_waited
        ):
            # Give the device-type probe a moment so the banner shows the full
            # identity (type, CPU, display); never wait longer than 2.5 s.
            self._banner_pending = True
            QTimer.singleShot(2500, self._flush_pending_banner)
        else:
            self._show_banner_and_prompt()
        if self._prompt_state == "unknown":
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
        """Full device details arrived; show the waiting banner with them.

        They are final: a shell that opens later (the Terminal is opened when
        first shown) shows its banner at once instead of waiting 2.5 s for a
        device-type probe that has already answered, or never runs."""
        self._info = dict(details or {})
        self._banner_waited = True
        if self._banner_pending and not self._closing:
            self._show_banner_and_prompt()

    def expect_prompt(self):
        """The tab's connect-time probe will deliver ``user@host``: opening
        the shell before it arrives must not start a second query."""
        if self._prompt_state != "known":
            self._prompt_state = "pending"

    def apply_prompt(self, is_root, identity):
        """The probe's answer (``identity`` empty: it had none)."""
        if identity:
            self._on_prompt(is_root, identity)
        else:
            self.prompt_unavailable()

    def prompt_unavailable(self):
        """The probe could not tell: ask the device the usual way."""
        if self._prompt_state != "pending":
            return
        self._prompt_state = "unknown"
        if self.session is not None and not self._closing:
            self._start_prompt_probe()  # the shell is already open

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
            self._notice_refusal(data)
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
        # adbd may have restarted (reboot, adb root): ask for user@host again.
        self._prompt_state = "unknown"
        if not self._started:
            return  # never shown: the shell opens when the Terminal first is
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
        if self._closing or not self._started:
            return
        self._adb_restart_paused = True
        self._close_stream()
        self.term.set_alive(False, show_disconnect_notice=False)
        self.term._echo(
            f"\n  ↻  {reason} — this shell will reconnect automatically.\n\n",
            theme.ECHO_WARN,
        )

    def _on_prompt(self, is_root, identity=""):
        # The prompt copies the device's own user@host (root@adelegg), not the
        # friendly model name shown in the banner.
        if identity:
            self._prompt_id = identity
            self._prompt_state = "known"
        self._prompt_root = bool(is_root)
        if not self._closing:
            self.term.set_prompt(self._prompt_identity(is_root), is_root)

    def _prompt_identity(self, root=False):
        """The device's ``user@host`` once known, else ``shell@<codename>``."""
        if getattr(self, "_prompt_id", ""):
            return self._prompt_id
        host = (self._info or {}).get("device") or "android"
        return f"{'root' if root else 'shell'}@{host}"

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
    adbd_restart_requested = pyqtSignal(str)
    access_refused = pyqtSignal(str)
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

        # Opened when the Terminal is first on screen, not when the tab connects.
        self.android_widget = _AndroidShellWidget(
            handler, device_name=device_name, info=info, autostart=False
        )
        self.android_widget.log.connect(self.log)
        self.android_widget.disconnected.connect(self.disconnected)
        self.android_widget.access_refused.connect(self.access_refused)
        self.subtabs.addTab(self.android_widget, "Android shell")

        serial = getattr(handler, "serial", None)
        self.ps_widget = _LocalShellWidget("powershell", serial=serial, handler=handler)
        self.subtabs.addTab(self.ps_widget, "PowerShell")
        self.cmd_widget = _LocalShellWidget("cmd", serial=serial, handler=handler)
        self.subtabs.addTab(self.cmd_widget, "Command Prompt")
        for local in (self.ps_widget, self.cmd_widget):
            local.log.connect(self.log)
            local.adb_reboot_requested.connect(self.adb_reboot_requested)
            local.adbd_restart_requested.connect(self.adbd_restart_requested)
            local.access_refused.connect(self.access_refused)
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
        for local in (self.ps_widget, self.cmd_widget):
            local.hold_adb_shell()  # reopened by resume_adb_shells once it is back
            local.reset_adb_shell_context()

    def hold_for_adbd_restart(self, reason: str) -> None:
        """adbd restarts (adb root / unroot, making files writable): the Android
        shell pauses quietly and adb shells in PowerShell/CMD are remembered."""
        self.android_widget._pause_stream(reason)
        for local in (self.ps_widget, self.cmd_widget):
            local.hold_adb_shell()

    def resume_adb_shells(self) -> None:
        """The device is back: PowerShell/CMD reopen the adb shells they had."""
        for local in (self.ps_widget, self.cmd_widget):
            local.resume_adb_shell()

    def interrupt(self):
        curr = self.subtabs.currentWidget()
        if hasattr(curr, "interrupt"):
            curr.interrupt()

    def close_panel(self):
        self.android_widget.close_panel()
        self.ps_widget.close_panel()
        self.cmd_widget.close_panel()


def choose_control_layout(width, height, aspect, side_width, strip_height, current="side", margin=1.08):
    """``"side"`` or ``"below"``: where the device controls go so the device
    screen (*aspect* = width / height) is shown largest in a *width* x *height*
    area. The current choice is kept unless the other is clearly (*margin*)
    larger, so resizing near the boundary doesn't flip back and forth."""
    aspect = aspect if aspect and aspect > 0 else 16.0 / 9.0
    side = min(max(0.0, width - side_width), max(0.0, height) * aspect)
    below = min(max(0.0, width), max(0.0, height - strip_height) * aspect)
    if current == "below":
        return "side" if side > below * margin else "below"
    return "below" if below > side * margin else "side"


def plan_side_layout(width, height, aspect, chrome, toolbar_width, options, *,
                     frame=4, screen_min=300):
    """Split a Device Control row between the screen card and the controls
    beside it: ``(screen_width, controls_width, columns)``.

    *chrome* is the screen card's height above the picture with a one-row
    toolbar, *toolbar_width* the width that toolbar needs, and *options* the
    controls' ``(columns, narrowest, widest, height)`` layouts (see
    ``ControlsPanel.layout_options``; widths and heights of the whole card).

    * The screen card asks for the width its picture can use at this height —
      and at least its one-row toolbar, since every wrapped toolbar row costs
      the picture height.
    * The controls take the fewest columns that show every control without
      scrolling (fewer, fuller columns instead of a wide panel with empty space
      below it), or else the most columns that fit; each column stops growing
      at the panel's comfortable maximum.
    * Whatever is left goes to the screen card, which centres the picture.
    """
    aspect = aspect if aspect and aspect > 0 else 16.0 / 9.0
    width, height = max(0, int(width)), max(0, int(height))
    options = sorted(options) or [(1, 340, 400, 0)]
    first = options[0]
    picture = int(max(0, height - chrome) * aspect) + frame
    wanted = max(picture, int(toolbar_width), screen_min)
    wanted = min(wanted, max(screen_min, width - first[1]))  # one column always fits
    room = width - wanted
    fitting = [option for option in options if option[1] <= room] or [first]
    unscrolled = [option for option in fitting if option[3] <= height]
    columns, narrowest, widest, _height = unscrolled[0] if unscrolled else fitting[-1]
    controls = max(narrowest, min(widest, room))
    controls = max(1, min(controls, width - min(screen_min, width // 2)))
    return width - controls, controls, columns


class _ControlView(QWidget):
    """Device Control: the device screen with its controls beside or below it.

    The controls go wherever the screen ends up larger for this window and the
    screen's shape (choose_control_layout): a portrait phone keeps them at the
    side, a wide head-unit screen in a wide window gets them as a strip below.
    Beside the screen, plan_side_layout sizes both cards: the picture as large
    as the height allows with a one-row screen toolbar, the controls in the
    fewest comfortable columns that need no scrolling, and any spare width
    around the picture.

    Maximize view (the screen toolbar, Esc to restore) hides the controls so
    the screen card fills the tab; restoring brings back exactly what was shown.
    """

    SIDE_WIDTH = 360  # the controls' single-column width
    STRIP_MAX = 0.45  # a strip never takes more than this share of the height
    # Until the cards are laid out and can be measured: the controls card's
    # caption, and a card's border + margin on both sides.
    CARD_TITLE = 35
    CARD_FRAME = 4

    def __init__(self, mirror, screen_card, controls, controls_card, parent=None, toggle=None):
        super().__init__(parent)
        self.mirror = mirror
        self.screen_card = screen_card
        self.controls = controls
        self.controls_card = controls_card
        self.toggle = toggle  # the screen toolbar's show/hide-controls button
        self.mode = "side"
        self._user_sized = False
        self._applying = False
        self._controls_wanted = True  # the user's latest show/hide choice
        self._maximized = False
        # Maximize view hides the controls until the user asks for them again
        self._max_hides_controls = False

        self.split = QSplitter(Qt.Horizontal)
        self.split.setHandleWidth(6)
        self.split.addWidget(screen_card)
        self.split.addWidget(controls_card)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setSizes([700, self.SIDE_WIDTH])
        self.split.setChildrenCollapsible(False)
        self.split.splitterMoved.connect(self._on_splitter_moved)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(self.split)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._apply)
        mirror.display_shape_changed.connect(self.schedule_layout)
        mirror.toolbar_changed.connect(self.schedule_layout)
        mirror.max_view_changed.connect(self.set_maximized)
        if toggle is not None:
            toggle.toggled.connect(self.set_controls_visible)
        self._sync_controls()

    # ---- showing and hiding the controls ----
    def controls_visible(self) -> bool:
        """Whether the controls panel is shown: the user's choice, unless
        Maximize view hid the controls and the user hasn't asked for them since."""
        return self._controls_wanted and not self._max_hides_controls

    def set_controls_visible(self, visible) -> None:
        """The user's Show / Hide controls choice. It works while maximized too
        (the view stays maximized) and is what Restore view keeps."""
        self._controls_wanted = bool(visible)
        self._max_hides_controls = False
        self._sync_controls()

    def set_maximized(self, on) -> None:
        """Maximize view: the screen card fills the tab; off shows the
        controls as the user last chose."""
        on = bool(on)
        if on == self._maximized:
            return
        self._maximized = on
        self._max_hides_controls = on
        self._sync_controls()

    def _sync_controls(self) -> None:
        shown = self.controls_visible()
        self.controls_card.setVisible(shown)
        toggle = self.toggle
        if toggle is not None:
            # the button always says what a click does next, maximized or not
            toggle.blockSignals(True)
            toggle.setChecked(shown)
            toggle.blockSignals(False)
            text = "Hide controls" if shown else "Show controls"
            toggle.setText(text)
            toggle.setAccessibleName(text)
            toggle.setToolTip(
                "Hide the device controls panel" if shown else "Show the device controls panel"
            )
        self.schedule_layout()
        QTimer.singleShot(0, self.mirror._fit)

    # ---- layout ----
    def schedule_layout(self, *_args):
        self._timer.start(40)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.schedule_layout()

    def _on_splitter_moved(self, *_args):
        if not self._applying:
            self._user_sized = True  # respect a dragged divider until the mode changes

    def _card_frame(self) -> int:
        """Width a card adds around its page (border and margin, both sides)."""
        card, inner = self.screen_card, self.mirror
        if card.width() > 1 and inner.width() > 1 and card.width() > inner.width():
            return card.width() - inner.width()
        return self.CARD_FRAME

    def _card_title(self) -> int:
        card, inner = self.controls_card, self.controls
        if card.height() > 1 and inner.height() > 1 and card.height() > inner.height():
            return card.height() - inner.height() - self._card_frame()
        return self.CARD_TITLE

    def _controls_options(self):
        """The controls' column layouts as card sizes (frame and caption added)."""
        frame, title = self._card_frame(), self._card_title()
        return [
            (columns, narrowest + frame, widest + frame, height + title + frame)
            for columns, narrowest, widest, height in self.controls.layout_options()
        ]

    def _apply(self):
        width, height = self.split.width(), self.split.height()
        if width < 200 or height < 150 or not self.controls_card.isVisibleTo(self):
            return
        aspect = self.mirror.display_aspect()
        frame = self._card_frame()
        toolbar = self.mirror.toolbar_width() + frame
        # the picture's height with a one-row toolbar (+ the screen card's frame)
        chrome = self.mirror.chrome_height(toolbar - frame) + frame
        handle = self.split.handleWidth()
        strip = min(
            self.controls.content_height(width, strip=True) + self._card_title() + frame,
            int(height * self.STRIP_MAX),
        )
        mode = choose_control_layout(
            width - handle, height - chrome, aspect, self.SIDE_WIDTH, strip + handle, current=self.mode
        )
        self._applying = True
        try:
            if mode != self.mode:
                self.mode = mode
                self._user_sized = False
                self.split.setOrientation(Qt.Vertical if mode == "below" else Qt.Horizontal)
                self.controls.set_strip(mode == "below")
            if not self._user_sized:
                if mode == "below":
                    self.split.setSizes([max(1, height - strip - handle), strip])
                else:
                    screen, controls, _columns = plan_side_layout(
                        width - handle, height, aspect, chrome, toolbar, self._controls_options(),
                        screen_min=self.screen_card.minimumWidth(),
                    )
                    self.split.setSizes([screen, controls])
        finally:
            self._applying = False
        QTimer.singleShot(0, self.mirror._fit)


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


class _LazyPage(QWidget):
    """A section page that is built the first time it is shown.

    Logcat, Files, Apps, Phone and Webcam hold widgets, caches and (for Files
    and Webcam) start-up workers that an unvisited page never needs.  This
    holder is the persistent page object for the tab order and split view;
    :attr:`page` is the real panel once built.
    """

    def __init__(self, factory, parent=None):
        super().__init__(parent)
        self._factory = factory
        self.page = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

    def ensure_built(self):
        """Build the page now if it wasn't yet; returns it (None once released)."""
        if self.page is None and self._factory is not None:
            factory, self._factory = self._factory, None
            self.page = factory()
            self.layout().addWidget(self.page)
            if self.isVisible():
                self.page.show()
        return self.page

    def showEvent(self, event):
        super().showEvent(event)
        self.ensure_built()

    def release(self):
        """Never build after the tab closed; returns the page if it was built."""
        self._factory = None
        return self.page


def _lazy_page(key):
    """``DeviceTab.logcat`` and friends: the real page, built on first use,
    so code that reads the attribute keeps working while unvisited pages cost
    nothing.  Missing (AttributeError) before the tab has connected."""

    def get(self):
        holder = self.__dict__.get("_lazy_pages", {}).get(key)
        if holder is None:
            raise AttributeError(key)
        return holder.ensure_built()

    return property(get)


class DeviceTab(QWidget):
    log = pyqtSignal(str)
    # Engine diagnostic lines — for the log panel only, never a notification.
    trace = pyqtSignal(str)
    title_changed = pyqtSignal(str)
    # Relays Device Control's screen session state (True while video shows).
    screen_active = pyqtSignal(bool)
    # Once per tab, after its first successful connect has named the tab
    # (a later reconnect is not a new connection).
    connected = pyqtSignal()
    # progress of make_files_writable, from its worker thread
    access_step = pyqtSignal(str)
    # Another terminal tab for this device, please (More -> New terminal
    # session, or right-click the Terminal tab). MainWindow opens the same
    # extra terminal session that opening the device a second time offers.
    terminal_session_requested = pyqtSignal()

    # Built on first show (see _LazyPage); Terminal and Device Control are not.
    logcat = _lazy_page("logcat")
    files = _lazy_page("files")
    apps = _lazy_page("apps")
    phone = _lazy_page("phone")
    webcam = _lazy_page("webcam")
    # One-shot adb commands of one tab running at the same time (_AdbGate).
    ADB_SLOTS = 2

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
        # adb root / unroot / make writable running: terminals are held
        self._adbd_busy = False
        self._access_declined = {}  # device folder -> when its write-access offer was declined
        self.access_step.connect(self._on_access_step)
        self._info_thread = None
        self._probe_thread = None
        self._lazy_pages = {}
        self._adb_gate = _AdbGate(self.ADB_SLOTS)
        self._device_dispatcher = None
        self._session_closed = False
        self._announced_connected = False
        self._subtab_meta = {}
        # More Files tabs on this connection: "files-2" -> FileBrowser. The
        # first Files tab stays the lazy page in _lazy_pages["files"].
        self._extra_files = {}
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
        new_files = more_menu.addAction(
            icons.icon("plus", "blue"), "New Files tab", self._new_files_tab_here
        )
        new_files.setToolTip(
            "Open another Files tab on this device - Ctrl+Shift+T inside Files does "
            "the same, starting in that tab's folders"
        )
        new_terminal = more_menu.addAction(
            icons.icon("terminal", "teal"), "New terminal session",
            self.request_terminal_session,
        )
        new_terminal.setToolTip(
            "Open another terminal tab for this device, with its own Android shell, "
            "PowerShell and Command Prompt"
        )

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
        amenu.addAction("adb root", lambda: self._adbd_action("root", lambda h: h.root(safe=True)))
        amenu.addAction(
            "adb unroot", lambda: self._adbd_action("unroot", lambda h: h.unroot(safe=True)))
        writable = amenu.addAction("Make files writable…", lambda: self.make_files_writable())
        writable.setToolTip("Choose adb root, disable-verity and remount; reboots when needed")
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
            # Files tabs: right-click for New / Close; a middle-click closes an
            # extra one (see eventFilter)
            tb.setContextMenuPolicy(Qt.CustomContextMenu)
            tb.customContextMenuRequested.connect(self._tab_context_menu)
            tb.installEventFilter(self)
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
            for sig in ("ok", "details", "fail", "log", "trace"):
                try:
                    getattr(previous, sig).disconnect()
                except Exception:
                    pass
            if thread_running(previous):
                previous.cancel()
                park_thread(previous)
        cfg = config_from_session(self.session)
        self._ct = _ConnectThread(
            cfg, fetch_identity=not self._terminal_only, gate=self._adb_gate
        )
        self._ct.ok.connect(self._on_connected)
        self._ct.details.connect(self._on_probe_details)
        self._ct.fail.connect(self._on_fail)
        self._ct.log.connect(self.log)
        self._ct.trace.connect(self.trace)
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
            self._adb_gate.wrap(handler.get_state),
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
                self.shell.resume_adb_shells()
            except Exception as exc:
                self.log.emit(f"[ERROR] shell reconnect: {exc}")
        else:
            self.status.setText("Device didn't come back (timed out)")
            self._offer_reconnect()
            self.log.emit(
                "[ERROR] device did not return to 'device' state (timed out). "
                "If it's in recovery/bootloader this is expected; otherwise replug and click Reconnect."
            )

    def _run_action(self, label, fn, on_ok, *, on_error=None, gated=True):
        """Run one engine call off the UI thread and report its outcome.

        *fn* calls the engine with ``safe=True``.  A failed ``OperationResult``
        and a raised exception both go to *on_error* (default: an
        ``[ERROR] label: …`` log line); success passes the unwrapped value to
        *on_ok*.  The call waits for one of the tab's adb slots (_AdbGate)
        unless *gated* is False: a minutes-long bugreport must not hold one,
        and a reboot the header already announces must not wait for one.
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

        run_job(self._threads, self._adb_gate.wrap(fn) if gated else fn, finished, report)

    def _op(self, label, fn):
        if not self.handler:
            return
        handler = self.handler
        self.log.emit(f"[INFO] {label}…")
        self._run_action(
            label,
            lambda: fn(handler),
            lambda value: self.log.emit(f"[OK] {label}: {value}"),
        )

    # ---- adbd restarts (root / unroot) and write access ----
    # the typed adb root/unroot gets this long to take adbd down before waiting
    ADBD_RESTART_SETTLE_S = 2.0
    # a declined write-access offer for a folder is not repeated for this long
    ACCESS_DECLINE_S = 60.0

    def _adbd_can_restart(self) -> bool:
        if not self.handler or self._session_closed:
            return False
        if self._adbd_busy or self._reboot_in_progress or self._adb_restart_in_progress:
            self.log.emit("[INFO] Wait for the running device restart to finish first.")
            return False
        return True

    def _hold_terminals(self, reason: str) -> None:
        """adbd is about to restart: every terminal waits for it instead of
        reporting a lost device, and follows it afterwards."""
        self._adbd_busy = True
        self._enable_actions(False)
        shell = getattr(self, "shell", None)
        if shell is not None:
            shell.hold_for_adbd_restart(reason)

    def _release_terminals(self) -> None:
        """adbd is back (as root or not): the Android shell reconnects and asks
        for its prompt again (``#`` as root), PowerShell/CMD reopen their adb
        shells, and a built Files page lists its folder again."""
        self._adbd_busy = False
        if self._session_closed:
            return
        self._enable_actions(True)
        shell = getattr(self, "shell", None)
        if shell is not None:
            try:
                shell.reconnect()
                shell.resume_adb_shells()
            except Exception as exc:
                self.log.emit(f"[ERROR] shell reconnect: {exc}")
        for page in self._files_pages():
            page.refresh_remote()

    def _adbd_action(self, verb: str, fn) -> None:
        """More → Root and mount → adb root / adb unroot."""
        if not self._adbd_can_restart():
            return
        handler = self.handler
        label = f"adb {verb}"
        self.log.emit(f"[INFO] {label}…")
        self._hold_terminals(f"{label} — adbd restarting")

        def done(value):
            self.log.emit(f"[OK] {label}: {value}")
            self._release_terminals()

        def failed(message):
            self.log.emit(f"[ERROR] {label}: {message}")
            self._release_terminals()

        self._run_action(label, lambda: fn(handler), done, on_error=failed, gated=False)

    def _on_local_adbd_restart(self, verb: str) -> None:
        """``adb root`` / ``adb unroot`` was typed in PowerShell or CMD: hold the
        other terminals, wait for adbd to come back, then bring them along."""
        if not self._adbd_can_restart():
            return
        handler = self.handler
        self.log.emit(f"[INFO] adb {verb} typed in a terminal — the other terminals follow")
        self._hold_terminals(f"adb {verb} — adbd restarting")
        settle = self.ADBD_RESTART_SETTLE_S

        def wait():
            time.sleep(settle)  # let the typed command take adbd down first
            return handler.wait_for_device(20, safe=True)

        self._run_action(f"adb {verb}", wait, lambda _value: self._release_terminals(),
                         on_error=lambda _message: self._release_terminals(), gated=False)

    def _on_access_refused(self, line: str) -> None:
        """A terminal printed a refusal: offer write access in a toast, never a dialog."""
        if self._session_closed or self._adbd_busy or not self.handler:
            return
        fileutil.activity_toast(
            self.window(),
            f"The device refused: {line}",
            level="warning",
            action_text="Make writable…",
            action=lambda: self.make_files_writable(error=line),
        )

    def make_files_writable(self, *, path: str = "", error: str = "", action: str = "",
                            retry=None) -> None:
        """Ask which of adb root / disable-verity / remount to run, run them
        (rebooting when a step needs it), then call *retry*.

        Used by the Files page after a refused change, by a terminal's toast
        and by More → Root and mount → Make files writable."""
        if not self._adbd_can_restart():
            return
        declined = self._access_declined.get(path)
        if error and declined is not None and time.monotonic() - declined < self.ACCESS_DECLINE_S:
            return  # declined moments ago for this folder; the error is in the log
        handler = self.handler
        self._run_action(
            "device access state",
            lambda: handler.access_status(safe=True),
            lambda status: self._ask_write_access(status, path, error, action, retry),
            on_error=lambda _message: self._ask_write_access(None, path, error, action, retry),
        )

    def _ask_write_access(self, status, path, error, action, retry) -> None:
        if self._session_closed or not self.handler or self._adbd_busy:
            return
        dialog = device_access.WriteAccessDialog(status, path=path, error=error, action=action,
                                                 can_retry=retry is not None, parent=self)
        try:
            accepted = dialog.exec_() == QDialog.Accepted
            choices = dialog.choices()
        finally:
            dialog.deleteLater()
        if not accepted:
            self._access_declined[path] = time.monotonic()
            return
        wants_retry = choices.pop("retry")
        self._run_make_writable(choices, retry if wants_retry else None)

    def _run_make_writable(self, choices: dict, retry) -> None:
        if not self._adbd_can_restart():
            return
        handler = self.handler
        steps = [name for name, key in (("root", "root"), ("disable-verity", "disable_verity"),
                                        ("remount", "remount")) if choices.get(key)]
        self.log.emit(f"[INFO] making device files writable: {', '.join(steps)}…")
        self.status.setText("Making device files writable…")
        self._hold_terminals("Making device files writable — the device may reboot")
        step_signal = self.access_step

        def work():
            return handler.make_writable(on_step=step_signal.emit, safe=True, **choices)

        def connected_text():
            return f"Connected — {handler.serial or self.session.get('name')}"

        def done(report):
            self._release_terminals()
            if self._session_closed:
                return
            self.status.setText(connected_text())
            if report.get("reboot_needed"):
                self.log.emit("[WARNING] make writable: reboot the device for the changes "
                              "to take effect")
                return
            self.log.emit("[OK] device files are writable"
                          + (" (the device rebooted)" if report.get("rebooted") else ""))
            if retry is not None:
                retry()

        def failed(message):
            self._release_terminals()
            if self._session_closed:
                return
            self.status.setText(connected_text())
            self.log.emit(f"[ERROR] make writable: {message}")

        self._run_action("make writable", work, done, on_error=failed, gated=False)

    def _on_access_step(self, text: str) -> None:
        if self._session_closed or not self._adbd_busy:
            return
        self.status.setText(f"Making device files writable: {text}…")
        self.log.emit(f"[INFO] make writable: {text}")

    def _verity(self, enable):
        if not self.handler:
            return
        handler = self.handler
        label = "enable-verity" if enable else "disable-verity"

        def work():
            out = (handler.enable_verity if enable else handler.disable_verity)(safe=True)
            handler.shell("sync", safe=True)
            return out

        self.log.emit(f"[INFO] {label}…")
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

    def _announce_connected(self) -> None:
        if self._session_closed or self.handler is None or self._announced_connected:
            return
        self._announced_connected = True
        self.connected.emit()

    def _on_connected(self, handler, info=None):
        if self._session_closed or self.handler is not None:
            # A queued result can arrive after the tab closed (or after a
            # retry already connected).  Never build panels for it; release
            # its transport instead.
            self._release_handler(handler)
            return
        self.handler = handler
        self.btn_reconnect.hide()
        payload = dict(info) if isinstance(info, dict) else {}
        # The connect worker's probe (_DeviceProbe) sends the rest of the
        # device profile and the display list later, from the same adb shell.
        probe_pending = bool(payload.pop("_probe_pending", False))
        prompt = payload.pop("_prompt", None)
        quick = payload if payload.pop("_quick_identity", False) else {}
        friendly = " ".join(
            bit for bit in (quick.get("manufacturer"), quick.get("model")) if bit
        ).strip()
        dev_name = friendly or self.session.get("name") or handler.serial or "android"
        self.status.setText(f"Connected — {dev_name}")
        self.title_label.setText(dev_name)
        self.log.emit(f"[OK] {self.session.get('name')}: connected")
        self._enable_actions(True)
        # After this slot has named the tab from the device identity.
        QTimer.singleShot(0, self._announce_connected)

        # Do not block the usable terminal on a complete `getprop` dump.  Some
        # vendor/IVI images take seconds to serve it right after USB comes up.
        # The quick identity above supplies a friendly first banner; remaining
        # details continue loading in the background.
        self.shell = ShellPanel(handler, device_name=dev_name, info=quick)
        self.shell.log.connect(self.log)
        self.shell.disconnected.connect(self._on_shell_lost)
        self.shell.adb_reboot_requested.connect(self._on_local_adb_reboot)
        self.shell.adbd_restart_requested.connect(self._on_local_adbd_restart)
        self.shell.access_refused.connect(self._on_access_refused)
        for terminal in (
            self.shell.android_widget.term,
            self.shell.ps_widget.term,
            self.shell.cmd_widget.term,
        ):
            terminal.installEventFilter(self)
        # Connected without the connect worker (and not handed full details):
        # this tab runs the probe itself, below.
        runs_probe = not (self._terminal_only or probe_pending) and (info is None or bool(quick))
        # Before the Terminal is added: on a visible tab adding it opens the
        # shell, which would otherwise start its own user@host query.
        if prompt is not None:
            self.shell.android_widget.apply_prompt(*prompt)
        elif probe_pending or runs_probe:
            self.shell.android_widget.expect_prompt()
        if self._terminal_only:
            # No identity probe runs for a terminal-only tab: show the banner now.
            self.shell.android_widget.update_identity(quick)
            self._add_subtab(self.shell, "⌨", "Terminal")
            self._subtabs = {"shell": self.shell, "terminal": self.shell}
            if friendly:
                self.title_changed.emit(friendly)
            return
        self.combo_view = self._build_control_view(handler)

        # Logcat, Files, Apps, Phone and Webcam are built when first shown.
        # Phone loads call history and messages only then, too.
        self._add_subtab(self.shell, "⌨", "Terminal")
        self._add_lazy_page("logcat", "📜", "Logcat", self._build_logcat)
        self._add_lazy_page("files", "📁", "Files", self._build_files)
        self._add_subtab(self.combo_view, "🎛", "Device Control")
        self._add_lazy_page("apps", "📦", "Apps", self._build_apps)
        self._add_lazy_page("phone", "📞", "Phone", self._build_phone)
        self._add_lazy_page("webcam", "📹", "Webcam", self._build_webcam)

        pages = self._lazy_pages
        self._subtabs = {
            "shell": self.shell,
            "terminal": self.shell,
            "logcat": pages["logcat"],
            "files": pages["files"],
            "mirror": self.combo_view,
            "controls": self.combo_view,
            "apps": pages["apps"],
            "phone": pages["phone"],
            "webcam": pages["webcam"],
        }
        self.mirror_tab.displays_changed.connect(self._on_displays_found)
        self.mirror_tab.all_displays_requested.connect(self._show_all_displays)

        if probe_pending:
            if friendly:
                self.title_changed.emit(friendly)
            return  # the rest arrives as _on_probe_details

        if info is not None and not quick:
            # Retain the direct-call path used by tests and integrations, but
            # only after all widgets exist so it cannot delay first paint.
            self.mirror_tab.refresh_displays(quiet=True)
            self._on_device_info(handler, info)
            return

        if friendly:
            self.title_changed.emit(friendly)

        # Connected without the connect worker: the same single probe lists the
        # device profile and the displays (plain adb, never scrcpy).
        thread = _ProbeThread(handler, gate=self._adb_gate)
        thread.details.connect(lambda result, h=handler: self._on_probe_details(h, result))
        thread.finished.connect(thread.deleteLater)
        self._probe_thread = thread
        thread.start()

    def _add_lazy_page(self, key, emoji, label, factory):
        holder = _LazyPage(factory)
        self._lazy_pages[key] = holder
        self._add_subtab(holder, emoji, label)
        return holder

    def _built_page(self, key):
        """The page behind *key* if it has been built; never builds it."""
        holder = self._lazy_pages.get(key)
        return holder.page if holder is not None else None

    def _build_logcat(self):
        page = LogcatPanel(self.handler)
        page.log.connect(self.log)
        return page

    def _build_files(self):
        page = FileBrowser(self.handler, start="/sdcard", adb_gate=self._adb_gate)
        self._wire_files_page(page)
        return page

    def _wire_files_page(self, page):
        """Connect a Files page to this tab - the first one and every extra one
        get exactly the same wiring."""
        page.log.connect(self.log)
        page.trace.connect(self.trace)  # per-file batch lines: log panel only
        page.write_access_needed.connect(lambda request: self.make_files_writable(**request))
        page.new_tab_requested.connect(self.open_files_tab)

    # ---- more Files tabs on this connection ------------------------------
    # The first Files tab plus up to seven more: each is a full browser with
    # its own listings and transfer queue, so there is a sensible ceiling.
    MAX_FILES_TABS = 8

    def _files_pages(self):
        """Every built Files page: the first one (if it was ever shown) and
        each extra one."""
        pages = []
        first = self._built_page("files")
        if first is not None:
            pages.append(first)
        pages.extend(self._extra_files.values())
        return pages

    def _extra_files_key(self, widget):
        """The key of an extra Files tab (``"files-2"``), or None."""
        return next(
            (key for key, page in getattr(self, "_extra_files", {}).items() if page is widget),
            None,
        )

    def _files_page_for(self, widget):
        """The FileBrowser behind a tab, if it is a Files tab (None for the
        first Files tab while it was never shown, and for every other tab)."""
        if widget is None:
            return None
        if self._extra_files_key(widget) is not None:
            return widget
        holder = self._lazy_pages.get("files")
        if widget is holder:
            return holder.page
        return None

    def _files_insert_index(self) -> int:
        """Just after the last Files tab, so the Files tabs stay together."""
        widgets = [self._lazy_pages.get("files"), *self._extra_files.values()]
        indices = [self.inner.indexOf(widget) for widget in widgets if widget is not None]
        indices = [index for index in indices if index >= 0]
        return max(indices) + 1 if indices else self.inner.count()

    def open_files_tab(self, remote_path: str = "", local_path: str = ""):
        """Open another Files tab on this connection, starting in *remote_path*
        on the device and *local_path* on the PC (the folders of the tab it was
        opened from). Each tab browses, selects and transfers on its own; they
        share this tab's device session. Returns the page, or None."""
        if self._session_closed or not self.handler or self._terminal_only:
            return None
        if 1 + len(self._extra_files) >= self.MAX_FILES_TABS:
            QMessageBox.information(
                self, "Files",
                f"This device already has {self.MAX_FILES_TABS} Files tabs open. "
                "Close one to open another.",
            )
            return None
        # The new tab must be where the user can see it: leave a split first,
        # before it is registered (leaving rebuilds the tab strip).
        if self._split_keys:
            self._leave_split()
        number = 2
        while f"files-{number}" in self._extra_files:
            number += 1  # the lowest free number: closing "Files 2" frees it
        key, label = f"files-{number}", f"Files {number}"
        page = FileBrowser(
            self.handler, start=remote_path or "/sdcard",
            local_start=local_path or None, adb_gate=self._adb_gate,
        )
        self._wire_files_page(page)
        self._extra_files[key] = page
        tabs = getattr(self, "_subtabs", None)
        if tabs is None:
            tabs = self._subtabs = {}
        tabs[key] = page
        self.inner.insertTab(self._files_insert_index(), page, label)
        self._add_subtab(page, "📁", label)
        self.inner.setCurrentWidget(page)
        return page

    def close_files_tab(self, page) -> bool:
        """Close an extra Files tab (the first Files tab stays). While it is
        still copying, ask first. Returns True when it closed."""
        key = self._extra_files_key(page)
        if key is None:
            return False
        label = self._subtab_meta.get(page, ("", key))[1]
        active = sum(1 for item in page.transfers if item.active)
        if active:
            answer = QMessageBox.question(
                self, f"Close {label}",
                f"{label} is still copying ({active} transfer"
                f"{'' if active == 1 else 's'} running or queued).\n\n"
                "Close it and cancel them?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return False
        if key in self._split_keys:
            self._leave_split()  # puts the page back in the strip, then it goes
        del self._extra_files[key]
        getattr(self, "_subtabs", {}).pop(key, None)
        index = self.inner.indexOf(page)
        if index >= 0:
            self.inner.removeTab(index)
        self._subtab_meta.pop(page, None)
        page.close_panel()
        page.setParent(None)
        page.deleteLater()
        return True

    def _new_files_tab_here(self):
        """More -> New Files tab: start in the folders of the Files tab on
        screen, else of the first Files tab, else /sdcard and the home folder."""
        source = self._files_page_for(self.inner.currentWidget()) or self._built_page("files")
        self.open_files_tab(
            getattr(source, "remote_cwd", "") or "", getattr(source, "local_cwd", "") or ""
        )

    def _attach_close_button(self, index, page):
        """A small x on an extra Files tab; the first Files tab has none."""
        bar = self.inner.tabBar()
        button = QToolButton(bar)
        button.setObjectName("tabCloseButton")
        button.setAutoRaise(True)
        button.setIcon(icons.icon("x", "dim"))
        button.setIconSize(QSize(12, 12))
        button.setFixedSize(18, 18)
        button.setFocusPolicy(Qt.NoFocus)
        button.setCursor(Qt.ArrowCursor)
        button.setToolTip("Close this Files tab (a middle-click on the tab closes it too)")
        button.clicked.connect(lambda _=False, page=page: self.close_files_tab(page))
        bar.setTabButton(index, QTabBar.RightSide, button)

    def request_terminal_session(self) -> None:
        """Ask the main window for another terminal tab for this device."""
        if not self._session_closed:
            self.terminal_session_requested.emit()

    def _tab_context_menu(self, pos):
        """Right-click on a tab: the Terminal tab offers another terminal
        session; a Files tab offers New Files tab here, and Close for an extra
        one. Other tabs have no menu."""
        bar = self.inner.tabBar()
        index = bar.tabAt(pos)
        widget = self.inner.widget(index) if index >= 0 else None
        if widget is None or self._session_closed:
            return
        if widget is getattr(self, "shell", None):
            # also in a terminal-only tab, whose header has no More menu
            menu = QMenu(self)
            menu.addAction(icons.icon("terminal", "teal"), "New terminal session",
                           self.request_terminal_session)
            menu.exec_(bar.mapToGlobal(pos))
            menu.deleteLater()
            return
        extra = self._extra_files_key(widget) is not None
        first = widget is self._lazy_pages.get("files")
        if not (extra or first) or not self.handler or self._terminal_only:
            return
        source = self._files_page_for(widget)
        menu = QMenu(self)
        menu.addAction(
            icons.icon("plus", "blue"), "New Files tab here",
            lambda: self.open_files_tab(
                getattr(source, "remote_cwd", "") or "", getattr(source, "local_cwd", "") or ""
            ),
        )
        if extra:
            menu.addAction(icons.icon("x", "dim"), "Close this Files tab",
                           lambda: self.close_files_tab(widget))
        menu.exec_(bar.mapToGlobal(pos))
        menu.deleteLater()

    def _build_apps(self):
        page = AppsPanel(self.handler, automotive=self._automotive, adb_gate=self._adb_gate)
        page.log.connect(self.log)
        page.trace.connect(self.trace)  # listing counts: log panel only
        return page

    def _build_phone(self):
        page = PhonePanel(self.handler)
        page.log.connect(self.log)
        return page

    def _build_webcam(self):
        page = CameraPanel()
        page.log.connect(self.log)
        return page

    def _on_probe_details(self, handler, result) -> None:
        """The connect-time probe finished: device profile, prompt and displays."""
        if self._session_closed or handler is not self.handler:
            return
        result = result if isinstance(result, dict) else {}
        shell = getattr(self, "shell", None)
        android = getattr(shell, "android_widget", None)
        if android is not None:
            prompt = result.get("prompt")
            if prompt is not None:
                android.apply_prompt(*prompt)
            else:
                android.prompt_unavailable()
            if result.get("info") is None:
                # No profile is coming: the banner keeps the quick identity and
                # shows as soon as the Terminal opens, not 2.5 s later.
                android.update_identity(android._info)
        mirror = getattr(self, "mirror_tab", None)
        if mirror is not None:
            # Displays first: an automotive profile below opens the displays
            # tab, which would otherwise start a scan of its own.
            displays = result.get("displays")
            if displays is not None:
                mirror.set_displays(displays, quiet=True)
            else:
                # The probe did not get that far: the panel's own quiet adb scan.
                mirror.refresh_displays(quiet=True)
        info = result.get("info")
        if info is None:
            error = result.get("error") or "no reply"
            info = OperationResult(False, "device_info", error=error)
        self._on_device_info(handler, info)

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
        apps = self._built_page("apps")  # an unbuilt Apps page reads _automotive when built
        if apps is not None:
            apps.set_automotive_default(self._automotive)
        mirror = getattr(self, "mirror_tab", None)
        if mirror is not None:
            mirror.set_automotive_default(self._automotive)
        wall = getattr(self, "display_wall", None)
        if wall is not None:
            wall.set_automotive(self._automotive)
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
        """Show every display of a multi-display device (an IVI head unit's centre
        stack, cluster, passenger screen…) live, side by side, in its own tab."""
        mirror = getattr(self, "mirror_tab", None)
        if mirror is None or self.handler is None:
            return
        label = "IVI Displays" if self._automotive else "Displays"
        wall = getattr(self, "display_wall", None)
        if wall is not None:
            self._add_subtab(wall, "▦", label)  # e.g. automotive was detected later
            return
        from .display_wall import DisplayWall

        wall = DisplayWall(
            self.handler,
            self.session,
            automotive=self._automotive,
            dispatcher=self._device_dispatcher,
        )
        wall.log.connect(self.log)
        wall.rescan_requested.connect(lambda: mirror.refresh_displays())
        self.display_wall = wall
        self._add_subtab(wall, "▦", label)
        self._subtabs["ivi"] = wall
        displays = list(getattr(mirror, "_displays", None) or [])
        if displays:
            wall.set_displays(displays)
        else:
            mirror.refresh_displays(quiet=True)

    def _on_displays_found(self, displays) -> None:
        """A display scan finished: several displays (or a car) get the displays tab."""
        displays = list(displays or [])
        if len(displays) > 1 or self._automotive:
            self._add_ivi_tab()
        wall = getattr(self, "display_wall", None)
        if wall is not None:
            wall.set_displays(displays)

    def _show_all_displays(self) -> None:
        """Options → All displays: open the displays tab (it starts every display)."""
        self._add_ivi_tab()
        wall = getattr(self, "display_wall", None)
        if wall is None:
            return
        self.show_subtab("ivi")
        if not wall._tiles:
            self.mirror_tab.refresh_displays()

    def _add_subtab(self, widget, emoji, label):
        # The glyph is kept for split-view pane titles; the tab itself shows the
        # section's colour-coded vector icon.
        self._subtab_meta[widget] = (emoji, label)
        idx = self.inner.indexOf(widget)
        if idx < 0:
            idx = self.inner.addTab(widget, label)
        else:
            self.inner.setTabText(idx, label)
        extra = self._extra_files_key(widget) is not None
        glyph, tone = _SECTION_ICONS.get("Files" if extra else label, ("apps", None))
        self.inner.setTabIcon(idx, icons.icon(glyph, tone))
        if extra:
            self._attach_close_button(idx, widget)
        return idx

    def _split_meta(self, key):
        """``(glyph, label)`` of a split-view page, extra Files tabs included;
        None for a key that can't go into a split."""
        if key in _SPLIT_VIEW_META:
            return _SPLIT_VIEW_META[key]
        page = getattr(self, "_extra_files", {}).get(key)
        if page is not None:
            return self._subtab_meta.get(page, ("📁", key))
        return None

    def _split_choices(self):
        """Return persistent workspace pages available for the current device.

        Extra Files tabs come last, so two Files tabs can sit side by side
        (the defaults of the split dialog are unchanged)."""
        tabs = getattr(self, "_subtabs", {})
        choices = [
            (key, icon, label)
            for key, (icon, label) in _SPLIT_VIEW_META.items()
            if tabs.get(key) is not None
        ]
        for key in sorted(getattr(self, "_extra_files", {}), key=_files_tab_number):
            if tabs.get(key) is not None:
                icon, label = self._split_meta(key)
                choices.append((key, icon, label))
        return choices

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
        icon, label = self._split_meta(key)
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
        if any(self._split_meta(key) is None or tabs.get(key) is None for key in keys):
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
        extra = sorted(getattr(self, "_extra_files", {}), key=_files_tab_number)
        for key in ("shell", "logcat", "files", *extra, "controls", "apps", "phone",
                    "webcam", "ivi"):
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
            display_provider=self.mirror_tab.input_display_id,
        )
        self.controls.log.connect(self.log)
        m_card = card("", self.mirror_tab, "mirrorView")
        m_card.setMinimumWidth(340)
        c_card = card("Device controls", self.controls, "sidePanel")
        c_card.setMinimumWidth(280)

        # An icon toggle at the end of the screen toolbar (checked while the
        # controls show), so the toolbar keeps to one row; its label is the
        # tooltip and accessible name. _ControlView keeps it in step.
        btn_tog = QToolButton()
        btn_tog.setObjectName("iconButton")
        btn_tog.setIcon(icons.icon("sliders", "purple"))
        btn_tog.setIconSize(QSize(18, 18))
        btn_tog.setToolButtonStyle(Qt.ToolButtonIconOnly)
        btn_tog.setCheckable(True)
        btn_tog.setChecked(True)
        self._device_controls_card = c_card
        self._device_controls_toggle = btn_tog
        self.mirror_tab.add_toolbar_widget(btn_tog)

        view = _ControlView(self.mirror_tab, m_card, self.controls, c_card, toggle=btn_tog)
        self._control_view = view
        return view

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
        if isinstance(w, _LazyPage):
            w = w.page
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
                        key for key in (*_SPLIT_VIEW_META, *self._extra_files)
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
        if sh is not None and w is sh and not self._session_closed:
            # (closing: the strip rebuilt by _leave_split must not start a
            # PowerShell/CMD session through focus_terminal)
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
        """Let any terminal take focus away from an embedded screen immediately;
        a middle-click on an extra Files tab closes it."""
        inner = getattr(self, "inner", None)
        if (inner is not None and watched is inner.tabBar()
                and event.type() == QEvent.MouseButtonRelease
                and event.button() == Qt.MiddleButton):
            widget = inner.widget(watched.tabAt(event.pos()))
            if widget is not None and self._extra_files_key(widget) is not None:
                self.close_files_tab(widget)
                return True
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
        self.log.emit("[INFO] Reading device health…")
        self._run_action("health", lambda: handler.health_report(safe=True), self._show_health_dialog)

    def show_build(self):
        if not self.handler:
            return
        handler = self.handler
        self.log.emit("[INFO] Reading the build report…")
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
        self.log.emit("[INFO] Switching the device to wireless (USB → Wi-Fi)…")
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
        self.log.emit("[INFO] Capturing a bugreport (this takes a few minutes)…")
        handler = self.handler
        self._run_action(
            "bugreport",
            lambda: handler.bugreport(path, safe=True),
            lambda p: self.log.emit(f"[OK] bugreport saved: {p}"),
            gated=False,
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
            # The header already says "Rebooting…" with actions disabled: the
            # command must go out now, never queue behind busy slots.
            gated=False,
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
        # Background jobs still waiting for an adb slot give up now: nothing
        # may start adb for this tab once it is closing.
        self._adb_gate.close()

        # Nothing may start while the tab is torn down.  Leaving split view
        # below puts the pages back in the tab strip and shows the current one:
        # a first show would open the Terminal's adb shell (only to kill it
        # again) or build a lazy page.  release() drops a page's factory and
        # returns the page if it was built.
        shell = getattr(self, "shell", None)
        if shell is not None:
            shell.android_widget._closing = True
        pages = {key: holder.release() for key, holder in self._lazy_pages.items()}

        # Keep ownership simple while the persistent child panels stop their
        # workers.  This does not recreate anything; it only detaches split
        # wrappers before their real pages are closed below.
        self._leave_split()

        # These workers may have already finished and deleted themselves
        # (finished -> deleteLater), so every Qt call is guarded; one stale
        # wrapper must not abort the rest of the teardown.
        for attr in ("_ct", "_rc", "_info_thread", "_probe_thread"):
            t = getattr(self, attr, None)
            if t is None:
                continue
            try:
                if hasattr(t, "cancel"):
                    t.cancel()
            except RuntimeError:
                pass
            for sig in ("ok", "details", "fail", "log", "done"):
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

        # A page that was never shown was never built (pages[key] is None).
        for attr in (
            "shell", "logcat", "files", "apps", "controls",
            "mirror_tab", "display_wall", "webcam", "phone",
        ):
            p = pages[attr] if attr in pages else self.__dict__.get(attr)
            if p is not None:
                try:
                    p.close_panel()
                except Exception:
                    pass
        # The extra Files tabs are not in the list above: close them as well,
        # or their workers and transfers would outlive the session.
        for page in list(getattr(self, "_extra_files", {}).values()):
            try:
                page.close_panel()
            except Exception:
                pass
        mirror = getattr(self, "mirror_tab", None)
        if mirror is not None and hasattr(mirror, "shutdown_threads"):
            try:
                pending_workers.extend(mirror.shutdown_threads())
            except RuntimeError:
                pass
        wall = getattr(self, "display_wall", None)
        if wall is not None:
            try:
                pending_workers.extend(wall.shutdown_threads())
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
