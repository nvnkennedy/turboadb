"""A per-device tab: connects in the background, then exposes an interactive
Shell, a live Logcat viewer, a file browser, and an app manager — plus quick
Mirror (scrcpy), Screenshot, and Reboot actions in a header bar."""

from __future__ import annotations

import os
import queue
import shlex
import threading
import time
import weakref
from collections import deque

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


class _Connections:
    """Which open device tabs use which network connection, and which of those
    connections an ``adb connect`` of this app made.

    Closing a tab used to run ``adb disconnect host:port`` whatever the
    connection was to anyone else: it cut the device off from another tab on
    it (a terminal-only session), from other tools (Android Studio's wireless
    debugging) and, through a remote adb server, from every user of that
    server.  A connection is now dropped only once the last open tab using it
    lets go, only when this app made it, and never on a remote server.  Keys
    are ``(adb server host or "", adb server port, serial)``.
    """

    def __init__(self):
        self._lock = threading.Lock()  # the connect worker lets go off the UI thread
        self._users = {}  # key -> WeakSet of the DeviceTabs using it
        # key -> a handler whose `adb connect` made that connection (and can
        # drop it), kept until the last tab using it lets go
        self._made_here = {}

    @staticmethod
    def key(config, serial):
        if config is None or not serial:
            return None
        return (config.adb_server_host or "", config.adb_server_port, str(serial))

    def use(self, key, user) -> None:
        if key is None:
            return
        with self._lock:
            self._users.setdefault(key, weakref.WeakSet()).add(user)

    def leave(self, key, user=None, handler=None):
        """*user* (a DeviceTab; None for a handler no tab took) no longer uses
        *key*, through *handler*. Returns the handler to drop the connection
        with once this app made it and no open tab uses it, else None."""
        if key is None:
            return None
        with self._lock:
            users = self._users.get(key)
            if users is not None:
                if user is not None:
                    users.discard(user)
                if not users:
                    del self._users[key]
            if key[0]:  # a remote server: its other users may rely on it
                return None
            if getattr(handler, "owns_connection", False):
                self._made_here[key] = handler
            if key in self._users:
                return None
            return self._made_here.pop(key, None)


_CONNECTIONS = _Connections()


def _connection_key(handler):
    """*handler*'s ``_Connections`` key, or None (no target, or a stand-in)."""
    return _Connections.key(getattr(handler, "config", None), getattr(handler, "serial", None))


def _disconnect_unless_shared(handler) -> None:
    """``adb disconnect`` *handler*'s target (blocking). A local server shared
    with other machines (``turboadb serve``) keeps it: they may be using it.

    Only while the local server answers, and under the lock its one stopper
    (``tools.kill_adb_server``) holds: ``adb disconnect`` starts a server when
    none answers, so one that ran just after the exit (or a restart) stopped
    the server started a new one, and that adb.exe outlived TurboADB.  A
    server that is down has no connection left to drop anyway."""
    try:
        from .. import tools

        config = handler.config
        if config.adb_server_host:
            return  # never on a remote server: its other users rely on it
        port = config.adb_server_port
        lock = tools.adb_server_lock()
    except Exception:
        return
    try:
        getattr(handler, "adb_path", None)  # resolved now, never under the lock
    except Exception:
        pass
    with lock:
        try:
            if not tools.is_adb_server_alive(port=port):
                return
        except Exception:
            return
        try:
            from ..devices import server_is_shared

            if server_is_shared(port):
                return
        except Exception:
            pass
        try:
            handler.disconnect()
        except Exception:
            pass


def _drop_connection_later(handler, after=()) -> None:
    """Disconnect *handler*'s target once the QThreads in *after* have finished
    (they may still use the connection, and a reconnect among them could make
    it again). With *after*, call it on the UI thread.

    Never on the UI thread: against a server that stopped answering it froze
    the window.  A daemon thread, not a QThread: a QThread still running when
    the app exits aborts it.  The workers are waited for through their
    ``finished`` signals, never ``QThread.wait()`` from another thread, which
    raced their deleteLater."""
    from .qtutil import thread_running

    waiting = [worker for worker in after if thread_running(worker)]
    started = []

    def start():
        if not started:
            started.append(True)
            threading.Thread(target=_disconnect_unless_shared, args=(handler,),
                             name="turboadb-disconnect", daemon=True).start()

    def finished(worker):
        if worker in waiting:
            waiting.remove(worker)
        if not waiting:
            start()

    for worker in list(waiting):
        worker.finished.connect(lambda w=worker: finished(w))
    for worker in list(waiting):  # ended before its signal was connected
        if not thread_running(worker):
            finished(worker)
    if not waiting:
        start()


def _release_connection(handler) -> None:
    """Let go of a handler no tab took (a cancelled or superseded connect)."""
    drop = _CONNECTIONS.leave(_connection_key(handler), None, handler)
    if drop is not None:
        _drop_connection_later(drop)


class _ConnectThread(QThread):
    ok = pyqtSignal(object, object)
    fail = pyqtSignal(str)
    # The handler's engine diagnostics ("[DEBUG] $ adb …", "… failed: …" from
    # ADBHandler._guard): log panel only, never a notification — the panel
    # that ran a failing command reports it itself.
    trace = pyqtSignal(str)
    # (handler, probe result): the rest of the connect-time probe, after ``ok``
    details = pyqtSignal(object, object)

    IDENTITY_TIMEOUT = 1.5

    def __init__(self, cfg, *, fetch_identity: bool = True, gate=None, known_state=None):
        super().__init__()
        self.cfg = cfg
        self.fetch_identity = bool(fetch_identity)
        self.gate = gate
        # The main window's device tracker on the target (DeviceTab.start_connect):
        # "device" spares the connect its wait-for-device and get-state.
        self.known_state = known_state
        self._probe = None
        self._cancelled = False
        # Set by cancel(): the engine kills an `adb connect` or `wait-for-device`
        # still waiting, instead of it running on behind a closed tab.
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancelled = True
        self._cancel_event.set()
        probe = self._probe
        if probe is not None:
            probe.close()  # never leave the probe's adb shell behind a closed tab

    def _make_handler(self):
        """The tab's handler. Its engine log lines go to ``trace``: the log
        panel, never a notification."""
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
            res = h.connect(known_state=self.known_state, cancel=self._cancel_event)
            if self._cancelled:
                _release_connection(h)  # only a connection it made, that no tab uses
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
            if not emitted:  # the tab never got this handler
                _release_connection(handler)
            return
        self.details.emit(handler, result)


class _ProbeThread(QThread):
    """Run the connect-time :class:`_DeviceProbe` for a tab that was handed a
    handler directly (not through its own connect worker), or again when the
    first probe got no device profile."""

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
        # The details are optional: a failure is a result like any other (as
        # in _ConnectThread._probe_and_emit), never an exception that reaches
        # the global error popup while the tab waits for details forever.
        try:
            result = self.probe.run(gate=self.gate)
        except Exception as exc:
            self.probe.close()
            result = {"info": None, "prompt": None, "displays": None,
                      "error": f"{type(exc).__name__}: {exc}"}
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

        Whatever happens on the way, even an error in the first read, the
        probe's adb process ends and the tab's adb slot is given back: a slot
        kept by a failed probe held up every later background command.
        """
        slot = started = False
        try:
            try:
                if gate is not None:
                    gate.acquire()
                    slot = True
                started = self.start()
                if started:
                    self.read_until("kind", identity_timeout)
            except _GateClosed as exc:
                self.error = str(exc)
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
        # the probe's own worker (it waited) can afford to reap it as well
        self._kill(proc, reap=bool(wait))

    @staticmethod
    def _kill(proc, reap: bool = False) -> None:
        """Kill the probe's adb and close its output, so the pump returns."""
        from ..proctree import stop_process

        stop_process(proc, grace=0, close_pipes=True, reap=reap)

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
        self._break_drawn_at = 0.0  # see _drop_drawn_break

    # how long the line break after an Enter for a program's prompt may take
    _BREAK_WAIT_S = 2.0

    def _drop_drawn_break(self, data: bytes) -> bytes:
        """The console drew the line end for an empty answer (see _send): the
        program's own line break right after it is the same line end."""
        if time.monotonic() - self._break_drawn_at > self._BREAK_WAIT_S:
            self._break_drawn_at = 0.0
            return data
        rest = data.lstrip(b"\r")
        if not rest:
            return b""  # only CRs yet: the LF decides
        self._break_drawn_at = 0.0
        return rest[1:] if rest[:1] == b"\n" else data

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
        self.btn_stop = stop

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

    def _console_columns(self) -> int:
        """How many characters fit on one line of the terminal: whole cells,
        margins and scroll bar left out (see AnsiConsole.cell_size)."""
        return self._cell_size()[0]

    def _console_rows(self) -> int:
        """How many lines fit in the terminal (see :meth:`_console_columns`)."""
        return self._cell_size()[1]

    def _cell_size(self):
        """``(columns, rows)`` of the terminal.  The page is laid out first:
        one just switched to (or not shown yet) may not be."""
        try:
            layout = self.layout()
            if layout is not None and layout.geometry() != self.rect():
                layout.setGeometry(self.rect())
            return self.term.cell_size()
        except Exception:
            return 0, 0

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
            # Each Tab press is an `adb shell` one-shot: it waits for one of the
            # device tab's adb slots (DeviceTab sets adb_gate) like every other.
            gate = getattr(self, "adb_gate", None)
            shell = gate.wrap(handler.shell) if gate is not None else handler.shell
            if first_word:
                globs = " ".join(f"{path}/{quote(token)}*" for path in self._BIN_DIRS)
                result = shell(f"ls -d {globs} 2>/dev/null", timeout=1.5, safe=True)
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

            result = shell(
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


# ---- the local PowerShell / CMD terminals ---------------------------------
# cmd.exe and powershell.exe exist on Windows only: elsewhere their terminals
# are not offered (they could only fail to start).
_LOCAL_SHELLS = os.name == "nt"
# Shells an `adb shell` starts on the device when it names no other command:
# they need a device terminal (-t -t) for their prompt and echo.
_DEVICE_SHELLS = ("su", "sh", "mksh", "bash")
# Full-screen programs, which need one for their keys and their screen.
_FULL_SCREEN_PROGRAMS = ("top", "htop", "vi", "vim", "less", "more", "nano", "watch")
# A findstr/find search word that means the same as a literal string.
_PLAIN_WORD = re.compile(r"[\w@#%+=:,\-]+\Z")


def _split_command_line(line: str, quotes: str = '"'):
    """Split *line* at whitespace outside quotes into ``(text, start, end)``:
    *text* without its quote characters, ``line[start:end]`` the token as
    typed.  Backslashes are ordinary characters, as in Windows paths."""
    tokens = []
    text, start, quote = [], None, None
    for index, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
            else:
                text.append(ch)
            continue
        if ch in quotes:
            quote = ch
        elif ch.isspace():
            if start is not None:
                tokens.append(("".join(text), start, index))
                text, start = [], None
            continue
        else:
            text.append(ch)
        if start is None:
            start = index
    if start is not None:
        tokens.append(("".join(text), start, len(line)))
    return tokens


def _unquoted_positions(line: str, chars: str, quotes: str = '"'):
    """Indexes in *line* of the characters in *chars* that are outside quotes."""
    found, quote = [], None
    for index, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in quotes:
            quote = ch
        elif ch in chars:
            found.append(index)
    return found


def _adb_invocation(words):
    """``(options, index)`` of an adb command line given as *words*: its global
    options (lower-case flag -> value, or True) and the index of its
    subcommand; None when *words* is not an adb command with a subcommand."""
    if not words or re.split(r"[\\/]", words[0])[-1].lower() not in ("adb", "adb.exe"):
        return None
    options = {}
    index = 1
    while index < len(words) and words[index].startswith("-"):
        flag = words[index].lower()
        if flag in ("-s", "-t", "-h", "-p", "-l", "--one-device"):
            if index + 1 >= len(words):
                return None
            options[flag] = words[index + 1]
            index += 2
        else:
            options[flag] = True
            index += 1
    if index >= len(words):
        return None
    return options, index


def _last_argument(line: str):
    """``(start, text)`` of the argument being typed at the end of *line*: a
    double-quoted part may hold spaces, and *text* has the quotes removed
    (``cd "My Folder"\\su`` -> ``My Folder\\su``)."""
    start, quoted = 0, False
    for index, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch.isspace() and not quoted:
            start = index + 1
    return start, line[start:].replace('"', "")


_NETWORK_DRIVES = {}  # "Z:" -> (is a network drive, when that was checked)


def _is_network_drive(drive: str) -> bool:
    """Whether *drive* (``"Z:"``) is a mapped share or another redirector.  Asks
    the object manager only (QueryDosDevice), which never waits on a server."""
    drive = drive.upper()
    now = time.monotonic()
    cached = _NETWORK_DRIVES.get(drive)
    if cached is not None and now - cached[1] < 60.0:
        return cached[0]
    remote = False
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.QueryDosDeviceW(drive, buf, len(buf)):
            target = buf.value.lower()
            local = target.startswith("\\device\\harddisk") or (
                target.startswith("\\??\\") and not target.startswith("\\??\\unc\\")
            )
            remote = not local
    except Exception:
        pass
    _NETWORK_DRIVES[drive] = (remote, now)
    return remote


def _quick_isdir(path: str) -> bool:
    """``os.path.isdir`` that never waits on the network.

    A share or a mapped network drive is taken as it is: checking a folder on
    a server that no longer answers stalled the whole window for seconds, and
    this runs for every prompt the shell prints."""
    if not path:
        return False
    if os.name == "nt":
        if path.startswith(("\\\\", "//")):
            return True
        if len(path) >= 2 and path[1] == ":" and _is_network_drive(path[:2]):
            return True
    return os.path.isdir(path)


class _LocalShellWidget(_TerminalWidgetBase):
    """Dedicated interactive terminal tab for local PowerShell or CMD.

    The widget follows whether the shell waits at its own prompt (the prompt
    mark :data:`local_terminal.PROMPT_MARK_RE`, or a prompt-looking last
    line).  Only a line typed there is a command: the conveniences below
    (``ls``, ``clear``, ``where``, ``python``, an ``adb shell`` on a device
    terminal, streaming filters) apply to it; anything typed while a command
    runs is input for that command and goes out exactly as typed."""
    adb_reboot_requested = pyqtSignal(object)
    # "root" / "unroot" typed here for this device: adbd restarts
    adbd_restart_requested = pyqtSignal(str)
    # (session, CommandStop) from the thread that ended a stopped command
    _command_stopped = pyqtSignal(object, object)
    # (session, CtrlC) from the thread that raised Stop's Ctrl+C
    _ctrl_c_done = pyqtSignal(object, object)

    # how long after the device is back an adb shell may still be ending
    ADB_RESUME_WAIT_S = 30.0
    # Stop on a local command: how long the shell gets to show its prompt again
    # before it is replaced by a fresh one, and when a shell that stays quiet
    # is asked for its prompt (an empty line)
    LOCAL_STOP_WAIT_MS = 1500
    LOCAL_STOP_PROBE_MS = 400
    # Hint shown (once a session) for a filter that holds its output back
    _HINT_COLOR = theme.ANSI_FG[90]

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
        self.btn_stop.setToolTip(
            "Stop the running command (Ctrl+C); the shell keeps its folder and variables"
        )

        self._shell_cwd = os.path.expanduser("~")

        self.term = AnsiConsole(send_fn=self._send)
        self.term.set_emulate_prompt(False)
        self.term.set_completion_fn(self._complete_local_async)
        self.term.set_prompt_provider_fn(self._get_prompt)
        self.term.set_interrupt_fn(self.interrupt)
        self.term.set_send_key_fn(self._send_key)
        self.term.set_key_offered_fn(self._key_offered)
        self._attach_terminal(lay)

        self._started = False
        self._in_adb_shell = False
        self._adb_shell_cwd = "/"
        self._adb_shell_command = ""  # the adb shell command as sent, to reopen it
        self._adb_shell_foreign = False  # that adb shell is another device's
        # the adb shell printed its prompt or an adb error, so a local prompt
        # after that means it ended (not an earlier prompt still arriving)
        self._adb_answered = False
        self._adb_reentry_cwd = None  # device folder to cd into once reopened
        self._adb_interrupt_pending = False  # Ctrl+C sent, device prompt not back yet
        self._adb_interrupt_heard = False
        self._prompt_to_mark = False  # a device prompt (or the end of an adb shell) was seen
        self._adb_shell_ended = False  # ... the end, in the output being fed (see _feed_from)
        self._adb_interrupt_timer = QTimer(self)
        self._adb_interrupt_timer.setSingleShot(True)
        self._adb_interrupt_timer.timeout.connect(self._on_adb_interrupt_timeout)
        # (command, device folder) of an adb shell to reopen once adbd is back
        self._adb_resume = None
        self._adb_resume_until = 0.0  # set when the device is back; monotonic
        self._strip_startup_banner = False  # set for each new CMD session
        self._banner_chunks = 0
        self._prompt_tail = ""
        self._tail_end = 0        # characters of output tracked so far (the tail's end)
        self._echo_scan_to = 0    # ... and up to where prompts were checked for an echo
        # The shell waits at its own prompt: set when its prompt ends the
        # output, cleared by every line sent.  A fresh shell reads its first
        # line as a command too.
        self._own_prompt = True
        self._prompt_seen = False  # this shell has shown its prompt
        self._break_drawn_at = 0.0  # see _drop_drawn_break
        self._pipe_hint_shown = False
        self._refused_at = 0.0
        self._ps_width = None  # PowerShell's buffer width as last set
        self._grep_probe = {}  # {"gnu": bool} once the session's grep is known
        # Stop on a local command (see interrupt)
        self._stop_pending = False
        self._stop_phase = ""      # "ctrl-c": a console Ctrl+C; "kill": ending processes
        self._ctrl_c_sent = False  # that Ctrl+C reached the shell's console
        self._stop_heard = False   # ... and output came after it
        self._stop_marked = False  # a "^C" is on screen for this stop
        self._batch_answered = False
        self._stop_timer = QTimer(self)
        self._stop_timer.setSingleShot(True)
        self._stop_timer.timeout.connect(self._on_local_stop_timeout)
        self._stop_probe_timer = QTimer(self)
        self._stop_probe_timer.setSingleShot(True)
        self._stop_probe_timer.timeout.connect(self._probe_local_prompt)
        self._command_stopped.connect(self._on_command_stopped)
        self._ctrl_c_done.connect(self._on_ctrl_c_sent)

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

    def _complete_local_async(self, line: str):
        """Tab: complete on a worker thread, like the Android completion.

        Completion reads folders (the current one, every PATH entry); on a
        share whose server stopped answering each read stalled for seconds,
        which froze the whole window.  The folder and environment are taken
        now, so the worker never reads the widget."""
        cwd = self._shell_cwd
        env = getattr(self.session, "env", None)
        return self._start_async_completion(
            line, lambda text: self._local_complete(text, cwd=cwd, env=env)
        )

    def _local_complete(self, line: str, *, cwd: str = None, env=None):
        """``(completed line or None, options)`` for *line* (blocking: it reads
        folders).  *cwd* and *env* default to the shell's current ones."""
        if cwd is None:
            cwd = self._shell_cwd
        if env is None:
            env = getattr(self.session, "env", None)
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
            path_cmds = self._get_path_commands(env)
            for cmd_name in path_cmds:
                if cmd_name.startswith(prefix):
                    matches.add(cmd_name)

            # Look in current working directory
            if os.path.isdir(cwd):
                try:
                    pathext = tuple(e.lower() for e in os.environ.get("PATHEXT", ".exe;.bat;.cmd;.ps1").split(";") if e)
                    if self.shell_type == "powershell":
                        pathext += (".ps1",)
                    with os.scandir(cwd) as it:
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

        # 5. Directory / file path completion.  The argument may be quoted
        # anywhere (completion itself quotes names with spaces): `cd "My
        # Folder"\su` is `My Folder\su`, and it is replaced from where it starts.
        dirs_only = first in ("cd", "chdir", "pushd", "set-location")
        arg_start, prefix = _last_argument(line)
        prefix = prefix.strip("'")
        if (
            prefix and line.endswith('"') and not prefix.endswith(("\\", "/"))
            and os.path.isdir(prefix if os.path.isabs(prefix) else os.path.join(cwd, prefix))
        ):
            # a folder completion closed with its quote: Tab goes into it
            prefix += "\\"

        # ``/name`` is an Android/Unix-style path, not an absolute Windows
        # path. Treat it as relative in a local PowerShell/CMD session so Tab
        # never suggests `/name` and causes PowerShell to look for `C:\\name`.
        normalized_line = line
        if os.name == "nt" and prefix.startswith("/") and not prefix.startswith("//"):
            prefix = prefix.lstrip("/")
            if not trailing_space:
                normalized_line = line[:arg_start] + prefix

        if os.path.isabs(prefix):
            search_dir = os.path.dirname(prefix) or prefix
            base = os.path.basename(prefix)
        else:
            rel_dir = os.path.dirname(prefix)
            search_dir = os.path.join(cwd, rel_dir) if rel_dir else cwd
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
            return line[:arg_start] + formatted_matches[0], []
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
                return line[:arg_start] + completed, formatted_matches
            # Even if the names have no longer common prefix, remove an
            # accidental Unix slash before presenting the choices. Otherwise
            # Enter would send `cd /name` to PowerShell as `C:\\name`.
            return (normalized_line if normalized_line != line else None), formatted_matches
        return None, formatted_matches

    def _adb_shell_complete(self, line: str):
        """Schedule Android completion and immediately return to Qt's event loop."""
        if self._adb_shell_foreign:
            return None, []  # another device's shell: this tab's device can't answer
        return self._start_async_completion(line, self._query_adb_shell_complete)

    def _query_adb_shell_complete(self, line: str):
        """Complete commands and paths while CMD/PowerShell hosts `adb shell`.

        The previous implementation disabled completion here to avoid using
        Windows paths for Android paths.  That made Tab appear broken exactly
        when it was most useful.  A small parallel ADB query is safe while the
        interactive shell is open and returns Android-native candidates.
        """
        return self._query_android_completion(line, self._adb_shell_cwd)

    def _track_adb_shell_cd(self, line: str) -> None:
        """A ``cd`` typed in the adb shell moves completion there at once.

        The console's own parser follows ``cd -``, ``~``, quotes and a compound
        line (``cd /sdcard && ls``), and takes a cd back when the device says
        it failed; the device prompt (``host:/path $``) then has the last word
        (see :meth:`_track_adb_shell_output`)."""
        parts = line.split(maxsplit=1)
        if not parts or parts[0] != "cd":
            return
        self.term._cwd = self._adb_shell_cwd
        self.term._apply_cd(line)
        self._adb_shell_cwd = self.term._cwd

    def reset_adb_shell_context(self) -> None:
        """Return completion/prompt bookkeeping to the local host shell.

        A device reboot terminates an interactive ``adb shell`` subprocess.
        Without this reset the visible PowerShell/CMD prompt was local again,
        but Tab completion still treated subsequent commands as Android input.
        """
        self._in_adb_shell = False
        self._adb_shell_cwd = "/"
        self._adb_shell_command = ""
        self._adb_shell_foreign = False
        self._adb_answered = False
        self._adb_reentry_cwd = None
        self._clear_adb_interrupt()
        self.term._cwd = self._shell_cwd
        self.term.set_completion_fn(self._complete_local_async)
        self.term.set_shell_at_prompt(self._own_prompt)
        self.term.set_typing_ahead(not self._prompt_seen)
        self.term.set_screen_input(None)  # the shell reads a pipe again
        if self.session is not None:
            self.session.utf8_input = False  # ... in its console code page (CMD)

    def _enter_adb_shell(self, command: str, *, foreign: bool = False, one_shot: bool = False) -> bytes:
        """Follow an interactive adb shell started here; returns the line to send.

        It runs on a device terminal, which echoes every line typed, also
        while a device command runs: the console is left to expect that echo
        (``set_shell_at_prompt(None)``) for as long as the adb shell lasts,
        and a full-screen program in it gets the console's screen.
        *one_shot*: a full-screen program started straight from ``adb shell``
        (``adb shell top``): no device prompt comes, the local one ends it,
        and Stop never starts it again."""
        self._in_adb_shell = True
        self._own_prompt = False
        self._adb_shell_command = "" if one_shot else command
        self._adb_shell_foreign = foreign
        self._adb_shell_cwd = "/"
        self._adb_answered = one_shot
        self._adb_reentry_cwd = None
        self._prompt_tail = ""
        self._clear_adb_interrupt()
        self.term._cwd = self._adb_shell_cwd
        self.term.set_completion_fn(self._adb_shell_complete)
        self.term.set_shell_at_prompt(None)
        # until the device prompt shows, a line typed is echoed after it
        self.term.set_typing_ahead(True)
        self.term.set_screen_input(self._screen_write)
        return (command + "\r\n").encode("utf-8")

    def _screen_write(self, data: bytes) -> bool:
        """Keys and answers for a program on the adb shell's device terminal
        (see AnsiConsole.set_screen_input): as they are, through adb."""
        if not (self.session and self.session.running):
            return False
        self._clear_adb_interrupt()  # the program is being used: a Stop starts over
        return self.session.send(data) is not False

    def _local_adb_command(self, tokens: list[str]):
        """``(subcommand, arguments)`` of an adb command line aimed at this
        terminal's device (``adb -s OTHER …`` is not), else None."""
        parsed = _adb_invocation(tokens)
        if parsed is None:
            return None
        options, index = parsed
        selected_serial = options.get("-s")
        if selected_serial and selected_serial != self.serial:
            # another device, or one this terminal can't tell is its own: a
            # reboot of it must not put this tab into its reboot recovery
            return None
        return tokens[index].lower(), tokens[index + 1:]

    def _typed_adb_shell(self, line: str):
        """``(line to send, foreign, one_shot)`` for an interactive ``adb …
        shell`` typed at the PC prompt, else None.

        adb gives a shell a device terminal only when its own input is one, so
        from these pipes the device showed no prompt and no echo, its stdio
        held output back and Ctrl+C could not reach it.  ``-t -t`` forces the
        terminal: added right after ``shell`` for the interactive forms (no
        device command, or a bare su/sh/mksh/bash) of any adb (``adb.exe``, a
        full path, ``-s``/``-t``/``-H``/``-P``/``-d``/``-e``), and for a
        full-screen program (``adb shell top``: *one_shot*).  Not for another
        one-shot command or a line with host-side ``|``, ``<``, ``>``, ``&``
        or ``;``: a terminal would merge stderr into stdout and turn line ends
        into CR LF in a redirected file.  An explicit ``-t``/``-T``/``-x``/
        ``-n`` is kept as typed (a shell with ``-t -t`` is still followed).
        *foreign*: the shell is another device's (``-s OTHER``, another server)."""
        powershell = self.shell_type == "powershell"
        quotes = "\"'" if powershell else '"'
        tokens = _split_command_line(line, quotes)
        if powershell and tokens and tokens[0][0] == "&":
            tokens = tokens[1:]  # the call operator: & "C:\pt\adb.exe" shell
        words = [token[0] for token in tokens]
        parsed = _adb_invocation(words)
        if parsed is None or words[parsed[1]].lower() != "shell":
            return None
        options, index = parsed
        leading_call = 1 if powershell and line.lstrip().startswith("&") else 0
        if len(_unquoted_positions(line, "|<>&;", quotes)) > leading_call:
            return None
        flags = ""
        rest = index + 1
        while rest < len(words) and words[rest].startswith("-") and len(words[rest]) > 1:
            flag = words[rest]
            rest += 1
            if flag == "-e":
                rest += 1  # the escape character
            else:
                flags += flag[1:]
        command = words[rest:]
        one_shot = bool(command) and re.split(r"[\\/]", command[0])[-1].lower() in _FULL_SCREEN_PROGRAMS
        if command and not one_shot and not (len(command) == 1 and command[0].lower() in _DEVICE_SHELLS):
            return None
        serial = options.get("-s")
        foreign = bool(serial and self.serial and serial != self.serial) or any(
            flag in options for flag in ("-h", "-p", "-l")
        )
        if any(flag in flags for flag in "tTxn"):
            if flags.count("t") >= 2 and not any(flag in flags for flag in "Tn"):
                return line, foreign, one_shot  # a device terminal already: followed as typed
            return None
        shell_end = tokens[index][2]
        return line[:shell_end] + " -t -t" + line[shell_end:], foreign, one_shot

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

    def _adb_server(self):
        """``(host, port)`` of the adb server the device tab's handler uses:
        typed adb commands go there too (``adb -H``/``-P`` in the engine)."""
        config = getattr(self.handler, "config", None)
        host = getattr(config, "adb_server_host", None)
        port = getattr(config, "adb_server_port", None)
        return (
            host if isinstance(host, str) and host else None,
            port if isinstance(port, int) and not isinstance(port, bool) else None,
        )

    def _start_session(self, *, show_banner=True):
        from .local_terminal import LocalShellSession
        from ..config import format_host_port
        from ..tools import DEFAULT_ADB_SERVER_PORT

        from .local_terminal import ps_buffer_width

        host, port = self._adb_server()
        columns = self._console_columns()
        try:
            self.session = LocalShellSession(
                self.shell_type, serial=self.serial, cwd=self._shell_cwd,
                adb_server_host=host, adb_server_port=port,
                columns=columns,
            )
            # the buffer width PowerShell's startup gave it (see _with_new_width)
            self._ps_width = ps_buffer_width(columns)
            # CMD's copyright banner, also after Stop reopens the shell
            self._strip_startup_banner = True
            self._banner_chunks = 0
            if show_banner:
                from .. import __version__

                name = "PowerShell" if self.shell_type == "powershell" else "Command Prompt"
                target = f"  ·  ANDROID_SERIAL={self.serial}" if self.serial else ""
                if host:
                    server = format_host_port(host, port or DEFAULT_ADB_SERVER_PORT)
                    target += f"  ·  adb server {server}"
                elif port and port != DEFAULT_ADB_SERVER_PORT:
                    target += f"  ·  adb server port {port}"
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
            self.term.notice(f"[Could not start local {self.shell_type}: {exc}]", theme.ECHO_ERROR)
            return

        sess = self.session
        # a fresh shell reads its first line as a command, once its prompt is
        # out: it echoes a line typed before that after the prompt
        self._own_prompt = True
        self._prompt_seen = False
        self._break_drawn_at = 0.0
        self._prompt_tail = ""
        self._pipe_hint_shown = False
        self.term.set_shell_at_prompt(True)
        self.term.set_typing_ahead(True)
        self.term.drop_typed_ahead()
        self._start_probes(sess)

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

    def _start_probes(self, session) -> None:
        """Off the UI thread, for a new session: warm the PATH command list for
        the first Tab, and (CMD) learn whether its ``grep`` is GNU grep, which
        takes ``--line-buffered`` (see :meth:`_pipeline_rewrite`)."""
        env = getattr(session, "env", None)
        self._grep_probe = probe = {}
        if not isinstance(env, dict) or not env:
            return  # a stand-in session

        def run():
            try:
                type(self)._get_path_commands(env)
            except Exception:
                pass
            if self.shell_type == "cmd":
                from .local_terminal import gnu_grep

                probe["gnu"] = gnu_grep(env)

        threading.Thread(target=run, name="turboadb-local-shell-probe", daemon=True).start()

    # cmd's copyright banner, which it may write in several pieces
    _CMD_BANNER = re.compile(
        r"(?:\s*(?:Microsoft Windows \[Version [^\]\r\n]*\]?|\(c\)[^\r\n]*))*\s*"
    )

    def _strip_cmd_banner(self, data: bytes) -> bytes:
        """Drop cmd's startup banner and the blank line after it, however the
        pipe splits it (a banner written in two reads stayed on screen)."""
        self._banner_chunks += 1
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return data
        clean = text[self._CMD_BANNER.match(text).end():]
        if clean or self._banner_chunks >= 8:
            self._strip_startup_banner = False
        return clean.encode("utf-8")

    def _feed_from(self, reader, data):
        if reader is not self.reader:
            return
        if self._strip_startup_banner and self.shell_type == "cmd":
            data = self._strip_cmd_banner(data)
            if not data:
                return
        if self._break_drawn_at:
            data = self._drop_drawn_break(data)
            if not data:
                return
        if self._adb_interrupt_pending:
            self._adb_interrupt_heard = True
        if self._in_adb_shell:
            self._notice_refusal(data)
        self._track_prompt_cwd(data)
        if self._adb_shell_ended:
            # The adb shell ended with this output: a program's screen left
            # behind goes before what adb and the local shell printed after
            # it, or vi's frame stayed up after the device went away, with
            # the local prompt drawn on it where no key reached anything.
            self._adb_shell_ended = False
            end = self._adb_shell_words(data)
            self.term.feed(data[:end])
            self.term.end_screen()
            data = data[end:]
        self.term.feed(data)
        if self._prompt_to_mark:
            self._prompt_to_mark = False
            self.term.mark_prompt()
        if self.term.typing_ahead() and (
            self._adb_answered if self._in_adb_shell else self._prompt_seen
        ):
            self.term.set_typing_ahead(False)  # the shell reads what is typed now
        if self.term.has_typed_ahead() and not self._in_adb_shell:
            self._retract_echoed_lines()
        if self._in_adb_shell:
            # a cd the device refused was taken back by the console
            self._adb_shell_cwd = self.term._cwd
            self.term.set_shell_at_prompt(None)
        else:
            # A continuation prompt (PowerShell's ">>", cmd's "More?") reads
            # the rest of a command: the shell echoes that line, as it does a
            # command, but it gets no conveniences of its own.
            continued = bool(self._CONTINUATION_TAIL.search(self._prompt_tail))
            self.term.set_shell_at_prompt(self._own_prompt or continued)
            if self._stop_pending:
                if self._ctrl_c_sent:
                    self._stop_heard = True  # the command answered the Ctrl+C
                self._follow_local_stop()

    _PS_PROMPT_TAIL = re.compile(r"(?:^|[\r\n])PS ([^\r\n>]+)> ?$")
    _CMD_PROMPT_TAIL = re.compile(r"(?:^|[\r\n])([A-Za-z]:\\[^\r\n<>|*?\"]*)>$")
    _CONTINUATION_TAIL = re.compile(r"(?:^|[\r\n])(?:>> ?|More\? ?)$")
    # an Android shell prompt ("PD2318:/sdcard $ ", "/ # ") ending the output
    _DEVICE_PROMPT_TAIL = re.compile(r"[:/][^\r\n]* [$#] ?$")
    # ... and the folder in it ("130|PD2318:/sdcard $ " -> /sdcard)
    _DEVICE_PROMPT_CWD = re.compile(
        r"(?:^|\n|[$#] )(?:\d+\|)?(?:[^\s:/]*:)?(/(?:(?! [$#] )[^\n])*?) [$#] ?$"
    )
    _ADB_ERROR_LINE = re.compile(r"(?:^|[\r\n])(?:adb(?:\.exe)?|error): ")
    # ... where one starts, in the output
    _ADB_ERROR_START = re.compile(rb"(?:^|(?<=[\r\n]))(?:adb(?:\.exe)?|error): ")
    # cmd after a batch file's command was stopped (English "Terminate batch
    # job (Y/N)? ", German "(J/N)?", French "(O/N) ?"): group 1 answers yes
    _BATCH_QUESTION = re.compile(r"\^C[^\r\n]*\((\w)/\w\) ?\? ?$")

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
        self._tail_end += len(data)  # where the tail ends in the whole output
        if self._in_adb_shell:
            self._track_adb_shell_output(tail)
            return
        folder = self._local_prompt_match(tail)
        if folder is not None:
            self._own_prompt = True
            self._prompt_seen = True
            self._take_prompt_folder(folder)

    def _retract_echoed_lines(self) -> None:
        """A line typed while a command ran was drawn at once, as input for
        that command.  When the shell reads it as a command after all (the
        command ended without reading it), it echoes it after its prompt: the
        early copy then goes, so the line shows once, where it ran."""
        from .local_terminal import PROMPT_MARK_RE

        tail = self._prompt_tail
        base = self._tail_end - len(tail)
        for mark in PROMPT_MARK_RE.finditer(tail):
            at = base + mark.end()
            if at <= self._echo_scan_to:
                continue
            rest = tail[mark.end():]
            end = rest.find("\n")
            if end < 0:
                return  # the echo is not complete yet
            self._echo_scan_to = at
            line = strip_ansi(rest[:end]).strip()
            if line:
                self.term.forget_typed_line(line)

    def _local_prompt_match(self, tail: str):
        """The folder of the shell prompt that ends *tail* (``""`` when it names
        none), or None when the shell is not at its prompt.

        The prompt mark says so for any prompt.  Without it (a nested shell,
        a prompt redefined later) a default prompt of either shell counts
        (``cmd`` typed in PowerShell prints cmd's), when its folder exists."""
        from .local_terminal import PROMPT_MARK_RE

        mark = None
        for mark in PROMPT_MARK_RE.finditer(tail, max(0, len(tail) - 600)):
            pass
        if mark is not None and mark.end() == len(tail):
            return mark.group(1)
        for pattern in (self._PS_PROMPT_TAIL, self._CMD_PROMPT_TAIL):
            match = pattern.search(tail)
            if match and _quick_isdir(match.group(1)):
                return match.group(1)
        return None

    def _take_prompt_folder(self, folder: str) -> None:
        """The prompt shows *folder*: Tab completion and a restart use it."""
        if folder and folder != self._shell_cwd and _quick_isdir(folder):
            self._shell_cwd = folder
        if not self._in_adb_shell:
            self.term._cwd = self._shell_cwd

    def _at_idle_prompt(self) -> bool:
        """The shell waits at its prompt and nothing was printed since."""
        return (
            self._own_prompt and not self._in_adb_shell
            and self._local_prompt_match(self._prompt_tail) is not None
        )

    def _track_adb_shell_output(self, tail: str) -> None:
        """Follow an adb shell started here from its output.

        The device prompt settles a pending Ctrl+C, names the device folder
        and, after a reopen, moves back to the old one. PowerShell's or CMD's
        own prompt after the adb shell answered means it ended by itself
        (``exit`` in a script, the device unplugged), so Tab completion and
        Stop act locally again."""
        text = strip_ansi(tail)
        if self._DEVICE_PROMPT_TAIL.search(text):
            self._adb_answered = True
            self._clear_adb_interrupt()
            cwd, self._adb_reentry_cwd = self._adb_reentry_cwd, None
            if cwd and self.session and self.session.running:
                self.session.send(("cd " + shlex.quote(cwd) + "\r\n").encode("utf-8"))
                return  # the prompt after that cd names the folder
            match = self._DEVICE_PROMPT_CWD.search(text)
            if match:
                self._adb_shell_cwd = self.term._cwd = match.group(1)
                self.term._cd_revert = None  # the device said where it is
            self._prompt_to_mark = True  # its colours, once this output is fed
            return
        if self._ADB_ERROR_LINE.search(text):
            self._adb_answered = True
        if not self._adb_answered:
            return
        folder = self._local_prompt_match(tail)
        if folder is not None:
            self.reset_adb_shell_context()
            self._own_prompt = True
            self._take_prompt_folder(folder)
            self.term.set_shell_at_prompt(True)
            self._prompt_to_mark = True  # its colours
            self._adb_shell_ended = True  # a screen the adb shell had goes before it
            if self._adb_resume is not None and time.monotonic() < self._adb_resume_until:
                self._resume_adb_shell_now()  # adbd restarted and the device is back

    def _adb_shell_words(self, data: bytes) -> int:
        """Where the last words of an adb shell that ended start in *data*
        (the output its end came with): the error adb printed, or the line
        of the local prompt that ends *data*.  What comes before is still
        its program's."""
        line = data.rfind(b"\n") + 1
        error = self._ADB_ERROR_START.search(data, 0, line)
        return line if error is None else error.start()

    def _on_closed(self):
        if not self._closing:
            # one notice, after the shell's last output
            self.term.set_alive(False, show_disconnect_notice=False)
            self.term.notice("[Process terminated]", theme.ECHO_ERROR)

    def _send(self, data: bytes):
        """A submitted line.  At the shell's own prompt it is a command and
        gets the conveniences of :meth:`_shell_command`; while a command runs
        it is input for that command and goes out exactly as typed (an answer
        ``ls`` to ``set /p`` used to arrive as ``dir``).  Inside an adb shell
        started here it belongs to the device."""
        self.ensure_started()
        if not (self.session and self.session.running):
            return
        adbd_restart = None
        self._break_drawn_at = 0.0
        if self._stop_pending and self._stop_heard:
            # input for a program that answered Stop's Ctrl+C and runs on (a
            # REPL): that Stop is over, and the next one starts with Ctrl+C
            self._clear_local_stop()
        try:
            line = data.decode("utf-8", "replace").strip()
            # typing takes over: an adb shell waiting to reopen stays closed
            self._adb_resume = None
            if self._in_adb_shell:
                self._adb_shell_line(line)
            elif self._own_prompt:
                if self._prompt_seen:
                    # it waited at its prompt: it has read every line typed before
                    self.term.drop_typed_ahead()
                data, adbd_restart = self._shell_command(line, data)
            elif line:
                # input for the running command; an echo of it after a prompt
                # from now on means the shell read it (see _retract_echoed_lines)
                self._echo_scan_to = self._tail_end
            elif (strip_ansi(self._prompt_tail.rsplit("\n", 1)[-1]).strip()
                  and not self._CONTINUATION_TAIL.search(self._prompt_tail)):
                # Enter for a program's own prompt: the console ends that line
                # at once, and pause, choice (or `cmd /c pause`) end it again
                # after reading the key, which left a blank line behind
                self._break_drawn_at = time.monotonic()
        except Exception:
            adbd_restart = None
        if not self._in_adb_shell:
            # the shell, or the command it runs, has a line to work on now
            self._own_prompt = False
            self.term.set_shell_at_prompt(False)
        if self.session.send(data) is False:
            self._input_refused()
        if self._in_adb_shell:
            # adb reads what is typed now (not the line that started it, which
            # was cmd's): it hands every byte to the device, which reads UTF-8
            self.session.utf8_input = True
        if adbd_restart:
            # After the command is on its way: the device tab holds the other
            # terminals and brings them back once adbd has restarted.
            self.adbd_restart_requested.emit(adbd_restart)

    def _adb_shell_line(self, line: str) -> None:
        """A line for the adb shell started here: follow ``cd`` and ``exit``."""
        # input after a Ctrl+C: the next Stop sends Ctrl+C again
        self._clear_adb_interrupt()
        if line.lower() in ("exit", "exit 0", "logout"):
            self.reset_adb_shell_context()
            return
        self._track_adb_shell_cd(line)

    def _shell_command(self, line: str, data: bytes):
        """``(bytes to send, adbd restart verb)`` for *line* typed at the shell's
        own prompt, with its conveniences applied."""
        quotes = "\"'" if self.shell_type == "powershell" else '"'
        words = [token[0] for token in _split_command_line(line, quotes)]
        if self.shell_type == "powershell" and words[:1] == ["&"]:
            words = words[1:]
        reboot_mode = self._local_adb_reboot_mode(words)
        adbd_restart = self._local_adbd_restart_verb(words)
        typed_shell = self._typed_adb_shell(line)
        if typed_shell is not None:
            command, foreign, one_shot = typed_shell
            data = self._enter_adb_shell(command, foreign=foreign, one_shot=one_shot)
            self._expect_echo(command)
        else:
            rewritten = self._interactive_rewrite(line)
            if rewritten is None and self.shell_type == "cmd":
                rewritten = self._cmd_rewrite(line)
            if rewritten is None:
                rewritten = self._prompt_rewrite(line)
            if rewritten is None:
                rewritten = self._pipeline_rewrite(line)
            if rewritten is not None:
                self._expect_echo(rewritten)
                data = (rewritten + "\r\n").encode("utf-8")
            if self.shell_type == "powershell" and line:
                data = self._with_new_width(rewritten or line, data)
            if self.shell_type == "powershell" and line.lower() in ("cls", "clear", "clear-host"):
                # Clear-Host clears only a console: nothing reaches the pipe
                self.term.clear_screen()
        if reboot_mode is not None:
            # The command itself is still sent to PowerShell/CMD.  This only
            # tells DeviceTab to preserve input and reconnect the Android
            # stream after an explicit local `adb reboot`.
            self.adb_reboot_requested.emit(reboot_mode or None)
        return data, adbd_restart

    def _expect_echo(self, text: str) -> None:
        """The shell will echo *text* (a rewritten command): hide it, not the
        line as typed."""
        self.term._pending_echo = text
        self.term._pending_echo_at = time.monotonic()

    # statements that must come first on their line
    _PS_FIRST_ONLY = re.compile(r"\s*(?:using\s|param\s*\()", re.IGNORECASE)
    # A command reading the status of the one before it: the width statement
    # in front of it would be that one (`$?` said True after a failure).
    _PS_READS_STATUS = re.compile(r"\$\{?\?")

    def _with_new_width(self, command: str, data: bytes) -> bytes:
        """*data*, *command* typed at PowerShell's own prompt, with the buffer
        width for the view in front when that changed since PowerShell last
        heard it (the window or the font was resized): PowerShell formats
        tables and Select-String matches to it.  At its prompt nothing runs,
        so nothing is ever typed into a running command; the echo of the
        whole line stays hidden.  A command that reads ``$?`` goes as typed
        and the next one takes the width."""
        from .local_terminal import ps_buffer_width, ps_resize_command

        width = ps_buffer_width(self._console_columns())
        if (self._ps_width is None or width == self._ps_width
                or self._PS_FIRST_ONLY.match(command) or self._PS_READS_STATUS.search(command)
                or self.term.typing_ahead()):
            return data  # (typed ahead, the echo shows: the next command takes it)
        self._ps_width = width
        command = ps_resize_command(width) + command
        self._expect_echo(command)
        return (command + "\r\n").encode("utf-8")

    # cmd's `prompt [text]` and `set prompt=[text]`; PowerShell defining its
    # prompt function in one line
    _CMD_PROMPT_COMMAND = re.compile(r"(\s*prompt)(?:\s+(.*?))?\s*\Z", re.IGNORECASE)
    _CMD_PROMPT_VARIABLE = re.compile(r'(\s*set\s+)("?)(prompt=)(.*?)\2\s*\Z', re.IGNORECASE)
    _PS_PROMPT_FUNCTION = re.compile(r"\bfunction\s+(?:global:|script:)?prompt\b", re.IGNORECASE)

    def _prompt_rewrite(self, line: str):
        """A prompt the user sets keeps the invisible mark at its end.

        Without it the terminal no longer knew the shell waits for a command:
        each command showed twice (the shell's echo was not expected), the
        conveniences stopped, and Stop at the prompt replaced the shell.  cmd
        gets the mark added to the new prompt text; PowerShell wraps the new
        prompt function again, as at startup (a definition spread over several
        lines is left alone)."""
        from .local_terminal import _CMD_PROMPT_MARK, _PS_PROMPT_MARK

        if self.shell_type == "cmd":
            if _unquoted_positions(line, "&|<>"):
                return None  # more than the prompt command
            match = self._CMD_PROMPT_COMMAND.match(line)
            if match:
                text = match.group(2) or "$P$G"  # a bare `prompt` restores the default
                if text.endswith(_CMD_PROMPT_MARK):
                    return None
                return f"{match.group(1)} {text}{_CMD_PROMPT_MARK}"
            match = self._CMD_PROMPT_VARIABLE.match(line)
            if match:
                head, quote, name, text = match.groups()
                text = text or "$P$G"  # an empty PROMPT means the default one
                if text.endswith(_CMD_PROMPT_MARK):
                    return None
                return f"{head}{quote}{name}{text}{_CMD_PROMPT_MARK}{quote}"
            return None
        if not self._PS_PROMPT_FUNCTION.search(line) or line.count("{") != line.count("}"):
            return None
        return (line.rstrip().rstrip(";") + ";if($ExecutionContext.SessionState.LanguageMode"
                " -eq 'FullLanguage'){try{" + _PS_PROMPT_MARK + "}catch{}}")

    @staticmethod
    def _cmd_rewrite(line: str):
        """CMD: ``ls`` runs ``dir`` and ``clear`` runs ``cls``."""
        low = line.lower()
        if low == "ls":
            return "dir"
        if low.startswith("ls "):
            rest = line[3:].strip()
            if rest in ("-la", "-al", "-l", "-a"):
                return "dir /a" if "a" in rest else "dir"
            return f"dir {rest}"
        if low == "clear":
            return "cls"
        return None

    def _pipeline_rewrite(self, line: str):
        """A command line whose filter shows its matches as they come, or None.

        Over these pipes a filter's output is a pipe too, and findstr or GNU
        grep hold it back until 4 KB have gathered or the command ends: ``adb
        logcat | findstr X`` showed nothing for minutes.  CMD: ``| grep`` gets
        ``--line-buffered`` (GNU grep only, :meth:`_start_probes`) and a plain
        ``| findstr [/i] [/v] word`` shown on screen runs Windows' find.exe,
        which writes every line at once (it cuts a line after 4 KB, which only
        the longest logcat lines reach; output sent to a file keeps findstr).
        PowerShell passes a program's output on to another program only when
        the first one ends, whatever the filter, but streams into
        Select-String: the plain findstr form runs that.  Other forms get a
        one-time hint instead."""
        powershell = self.shell_type == "powershell"
        quotes = "\"'" if powershell else '"'
        bars = [
            index for index in _unquoted_positions(line, "|", quotes)
            if line[index - 1:index] != "|" and line[index + 1:index + 2] != "|"
        ]
        if not bars:
            return None
        bounds = [0] + [index + 1 for index in bars] + [len(line) + 1]
        stages = [line[bounds[i]:bounds[i + 1] - 1] for i in range(len(bounds) - 1)]
        names = []
        for stage in stages[1:]:
            words = stage.split()
            names.append(re.split(r"[\\/]", words[0])[-1].lower() if words else "")
        rewritten, hint = None, False
        if not powershell and self._grep_probe.get("gnu"):
            changed = False
            for number, (stage, name) in enumerate(zip(stages[1:], names), 1):
                if name in ("grep", "grep.exe") and "--line-buffered" not in stage:
                    head = len(stage) - len(stage.lstrip()) + len(stage.split()[0])
                    stages[number] = stage[:head] + " --line-buffered" + stage[head:]
                    changed = True
            if changed:
                rewritten = "|".join(stages)
        if names[-1] in ("findstr", "findstr.exe"):
            plain = self._plain_findstr(stages[-1]) if len(stages) == 2 else None
            if plain is None:
                hint = True
            else:
                flags, word = plain
                if powershell:
                    tail = (" Select-String -SimpleMatch"
                            + ("" if "i" in flags else " -CaseSensitive")
                            + (" -NotMatch" if "v" in flags else "")
                            + f" '{word}' | ForEach-Object Line")
                else:
                    from .local_terminal import _system_exe

                    tail = (f" {_system_exe('find.exe')}"
                            + "".join(f" /{flag.upper()}" for flag in flags)
                            + f' "{word}"')
                rewritten = (rewritten or line)[: len(rewritten or line) - len(stages[-1])] + tail
        elif powershell and any(name in ("grep", "grep.exe", "find", "find.exe") for name in names):
            hint = True
        if hint and _unquoted_positions(line, "<>", quotes):
            hint = False  # the matches go to a file anyway
        if hint and not self._pipe_hint_shown:
            self._pipe_hint_shown = True
            if powershell:
                text = ("PowerShell hands a program's output to another program only when "
                        "it ends; `| Select-String text` shows matches as they come.")
            else:
                text = ("findstr holds its output back here until the command ends; "
                        '`| find "text"` shows matches as they come.')
            self.term.notice(text, self._HINT_COLOR, new_stream=False)
        return rewritten

    @staticmethod
    def _plain_findstr(stage: str):
        """``(flags, word)`` of ``findstr [/i] [/v] word`` searching one literal
        word, else None (a regex, several words, a file, other options)."""
        words = stage.split()[1:]
        flags, rest = "", []
        for word in words:
            if word.startswith("/") and len(word) == 2 and word[1].lower() in "iv":
                flags += word[1].lower()
            elif word.startswith("/"):
                return None
            else:
                rest.append(word)
        if len(rest) != 1:
            return None
        word = rest[0]
        if len(word) > 2 and word[0] == word[-1] and word[0] in "\"'":
            word = word[1:-1]
        if not _PLAIN_WORD.match(word):
            return None
        return "".join(sorted(set(flags), key=flags.index)), word

    def _key_offered(self, data: bytes) -> bool:
        """What *Send key* offers: inside an adb shell here the keys reach a
        device terminal; the local shell reads a pipe, where only Enter and
        Ctrl+Z (end of input, e.g. for a Python prompt) mean anything."""
        return self._in_adb_shell or data in (b"\n", b"\x1a")

    def _send_key(self, data: bytes) -> bool:
        """The console's *Send key* menu: raw bytes for the shell, not a line."""
        if data == b"\x1a" and self._in_adb_shell:
            # adb.exe reads its input in text mode: Ctrl+Z is end of file to it,
            # and the adb shell would never take another key
            self.term.notice(
                "Ctrl+Z is not sent: adb would stop reading input for good (type exit to leave)",
                theme.ECHO_WARN, new_stream=False,
            )
            return False
        self.ensure_started()
        if not (self.session and self.session.running):
            return False
        return self.session.send(data) is not False

    def _input_refused(self) -> None:
        """The session refused input: the running command reads none of it."""
        now = time.monotonic()
        if now - self._refused_at > 10.0:
            self._refused_at = now
            self.term.notice(
                "Input not sent: the running command is not reading it (Stop ends it)",
                theme.ECHO_WARN, new_stream=False,
            )

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
        """Stop the running command (Stop, Ctrl+C).

        Inside an adb shell started here, Ctrl+C goes to the device: the shell
        runs on a device terminal (``-t -t``), so only the device command stops
        and the adb shell stays. If the adb shell doesn't answer, or Stop is
        pressed again before its prompt is back, the same adb shell is reopened
        in the same device folder.

        A local command gets a real Ctrl+C in the shell's (hidden) console
        (``send_ctrl_c``), as in a console window: ``ping`` prints its
        summary, a REPL its KeyboardInterrupt, and the shell abandons the rest
        of the command line, script or batch file (cmd's "Terminate batch
        job" question is answered yes).  The shell stays with its folder,
        variables and history.  A command that ignores Ctrl+C without a word
        for :attr:`LOCAL_STOP_WAIT_MS`, or Stop pressed again, has its
        processes ended (``kill_command``); if the prompt is still not back
        after another :attr:`LOCAL_STOP_WAIT_MS` (a loop inside PowerShell
        itself), or Stop is pressed once more, a fresh shell replaces it in
        the same folder.  So does a shell that waits for a line itself
        (``pause``, ``set /p``, ``Read-Host``, a ``-Confirm`` question, the
        rest of a ``>>`` block): it notices Ctrl+C only after reading one,
        and Stop never types an answer for the user.  At an idle prompt
        Ctrl+C only starts a new prompt line: it used to replace the whole
        shell, and its background jobs with it."""
        if self._closing or not (self.session and self.session.running):
            return
        if self._in_adb_shell:
            if self._adb_interrupt_pending:
                self._reopen_adb_shell()
            else:
                self._adb_interrupt_pending = True
                self._adb_interrupt_heard = False
                self.session.send(b"\x03")
                # the device command's output stops at once (Save keeps it)
                self.term.interrupt_output()
                self._adb_interrupt_timer.start(self.ADB_INTERRUPT_WAIT_MS)
                self.term.setFocus(Qt.OtherFocusReason)
            return
        self.term.setFocus(Qt.OtherFocusReason)
        if self._stop_pending:
            # Stop again: don't wait any longer
            if self._stop_phase == "ctrl-c":
                self._end_local_command(self.session)
            else:
                self._restart_local_shell("Stopped")
            return
        if self._at_idle_prompt():
            # nothing runs: a new prompt line, as Ctrl+C gives in a console
            self.term._echo("^C")
            self.session.send(b"\n")  # its echo ends the line, the prompt follows
            return
        session = self.session
        self._stop_pending = True
        self._stop_marked = False
        self._batch_answered = False
        self._ctrl_c_sent = False
        self._stop_heard = False
        # what the command printed and is not drawn yet goes (Save keeps it)
        self.term.interrupt_output(until_echo=False)
        if self.shell_type == "powershell":
            self._stop_marked = True
            self.term.notice("^C", theme.ECHO_ERROR)
        else:
            # cmd prints ^C itself when the command it waits for ends as by
            # Ctrl+C; the stream may still hold the command's half sequence
            self.term.reset_stream()
        ctrl_c = getattr(session, "send_ctrl_c", None)
        if ctrl_c is not None:
            self._stop_phase = "ctrl-c"
            self._stop_timer.start(self.LOCAL_STOP_WAIT_MS)
            if ctrl_c(lambda result, s=session: self._emit_ctrl_c_sent(s, result)):
                return
        self._end_local_command(session)

    def _end_local_command(self, session) -> None:
        """End the processes the shell runs (``kill_command``): Stop without a
        console Ctrl+C, or for a command that ignored one."""
        self._stop_phase = "kill"
        kill = getattr(session, "kill_command", None)
        if kill is None:
            self._restart_local_shell()
            return
        self._stop_timer.start(self.LOCAL_STOP_WAIT_MS)
        if not kill(lambda result, s=session: self._emit_command_stopped(s, result)):
            self._restart_local_shell()

    def _emit_ctrl_c_sent(self, session, result) -> None:
        """From the Ctrl+C thread: hand the result to the UI thread."""
        try:
            self._ctrl_c_done.emit(session, result)
        except RuntimeError:
            pass  # the terminal closed meanwhile

    def _on_ctrl_c_sent(self, session, result) -> None:
        """Stop's Ctrl+C was raised in the shell's console (``send_ctrl_c``)."""
        if session is not self.session or not self._stop_pending or self._stop_phase != "ctrl-c":
            return
        sent, programs = result
        if not sent:
            self._end_local_command(session)  # no console to reach: end the processes
            return
        self._ctrl_c_sent = True
        if self._own_prompt:
            # the shell waits at its prompt (it was a background job): it
            # prints no new one by itself
            self._stop_probe_timer.start(self.LOCAL_STOP_PROBE_MS)
        elif programs is False:
            # Nothing runs but the shell itself.  Its own work (Start-Sleep, a
            # loop) ends at once; but waiting for a line (pause, set /p,
            # Read-Host, a -Confirm question, the rest of a ">>" block) it
            # notices the Ctrl+C only once it has read one, and any line could
            # answer the question (Enter confirms -Confirm, ends a ">>" block
            # and runs it).  If its prompt is not back soon, only a fresh
            # shell stops it without answering for the user.
            self._stop_phase = "blocked"
            self._stop_timer.start(self.LOCAL_STOP_PROBE_MS)

    def _emit_command_stopped(self, session, result) -> None:
        """From the stop thread: hand the result to the UI thread."""
        try:
            self._command_stopped.emit(session, result)
        except RuntimeError:
            pass  # the terminal closed meanwhile

    def _on_command_stopped(self, session, result) -> None:
        """The stopped command's processes are gone (``kill_command``)."""
        if session is not self.session or not self._stop_pending:
            return
        killed, foreground = result
        if killed is None:
            self._restart_local_shell("Stopped")  # the processes could not be listed
            return
        if not self._stop_marked:
            self._stop_marked = True
            if not (self.shell_type == "cmd" and foreground):
                self.term.notice("^C", theme.ECHO_ERROR)
        if self._at_idle_prompt():
            self._clear_local_stop()
            return
        # A shell that runs nothing prints no prompt of its own (a background
        # job was ended): ask for one.
        self._stop_probe_timer.start(0 if not killed else self.LOCAL_STOP_PROBE_MS)

    def _probe_local_prompt(self) -> None:
        """Send the shell an empty line for a new prompt, only when it waits
        at its prompt already (a background job was ended).  Anywhere else a
        line can answer a question: pause, set /p or Read-Host in a script
        went on as if the user had pressed Enter, a -Confirm question took
        it for "Yes" and deleted, and a PowerShell ">>" block ran."""
        if not self._stop_pending or self._batch_answered or self._at_idle_prompt():
            return
        if not self._own_prompt:
            return
        if self.session and self.session.running:
            self.term._pending_echo = "\n"  # read as a command: its echo is only a line break
            self.term._pending_echo_at = time.monotonic()
            self.session.send(b"\n")

    def _follow_local_stop(self) -> None:
        """Output after Stop: the prompt settles it; cmd's batch-file question
        is answered yes (Ctrl+C in a batch file ends it)."""
        if self._at_idle_prompt():
            self._clear_local_stop()
            return
        if self.shell_type != "cmd" or self._batch_answered:
            return
        match = self._BATCH_QUESTION.search(strip_ansi(self._prompt_tail))
        if match and self.session and self.session.running:
            self._batch_answered = True
            answer = match.group(1)
            self.term._echo(answer)  # as if typed; cmd's echo of it is hidden
            self._expect_echo(answer)
            self.session.send((answer + "\n").encode("ascii", "replace"))

    def _on_local_stop_timeout(self) -> None:
        if not self._stop_pending or self._closing or self._at_idle_prompt():
            return
        if self._stop_phase == "blocked":
            self._restart_local_shell("Stopped")  # the shell waits for a line (see _on_ctrl_c_sent)
            return
        if self._stop_phase == "ctrl-c" and self.session is not None:
            if not self._stop_heard:
                self._end_local_command(self.session)  # it ignored the Ctrl+C
            # else it answered and runs on (a REPL's KeyboardInterrupt and its
            # prompt): ending it now would kill it under the user; Stop again does
            return
        self._restart_local_shell("Still running")

    def _clear_local_stop(self) -> None:
        self._stop_pending = False
        self._stop_phase = ""
        self._ctrl_c_sent = False
        self._stop_heard = False
        self._stop_timer.stop()
        self._stop_probe_timer.stop()

    def _restart_local_shell(self, why: str = "") -> None:
        """Replace the local shell with a fresh one in the same folder.  *why*
        starts the notice when a ``^C`` is on screen for this stop already."""
        marked = self._stop_pending and self._stop_marked
        self._clear_local_stop()
        self._stop_session(interrupt=True)
        self.reset_adb_shell_context()
        self._started = True
        self.term.notice(
            f"{why} — fresh local shell ready" if marked and why
            else "^C  — stopped; fresh local shell ready",
            theme.ECHO_ERROR,
        )
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
            self.term.notice(f"^C  — stopped; reopening adb shell in {cwd}", theme.ECHO_ERROR)
        else:
            self.reset_adb_shell_context()
            self.term.notice("^C  — stopped; fresh local shell ready", theme.ECHO_ERROR)
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
        typed = self._typed_adb_shell(command)
        line = self._enter_adb_shell(command, foreign=bool(typed and typed[1]))
        self._adb_shell_cwd = self.term._cwd = cwd
        self._adb_reentry_cwd = cwd if cwd != "/" else None
        self.session.send(line)
        self.session.utf8_input = True  # adb reads the input from now on (see _send)

    def hold_adb_shell(self) -> None:
        """adbd is about to restart (adb root / unroot, a reboot): remember an
        adb shell started here, so :meth:`resume_adb_shell` can reopen it."""
        if self._in_adb_shell and self._adb_shell_command:
            self._adb_resume = (self._adb_shell_command, self._adb_shell_cwd)
            self._adb_resume_until = 0.0

    def expect_adb_shell_end(self) -> None:
        """The device is going away (a reboot): an adb shell started here ends
        with it.  The terminal stays in that adb shell until PowerShell's or
        CMD's own prompt shows it has ended (even one that never answered):
        until then Stop still sends Ctrl+C to the device, and the device
        coming back never types into an adb shell that is still running (a
        reboot that failed, or one slow to drop the device)."""
        if self._in_adb_shell:
            self._adb_answered = True

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
        self.term.notice(f"↻  device back — reopening adb shell in {cwd}", theme.ECHO_WARN)
        self._send_adb_shell(command, cwd)

    def reopen(self):
        self._clear_local_stop()
        self._stop_session()
        self.term.clear(keep_prompt=False)  # the old shell's prompt goes with it
        self._closing = False
        self._adb_resume = None
        self.reset_adb_shell_context()
        self._started = False
        self.ensure_started()

    def _announce_adb_restart(self) -> None:
        # the local shell itself carries on: not a new stream
        self.term.notice("Restarting shared ADB server…", theme.ECHO_WARN, new_stream=False)

    def close_panel(self):
        self._closing = True
        self._adb_interrupt_timer.stop()
        self._clear_local_stop()
        self._park_completion_thread()
        self._stop_session()
        try:
            self.term.close_archive()
        except Exception:
            pass


# ---- the Android shell --------------------------------------------------------
# A run of CRs before a line feed: a device terminal ends its lines with CR LF,
# and adb.exe (its stdout is in text mode on Windows) writes CR CR LF.
_CR_RUN_LF = re.compile(rb"\r+\n")
# adb saying the device itself is out of reach, not that it gave no terminal:
# the shell is lost, and one over plain pipes would fail the same way.
_DEVICE_GONE_RE = re.compile(
    r"device (?:'[^']*' )?(?:offline|not found|unauthorized|still authorizing)"
    r"|no devices|no emulators|more than one (?:device|emulator)"
    r"|cannot connect|failed to (?:check server version|connect)|connection reset",
    re.IGNORECASE,
)
# adb's error line ("error: closed", "adb: error: …"), not its warnings
_ADB_ERROR_RE = re.compile(r"(?:^|[\r\n])(?:adb(?:\.exe)?: )?error: ")
# a bare device prompt: "$ " or "# " (after mksh's "N|" exit status)
_BARE_PROMPT = re.compile(r"(?:\d+\|)?[$#] ?")
# what a device terminal echoes for a Ctrl+C by itself ("^C", a line break)
_CTRL_C_ECHO_RE = re.compile(r"\^C|\s+")


def _fold_crs(data: bytes, carry: bytes = b""):
    """``(data, carry)``: *data* with every run of CRs before a LF made one CR,
    and the CRs it ends with held back as *carry* for the next read, where
    their LF may be.  adb.exe writes a device terminal's CR LF as CR CR LF,
    which also put a blank line after every line of a saved log."""
    data = carry + data
    body = data.rstrip(b"\r")
    return _CR_RUN_LF.sub(b"\r\n", body), data[len(body):]


class _ShellInput:
    """Writes one adb shell's input on a thread of its own, in order.

    A write blocks once adb.exe stops reading its input (the device terminal
    is full: a command that reads none of it, a long paste), and written on
    the UI thread it froze the whole window until then.  :meth:`send` hands
    the bytes to the thread and waits a moment for them, so an ordinary line
    goes out at once and a shell that has ended answers False; it never
    waits longer than :attr:`WAIT_S`, and never while earlier input is still
    on its way (that input is what is stuck)."""

    WAIT_S = 0.2
    # Input queued behind a stuck write: more than this is refused.
    QUEUE_MAX = 1 << 20

    def __init__(self, session):
        self._session = session
        self._cond = threading.Condition()
        self._items = deque()     # (ticket, bytes) not handed to the shell yet
        self._queued = 0          # ... and their size
        self._taken = 0           # tickets given out
        self._done = 0            # the last ticket written (or failed)
        self._busy = False        # a write is in progress
        self._failed = False      # a write failed: the shell is gone
        self._closed = False
        self._thread = None

    def send(self, data: bytes) -> bool:
        """Queue *data* for the shell; False when it was refused (the shell
        is gone, or input is stuck behind :attr:`QUEUE_MAX` bytes already)."""
        with self._cond:
            if self._closed or self._failed or self._queued + len(data) > self.QUEUE_MAX:
                return False
            idle = not self._items and not self._busy
            self._taken += 1
            ticket = self._taken
            self._items.append((ticket, data))
            self._queued += len(data)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._write_all, name="turboadb-shell-input", daemon=True
                )
                self._thread.start()
            self._cond.notify_all()
            if not idle:
                return True
            self._cond.wait_for(
                lambda: self._done >= ticket or self._closed or self._failed, self.WAIT_S
            )
            return not self._failed

    def discard(self) -> None:
        """Forget input not handed to the shell yet (Ctrl+C: as a terminal
        drops what was typed ahead)."""
        with self._cond:
            self._items.clear()
            self._queued = 0
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._items.clear()
            self._queued = 0
            self._cond.notify_all()

    def _write_all(self) -> None:
        while True:
            with self._cond:
                while not self._items and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                ticket, data = self._items.popleft()
                self._queued -= len(data)
                self._busy = True
            try:
                ok = self._session.send(data) is not False
            except Exception:
                ok = False
            with self._cond:
                self._busy = False
                self._done = ticket
                if not ok:
                    self._failed = True
                    self._items.clear()
                    self._queued = 0
                self._cond.notify_all()
            if not ok:
                return


class _AndroidShellWidget(_TerminalWidgetBase):
    """The device's interactive ``adb shell``, with Tab completion.

    It runs on a device terminal (``adb shell -t -t``, a PTY) by default: the
    device prints its own prompt and echo, programs write their output as it
    comes (``logcat | grep``, ``sed``, ``awk``), programs that wait for input
    work (``read``, ``su``), and Stop sends a real Ctrl+C, so ``ping`` prints
    its summary and the shell stays.  Full-screen programs (``top``,
    ``watch``, ``vi``) get the console's screen, with every key going to
    them; a view resized meanwhile resizes the device terminal too
    (``resize_terminal``, so the program redraws).  A hidden first line
    switches mksh's line editor off (adb gives the terminal no size, so the
    editor scrolled every line longer than 80 columns sideways and garbled
    its echo), gives the terminal this view's size and reports the
    terminal's name; nothing is shown until it ran.  Output stays in the
    terminal's own colours (``ls`` and ``grep`` are not made to colour it):
    the console colours the words that say what happened.
    The console keeps its cooked mode: one Enter sends one line (a LF), the
    device's echo of it is hidden, and the device prompt tells when the shell
    is ready (for the next pasted line, a Ctrl+C that was answered, the
    folder Tab completion looks in).

    Over plain pipes (the ``android_shell_pty`` setting off, or a device or
    adb that gives no terminal: an adb without ``-t``, a locked-down build)
    the console draws the prompt itself and Stop reopens the shell.  A device
    without shell_v2 (Android 6 and older) gives every adb shell a terminal:
    over pipes its first prompt shows that, and it is handled as one."""
    disconnected = pyqtSignal()

    # printed by the hidden first line, with the terminal's name (``tty``);
    # the output before it is not shown
    _INIT_MARK = re.compile(rb"\x1b\]7718;ready(?:;([^\x07\x1b]*))?\x07")
    # how long the view's size must stay put before the device terminal
    # hears it while a program has the screen
    RESIZE_DELAY_MS = 250
    # how long that output may be held back before all of it is shown
    INIT_WAIT_MS = 3000
    # A terminal shell that ends (or adb reports an error) this soon, before
    # its first prompt, got no terminal: the shell falls back to pipes.
    PTY_CHECK_S = 3.0
    # how long Ctrl+C gets to bring the prompt back before the shell reopens
    INTERRUPT_WAIT_MS = 3000
    # the longest line a device terminal takes (canonical mode: 4095
    # characters and the line feed); a longer one would be cut and run
    _LINE_MAX = 4095
    # more output than this before the first line ran: show it after all
    _HOLD_MAX = 64 * 1024
    _PROMPT_TAIL = _LocalShellWidget._DEVICE_PROMPT_TAIL
    _PROMPT_CWD = _LocalShellWidget._DEVICE_PROMPT_CWD

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
        self._pause_reason = ""
        # "unknown": the next shell open asks the device for user@host;
        # "pending": the tab's connect-time probe will deliver it;
        # "known": delivered, so reopening after Stop asks nothing.
        self._prompt_state = "unknown"
        self._prompt_root = False
        # the shell that runs (see the class docstring)
        self._input = None               # its input, written off the UI thread (_ShellInput)
        self._prompt_seen = False        # it has shown its prompt
        self._early_lines = deque()      # lines typed before that, sent one per prompt
        self._first_prompt = False       # the output due next is that prompt (see _show)
        self._pty = False                # on a device terminal
        self._tty_requested = False      # opened with -t -t
        self._pty_fallback = False       # this device gave no terminal: pipes from now on
        self._opened_by_fallback = False
        self._open_cwd = None
        self._opened_at = 0.0
        self._ready = False              # its first line ran, or its prompt showed
        self._hold = None                # output held back until the first line ran
        self._hold_marked = False        # ... which it has; the banner goes first
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._release_hold)
        self._tail = ""                  # the end of the output
        self._tty_size = None            # (columns, rows) the device terminal was told
        self._tty_name = None            # its device (/dev/pts/N), from the first line
        self._jobs = []                  # resize_terminal calls under way
        self._resizing = False           # ... one of them for this view (one at a time)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(self.RESIZE_DELAY_MS)
        self._resize_timer.timeout.connect(self._resize_device)
        self._lines_sent = 0             # lines sent since the shell opened
        self._at_device_prompt = False
        self._device_prompt = ""         # the last device prompt, for Tab's list
        self._interrupt_pending = False  # Ctrl+C sent, the prompt not back yet
        self._interrupt_heard = b""      # ... and the output since (its end)
        self._interrupt_timer = QTimer(self)
        self._interrupt_timer.setSingleShot(True)
        self._interrupt_timer.timeout.connect(self._on_interrupt_timeout)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._build_toolbar(
            lay,
            restart_tip="Close and reopen the Android shell",
            restart_slot=self.restart_shell,
            info_text="Tab completes • right-click Copy/Paste",
        )
        self.btn_stop.setToolTip(
            "Stop the running command (Ctrl+C); the shell stays, or reopens in the "
            "same folder when the command does not stop"
        )

        self.term = AnsiConsole(send_fn=self._send)
        self.term.set_completion_fn(self._complete)
        self.term.set_interrupt_fn(self.interrupt)
        self.term.set_send_key_fn(self._send_key)
        # over pipes the keys would only be typed into the next command line
        self.term.set_key_offered_fn(lambda data: self._pty or data == b"\n")
        self.term.set_prompt_provider_fn(self._completion_prompt)
        self.term.screen_resized.connect(lambda *_size: self._resize_timer.start())
        self.term.screen_changed.connect(lambda on: self._resize_timer.start() if on else None)
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

    def _completion_prompt(self) -> str:
        """The prompt drawn again below a Tab completion list: the device's
        own on a terminal (over pipes the console draws its own)."""
        return self._device_prompt if self._pty and self._at_device_prompt else ""

    def _open(self, *, focus=True, cwd=None, fallback=False) -> bool:
        """Open the interactive adb shell (in folder *cwd*); False when none runs.

        A failed open leaves no prompt behind, where typing would go nowhere:
        the terminal says so and tells the device tab (:attr:`disconnected`),
        which brings the shell back as after a lost connection.  *fallback*:
        the pipes that replace a terminal the device did not give (see
        :meth:`_shell_ended`)."""
        if not self.handler:
            return False
        self._started = True
        if self._adb_restart_paused:
            # adbd or the adb server is restarting: reconnect() opens the
            # shell once that is over
            self.term.set_alive(False, show_disconnect_notice=False)
            self._pause_notice()
            return False
        pty = not self._pty_fallback and bool(settings_mod.get("android_shell_pty"))
        res = self.handler.open_shell(tty=pty)
        self.session = res.value if isinstance(res, OperationResult) else res
        if self.session is None:
            error = res.error if isinstance(res, OperationResult) else None
            self.term.notice(
                "[could not open adb shell" + (f": {error}" if error else "") + "]",
                theme.ECHO_ERROR,
            )
            self.term.set_alive(False, show_disconnect_notice=False)
            if not self._closing:
                self.disconnected.emit()
            return False

        sess = self.session
        self._input = _ShellInput(sess)
        self._set_mode(pty)
        self._tty_requested = pty
        self._opened_by_fallback = fallback
        self._open_cwd = cwd
        self._opened_at = time.monotonic()
        self._ready = False
        self._tail = ""
        self._lines_sent = 0
        self._tty_size = None
        self._tty_name = None
        carry = [b""]

        def read_fn():
            running = sess.running
            data = sess.read(65536)
            if data:
                data, carry[0] = _fold_crs(data, carry[0])
                if self._pty:
                    # a form feed only moves down a line on a terminal; the
                    # console would clear the screen for it (cmd's cls)
                    data = data.replace(b"\x0c", b"\n")
                return data
            if running:
                return b""
            rest, carry[0] = carry[0], b""
            return rest or None

        self.reader = ReaderThread(read_fn, decode=False)
        rd = self.reader
        self.reader.data.connect(lambda d: self._feed_from(rd, d))
        self.reader.closed.connect(self._on_reader_closed)
        self.reader.start()
        if pty:
            self._start_terminal(cwd)
        elif cwd and cwd != "/":
            self._write(("cd " + shlex.quote(cwd) + "\n").encode("utf-8"))
        if focus:
            self.term.setFocus(Qt.OtherFocusReason)

        self.term.set_prompt(self._prompt_identity(self._prompt_root), root=self._prompt_root)
        if not self.term._alive:
            self.term.set_alive(True)  # before the banner, which needs a live terminal
        if not self._banner_shown and self.term._alive and not (
            self._info.get("kind") or self._banner_waited
        ):
            # Give the device-type probe a moment so the banner shows the full
            # identity (type, CPU, display); never wait longer than 2.5 s.
            self._banner_pending = True
            QTimer.singleShot(2500, self._flush_pending_banner)
        else:
            self._show_banner_and_prompt()
        if self._prompt_state == "unknown" and not pty:
            self._start_prompt_probe()  # for the prompt the console draws over pipes
        return True

    def _set_mode(self, pty: bool) -> None:
        """Leave the prompt and the echo to the device (a terminal), or draw
        the prompt and expect no echo (pipes).

        On a terminal a line typed before the shell's first prompt waits for
        that prompt (see :meth:`_send`): the device echoes it after the
        prompt, where the console leaves it to be shown."""
        self._pty = pty
        self._at_device_prompt = False
        self._prompt_seen = False
        self._early_lines.clear()
        self._first_prompt = False
        self.term.set_emulate_prompt(not pty)
        self.term.set_shell_at_prompt(None)
        self.term.set_typing_ahead(pty)
        # a terminal's programs may take the screen, keys and all
        self.term.set_screen_input(self._screen_write if pty else None)

    def _terminal_size(self):
        """``(columns, rows)`` of this view for ``stty``, or None when it is
        not laid out yet (too small to mean anything)."""
        cols, rows = self._console_columns(), self._console_rows()
        return (cols, rows) if cols >= 20 and rows >= 2 else None

    def _start_terminal(self, cwd=None, held=b""):
        """Send a terminal shell's hidden first line; its output is held back
        until the line has run (see :meth:`_through_hold`)."""
        size = self._terminal_size()
        steps = []
        if size is not None:
            steps.append("stty cols {} rows {}".format(*size))
        self._tty_size = size
        steps += ["set +o emacs", "set +o vi"]  # mksh's line editor (see the class docstring)
        if cwd and cwd != "/":
            steps.append("cd " + shlex.quote(cwd))
        # The leading space keeps it out of a shell history that honours that.
        line = " " + "; ".join(step + " 2>/dev/null" for step in steps)
        line += "; printf '\\033]7718;ready;%s\\007' \"$(tty 2>/dev/null)\"\n"
        self._hold = held
        self._hold_marked = False
        self._hold_timer.start(self.INIT_WAIT_MS)
        self._write(line.encode("utf-8"))

    def _show_banner_and_prompt(self):
        self._banner_pending = False
        if not self._banner_shown and self.term._alive:
            self._banner_shown = True
            try:
                self.term.banner(self._welcome_banner())
            except Exception:
                pass
        if self.term._alive:
            self.term.show_prompt()  # none on a terminal: the device prints its own
        if self._hold is not None and self._hold_marked:
            self._show(self._take_hold())  # the device's first prompt, below the banner

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
        if self.session is not None and not self._closing and not self._pty:
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

    # ---- output ----
    def _feed_from(self, reader, data):
        if reader is not self.reader:
            return
        if self._hold is not None:
            data = self._through_hold(data)
        elif not (self._pty or self._lines_sent) and self._is_prompt(
            self._last_line(self._tail + data[-2048:].decode("utf-8", "replace"))
        ):
            self._adopt_terminal(data)
            return
        if data:
            self._show(data)

    def _show(self, data: bytes) -> None:
        """Draw shell output, and follow what it says about the shell."""
        if self._break_drawn_at:
            data = self._drop_drawn_break(data)
            if not data:
                return
        self._notice_refusal(data)
        self.term.feed(data)
        # only the end matters (a flood's chunk can be a megabyte)
        self._tail = (self._tail + data[-2048:].decode("utf-8", "replace"))[-1024:]
        if self._interrupt_pending:
            self._interrupt_heard = (self._interrupt_heard + data[-4096:])[-4096:]
        if self._pty:
            self._follow_prompt()
            if self._first_prompt and data:
                self._first_prompt = False
                if not self._prompt_seen:
                    self._first_prompt_shown()

    def _first_prompt_shown(self) -> None:
        """What the shell printed after its hidden first line is its first
        prompt, also one that does not look like one here (a device's own
        PS1, ``[shell@PD2318 /]$ ``; waiting for one that did, no line was
        ever sent): a line typed now goes at once, and those typed before go
        now, all together, since no later prompt will look like one either.
        The device echoes them after this one."""
        self._prompt_seen = True
        self.term.set_typing_ahead(False)
        while self._early_lines:
            self._write(self._terminal_input(self._early_lines.popleft(), sized=False))

    @staticmethod
    def _last_line(text: str) -> str:
        """The line *text* ends with, as shown (a CR starts it again)."""
        return strip_ansi(text.rsplit("\n", 1)[-1]).rsplit("\r", 1)[-1]

    def _is_prompt(self, line: str) -> bool:
        """Whether *line* is a device shell prompt waiting for input: mksh's
        ``[N|]HOST:/path $ `` (``#`` as root), busybox's ``/path # ``, a bare
        ``$ ``.  su, sh and exec start a new shell, which prints its own."""
        return bool(line) and bool(self._PROMPT_TAIL.search(line) or _BARE_PROMPT.fullmatch(line))

    def _follow_prompt(self) -> None:
        """At the device prompt the shell waits for a command: the console
        takes the next line as one, a pasted line may run, a Ctrl+C has been
        answered, and the prompt names the folder Tab completion looks in.
        Otherwise a command runs, and a line typed now is its input, which
        the terminal echoes all the same: the console is told "unknown"
        (None), never "no", or it would show that line twice."""
        prompt = self._last_line(self._tail)
        if not self._is_prompt(prompt):
            self._at_device_prompt = False
            self.term.set_shell_at_prompt(None)
            return
        self._ready = True
        self._at_device_prompt = True
        self._device_prompt = prompt
        self._clear_interrupt()
        self.term.set_shell_at_prompt(True)
        self.term.mark_prompt()  # its colours; a program's screen that was showing goes
        match = self._PROMPT_CWD.search(prompt)
        if match:
            self.term._cwd = match.group(1)
            self.term._cd_revert = None  # the device said where it is
        if not self._prompt_seen:
            self._prompt_seen = True
            self.term.set_typing_ahead(False)
        if self._early_lines:
            # a line typed before the first prompt: its turn now (the device
            # echoes it after this prompt; the console never drew it)
            self._write(self._terminal_input(self._early_lines.popleft(), sized=False))

    def _through_hold(self, data: bytes) -> bytes:
        """Output while a terminal shell's first line runs: b"" while it is held
        back, else what to show now."""
        held = self._hold + data
        if not self._hold_marked:
            mark = self._INIT_MARK.search(held)
            if mark is None:
                if self._pty_starting() and _ADB_ERROR_RE.search(
                    held.decode("utf-8", "replace")
                ):
                    self._hold = held
                    self._on_early_error()
                    return b""
                if len(held) > self._HOLD_MAX:
                    self._end_hold()
                    return held
                self._hold = held
                return b""
            self._hold_marked = True
            self._ready = True
            self._first_prompt = True  # what follows the mark is the shell's prompt
            name = (mark.group(1) or b"").decode("utf-8", "replace").strip()
            self._tty_name = name if re.fullmatch(r"/dev/pts/\d+", name) else None
            held = held[mark.end():]
        if self._banner_pending:
            self._hold = held  # the banner goes first
            return b""
        self._end_hold()
        return held

    def _release_hold(self):
        """The first line's mark did not come in time: show what the shell
        printed, which by now ends with its prompt."""
        if self._hold is None or self._closing:
            return
        self._first_prompt = True
        if self._banner_pending:
            self._show_banner_and_prompt()  # which shows a marked hold itself
        held = self._take_hold()
        if held:
            self._show(held)

    def _take_hold(self) -> bytes:
        held = self._hold or b""
        self._end_hold()
        return held

    def _end_hold(self) -> None:
        self._hold = None
        self._hold_marked = False
        self._hold_timer.stop()

    def _adopt_terminal(self, data: bytes) -> None:
        """A shell over pipes printed a prompt before any command: the device
        gave it a terminal after all (no shell_v2: Android 6 and older), with
        its own prompt and echo.  It is set up like one opened with ``-t``, and
        this first prompt waits with the first line's output."""
        self._set_mode(True)
        self._start_terminal(held=data)

    def _pty_starting(self) -> bool:
        """A terminal shell that has not shown its first prompt, opened moments ago."""
        return self._tty_requested and self._starting()

    def _starting(self) -> bool:
        return not self._ready and time.monotonic() - self._opened_at < self.PTY_CHECK_S

    def _on_reader_closed(self):
        if self._closing or self._adb_restart_paused:
            return
        self._shell_ended(self._take_hold())

    def _on_early_error(self):
        """adb reported an error before the terminal shell's first prompt."""
        held = self._take_hold()
        self._close_stream()
        self._shell_ended(held)

    def _shell_ended(self, held: bytes) -> None:
        """The shell ended (or adb failed to start it); *held*: its output that
        was held back.

        A terminal shell that ended before its first prompt, for another
        reason than the device being out of reach, got no terminal: it is
        opened again over pipes.  If that fails at once too, the device was
        the problem after all, and the next open tries a terminal again."""
        self._clear_interrupt()
        early = self._starting()
        text = strip_ansi(held.decode("utf-8", "replace") if held else self._tail)
        if early and self._tty_requested and not _DEVICE_GONE_RE.search(text):
            self._fall_back_to_pipe(text)
            return
        if early and self._opened_by_fallback:
            self._pty_fallback = False
        self._opened_by_fallback = False
        if held:
            self.term.feed(held)  # what the shell printed before it ended
        self.term.set_alive(False)
        self.disconnected.emit()

    def _fall_back_to_pipe(self, text: str) -> None:
        """The device (or its adb) gave no terminal: an adb without ``-t``, a
        build that allows none.  This tab's Android shell runs over pipes from
        now on."""
        cwd = self._open_cwd
        self._pty_fallback = True
        self._close_stream()
        detail = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
        self.term.notice(
            "No device terminal" + (f" ({detail[:160]})" if detail else "")
            + " — the Android shell runs over plain pipes: TurboADB draws its prompt,"
            " and Stop reopens it",
            theme.ECHO_WARN,
        )
        if self._open(focus=False, cwd=cwd, fallback=True):
            self.term.set_alive(True)

    def _close_stream(self, wait_ms=300):
        """Close this widget's adb shell and its reader thread.

        The reader is parked if it doesn't stop within *wait_ms*, so dropping
        the reference can never destroy a still-running QThread.
        """
        reader, self.reader = self.reader, None
        session, self.session = self.session, None
        writer, self._input = self._input, None
        self._early_lines.clear()  # typed for this shell, which never showed its prompt
        self._end_hold()
        self._clear_interrupt()
        self._break_drawn_at = 0.0
        self._at_device_prompt = False
        if reader is not None:
            try:
                reader.closed.disconnect(self._on_reader_closed)
            except Exception:
                pass
        if writer is not None:
            writer.close()  # input still queued was for this shell
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

    # ---- the device goes away and comes back ----
    def reconnect(self, *, focus=True):
        self._adb_restart_paused = False
        # adbd may have restarted (reboot, adb root): ask for user@host again.
        self._prompt_state = "unknown"
        if not self._started:
            return  # never shown: the shell opens when the Terminal first is
        self._close_stream()
        self.term.reset_stream()  # the new shell starts on a fresh line
        self.term._cwd = self.term._prev_cwd = "/"  # where a new adb shell starts
        if self._open(focus=focus):
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
        """Close the shell until :meth:`reconnect`.  A terminal never opened yet
        waits too: opened now, it would only die with adbd."""
        if self._closing:
            return
        self._adb_restart_paused = True
        self._pause_reason = reason
        if not self._started:
            return
        self._close_stream()
        self.term.set_alive(False, show_disconnect_notice=False)
        self._pause_notice()

    def _pause_notice(self):
        self.term.notice(
            f"  ↻  {self._pause_reason} — this shell will reconnect automatically.",
            theme.ECHO_WARN,
        )

    def adb_restart_failed(self):
        """Restart ADB failed, so nothing reconnects this shell by itself: say
        so, and let Stop or Restart shell open it again."""
        if self._closing or not self._adb_restart_paused:
            return
        self._adb_restart_paused = False
        if self._started:
            self.term.notice(
                "ADB restart failed — press Reconnect or Restart shell", theme.ECHO_ERROR
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

    # ---- input ----
    def _write(self, data: bytes) -> bool:
        """Hand *data* to the shell (see :class:`_ShellInput`); False when it
        was refused."""
        writer = self._input
        return writer is not None and writer.send(data)

    def _send(self, data: bytes):
        if not (self.session and self.session.running):
            return
        # Ctrl+Z is end of input to adb.exe: it would never pass on another key
        data = data.replace(b"\x1a", b"")
        if not data:
            return
        self._lines_sent += 1
        if self._pty and not self._prompt_seen:
            # Before its first prompt the terminal would echo the line in the
            # middle of the shell's start (or it went with the hidden first
            # line); from its prompt on it is echoed where it runs.
            self._early_lines.append(data)
            return
        if self._pty:
            data = self._terminal_input(data)
        self._write(data)

    def _terminal_input(self, data: bytes, *, sized=True) -> bytes:
        """A submitted line, as the device terminal gets it.  *sized*: it may
        carry the view's new size (see :meth:`_with_new_size`); not a line the
        console left for the device's echo to show."""
        # One Enter is one LF: Linux adb hands a CR to the terminal as it is (a
        # second Enter), and adb.exe holds a trailing bare CR until more comes.
        if data.endswith(b"\r\n"):
            data = data[:-2] + b"\n"
        elif data.endswith(b"\r"):
            data = data[:-1] + b"\n"
        line = data[:-1] if data.endswith(b"\n") else data
        self._clear_interrupt()  # input after a Ctrl+C: the next Stop sends another
        at_prompt, self._at_device_prompt = self._at_device_prompt, False
        # A command runs now, and the terminal echoes a line typed meanwhile
        # too: "unknown" (not "no", which the console just set) until the
        # output says more (see _follow_prompt).
        self.term.set_shell_at_prompt(None)
        if len(line) > self._LINE_MAX:
            self.term.notice(
                f"Not sent: a device terminal takes at most {self._LINE_MAX} characters "
                f"on one line (this one has {len(line)}); run it from a script file",
                theme.ECHO_WARN, new_stream=False,
            )
            self.term._pending_echo = "\r\n"  # the Enter sent instead is echoed
            self.term._pending_echo_at = time.monotonic()
            return b"\n"
        text = line.decode("utf-8", "replace")
        if at_prompt:
            if text.split(maxsplit=1)[:1] == ["cd"]:
                self.term._apply_cd(text)  # Tab follows at once; the prompt confirms it
            if text.strip() and sized:
                data = self._with_new_size(text, data)
        elif not text and self.term._pending_echo is None and self._last_line(self._tail):
            # Enter for a program's own prompt ("Continue? "): the console
            # ended that line already, and the device echoes the Enter as well
            self._break_drawn_at = time.monotonic()
        return data

    # A command reading what the one before it left ($?, $_, PIPESTATUS):
    # stty in front of it would be that one (`echo $?` said 0 after a failure).
    _READS_STATUS = re.compile(r"\$\{?[?_]|PIPESTATUS")

    def _with_new_size(self, text: str, data: bytes) -> bytes:
        """*data*, a command typed at the device prompt, with ``stty`` for the
        view's size in front when its width changed since the terminal last
        heard it (the window or the font was resized; a row more or less, as
        a scroll bar comes and goes, only matters to full-screen programs).
        At the prompt nothing runs, so the size is in place for this command
        and never typed into a running program; the echo of the whole line
        stays hidden.  A command that reads ``$?`` (or ``$_``, ``PIPESTATUS``)
        goes as typed and the next one takes the size."""
        size = self._terminal_size()
        if size is None or (self._tty_size is not None and size[0] == self._tty_size[0]):
            return data
        if self._READS_STATUS.search(text):
            return data
        setting = " stty cols {} rows {} 2>/dev/null; ".format(*size)
        if len(setting.encode("utf-8")) + len(data) - 1 > self._LINE_MAX:
            return data  # no room on this line: the next command takes it
        self._tty_size = size
        self.term._pending_echo = setting + text
        self.term._pending_echo_at = time.monotonic()
        return setting.encode("utf-8") + data

    def _send_key(self, data: bytes) -> bool:
        """The console's *Send key* menu: raw bytes for the device."""
        if data == b"\x1a":
            self.term.notice(
                "Ctrl+Z is not sent: adb would stop reading this shell's input for good",
                theme.ECHO_WARN, new_stream=False,
            )
            return False
        if not (self.session and self.session.running):
            return False
        return self._write(data)

    def _screen_write(self, data: bytes) -> bool:
        """Keys and answers for a program that has the console's screen
        (see AnsiConsole.set_screen_input): to the device as they are."""
        if not (self.session and self.session.running):
            return False
        self._clear_interrupt()  # the program is being used: a Stop starts over
        return self._write(data)

    def _resize_device(self):
        """A program has the screen and the view's size is not the device
        terminal's any more: tell the terminal, which tells the program
        (see ADBHandler.resize_terminal).  Without the terminal's name the
        next command at the prompt carries the size as usual.

        One size at a time: two in flight (the tab's adb gate lets two
        calls run) could reach the device in either order, and the size
        recorded was then not the terminal's.  The view's size when one is
        done goes next if it is another."""
        screen = self.term.screen()
        if (self._closing or self._resizing or screen is None or not self._tty_name
                or not self.handler):
            return
        size = (screen.cols, screen.rows)
        if size == self._tty_size:
            return
        handler, name = self.handler, self._tty_name
        gate = getattr(self, "adb_gate", None)
        resize = gate.wrap(handler.resize_terminal) if gate is not None else handler.resize_terminal

        def told(result, size=size, name=name):
            self._resizing = False
            if name != self._tty_name:
                self._resize_device()  # the shell was reopened meanwhile: its terminal
            elif isinstance(result, OperationResult) and result.success:
                self._tty_size = size
                self._resize_device()  # the view may have changed size meanwhile

        def failed(_message):
            self._resizing = False

        self._resizing = True
        run_job(self._jobs, lambda: resize(name, size[0], size[1], safe=True), told, failed)

    def focus_terminal(self):
        try:
            self.term.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass

    # ---- Stop and Restart shell ----
    def interrupt(self):
        """Stop the running command (Stop, Ctrl+C).

        On a device terminal Ctrl+C goes to the device: only the command stops
        (``ping`` prints its summary) and the shell stays.  If the command
        ignores it (within :attr:`INTERRUPT_WAIT_MS` neither the prompt nor
        anything but the terminal's own ``^C`` came back), or Stop is pressed
        again before the prompt is back, the shell is reopened in the same
        folder.  A program that answers the Ctrl+C and carries on (sqlite3's
        or a Python prompt, a shell whose prompt looks different) keeps the
        shell.  Over pipes a Ctrl+C cannot reach the device: the shell is
        reopened at once."""
        if self._closing or not self.handler or self._adb_restart_paused:
            return  # paused: the shell comes back by itself
        self.term._cancel_paste()  # the pasted lines were for the stopped command
        live = self.session is not None and self.session.running
        if self._pty and live and self._hold is None:
            if self._interrupt_pending:
                self._hard_reopen(self._reopened("Stopped"))
                return
            if self._input is not None:
                self._input.discard()  # what was typed ahead goes, as on a terminal
            if self._write(b"\x03"):
                # what the command printed before it stopped is not drawn any
                # more (Save still has it): the output stops at once
                self.term.interrupt_output()
                self._interrupt_pending = True
                self._interrupt_heard = b""
                self._interrupt_timer.start(self.INTERRUPT_WAIT_MS)
                self.term.setFocus(Qt.OtherFocusReason)
                return
        self._hard_reopen("^C  — stopped")

    def _interrupt_answered(self) -> bool:
        """Whether the device answered the Ctrl+C with more than the
        terminal's own ``^C``.  Output from before the terminal echoed it
        (the command's last lines, the echo of the line typed) is no answer."""
        text = strip_ansi(self._interrupt_heard.decode("utf-8", "replace"))
        return bool(_CTRL_C_ECHO_RE.sub("", text.rsplit("^C", 1)[-1]))

    def _on_interrupt_timeout(self):
        # A device that answered is alive: reopening would kill the program
        # that handled the Ctrl+C (and the shell's state) under the user.
        if self._interrupt_pending and not self._closing and not self._interrupt_answered():
            self._hard_reopen(self._reopened("Still running"))

    def _clear_interrupt(self):
        self._interrupt_pending = False
        self._interrupt_heard = b""
        self._interrupt_timer.stop()

    def _reopened(self, why: str) -> str:
        cwd = getattr(self.term, "_cwd", "/") or "/"
        return f"{why} — Android shell reopened" + (f" in {cwd}" if cwd != "/" else "")

    def _hard_reopen(self, notice, color=theme.ECHO_ERROR):
        """Close this adb shell and open a fresh one in the same folder."""
        cwd = getattr(self.term, "_cwd", "/") or "/"
        self.term.discard_output()  # queued notices stay

        # Closing this adb shell ends its own device-side process group (the
        # command being stopped).  Never kill processes device-wide: that also
        # killed the Logcat tab and any other tool's logcat/top.
        self._close_stream(wait_ms=800)

        self.term.notice(notice, color)
        self.term._last_feed = 0.0
        self.term._cwd = cwd
        if self._open(cwd=cwd):
            self.term.set_alive(True)

    def restart_shell(self):
        """Close and reopen the shell, whatever runs in it (Restart shell).  It
        also ends a pause that nothing else will (a failed ADB restart)."""
        if self._closing:
            return
        self._adb_restart_paused = False
        self._hard_reopen("Restarting Android shell…", theme.ECHO_WARN)

    def close_panel(self):
        from .qtutil import park_thread, thread_running

        self._closing = True
        self._adb_restart_paused = True
        self._resize_timer.stop()
        close_jobs(self._jobs)
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
        start of every shell's toolbar (the tab bar keeps the page order).

        Without Windows there is no PowerShell or CMD to start: no switcher is
        shown, so only the Android shell can be picked (the local pages stay
        for the code that addresses them, but never start)."""
        self.subtabs.tabBar().hide()
        self._switch_groups = []
        if not _LOCAL_SHELLS:
            return
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
            local.expect_adb_shell_end()

    def adb_restart_failed(self) -> None:
        """Restart ADB failed: the Android shell will not come back by itself."""
        self.android_widget.adb_restart_failed()

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
        self._probe_thread = None
        self._fail_box = None  # the "Connect failed" message (see _show_connect_failure)
        # A probe that got no device profile is run once more (see _on_probe_details).
        self._probe_retried = False
        self._probe_retry_timer = QTimer(self)
        self._probe_retry_timer.setSingleShot(True)
        self._probe_retry_timer.timeout.connect(self._retry_probe)
        self._lazy_pages = {}
        self._adb_gate = _AdbGate(self.ADB_SLOTS)
        self._conn_key = None  # the network connection this tab uses (_Connections)
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
            for sig in ("ok", "details", "fail", "trace"):
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
            cfg = self._ct.cfg
            self._ct.known_state = self._tracked_state(cfg)
            # a tab still connecting counts as a user too: another tab closing
            # meanwhile must not disconnect the device under it
            self._use_connection(_Connections.key(cfg, cfg.target))
            self._ct.start()

    def _tracked_state(self, cfg):
        """What the main window's device tracker last saw of *cfg*'s target
        ("device", "unauthorized", ...), or None. The tracker watches this PC's
        adb server, so a remote-server target never has one."""
        from ..tools import DEFAULT_ADB_SERVER_PORT

        if (
            cfg is None
            or not cfg.target
            or cfg.is_remote_server
            or cfg.adb_server_port != DEFAULT_ADB_SERVER_PORT
        ):
            return None
        for device in getattr(self.window(), "_live_devices", None) or ():
            if getattr(device, "serial", None) == cfg.target:
                return getattr(device, "state", None)
        return None

    def _use_connection(self, key) -> None:
        """This tab now uses *key*'s connection (see :class:`_Connections`)."""
        if key is None or key == self._conn_key:
            return
        _CONNECTIONS.leave(self._conn_key, self)
        _CONNECTIONS.use(key, self)
        self._conn_key = key

    def _leave_connection(self, handler):
        """The tab closes: stop using its connection. Returns the handler to
        drop that connection with, or None to leave it connected."""
        key, registered = _connection_key(handler), self._conn_key
        self._conn_key = None
        drop = _CONNECTIONS.leave(key, self, handler)
        if registered is not None and registered != key:
            drop = _CONNECTIONS.leave(registered, self) or drop
        return drop

    def _auto_start_connect(self):
        if not self._session_closed and not self._started_connect:
            self.start_connect()

    def reconnect_device(self):
        """Retry after a failed connect, a reconnect timeout, or a recovery reboot."""
        if self._session_closed or self._reconnecting:
            return
        self.btn_reconnect.hide()
        self._close_connect_failure()
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
        """Let go of a handler this tab will not use. Its connection is dropped
        (off the UI thread) only if it made it and no open tab uses it."""
        if handler is not None:
            _release_connection(handler)

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
        if self._adbd_busy:
            # adbd restarts on purpose (adb root, making files writable):
            # _release_terminals brings every terminal back once it is done.
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
            or self._adbd_busy
        ):
            return
        if state == "device" and hasattr(self, "shell"):
            self.log.emit("[INFO] Shell connection reset; restarting shell…")
            try:
                self.shell.reconnect()
            except Exception as exc:
                self.log.emit(f"[ERROR] shell reconnect: {exc}")
            # The device is back as far as the Logcat page is concerned too: a
            # capture that ended with the shell (the adb server went away)
            # waits for this to resume; one still running is left alone.
            self._logcat_recovery("resume_after_reconnect")
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

    def _logcat_recovery(self, action, **kwargs):
        """Tell a built Logcat page the device is going or back (a live capture
        pauses and resumes with it); a page never shown is left unbuilt."""
        page = self._built_page("logcat")
        if page is None:
            return
        try:
            getattr(page, action)(**kwargs)
        except Exception as exc:
            self.log.emit(f"[WARNING] logcat: {exc}")

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
        self._logcat_recovery("suspend_for_device_loss")

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
        # Nothing reconnects by itself now: Reconnect is the way back (after
        # _enable_actions, which hides it), and the paused shell says so.
        self._offer_reconnect()
        shell = getattr(self, "shell", None)
        if shell is not None:
            shell.adb_restart_failed()

    def _on_reconnected(self, ok):
        self._reconnecting = False
        self._reboot_in_progress = False
        if self._session_closed:
            return
        if ok:
            name = self.handler.serial or self.session.get("name")
            self.status.setText(f"Connected — {name}")
            self.log.emit("[OK] device back online — reconnected")
            if self._adbd_busy:
                # adbd is still being restarted on purpose: _release_terminals
                # enables the actions and brings the terminals back after it.
                return
            self._enable_actions(True)
            try:
                self.shell.reconnect()
                self.shell.resume_adb_shells()
            except Exception as exc:
                self.log.emit(f"[ERROR] shell reconnect: {exc}")
            self._logcat_recovery("resume_after_reconnect")
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
        """adbd is back (as root or not): the Android shell reconnects (its
        prompt shows ``#`` as root), PowerShell/CMD reopen their adb shells, a
        Logcat capture that ended with adbd resumes, and a built Files page
        lists its folder again."""
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
        # the shell-lost path, which resumes it otherwise, waits while adbd is held
        self._logcat_recovery("resume_after_reconnect")
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
        self._use_connection(_connection_key(handler))
        self.btn_reconnect.hide()
        self._close_connect_failure()  # an earlier attempt's message is out of date
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
        for widget in (self.shell.android_widget, self.shell.ps_widget, self.shell.cmd_widget):
            widget.adb_gate = self._adb_gate  # Tab completion's `adb shell ls` takes a slot
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
        page = PhonePanel(self.handler, adb_gate=self._adb_gate)
        page.log.connect(self.log)
        return page

    def _build_webcam(self):
        page = CameraPanel()
        page.log.connect(self.log)
        return page

    # A probe that got no device profile (adbd refused a second shell right
    # after connecting, the device served its properties slowly) runs once
    # more after this long.  Without it a head unit whose first probe failed
    # was a phone for the whole session: no IVI Displays tab, the phone
    # defaults in Apps and Device Control, no automotive reboot warning.
    PROBE_RETRY_MS = 2500

    def _on_probe_details(self, handler, result, *, retried=False) -> None:
        """The connect-time probe finished: device profile, prompt and displays.

        *retried*: the result of the one repeat of a probe that got no profile."""
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
            elif not retried:
                # The probe did not get that far: the panel's own quiet adb
                # scan (once: the repeated probe does not start another).
                mirror.refresh_displays(quiet=True)
        info = result.get("info")
        if info is None:
            error = result.get("error") or "no reply"
            if not retried and not self._probe_retried:
                self._probe_retried = True
                self._probe_retry_timer.start(self.PROBE_RETRY_MS)
                self.log.emit(
                    f"[INFO] Device details are still unavailable: {error} "
                    "(asking the device again in a moment)"
                )
                return
            info = OperationResult(False, "device_info", error=error)
        self._on_device_info(handler, info)

    def _retry_probe(self) -> None:
        """Run the connect-time probe once more (see :attr:`PROBE_RETRY_MS`),
        through the tab's adb slots like the first one."""
        handler = self.handler
        if self._session_closed or handler is None:
            return
        if (self._reconnecting or self._reboot_in_progress
                or self._adb_restart_in_progress or self._adbd_busy):
            # The device is away or adbd restarts: ask once it is back.
            self._probe_retry_timer.start(self.PROBE_RETRY_MS)
            return
        from .qtutil import park_thread, thread_running

        previous = self._probe_thread
        if thread_running(previous):
            park_thread(previous)
        thread = _ProbeThread(handler, gate=self._adb_gate)
        thread.details.connect(
            lambda result, h=handler: self._on_probe_details(h, result, retried=True)
        )
        thread.finished.connect(thread.deleteLater)
        self._probe_thread = thread
        thread.start()

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
        stack, cluster, passenger screen…) side by side, in its own tab. Every
        display starts stopped: the user starts one, or Start all."""
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
        had_wall = getattr(self, "display_wall", None) is not None
        if len(displays) > 1 or self._automotive:
            self._add_ivi_tab()
        wall = getattr(self, "display_wall", None)
        if wall is None or (displays and not had_wall):
            # (a wall created just now was built from mirror_tab._displays,
            # which is this very scan: _got_displays sets it before emitting)
            return
        wall.set_displays(displays)

    def _show_all_displays(self) -> None:
        """Options → All displays: open the displays tab. It only navigates —
        every display stays stopped until the user starts it (or Start all)."""
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
        self._show_connect_failure(msg)

    def _show_connect_failure(self, msg) -> None:
        """Say why the connect failed, without blocking anything.

        The failure arrives from the connect worker, maybe for a tab in the
        background.  A modal message box ran an event loop of its own: every
        other tab waited for it, and quitting (or closing this tab) meanwhile
        destroyed the box under that loop, which crashed.  This one belongs to
        the tab and goes with it; a newer failure replaces it."""
        self._close_connect_failure()
        box = QMessageBox(QMessageBox.Warning, "Connect failed", str(msg), QMessageBox.Ok, self)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.setWindowModality(Qt.NonModal)
        self._fail_box = box
        box.show()

    def _close_connect_failure(self) -> None:
        box, self._fail_box = getattr(self, "_fail_box", None), None
        if box is not None:
            try:
                box.done(0)  # closes (and deletes) it without asking it first
            except RuntimeError:
                pass  # already closed (and deleted) by the user

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
        self._logcat_recovery("suspend_for_device_loss", reboot=True)

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
        self._probe_retry_timer.stop()
        self._close_connect_failure()

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
        for attr in ("_ct", "_rc", "_probe_thread"):
            t = getattr(self, attr, None)
            if t is None:
                continue
            try:
                if hasattr(t, "cancel"):
                    t.cancel()
            except RuntimeError:
                pass
            for sig in ("ok", "details", "fail", "done"):
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
        for panel in (getattr(self, "display_wall", None), self.__dict__.get("controls")):
            if panel is not None:
                try:
                    pending_workers.extend(panel.shutdown_threads())
                except RuntimeError:
                    pass
        if self._device_dispatcher is not None:
            self._device_dispatcher.stop()
            if thread_running(self._device_dispatcher):
                park_thread(self._device_dispatcher)
                pending_workers.append(self._device_dispatcher)
        # Only a network connection this app made and no other open tab uses
        # is dropped, after the workers still using it (see _Connections);
        # anything else stays connected for its other users.  So does one a
        # separate screen window uses that the exit leaves open (Settings →
        # Startup): dropped, it cut that window off its device.
        drop = self._leave_connection(self.handler)
        if drop is not None and not self._keeps_a_screen():
            _drop_connection_later(drop, after=list(dict.fromkeys(pending_workers)))

    def _keeps_a_screen(self) -> bool:
        """True when a separate scrcpy window of this tab outlives it (see
        MirrorPanel.keep_window_open)."""
        try:
            return any(getattr(panel, "_window_kept", False)
                       for panel in self.findChildren(MirrorPanel))
        except RuntimeError:
            return False

    def closeEvent(self, event):
        self.close_session()
        super().closeEvent(event)
