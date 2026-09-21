"""
The main ADB handler: device connect (USB + TCP/IP + pairing), shell (one-shot
and interactive), live logcat streaming with match + tee, file push/pull with
progress, app install/uninstall/control, screenshots & screen recording, port
forward/reverse, and scrcpy mirroring.

Designed for three consumers from one object:
  * test-automation framework  -> raise-on-error (default)
  * standalone CLI script       -> see turboadb.cli
  * PyQt5 tool                  -> safe=True + log_callback (see turboadb.gui)

Everything shells out to Google's ``adb`` (and ``scrcpy``); the raw executable
path is always available via :attr:`adb_path`, so anything adb can do is possible.
"""

from __future__ import annotations

import os
import re
import time
import shlex
import itertools
import logging
import posixpath
import threading
import queue
import dataclasses
import subprocess
from typing import Callable, Optional, Sequence, Union

from .config import ADBConfig, ScrcpyOptions, format_host_port, validate_port
from .devices import list_devices
from . import touch
from .results import CommandResult, TransferResult, StreamResult, OperationResult, strip_ansi
from .tools import DEFAULT_ADB_SERVER_PORT, NO_WINDOW, find_adb, is_adb_server_alive
from .exceptions import (
    ADBCommandError,
    ADBConnectionError,
    ADBError,
    ADBInstallError,
    ADBNotConnectedError,
    ADBTimeoutError,
    ADBTransferError,
    ADBNotFoundError,
)


# Hoisted out of the per-poll read path: ShellSession.read() runs many times
# a second per open terminal, and re-importing these each time is wasted work.
if os.name == "nt":
    import ctypes as _ctypes
    import msvcrt as _msvcrt
    from ctypes import wintypes as _wintypes

    _PEEK_NAMED_PIPE = _ctypes.windll.kernel32.PeekNamedPipe
else:  # pragma: no cover - POSIX has no named-pipe peek
    _ctypes = _msvcrt = _wintypes = None
    _PEEK_NAMED_PIPE = None


_TOKEN_COUNTER = itertools.count()


def _unique_token() -> str:
    """A name fragment unique within this process and across processes.

    ``time.monotonic_ns()`` alone is not enough: on Windows it ticks only every
    ~15 ms, so two quick calls from one thread produced the same name."""
    return f"{os.getpid()}_{threading.get_ident()}_{next(_TOKEN_COUNTER)}_{time.time_ns()}"


_ROW_SPLIT_RE = re.compile(r"(?m)^Row: \d+ ")


def _iter_records(text):
    """Yield ``content query`` records lazily.

    re.split() built the whole list first, so a limit of 50 still paid
    for every row on the device."""
    previous = None
    for match in _ROW_SPLIT_RE.finditer(text or ""):
        if previous is not None:
            yield text[previous:match.start()]
        previous = match.end()
    if previous is not None:
        yield text[previous:]


# --------------------------------------------------------------------------- #
# Interactive shell session (adb shell over a subprocess pipe)
# --------------------------------------------------------------------------- #
class ShellSession:
    """A persistent interactive ``adb shell`` (one subprocess) for flows that
    need state between commands or full send/expect interaction. Used by the GUI
    terminal; usable directly in scripts too."""

    def __init__(self, proc: subprocess.Popen, encoding: str = "utf-8"):
        self._proc = proc
        self.encoding = encoding
        self._eof = False

    @property
    def proc(self) -> subprocess.Popen:
        return self._proc

    @property
    def running(self) -> bool:
        return self._proc.poll() is None

    @property
    def at_eof(self) -> bool:
        """True once the shell's output pipe is closed or broken (the shell is
        gone). :meth:`read` returns ``b''`` both when nothing is pending and at
        EOF — this tells the two apart."""
        return self._eof

    def send(self, data: Union[str, bytes]) -> bool:
        """Write *data* to the shell. Returns False (instead of silently
        pretending it was sent) when the pipe is closed — the shell exited."""
        if isinstance(data, str):
            data = data.encode(self.encoding, "replace")
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            # BrokenPipeError (an OSError) / write to a closed file
            self._eof = True
            return False

    def send_line(self, line: str) -> bool:
        return self.send(line + "\n")

    def read(self, size: int = 65536) -> bytes:
        """Read available bytes without hanging on Windows pipe buffers.

        Returns ``b''`` when no output is pending AND at EOF; check
        :attr:`at_eof` to distinguish an idle shell from a dead one."""
        try:
            if not self._proc or not self._proc.stdout:
                self._eof = True
                return b""
            if os.name == "nt":
                h = _msvcrt.get_osfhandle(self._proc.stdout.fileno())
                avail = _wintypes.DWORD()
                if not _PEEK_NAMED_PIPE(h, None, 0, None, _ctypes.byref(avail), None):
                    self._eof = True  # broken pipe: the writer (adb) has exited
                    return b""
                if avail.value == 0:
                    return b""
                return os.read(self._proc.stdout.fileno(), min(size, avail.value))
            data = self._proc.stdout.read1(size)  # type: ignore[attr-defined]
            if not data:
                self._eof = True
            return data
        except AttributeError:
            data = self._proc.stdout.read(size)
            if not data:
                self._eof = True
            return data
        except (OSError, ValueError):
            self._eof = True
            return b""
        except Exception:
            return b""


    def resize(self, cols: int, rows: int) -> None:
        # adb shell over a pipe has no PTY to resize; kept for API parity.
        pass

    def close(self) -> None:
        self._eof = True
        try:
            if self.running:
                self._proc.terminate()
            if self._proc.stdin:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
            if self._proc.stdout:
                try:
                    self._proc.stdout.close()
                except Exception:
                    pass
            try:
                self._proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=1.0)
                except Exception:
                    pass
        except Exception:
            pass

    def __enter__(self) -> "ShellSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Port-forward handle
# --------------------------------------------------------------------------- #
class ForwardHandle:
    """A live ``adb forward`` / ``adb reverse`` rule you can later stop."""

    def __init__(self, handler: "ADBHandler", kind: str, local: str, remote: str):
        self._h = handler
        self.kind = kind  # "forward" or "reverse"
        self.local = local
        self.remote = remote
        self._closed = False

    def close(self) -> bool:
        """Remove the rule from THIS handler's device (``adb -s SERIAL forward
        --remove LOCAL`` / ``reverse --remove REMOTE``). Returns True when adb
        removed it. On failure a warning is logged, False is returned and the
        handle stays open so ``close()`` can be retried."""
        if self._closed:
            return True
        if self.kind == "forward":
            args, spec = ["forward", "--remove", self.local], self.local
        else:
            args, spec = ["reverse", "--remove", self.remote], self.remote
        try:
            res = self._h._run(args, timeout=15, check=False)
        except Exception as exc:
            self._h._emit(logging.WARNING, f"could not remove {self.kind} {spec}: {exc}")
            return False
        if not res.ok:
            detail = self._h._combined_output(res) or f"exit {res.exit_code}"
            self._h._emit(logging.WARNING, f"could not remove {self.kind} {spec}: {detail}")
            return False
        self._closed = True
        return True

    def __enter__(self) -> "ForwardHandle":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        arrow = "->" if self.kind == "forward" else "<-"
        return f"<ForwardHandle {self.kind} {self.local} {arrow} {self.remote}>"


# --------------------------------------------------------------------------- #
# The handler
# --------------------------------------------------------------------------- #
class ADBHandler:
    """High-level adb + scrcpy handler. See the package README for recipes."""

    def __init__(
        self,
        config: Optional[ADBConfig] = None,
        *,
        serial: Optional[str] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        logger: Optional[logging.Logger] = None,
        safe: bool = False,
        quiet: bool = False,
    ):
        if config is None:
            config = ADBConfig(serial=serial)
        elif serial:
            config.serial = serial
        self.config = config
        self._safe_default = safe
        self._log_callback = log_callback
        self._quiet = quiet
        self.log = logger or self._build_logger()

        self._adb: Optional[str] = None
        self._serial: Optional[str] = config.target  # active -s target
        self._connected = False
        self._owned_target: Optional[str] = None  # a TCP target THIS handler connected
        # guards the one-time adb resolution; re-entrant, so a log callback
        # that reads adb_path while it is being resolved can't deadlock
        self._adb_lock = threading.RLock()
        self._cap_method: Optional[int] = None  # cached screencap method idx
        self._touch: Optional[tuple] = None  # cached (TouchDevice|None, writable)
        self._screencap_ids: dict = {}  # logical display id -> physical id

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    def _build_logger(self) -> logging.Logger:
        """This target's library logger. No handler is attached and no level is
        set — that is the application's decision (the CLI and GUI configure
        the ``turboadb`` logger). Attaching a StreamHandler here duplicated
        output and reset the level for every handler sharing the name."""
        return logging.getLogger(f"turboadb.{self.config.target or 'device'}")

    def _emit(self, level: int, msg: str) -> None:
        if not (self._quiet and level < logging.WARNING):
            self.log.log(level, msg)
        if self._log_callback:
            try:
                self._log_callback(f"[{logging.getLevelName(level)}] {msg}")
            except Exception:
                pass

    def set_log_callback(self, fn: Optional[Callable[[str], None]]) -> None:
        """Route every log line (the real adb command, its exit code/duration,
        and full error text) to *fn* — used by the GUI to surface meaningful
        logs instead of one-word status lines."""
        self._log_callback = fn

    # ------------------------------------------------------------------ #
    # Safe-mode wrapper
    # ------------------------------------------------------------------ #
    def _guard(self, action: str, fn, *args, safe: Optional[bool] = None, **kwargs):
        use_safe = self._safe_default if safe is None else safe
        if not use_safe:
            return fn(*args, **kwargs)
        try:
            return OperationResult(True, action, value=fn(*args, **kwargs))
        except Exception as exc:
            self._emit(logging.ERROR, f"{action} failed: {exc}")
            return OperationResult(False, action, error=exc)

    # ------------------------------------------------------------------ #
    # adb plumbing
    # ------------------------------------------------------------------ #
    @property
    def adb_path(self) -> str:
        """Absolute path to the resolved ``adb`` executable (cached).

        On first resolution this also runs the one-time auto-fetch (downloads the
        latest platform-tools + scrcpy after an install/upgrade), unless a
        specific ``adb_path`` was configured or ``TURBOADB_AUTO_FETCH=0`` is set.
        """
        if self._adb is None:
            with self._adb_lock:
                if self._adb is None:
                    self._resolve_adb()
        return self._adb

    def _resolve_adb(self) -> None:
        """Resolve adb once, under :attr:`_adb_lock`."""
        try:
            self._adb = find_adb(self.config.adb_path)
        except ADBNotFoundError:
            # An explicit path is a caller error; never replace it with a
            # download behind their back.  For normal auto-detection, honour
            # the documented one-time managed-tools fallback.
            if self.config.adb_path:
                raise
            from . import toolsdl

            result = toolsdl.ensure_tools(notify=lambda m: self._emit(logging.INFO, m))
            try:
                self._adb = find_adb()
            except ADBNotFoundError as exc:
                errors = (result or {}).get("errors", {})
                detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
                raise ADBNotFoundError(
                    "adb was not found and TurboADB could not install platform-tools"
                    + (f" ({detail})" if detail else "")
                ) from exc

    @property
    def serial(self) -> Optional[str]:
        """The active device serial (``host:port`` for a network target)."""
        return self._serial

    def _base(self, target: bool = True) -> list:
        cmd = [self.adb_path]
        if self.config.adb_server_host:  # route through a remote adb server
            cmd += ["-H", self.config.adb_server_host, "-P", str(self.config.adb_server_port)]
        elif self.config.adb_server_port != DEFAULT_ADB_SERVER_PORT:
            cmd += ["-P", str(self.config.adb_server_port)]  # local server, other port
        if target and self._serial:
            cmd += ["-s", self._serial]
        return cmd

    def _exec(
        self, args: Sequence[str], *, timeout, target: bool, check: bool, binary: bool = False
    ) -> CommandResult:
        cmd = self._base(target=target) + list(args)
        eff_timeout = timeout if timeout is not None else self.config.command_timeout
        # started_at and duration are one matched pair: callers add them to
        # get the end time, so both stay on the wall clock.  Deadlines that
        # drive control flow use time.monotonic() instead (see iter_lines).
        start = time.time()
        try:
            out = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=eff_timeout,
                creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise ADBTimeoutError(
                f"adb command timed out after {eff_timeout}s: {' '.join(args)!r}"
            ) from exc
        except FileNotFoundError as exc:  # pragma: no cover
            raise ADBError(f"adb executable not runnable: {exc}") from exc
        enc = "utf-8" if binary else self.config.encoding
        stdout = (out.stdout or b"") if binary else (out.stdout or b"").decode(enc, "replace")
        result = CommandResult(
            " ".join(args),
            out.returncode,
            stdout,
            (out.stderr or b"").decode(enc, "replace"),
            time.time() - start,
            device=self._serial or "",
            started_at=start,
        )
        if check and not result.ok:
            raise ADBCommandError(" ".join(args), result)
        return result

    def _run(self, args, *, timeout=None, check=False, binary=False) -> CommandResult:
        """Run an adb command scoped to this device (``adb -s SERIAL ...``)."""
        return self._exec(args, timeout=timeout, target=True, check=check, binary=binary)

    def _run_global(self, args, *, timeout=None, check=False) -> CommandResult:
        """Run an adb command not scoped to a device (connect/devices/pair...)."""
        return self._exec(args, timeout=timeout, target=False, check=check)

    def _logged_run(self, label, args, *, timeout=None, check=False) -> CommandResult:
        """Run a device command, logging the real adb command line and its
        outcome (duration on success; exit code + stderr on failure) so the GUI
        log explains exactly what happened, not just 'ok'."""
        self._emit(logging.DEBUG, f"$ adb {' '.join(args)}")
        res = self._run(args, timeout=timeout)
        if res.ok:
            self._emit(logging.DEBUG, f"  -> {label}: ok ({res.duration:.2f}s)")
        else:
            detail = (res.stderr.strip() or res.text or "").splitlines()
            detail = " ".join(detail)[:240] if detail else "no output"
            self._emit(
                logging.WARNING,
                f"  -> {label}: exit {res.exit_code} ({res.duration:.2f}s): {detail}",
            )
        if check and not res.ok:
            raise ADBCommandError(" ".join(args), res)
        return res

    # ------------------------------------------------------------------ #
    # Output helpers shared by every action
    # ------------------------------------------------------------------ #
    @staticmethod
    def _shell_args(*argv) -> list:
        """``["shell", ...]`` with every argument quoted for the device shell.

        adb joins the words after ``shell`` with spaces and hands the result to
        ``sh -c``, so an unquoted caller value — a URL containing ``&``, a
        package or path with spaces or ``;`` — would be split or run as a
        separate command. Shell-safe values (``[A-Za-z0-9_@%+=:,./-]``) pass
        through unchanged. :meth:`shell` stays a deliberate raw command line."""
        return ["shell"] + [shlex.quote(str(a)) for a in argv]

    @staticmethod
    def _display_flag(display_id) -> list:
        """``["-d", ID]`` for ``input`` / ``screencap``, or ``[]`` for the
        default display. The one builder for both: it VALIDATES the id as an
        integer and quotes it, because these words are re-split by ``sh -c``
        on the device."""
        if display_id is None:
            return []
        return ["-d", shlex.quote(str(int(display_id)))]

    @staticmethod
    def _prepare_local_path(local_path):
        """*local_path* with ``~`` expanded and its folder created (None stays
        None). Every method that writes a file on this machine goes through it,
        so ``--save ~/boot.log`` and ``out/clip.mp4`` behave the same
        everywhere."""
        if not local_path:
            return local_path
        path = os.path.expanduser(local_path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return path

    @staticmethod
    def _combined_output(res) -> str:
        """stdout AND stderr (stripped) — adb puts the real failure reason on
        stderr while stdout often holds only progress chatter."""
        parts = [res.text, (res.stderr or "").strip()]
        return "\n".join(p for p in parts if p)

    @classmethod
    def _command_error(cls, command: str, res) -> ADBCommandError:
        """ADBCommandError whose message carries the combined output."""
        detail = cls._combined_output(res)
        if detail and detail != (res.stderr or "").strip():
            res = dataclasses.replace(res, stderr=detail)
        return ADBCommandError(command, res)

    # Words that mean "this didn't happen" in the output of svc / cmd / am /
    # settings, which commonly exit 0 while printing a refusal.
    _FAILURE_MARKERS = (
        "unknown command",
        "no such",
        "usage:",
        "error",
        "exception",
        "not allowed",
        "permission",
        "denied",
        "invalid",
        "failed",
        "killed",
        "not found",
        "unsupported",
        "aborted",
        "unable to resolve",
        "no activities",
        "does not exist",
    )
    # `am start` / `am broadcast` echo the intent they were given, e.g.
    # "Starting: Intent { dat=https://example.com/error }" — never sniff that.
    _ECHO_LINE_RE = re.compile(r"^\s*(?:Starting|Broadcasting): Intent \{.*$", re.M)

    @classmethod
    def _output_failed(cls, res, extra=()) -> bool:
        """True when a command visibly did nothing: non-zero exit, or a refusal
        in its output (ignoring the intent echo line)."""
        if not res.ok:
            return True
        out = cls._ECHO_LINE_RE.sub("", cls._combined_output(res)).lower()
        return any(marker in out for marker in cls._FAILURE_MARKERS + tuple(extra))

    def adb(self, *args, timeout=None, check=False, safe: Optional[bool] = None):
        """Escape hatch: run an arbitrary ``adb -s SERIAL`` command and get a
        CommandResult. e.g. ``dev.adb("shell", "wm", "size")``."""
        return self._guard(
            "adb", lambda: self._run(list(args), timeout=timeout, check=check), safe=safe
        )

    # ------------------------------------------------------------------ #
    # Connect / disconnect
    # ------------------------------------------------------------------ #
    def connect(self, *, safe: Optional[bool] = None):
        """Bring the configured target online.

        Network target: ``adb connect host:port``. USB target: just verify the
        device is present (optionally waiting for it). Returns this handler (or
        an OperationResult in safe mode).
        """
        return self._guard("connect", self._connect, safe=safe)

    def _connect(self) -> "ADBHandler":
        cfg = self.config
        remote = cfg.is_remote_server
        # make sure the (local) server is up; for a remote server we don't start
        # a local one — we talk to theirs.
        if not remote and not is_adb_server_alive(port=cfg.adb_server_port):
            # The shared launcher observes the port directly and is protected
            # by one process-local lock.  Spawning a second `adb start-server`
            # here raced the GUI pre-warm worker and made first connection slow.
            from .tools import ensure_adb_server

            if not ensure_adb_server(cfg.adb_path, timeout=12.0, port=cfg.adb_server_port):
                raise ADBConnectionError(
                    f"ADB server did not become ready on port {cfg.adb_server_port}"
                )

        if cfg.host and cfg.auto_connect and not remote:
            target = cfg.target
            if not target:  # guarded by cfg.host, keeps static type checkers honest
                raise ADBConnectionError("network target is missing a host")
            self._emit(logging.INFO, f"Connecting to {target}…")
            owned = self._adb_connect(
                target,
                cfg.connect_timeout,
                hint=(
                    f". Check the head unit's IP, that 'adb tcpip {cfg.port}' was run "
                    f"over USB first (or wireless debugging is on), and that you can "
                    f"reach it on the network."
                ),
            )
            self._owned_target = target if owned else None
            self._serial = target

        if self._serial is None:
            # no explicit target: bind to the only online device on the (local or
            # remote) adb server
            try:
                devs = list_devices(
                    cfg.adb_path, server_host=cfg.adb_server_host, server_port=cfg.adb_server_port
                )
            except ConnectionError as exc:
                raise ADBConnectionError(str(exc)) from exc
            online = [d for d in devs if d.is_online]
            if len(online) == 1:
                self._serial = online[0].serial
            elif len(online) > 1:
                raise ADBConnectionError(
                    f"Multiple devices on {'the remote server' if remote else 'USB'} "
                    f"({', '.join(d.serial for d in online)}); set serial=... to "
                    f"choose one."
                )
            elif remote:
                raise ADBConnectionError(
                    f"No online devices on the adb server at {cfg.adb_server_host}:"
                    f"{cfg.adb_server_port}. Plug a device into that machine and "
                    f"check 'adb devices' there."
                )

        if cfg.auto_wait:
            self._wait_for_device(cfg.connect_timeout)

        state = self.get_state()
        if state != "device":
            raise ADBConnectionError(
                f"Device {self._serial or '(any)'} is '{state}', not ready. "
                f"If 'unauthorized', accept the USB-debugging / wireless prompt on "
                f"the screen; if 'offline', replug or re-run adb connect."
            )
        self._connected = True
        self._emit(logging.INFO, f"Connected to {self._serial or 'device'} ({state}).")
        return self

    def _adb_connect(self, target: str, timeout: float, *, hint: str = "") -> bool:
        """``adb connect TARGET`` — the one place its output is judged. Raises
        ADBConnectionError on failure; returns True when this call created the
        connection (False when adb reports it was already connected)."""
        self._emit(logging.DEBUG, f"$ adb connect {target}")
        res = self._run_global(["connect", target], timeout=timeout)
        text = self._combined_output(res)
        low = text.lower()
        if not res.ok or any(w in low for w in ("cannot", "failed", "unable", "error:")):
            raise ADBConnectionError(
                f"adb connect {target} failed: {text or f'exit {res.exit_code}'}{hint}"
            )
        return "already connected" not in low

    def _wait_for_device(self, timeout: float) -> CommandResult:
        args = ["wait-for-device"]
        try:
            return self._run(args, timeout=timeout, check=False)
        except ADBTimeoutError as exc:
            raise ADBConnectionError(
                f"Timed out after {timeout}s waiting for {self._serial or 'a device'}."
            ) from exc

    # adb's own wording when the target isn't attached to the server at all —
    # a different problem from "it is there but not ready yet".
    _NOT_CONNECTED_RE = re.compile(
        r"not found|not connected|no devices?(?:/emulators?)? found", re.I
    )

    def wait_for_device(self, timeout: Optional[float] = None, *, safe: Optional[bool] = None):
        """Block until the device is online (``adb wait-for-device``).

        Raises ADBNotConnectedError when adb reports the target as not
        connected (a stale serial, or a network device that was never
        ``connect``ed), and ADBConnectionError for any other failure."""

        def _do():
            res = self._wait_for_device(timeout or self.config.connect_timeout)
            if not res.ok:
                detail = self._combined_output(res) or f"exit {res.exit_code}"
                target = self._serial or "a device"
                if self._NOT_CONNECTED_RE.search(detail):
                    raise ADBNotConnectedError(
                        f"{target} is not connected: {detail}. Call connect() "
                        f"(or 'adb connect host:port') first."
                    )
                raise ADBConnectionError(f"wait-for-device failed for {target}: {detail}")
            return True

        return self._guard("wait_for_device", _do, safe=safe)

    def disconnect(self, *, safe: Optional[bool] = None):
        """Disconnect. For network targets this runs ``adb disconnect host:port``;
        USB targets are simply released (the device stays attached)."""

        def _do():
            if self._serial and ":" in self._serial:
                self._run_global(["disconnect", self._serial], check=False)
                self._emit(logging.INFO, f"Disconnected {self._serial}.")
            self._owned_target = None
            self._connected = False
            return True

        return self._guard("disconnect", _do, safe=safe)

    close = disconnect

    @property
    def is_connected(self) -> bool:
        try:
            return self.get_state() == "device"
        except Exception:
            return False

    def get_state(self) -> str:
        """``device`` | ``offline`` | ``unauthorized`` | ``bootloader`` | … | ``unknown``.

        adb reports problem states as an error on stderr with a non-zero exit
        (``error: device unauthorized.``), so both streams are examined."""
        try:
            res = self._run(["get-state"], timeout=10, check=False)
        except ADBError:
            return "unknown"
        if res.ok and res.text:
            return res.text.split()[0]
        m = re.search(
            r"\b(unauthorized|offline|no permissions|authorizing|connecting)\b",
            self._combined_output(res),
            re.I,
        )
        return m.group(1).lower() if m else "unknown"

    def get_serialno(self, *, safe: Optional[bool] = None):
        """``adb get-serialno`` for the active target."""
        return self._guard(
            "get_serialno",
            lambda: self._run(["get-serialno"], timeout=10, check=False).text,
            safe=safe,
        )

    @staticmethod
    def devices(
        adb_path: Optional[str] = None,
        server_host: Optional[str] = None,
        server_port: int = DEFAULT_ADB_SERVER_PORT,
    ) -> list:
        """List devices on the local (or a remote) adb server."""
        return list_devices(adb_path, server_host=server_host, server_port=server_port)

    def restart_server(self, *, safe: Optional[bool] = None):
        """Kill and restart the adb server this handler talks to (``adb
        kill-server`` then ``adb start-server``, honouring a remote host or a
        non-default port). Fixes 'device not visible' after an adb version
        clash. Every shell/stream on that server is dropped.

        Returns the ``start-server`` CommandResult; raises ADBCommandError when
        the server could not be started (a failed kill — usually "not running"
        — is only logged as a warning)."""

        def _do():
            self._emit(logging.INFO, "Restarting the adb server…")
            stopped = self._kill_server()
            started = self._run_global(["start-server"], timeout=30)
            if not started.ok:
                raise self._command_error("start-server", started)
            if not stopped.ok:
                self._emit(
                    logging.WARNING,
                    "adb kill-server reported an error (the server was probably not "
                    "running); start-server brought it up.",
                )
            self._emit(logging.INFO, "ADB server restarted.")
            return started

        return self._guard("restart_server", _do, safe=safe)

    def _kill_server(self):
        """``adb kill-server``, then wait for a local daemon to release its port."""
        stopped = self._run_global(["kill-server"], timeout=15)
        if not self.config.adb_server_host:
            port = self.config.adb_server_port
            deadline = time.monotonic() + 5.0
            while is_adb_server_alive(port=port, timeout=0.1) and time.monotonic() < deadline:
                time.sleep(0.1)
        return stopped

    def stop_server(self, *, safe: Optional[bool] = None):
        """Stop the adb server this handler talks to (``adb kill-server``).

        True once it is gone — including when none was running. Every shell and
        stream on that server ends with it, so this is for leaving: the GUI
        calls it on exit (Settings → Startup) so no adb daemon is left behind."""

        def _do():
            self._kill_server()
            if self.config.adb_server_host:
                return True  # a remote server answers only its own machine
            alive = is_adb_server_alive(port=self.config.adb_server_port, timeout=0.2)
            if alive:
                self._emit(logging.WARNING, "the adb server is still running after kill-server")
            else:
                self._emit(logging.INFO, "ADB server stopped.")
            return not alive

        return self._guard("stop_server", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # TCP/IP + pairing (Android 11+ wireless debugging)
    # ------------------------------------------------------------------ #
    def tcpip(self, port: int = 5555, *, safe: Optional[bool] = None):
        """Restart adbd on the USB-attached device listening on TCP *port*, so it
        can be reached over the network. Run this while on USB; afterwards use
        ``ADBConfig(host=..., port=port)`` to connect wirelessly."""

        def _do():
            p = validate_port(port)
            res = self._run(["tcpip", str(p)], timeout=20)
            text = self._combined_output(res)
            if not res.ok or "error" in text.lower():
                raise self._command_error(f"tcpip {p}", res)
            self._emit(logging.INFO, f"adbd now listening on tcp:{p}.")
            return text

        return self._guard("tcpip", _do, safe=safe)

    @staticmethod
    def _pick_route_ip(route_text: str) -> str:
        """The best LAN source address from ``ip route``: Wi-Fi, then Ethernet,
        then other interfaces — never cellular/tunnel/loopback routes, which
        cannot be reached with ``adb connect`` from the PC."""
        cands = []
        for line in (route_text or "").splitlines():
            m_src = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", line)
            if not m_src or m_src.group(1).startswith("127."):
                continue
            m_dev = re.search(r"\bdev\s+(\S+)", line)
            dev = m_dev.group(1) if m_dev else ""
            if dev.startswith(("rmnet", "ccmni", "dummy", "tun", "lo", "p2p", "rndis")):
                continue
            rank = 0 if dev.startswith(("wlan", "wifi")) else 1 if dev.startswith("eth") else 2
            cands.append((rank, m_src.group(1)))
        cands.sort(key=lambda c: c[0])  # stable: keeps route order within a rank
        return cands[0][1] if cands else ""

    def device_ip(self, *, safe: Optional[bool] = None):
        """The device's own Wi-Fi/LAN IPv4 address (best-effort: the Wi-Fi /
        Ethernet ``ip route`` source first, then wlan0/eth0), or '' if it has none."""

        def _do():
            r = self._run(["shell", "ip", "route"], timeout=15, check=False)
            ip = self._pick_route_ip(r.stdout or "")
            if ip:
                return ip
            for iface in ("wlan0", "eth0"):
                r = self._run(
                    ["shell", "ip", "-f", "inet", "addr", "show", iface], timeout=15, check=False
                )
                m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", r.stdout or "")
                if m:
                    return m.group(1)
            return ""

        return self._guard("device_ip", _do, safe=safe)

    def go_wireless(self, port: int = 5555, *, safe: Optional[bool] = None):
        """One-shot USB → Wi-Fi switch: read the device's IP, restart adbd in
        TCP mode (``adb tcpip``), then ``adb connect`` to it. Returns the new
        ``host:port`` serial. Run while the device is on USB; afterwards the
        cable can be unplugged."""

        def _do():
            ip = self.device_ip(safe=False)
            if not ip:
                raise ADBConnectionError(
                    "the device reports no Wi-Fi/LAN IP — join it to the same "
                    "network first, then retry."
                )
            self.tcpip(port, safe=False)
            # adbd restarts in TCP mode; retry until it listens instead of
            # guessing with a fixed sleep
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    return self.connect_tcp(ip, port, safe=False)
                except ADBConnectionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.5)

        return self._guard("go_wireless", _do, safe=safe)

    def connect_tcp(self, host: str, port: int = 5555, *, safe: Optional[bool] = None):
        """``adb connect host:port`` and switch this handler to that target."""

        def _do():
            target = format_host_port(host, port)
            owned = self._adb_connect(target, 20)
            self._owned_target = target if owned else None
            self._serial = target
            self.config.host, self.config.port = host, int(port)
            return self._serial

        return self._guard("connect_tcp", _do, safe=safe)

    def pair(self, host: str, port: int, code: str, *, safe: Optional[bool] = None):
        """Pair with an Android 11+ device using its Wireless-debugging code
        (``adb pair host:port code``). Pairing port differs from the connect port."""

        def _do():
            try:
                proc = subprocess.run(
                    self._base(target=False) + ["pair", format_host_port(host, port)],
                    input=(code + "\n").encode(),
                    capture_output=True,
                    timeout=30,
                    creationflags=NO_WINDOW,
                )
            except subprocess.TimeoutExpired as exc:
                raise ADBTimeoutError(
                    f"adb pair {format_host_port(host, port)} timed out after 30s"
                ) from exc
            out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
            if "Successfully paired" not in out:
                raise ADBConnectionError(f"Pairing failed: {out.strip()}")
            self._emit(logging.INFO, f"Paired with {format_host_port(host, port)}.")
            return out.strip()

        return self._guard("pair", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Reboot / root / device state
    # ------------------------------------------------------------------ #
    def reboot(self, mode: Optional[str] = None, *, safe: Optional[bool] = None):
        """Reboot the device. *mode*: None (normal), ``recovery``, ``bootloader``,
        ``fastboot`` (userspace fastbootd) or ``sideload``."""
        args = ["reboot"] + ([mode] if mode else [])
        return self._guard("reboot", lambda: self._run(args, timeout=30, check=False).ok, safe=safe)

    def _checked_adb(self, args, markers, *, timeout=30, success_marker=None, shell=False):
        """Run *args*; raise ADBCommandError on a non-zero exit or when the
        output contains one of *markers* (these commands often exit 0 while
        refusing). *success_marker* in the output overrides the markers.
        Returns the combined output, or "ok" when there was none."""
        res = self._run(self._shell_args(*args) if shell else list(args), timeout=timeout)
        text = self._combined_output(res)
        low = text.lower()
        refused = any(m in low for m in markers)
        if success_marker and success_marker in low:
            refused = False
        if not res.ok or refused:
            raise self._command_error(" ".join(("shell",) + tuple(args)) if shell else " ".join(args), res)
        return text or "ok"

    def _adbd_restart(self, verb: str, already: str):
        text = self._checked_adb([verb], ("cannot run as root", "error"))
        if already not in text.lower():
            # adbd restarts; wait for it to come back instead of a fixed sleep
            try:
                self._wait_for_device(10.0)
            except ADBError:
                pass
        return text

    def root(self, *, safe: Optional[bool] = None):
        """Restart adbd as root (``adb root``). Raises ADBCommandError on
        production/secure builds ("adbd cannot run as root")."""
        return self._guard(
            "root", lambda: self._adbd_restart("root", "already running as root"), safe=safe
        )

    def unroot(self, *, safe: Optional[bool] = None):
        """Restart adbd without root (``adb unroot``)."""
        return self._guard(
            "unroot", lambda: self._adbd_restart("unroot", "not running as root"), safe=safe
        )

    _REMOUNT_REFUSALS = ("remount failed", "not running as root", "permission denied", "error:")

    def remount(self, *, safe: Optional[bool] = None):
        """Remount /system (and friends) read-write (needs root)."""
        return self._guard(
            "remount",
            lambda: self._checked_adb(
                ["remount"],
                self._REMOUNT_REFUSALS,
                timeout=60,
                success_marker="remount succeeded",
            ),
            safe=safe,
        )

    # "Now reboot your device for settings to take effect", "Reboot the device
    # for changes to take effect": adbd changed verity/overlayfs and needs a boot.
    _REBOOT_REQUEST_RE = re.compile(
        r"\breboot (?:your|the) device\b|\bnow reboot\b|\breboot to take effect\b", re.I
    )

    def access_status(self, *, safe: Optional[bool] = None):
        """What decides whether protected files can be changed, from one shell call.

        Returns a dict: ``root`` (adbd runs as uid 0), ``uid`` (int or None),
        ``debuggable`` (``ro.debuggable=1``: ``adb root`` is allowed),
        ``build_type`` (``user``, ``userdebug`` or ``eng``), ``verity``
        (``ro.boot.veritymode``: ``enforcing``, ``disabled``, ``logging`` or
        ``""``) and ``bootloader`` (``locked``, ``unlocked`` or ``""``)."""

        def _do():
            script = (
                'echo "@@$(id -u)|$(getprop ro.debuggable)|$(getprop ro.build.type)'
                '|$(getprop ro.boot.veritymode)|$(getprop ro.boot.vbmeta.device_state)'
                '|$(getprop ro.boot.flash.locked)"'
            )
            res = self._run(["shell", script], timeout=15, check=False)
            line = next(
                (ln[2:] for ln in reversed(res.text.splitlines()) if ln.startswith("@@")), None
            )
            if not res.ok or line is None:
                raise self._command_error("shell id -u; getprop", res)
            parts = (line.split("|") + [""] * 6)[:6]
            uid_text, debuggable, build_type, verity, device_state, flash_locked = (
                part.strip() for part in parts
            )
            bootloader = device_state.lower()
            if not bootloader and flash_locked in ("0", "1"):
                bootloader = "locked" if flash_locked == "1" else "unlocked"
            uid = int(uid_text) if uid_text.isdigit() else None
            return {
                "root": uid == 0,
                "uid": uid,
                "debuggable": debuggable == "1",
                "build_type": build_type,
                "verity": verity.lower(),
                "bootloader": bootloader if bootloader in ("locked", "unlocked") else "",
            }

        return self._guard("access_status", _do, safe=safe)

    def _remount_once(self):
        """``adb remount`` -> ``(output, reboot_needed)``; raises when refused."""
        res = self._run(["remount"], timeout=60, check=False)
        text = self._combined_output(res)
        low = text.lower()
        succeeded = "remount succeeded" in low
        reboot_needed = not succeeded and bool(self._REBOOT_REQUEST_RE.search(text))
        refused = not res.ok or any(marker in low for marker in self._REMOUNT_REFUSALS)
        if refused and not (succeeded or reboot_needed):
            raise self._command_error("remount", res)
        return text or "ok", reboot_needed

    def make_writable(
        self,
        *,
        root: bool = True,
        disable_verity: bool = False,
        remount: bool = True,
        reboot: bool = True,
        boot_timeout: float = 180.0,
        on_step=None,
        safe: Optional[bool] = None,
    ):
        """Prepare the device so protected files can be changed.

        In order: ``adb root``; ``adb disable-verity`` (then a reboot, since it
        only applies after one); ``adb remount`` — and when remount itself asks
        for a reboot (Android 10+ sets up overlayfs first), reboot, root and
        remount again. After every reboot adbd starts without root, so root
        is requested again when *root* is set. With *reboot=False* nothing
        reboots and ``reboot_needed`` says a reboot is still due.

        *on_step(text)* is called before each step (from the calling thread).
        Returns ``{"steps": [(step, output), …], "rebooted": bool,
        "reboot_needed": bool}``. Raises an ADBError when a step is refused
        (ADBCommandError for "adbd cannot run as root in production builds").
        """

        def _do():
            steps = []
            state = {"rebooted": False, "reboot_needed": False}

            def note(text):
                if on_step is not None:
                    on_step(text)

            def do_root():
                note("adb root")
                steps.append(("root", self.root(safe=False)))

            def reboot_and_wait(reason):
                if not reboot:
                    state["reboot_needed"] = True
                    return False
                note(f"reboot ({reason})")
                self.shell("sync", timeout=30, safe=False)
                if not self.reboot(safe=False):
                    raise ADBError(f"reboot after {reason} was refused")
                note("waiting for the device to boot")
                self.wait_for_boot(boot_timeout, safe=False)
                steps.append(("reboot", f"rebooted after {reason}"))
                state["rebooted"] = True
                if root:
                    do_root()
                return True

            if root:
                do_root()
            if disable_verity:
                note("adb disable-verity")
                out = self.disable_verity(safe=False)
                steps.append(("disable-verity", out))
                if "already disabled" not in out.lower():
                    reboot_and_wait("disable-verity")
            if remount:
                note("adb remount")
                out, reboot_needed = self._remount_once()
                steps.append(("remount", out))
                if reboot_needed and reboot_and_wait("remount"):
                    note("adb remount")
                    out, reboot_needed = self._remount_once()
                    steps.append(("remount", out))
                    if reboot_needed:
                        raise ADBError(
                            "adb remount still asks for a reboot after rebooting: " + out
                        )
            return {"steps": steps, **state}

        return self._guard("make_writable", _do, safe=safe)

    _VERITY_REFUSALS = (
        "only works for",
        "not running as root",
        "cannot be disabled",
        "cannot be enabled",
        "error:",
    )

    def disable_verity(self, *, safe: Optional[bool] = None):
        """``adb disable-verity`` — disable dm-verity (needs root; reboot after)."""
        return self._guard(
            "disable_verity",
            lambda: self._checked_adb(["disable-verity"], self._VERITY_REFUSALS),
            safe=safe,
        )

    def enable_verity(self, *, safe: Optional[bool] = None):
        """``adb enable-verity`` — re-enable dm-verity (reboot after)."""
        return self._guard(
            "enable_verity",
            lambda: self._checked_adb(["enable-verity"], self._VERITY_REFUSALS),
            safe=safe,
        )

    def mount_rw(self, path: str = "/", *, safe: Optional[bool] = None):
        """Remount a partition read-write via the shell (needs root).
        Default ``/`` (system-as-root); pass ``/system`` on older devices."""
        return self._guard(
            "mount_rw",
            lambda: self._checked_adb(
                ["mount", "-o", "remount,rw", path],
                ("mount:", "denied", "not permitted", "read-only", "no such"),
                shell=True,
            ),
            safe=safe,
        )

    # ------------------------------------------------------------------ #
    # Device input & controls (keys, text, connectivity) — handy for IVI /
    # head units with no keyboard: type from the PC into the focused field.
    # ------------------------------------------------------------------ #
    # common Android keycodes
    KEYS = {
        "home": 3,
        "back": 4,
        "call": 5,
        "menu": 82,
        "search": 84,
        "recents": 187,
        "power": 26,
        "sleep": 223,
        "wake": 224,
        "vol_up": 24,
        "vol_down": 25,
        "vol_mute": 164,
        "play_pause": 85,
        "stop": 86,
        "next": 87,
        "prev": 88,
        "rewind": 89,
        "fast_forward": 90,
        "media_play": 126,
        "media_pause": 127,
        "up": 19,
        "down": 20,
        "left": 21,
        "right": 22,
        "center": 23,
        "enter": 66,
        "del": 67,
        "tab": 61,
        "space": 62,
        "esc": 111,
        "brightness_up": 221,
        "brightness_down": 220,
        "notifications": 83,
    }

    def keyevent(
        self,
        key,
        *,
        longpress: bool = False,
        display_id: Optional[int] = None,
        safe: Optional[bool] = None,
    ):
        """Send a key. *key* is a keycode int or a name from :attr:`KEYS`
        (e.g. ``"home"``, ``"vol_up"``, ``"play_pause"``)."""

        def _do():
            code = self.KEYS.get(key, key) if isinstance(key, str) else key
            lp = ["--longpress"] if longpress else []
            args = self._shell_args("input", *self._display_flag(display_id), "keyevent", *lp, code)
            label = f"key {key}" if isinstance(key, str) else f"keyevent {code}"
            return self._logged_run(label, args, timeout=15).ok

        return self._guard("keyevent", _do, safe=safe)

    def keyevents(
        self,
        keys,
        *,
        display_id: Optional[int] = None,
        safe: Optional[bool] = None,
    ):
        """Send several keys, in order, with one ``input keyevent K1 K2 …`` call.

        Each ``input`` invocation starts a JVM on the device, which costs a few
        hundred milliseconds, so a burst of presses (for example Backspace
        tapped five times) is far faster as one call than as one call per key.
        *keys* holds keycode ints or names from :attr:`KEYS`; an empty sequence
        does nothing and succeeds."""

        def _do():
            codes = [self.KEYS.get(k, k) if isinstance(k, str) else k for k in keys]
            if not codes:
                return True
            args = self._shell_args(
                "input", *self._display_flag(display_id), "keyevent", *codes
            )
            label = "keyevents " + " ".join(str(c) for c in codes)
            return self._logged_run(label, args, timeout=15).ok

        return self._guard("keyevents", _do, safe=safe)

    @staticmethod
    def _input_text_chunks(text: str) -> list:
        """Split *text* into ``input text`` payloads.

        Android's ``input text`` has exactly one escape: every ``%s`` becomes a
        space (``\\%`` is NOT an escape — it types a literal backslash). Spaces
        are therefore sent as ``%s``, and a literal ``%s`` in the text is split
        across two invocations so its ``%`` and ``s`` never meet."""
        parts = str(text).split("%s")
        chunks = []
        for i, part in enumerate(parts):
            piece = ("s" if i else "") + part + ("%" if i < len(parts) - 1 else "")
            if piece:
                chunks.append(piece.replace(" ", "%s"))
        return chunks

    def input_text(
        self,
        text: str,
        *,
        display_id: Optional[int] = None,
        safe: Optional[bool] = None,
    ):
        """Type *text* into the currently focused field on the device — works as
        a keyboard for head units / devices with no on-screen keyboard."""

        def _do():
            disp = self._display_flag(display_id)
            for chunk in self._input_text_chunks(text):
                res = self._logged_run(
                    f"type {text!r}", self._shell_args("input", *disp, "text", chunk), timeout=15
                )
                if not res.ok:
                    return False
            return True

        return self._guard("input_text", _do, safe=safe)

    def tap(
        self,
        x: int,
        y: int,
        *,
        display_id: Optional[int] = None,
        safe: Optional[bool] = None,
    ):
        return self._guard(
            "tap",
            lambda: self._run(
                self._shell_args("input", *self._display_flag(display_id), "tap", x, y), timeout=15
            ).ok,
            safe=safe,
        )

    # A tap costs about this long, without any rate limit: one `input` call
    # starts a JVM on the device, while `sendevent` is a small native tool.
    _BURST_TAP_SECONDS = {"input": 0.25, "events": 0.05}
    _BURST_MAX_TAPS = 1_000_000

    def touch_device(self, *, refresh: bool = False, safe: Optional[bool] = None):
        """The device's touchscreen, from ``getevent -pl``, or None when it has
        none TurboADB can drive.

        ``{"path", "name", "max_x", "max_y", "protocol", "direct", "writable"}``.
        ``writable`` says whether the adb shell user may send events to it
        (often only after ``adb root``). Cached per handler; *refresh* re-reads."""

        def _do():
            device, writable = self._touch_screen(refresh=refresh)
            if device is None:
                return None
            return dict(device.as_dict(), writable=writable)

        return self._guard("touch_device", _do, safe=safe)

    def _touch_screen(self, *, refresh: bool = False):
        """``(TouchDevice|None, writable)``, read once per handler."""
        if self._touch is not None and not refresh:
            return self._touch
        text = self._run(["shell", "getevent", "-pl"], timeout=20, check=False).text
        device = touch.pick_touch_device(touch.parse_touch_devices(text))
        writable = False
        if device is not None:
            writable = self._run(self._shell_args("test", "-w", device.path),
                                 timeout=15, check=False).ok
        self._touch = (device, writable)
        return self._touch

    def _burst_taps(self, count, rate, duration) -> int:
        """How many taps a burst makes, from *count* or *rate*+*duration*."""
        if duration is not None:
            if not rate:
                raise ValueError("a burst measured in seconds needs a rate (taps per second)")
            if float(duration) <= 0:
                raise ValueError("duration must be greater than 0")
            count = int(float(rate) * float(duration))
        taps = int(count)
        if taps < 1:
            raise ValueError("a burst needs at least one tap")
        if taps > self._BURST_MAX_TAPS:
            raise ValueError(f"at most {self._BURST_MAX_TAPS} taps in one burst")
        return taps

    def _burst_command(self, x, y, *, method: str, display_id):
        """``(method, tap command, device)`` for the burst loop.

        ``"events"`` needs a touchscreen the shell may write to, and sends to
        whichever display that screen belongs to — so a burst aimed at another
        display always goes through ``input``."""
        wanted = (method or "auto").lower()
        if wanted not in ("auto", "input", "events"):
            raise ValueError('method must be "auto", "input" or "events"')
        device = writable = None
        if wanted != "input":
            device, writable = self._touch_screen()
        if wanted == "events":
            if device is None:
                raise ADBError(
                    "no touchscreen was found in 'getevent -pl' — use method='input'")
            if not writable:
                raise ADBError(
                    f"the adb shell may not write to {device.path} (try adb root) "
                    "— use method='input'")
            if display_id is not None:
                raise ADBError(
                    "a touchscreen belongs to its own display — send a burst to another "
                    "display with method='input'")
        if wanted == "auto" and (device is None or not writable or display_id is not None):
            device = None
        if device is None:
            flag = self._display_flag(display_id)
            return "input", touch.input_tap(int(x), int(y), flag), None
        screen = self._gesture_size(display_id=None)
        ev_x, ev_y = touch.scale_point(int(x), int(y), screen, device)
        return "events", touch.sendevent_tap(device, ev_x, ev_y), device

    def tap_burst(
        self,
        x,
        y,
        *,
        count: int = 100,
        rate: Optional[float] = None,
        duration: Optional[float] = None,
        display_id: Optional[int] = None,
        method: str = "auto",
        on_progress=None,
        timeout: Optional[float] = None,
        safe: Optional[bool] = None,
    ):
        """Tap one point many times — thousands of taps for a soak or stress test.

        The whole burst runs as ONE loop on the device, so no tap waits for the
        PC. *method* ``"events"`` sends touch events with ``sendevent`` (several
        times faster, and what ``"auto"`` picks when the shell may write to the
        touchscreen); ``"input"`` uses ``input tap`` and works on any device.
        *rate* caps taps per second (a delay between taps, so it is an upper
        bound); *duration* with *rate* says how long to keep tapping instead of
        how many taps. *on_progress(done, total)* is called as the device
        reports its progress.

        Returns ``{"method", "taps", "seconds", "rate", "device"}``. Raises
        ADBError when the device stopped tapping early (for example when it
        refused the events)."""

        def _do():
            if rate is not None and float(rate) <= 0:
                raise ValueError("rate must be greater than 0")
            taps = self._burst_taps(count, rate, duration)
            chosen, command, device = self._burst_command(x, y, method=method,
                                                          display_id=display_id)
            sleep_s = 1.0 / float(rate) if rate else 0.0
            every = max(1, min(100, taps // 20)) if taps > 20 else 0
            script = touch.burst_script(command, taps, sleep_s=sleep_s, progress_every=every)
            per_tap = sleep_s + self._BURST_TAP_SECONDS[chosen]
            limit = float(timeout) if timeout else max(60.0, taps * per_tap + 30.0)
            self._emit(logging.INFO,
                       f"tapping {x},{y} {taps} times ({chosen})"
                       + (f" at up to {rate:g}/s" if rate else ""))
            done, problems = 0, []
            keep_problems = 5  # only problems[:3] is ever reported
            started = time.monotonic()
            for line in self.iter_lines(["shell", script], timeout=limit):
                value = touch.parse_progress(line)
                if value is None:
                    if line.strip():
                        if len(problems) < keep_problems:
                            problems.append(line.strip())
                    continue
                done = value
                if on_progress is not None:
                    on_progress(done, taps)
            elapsed = max(time.monotonic() - started, 1e-6)
            if done < taps:
                detail = "; ".join(problems[:3]) or f"it stopped after {done} of {taps} taps"
                raise ADBError(f"the tap burst did not finish: {detail}")
            return {
                "method": chosen,
                "taps": done,
                "seconds": round(elapsed, 3),
                "rate": round(done / elapsed, 1),
                "device": device.path if device is not None else None,
            }

        return self._guard("tap_burst", _do, safe=safe)

    def swipe(
        self,
        x1,
        y1,
        x2,
        y2,
        ms: int = 200,
        *,
        display_id: Optional[int] = None,
        safe: Optional[bool] = None,
    ):
        return self._guard(
            "swipe",
            lambda: (
                self._run(
                    self._shell_args(
                        "input", *self._display_flag(display_id), "swipe", x1, y1, x2, y2, ms
                    ),
                    timeout=15,
                ).ok
            ),
            safe=safe,
        )

    def _screen_size(self, display_id: Optional[int] = None):
        """The display's CURRENT logical size (override size + rotation applied)
        — the coordinate space ``input tap/swipe`` uses. ``dumpsys window
        displays`` reports it as ``cur=WxH``; ``wm size`` (Override size before
        Physical size) is the fallback.

        Returns ``None`` when neither answers. A guessed 1080x1920 default sent
        every gesture off-screen on a 1920x720 head unit — and still reported
        success — so the callers refuse instead."""
        want = 0 if display_id is None else int(display_id)
        try:
            text = self._run(["shell", "dumpsys", "window", "displays"], timeout=10).text
            for block in re.split(r"(?m)^\s*Display: mDisplayId=", text)[1:]:
                m_id = re.match(r"(\d+)", block)
                if m_id and int(m_id.group(1)) == want:
                    m = re.search(r"\bcur=(\d+)x(\d+)", block)
                    if m:
                        return int(m.group(1)), int(m.group(2))
                    break
        except ADBError:
            pass
        try:
            cmd = ["shell", "wm", "size"] + self._display_flag(display_id)
            txt = self._run(cmd, timeout=10).text
            m = re.search(r"Override size:\s*(\d+)x(\d+)", txt) or re.search(r"(\d+)x(\d+)", txt)
            if m:
                return int(m.group(1)), int(m.group(2))
        except ADBError:
            pass
        return None

    def _gesture_size(self, display_id: Optional[int] = None):
        """:meth:`_screen_size`, or ADBError — a gesture must never be aimed at
        a made-up screen."""
        size = self._screen_size(display_id=display_id)
        if size is None:
            raise ADBError(
                "could not read the display size (neither 'dumpsys window displays' "
                f"nor 'wm size' reported one for display {0 if display_id is None else display_id})"
                " — a gesture aimed at a guessed size would land in the wrong place"
            )
        return size

    def scroll(
        self,
        direction: str,
        *,
        display_id: Optional[int] = None,
        safe: Optional[bool] = None,
    ):
        """Scroll the screen with a swipe gesture — works on **any touch screen**
        (unlike D-pad keys, which only work on D-pad/IVI launchers). *direction*:
        up | down | left | right."""

        def _do():
            w, h = self._gesture_size(display_id)
            cx, cy = w // 2, h // 2
            d = direction.lower()
            if d == "up":
                pts = (cx, int(h * 0.70), cx, int(h * 0.30))
            elif d == "down":
                pts = (cx, int(h * 0.30), cx, int(h * 0.70))
            elif d == "left":
                pts = (int(w * 0.70), cy, int(w * 0.30), cy)
            else:
                pts = (int(w * 0.30), cy, int(w * 0.70), cy)
            return self._logged_run(
                f"scroll {d}",
                self._shell_args("input", *self._display_flag(display_id), "swipe", *pts, 200),
                timeout=12,
            ).ok

        return self._guard("scroll", _do, safe=safe)

    def tap_center(self, *, display_id: Optional[int] = None, safe: Optional[bool] = None):
        """Tap the middle of the display. Raises ADBError when the device won't
        report its size, rather than tapping a guessed centre."""

        def _do():
            w, h = self._gesture_size(display_id)
            return self._logged_run(
                "tap center",
                self._shell_args("input", *self._display_flag(display_id), "tap", w // 2, h // 2),
                timeout=10,
            ).ok

        return self._guard("tap_center", _do, safe=safe)

    def _try_toggles(self, label, attempts, blocked_msg, *, timeout=15):
        """Run *attempts* (list of (argv, ok_message)) until one visibly works;
        return its message, else *blocked_msg* — an honest result instead of a
        false 'ok'. (These commands exit 0 with an error TEXT on many builds.)"""
        for args, msg in attempts:
            r = self._logged_run(label, self._shell_args(*args), timeout=timeout)
            if not self._output_failed(r):
                return msg
        return blocked_msg

    def set_wifi(self, on: bool, *, safe: Optional[bool] = None):
        """Toggle Wi-Fi. ``svc wifi`` first (classic), then ``cmd wifi
        set-wifi-enabled`` (the modern interface — ``svc wifi`` was removed in
        Android 12 and is permission-blocked on most automotive builds)."""
        st = "enable" if on else "disable"
        mode = "enabled" if on else "disabled"
        return self._guard(
            "set_wifi",
            lambda: self._try_toggles(
                f"wifi {st}",
                [
                    (["svc", "wifi", st], "ok"),
                    (["cmd", "wifi", "set-wifi-enabled", mode], "ok (cmd wifi)"),
                ],
                "wifi can't be toggled via adb on this device (the shell user is "
                "permission-denied — common on automotive builds)",
            ),
            safe=safe,
        )

    def set_bluetooth(self, on: bool, *, safe: Optional[bool] = None):
        """Toggle Bluetooth. ``svc bluetooth`` was REMOVED in Android 12+, so
        fall back to ``cmd bluetooth_manager`` there."""
        st = "enable" if on else "disable"
        return self._guard(
            "set_bluetooth",
            lambda: self._try_toggles(
                f"bluetooth {st}",
                [
                    (["svc", "bluetooth", st], "ok"),
                    (["cmd", "bluetooth_manager", st], "ok (cmd bluetooth_manager)"),
                ],
                "bluetooth can't be toggled via adb on this device (svc bluetooth "
                "was removed in Android 12+ and cmd bluetooth_manager is "
                "permission-blocked here)",
            ),
            safe=safe,
        )

    def set_mobile_data(self, on: bool, *, safe: Optional[bool] = None):
        """Toggle Cellular / Mobile Data via `svc data`."""
        st = "enable" if on else "disable"
        val = "1" if on else "0"
        return self._guard(
            "set_mobile_data",
            lambda: self._try_toggles(
                f"data {st}",
                [
                    (["svc", "data", st], "ok"),
                    (["cmd", "telephony", "data", st], "ok (telephony)"),
                    (
                        ["settings", "put", "global", "mobile_data", val],
                        "mobile_data setting written (only the setting changed — the "
                        "modem may ignore it until the radio restarts)",
                    ),
                ],
                "mobile data can't be toggled via adb on this device",
            ),
            safe=safe,
        )

    def set_airplane(self, on: bool, *, safe: Optional[bool] = None):
        """Toggle airplane mode: ``cmd connectivity`` (Android 11+), falling
        back to the settings key + state broadcast for older / locked-down
        builds."""
        st = "enable" if on else "disable"
        val = "1" if on else "0"

        def _do():
            r = self._logged_run(
                f"airplane {st}",
                self._shell_args("cmd", "connectivity", "airplane-mode", st),
                timeout=15,
            )
            if not self._output_failed(r):
                return "ok"
            # legacy fallback: set the global flag, then broadcast the change
            s = self._logged_run(
                f"airplane {st} (settings)",
                self._shell_args("settings", "put", "global", "airplane_mode_on", val),
                timeout=15,
            )
            if self._output_failed(s):
                return "airplane mode can't be toggled via adb on this device (permission-denied)"
            b = self._logged_run(
                f"airplane {st} (broadcast)",
                self._shell_args(
                    "am",
                    "broadcast",
                    "-a",
                    "android.intent.action.AIRPLANE_MODE",
                    "--ez",
                    "state",
                    "true" if on else "false",
                ),
                timeout=15,
            )
            if self._output_failed(b):
                # The flag alone doesn't switch the radios on modern builds, and
                # the broadcast is protected for the shell user since Android 7.
                return (
                    "airplane_mode_on setting written, but the change broadcast was "
                    "refused — the radios may not switch until the next reboot"
                )
            return "ok (settings fallback)"

        return self._guard("set_airplane", _do, safe=safe)

    def _open_tether_settings(self) -> bool:
        """Open the Hotspot/Tethering settings screen. Tries AOSP, OEM and
        Android-Automotive (car settings) components/actions in turn."""
        intents = [
            ["am", "start", "-a", "android.settings.TETHER_SETTINGS"],
            ["am", "start", "-n", "com.android.settings/.TetherSettings"],
            ["am", "start", "-n", "com.android.settings/.Settings\\$TetherSettingsActivity"],
            ["am", "start", "-n", "com.android.settings/.Settings\\$WifiTetherSettingsActivity"],
            # Android Automotive (car) settings
            ["am", "start", "-n", "com.android.car.settings/.wifi.WifiTetherActivity"],
            ["am", "start", "-n", "com.android.car.settings/.wifi.WifiSettingsActivity"],
            ["am", "start", "-a", "android.settings.WIRELESS_SETTINGS"],
            ["am", "start", "-a", "android.settings.SETTINGS"],  # last resort
        ]
        for it in intents:
            r = self._run(["shell"] + it, timeout=15, check=False)
            if not self._output_failed(r):
                return True
        return False

    def set_hotspot(self, on: bool, *, safe: Optional[bool] = None):
        """Toggle the Wi-Fi hotspot. No single adb command works across every
        Android version / OEM / automotive build, so we try the modern
        ``cmd wifi`` soft-AP commands and, if they aren't supported, fall back to
        opening the Hotspot/Tethering settings screen so it can still be toggled
        — making the button do something useful on every device."""

        def _do():
            if on:
                # random per-invocation password instead of a hardcoded one
                import secrets

                pw = "tb-" + secrets.token_hex(4)
                attempts = [
                    (
                        ["cmd", "wifi", "start-softap", "TurboADB", "wpa2", pw],
                        f"ok — SSID 'TurboADB', password: {pw}",
                    ),
                    (
                        ["cmd", "wifi", "start-softap", "TurboADB", "open"],
                        "ok — SSID 'TurboADB' (open network)",
                    ),
                    (["cmd", "connectivity", "start-tethering", "wifi"], "ok"),
                    (["cmd", "tethering", "start", "wifi"], "ok"),
                ]
            else:
                attempts = [
                    (["cmd", "wifi", "stop-softap"], "ok"),
                    (["cmd", "connectivity", "stop-tethering", "wifi"], "ok"),
                    (["cmd", "tethering", "stop", "wifi"], "ok"),
                ]
            msg = self._try_toggles(
                "hotspot " + ("on" if on else "off"), attempts, None, timeout=20
            )
            if msg is not None:
                return msg
            # nothing worked via adb (on locked-down/automotive builds the shell
            # user lacks the TETHER_PRIVILEGED permission) — open the settings UI
            if self._open_tether_settings():
                return (
                    "the hotspot can't be switched via adb on this device "
                    "(the shell user is permission-denied) - opened the "
                    "Hotspot/Tethering settings so you can toggle it there"
                )
            return "hotspot can't be controlled via adb on this device"

        return self._guard("set_hotspot", _do, safe=safe)

    def screen_on(self, *, safe: Optional[bool] = None):
        return self.keyevent("wake", safe=safe)

    def screen_off(self, *, safe: Optional[bool] = None):
        return self.keyevent("sleep", safe=safe)

    _MEDIA_KEYEVENTS = {
        "play": 126,  # KEYCODE_MEDIA_PLAY
        "pause": 127,  # KEYCODE_MEDIA_PAUSE
        "play-pause": 85,  # KEYCODE_MEDIA_PLAY_PAUSE
        "next": 87,  # KEYCODE_MEDIA_NEXT
        "previous": 88,  # KEYCODE_MEDIA_PREVIOUS
        "stop": 86,  # KEYCODE_MEDIA_STOP
        "fast-forward": 90,  # KEYCODE_MEDIA_FAST_FORWARD
        "rewind": 89,  # KEYCODE_MEDIA_REWIND
        "mute": 164,  # KEYCODE_VOLUME_MUTE
    }

    def media(self, action: str, *, safe: Optional[bool] = None):
        """Control media via the active session, falling back to media keyevents.
        *action*: play-pause | play | pause | next | previous | stop |
        fast-forward | rewind | mute."""
        def _do():
            r = self._logged_run(
                f"media {action}",
                self._shell_args("cmd", "media_session", "dispatch", action),
                timeout=15,
            )
            if not self._output_failed(r, extra=("can't find service",)):
                return True
            code = self._MEDIA_KEYEVENTS.get(action)
            if code is not None:
                return self.keyevent(code, safe=False)
            return False

        return self._guard("media", _do, safe=safe)

    def expand_notifications(self, *, safe: Optional[bool] = None):
        """Expand the notification shade (modern statusbar API with keyevent fallback)."""
        def _do():
            r = self._run(["shell", "cmd", "statusbar", "expand-notifications"], timeout=15)
            if r.ok:
                return True
            return self.keyevent(83, safe=False)  # legacy KEYCODE_NOTIFICATION

        return self._guard("expand_notifications", _do, safe=safe)

    def collapse_notifications(self, *, safe: Optional[bool] = None):
        """Collapse the notification shade."""
        def _do():
            r = self._run(["shell", "cmd", "statusbar", "collapse"], timeout=15)
            return r.ok

        return self._guard("collapse_notifications", _do, safe=safe)

    def get_brightness(self, *, safe: Optional[bool] = None):
        """The ``screen_brightness`` setting (0-255). Raises ADBError when the
        device doesn't report a number (no made-up default)."""

        def _do():
            r = self._run(["shell", "settings", "get", "system", "screen_brightness"], timeout=15)
            try:
                return int(r.text.strip())
            except ValueError:
                raise ADBError(
                    "could not read screen brightness: "
                    f"{self._combined_output(r) or f'exit {r.exit_code}, no output'}"
                ) from None

        return self._guard("get_brightness", _do, safe=safe)

    def set_brightness(self, value: int, *, safe: Optional[bool] = None):
        """Set screen brightness 0-255 (turns off auto-brightness first)."""

        def _do():
            v = max(0, min(255, int(value)))
            self._run(
                ["shell", "settings", "put", "system", "screen_brightness_mode", "0"],
                timeout=15,
                check=False,
            )
            self._run(
                ["shell", "settings", "put", "system", "screen_brightness", str(v)],
                timeout=15,
                check=True,
            )
            return v

        return self._guard("set_brightness", _do, safe=safe)

    def adjust_brightness(self, delta: int, *, safe: Optional[bool] = None):
        def _do():
            cur = self.get_brightness(safe=False)
            return self.set_brightness(cur + delta, safe=False)

        return self._guard("adjust_brightness", _do, safe=safe)

    def display_brightness(self, fraction: float, *, safe: Optional[bool] = None):
        """Set brightness as a fraction 0.0-1.0 via ``cmd display set-brightness``
        — applies to the live display immediately and is independent of the
        device's internal brightness range (the reliable, modern method)."""

        def _do():
            f = max(0.0, min(1.0, float(fraction)))
            return self._logged_run(
                f"brightness {int(f * 100)}%",
                ["shell", "cmd", "display", "set-brightness", f"{f:.3f}"],
                timeout=12,
            ).ok

        return self._guard("display_brightness", _do, safe=safe)

    # A URL/scheme hint -> the app packages that can handle it. On head units /
    # IVIs there's often no browser, so a VIEW intent fails; we then launch the
    # matching app directly if it's installed.
    _URL_APPS = {
        "youtube": [
            "com.google.android.youtube",
            "com.google.android.apps.youtube.music",
            "com.google.android.youtube.tv",
        ],
        "maps.google": ["com.google.android.apps.maps"],
        "google.com/maps": ["com.google.android.apps.maps"],
        "spotify": ["com.spotify.music", "com.spotify.tv.android"],
        "play.google": ["com.android.vending"],
        "market:": ["com.android.vending"],
    }

    # Named shortcuts open_url accepts (the Device controls' app tiles, the CLI).
    WEB_SHORTCUTS = {
        "browser": "https://www.google.com",
        "youtube": "https://www.youtube.com",
        "spotify": "https://open.spotify.com",
        "maps": "https://maps.google.com",
        "play-store": "https://play.google.com/store/apps",
    }

    # URI schemes whose "scheme:rest" form must never get an https:// prefix,
    # even when "rest" is all digits (tel:12345 is not host:port).
    _OPAQUE_URI_SCHEMES = frozenset(
        ("tel", "sms", "smsto", "mms", "mmsto", "geo", "mailto", "market", "intent",
         "content", "file", "about", "data", "javascript")
    )

    @classmethod
    def _normalise_url(cls, url: str) -> str:
        """Add ``https://`` only to scheme-less web addresses (``example.com``,
        ``localhost:8080``), never to URIs such as ``tel:``/``geo:``/``market:``."""
        url = str(url).strip()
        if "://" in url:
            return url
        m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):(.*)$", url, re.S)
        if m:
            port_like = re.match(r"^\d+(?:[/?#].*)?$", m.group(2), re.S) is not None
            if m.group(1).lower() in cls._OPAQUE_URI_SCHEMES or not port_like:
                return url
        return "https://" + url

    def open_url(self, url: str, *, safe: Optional[bool] = None):
        """Open a URL/URI with a VIEW intent — routes to the matching app
        (e.g. a youtube.com URL opens the YouTube app) or the browser. If that
        fails (common on IVIs with no browser), launch the matching app
        package directly. *url* may also be a :attr:`WEB_SHORTCUTS` name such
        as ``"youtube"`` or ``"maps"``."""

        def _do():
            target = self._normalise_url(self.WEB_SHORTCUTS.get(str(url).strip().lower(), url))
            r = self._logged_run(
                f"open {target}",
                self._shell_args("am", "start", "-a", "android.intent.action.VIEW", "-d", target),
                timeout=20,
            )
            if not self._output_failed(r):
                return True
            # VIEW failed — try launching the matching app directly
            try:
                installed = set(self.list_packages(safe=False))
            except Exception:
                installed = set()
            low = target.lower()
            for hint, pkgs in self._URL_APPS.items():
                if hint in low:
                    for pkg in pkgs:
                        if pkg in installed and self._launch_package(pkg):
                            return True
            return False

        return self._guard("open_url", _do, safe=safe)

    def web_search(self, query: str, *, safe: Optional[bool] = None):
        """Open a web search for *query* in the browser."""
        from urllib.parse import quote_plus

        return self.open_url("https://www.google.com/search?q=" + quote_plus(query), safe=safe)

    def open_settings(self, *, safe: Optional[bool] = None):
        """Open the system Settings app. ``am`` exits 0 while refusing, so the
        output decides (see :meth:`_output_failed`), not the exit code."""

        def _do():
            res = self._logged_run(
                "open settings",
                ["shell", "am", "start", "-a", "android.settings.SETTINGS"],
                timeout=15,
            )
            return not self._output_failed(res)

        return self._guard("open_settings", _do, safe=safe)

    # Known package names per app, so launching works on ANY OEM (Vivo/Samsung/
    # Xiaomi/Oppo/OnePlus/stock…) when the standard intent isn't honoured.
    _APP_PACKAGES = {
        "calculator": [
            "com.google.android.calculator",
            "com.android.calculator2",
            "com.sec.android.app.popupcalculator",
            "com.miui.calculator",
            "com.coloros.calculator",
            "com.oneplus.calculator",
            "com.vivo.calculator",
            "com.transsion.calculator",
        ],
        "gallery": [
            "com.google.android.apps.photos",
            "com.android.gallery3d",
            "com.sec.android.gallery3d",
            "com.miui.gallery",
            "com.coloros.gallery3d",
            "com.vivo.gallery",
            "com.oneplus.gallery",
        ],
        "camera": [
            "com.android.camera2",
            "com.android.camera",
            "com.sec.android.app.camera",
            "com.google.android.GoogleCamera",
            "com.oppo.camera",
            "com.vivo.camera",
            "com.oneplus.camera",
        ],
    }

    def _launch_package(self, package: str) -> bool:
        """Launch an installed app as robustly as possible across phones AND
        Android Automotive head units. Strategy, in order:

        1. Resolve the package's LAUNCHER activity and start it by explicit
           component (``am start -n pkg/Activity``) — works where ``monkey`` is
           blocked, which is common on automotive/IVI.
        1b. Resolve WITHOUT the LAUNCHER category — many in-house / system apps
            on IVI builds have a main activity that isn't launcher-categorised,
            which is why "nothing responded" when starting them.
        2. ``monkey -p pkg`` (the usual launcher shortcut).
        3. A bare MAIN/LAUNCHER intent.
        4. The package's first exported MAIN activity from
           ``cmd package query-activities`` — the last resort that reaches
           system apps with no launcher entry at all.
        """

        def _components(text):
            """``pkg/Activity`` lines for this package, in output order."""
            prefix = package + "/"
            lines = (ln.strip() for ln in (text or "").splitlines())
            return [ln for ln in lines if ln.startswith(prefix) and " " not in ln]

        def _start_component(comp):
            a = self._logged_run(
                f"start {comp}", self._shell_args("am", "start", "-n", comp), timeout=20
            )
            return not self._output_failed(a)

        self._emit(logging.INFO, f"launching {package}…")
        # 1) resolve the launcher activity, then start that component
        # 1b) same, without the LAUNCHER category (IVI/system apps often lack it)
        for category in (["-c", "android.intent.category.LAUNCHER"], []):
            try:
                r = self._run(
                    self._shell_args("cmd", "package", "resolve-activity", "--brief", *category, package),
                    timeout=15,
                    check=False,
                )
                comps = _components(r.text)
                if comps and _start_component(comps[-1]):
                    return True
            except ADBError:
                pass
        # 2) monkey
        m = self._logged_run(
            f"monkey {package}",
            self._shell_args("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"),
            timeout=20,
        )
        if m.ok and "No activities found" not in (m.stdout + m.stderr):
            return True
        # 3) bare MAIN/LAUNCHER intent scoped to the package
        b = self._logged_run(
            f"intent {package}",
            self._shell_args(
                "am", "start", "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER", package,
            ),
            timeout=20,
        )
        if not self._output_failed(b):
            return True
        # 4) any MAIN activity the package declares (query-activities) — reaches
        # system/in-house IVI apps that have no launcher entry at all
        try:
            q = self._run(
                [
                    "shell",
                    "cmd package query-activities --brief -a "
                    "android.intent.action.MAIN 2>/dev/null | "
                    f"grep -F {shlex.quote(package + '/')} | head -n 3",
                ],
                timeout=20,
                check=False,
            )
            for comp in _components(q.text):
                if _start_component(comp):
                    return True
        except ADBError:
            pass
        self._emit(
            logging.WARNING,
            f"could not launch {package} "
            "(no startable activity, or launching it is blocked on this "
            "device)",
        )
        return False

    def _open_app(self, label, intent_args, fallback_key, *, safe):
        """Try a standard intent; if it doesn't resolve, launch the first
        installed known package for this app type; finally, fall back to any
        installed package whose name hints the type — so it works on any device,
        including automotive head units with OEM-specific package names."""

        def _do():
            r = self._run(["shell", "am", "start"] + intent_args, timeout=15, check=False)
            # No "not started" marker here: AOSP prints "Activity not started,
            # its current task has been brought to the front" on SUCCESS. The
            # real refusals are already covered by _FAILURE_MARKERS.
            if not self._output_failed(r):
                return True
            try:
                installed = set(self.list_packages(safe=False))
            except Exception:
                installed = set()
            known = self._APP_PACKAGES.get(fallback_key, [])
            for pkg in known:
                if pkg in installed and self._launch_package(pkg):
                    return True
            # last resort: installed packages whose name hints the app type.
            # Each launch attempt costs several adb round-trips, so try only the
            # best few (hint in the last name segment first) — not every match.
            hinted = [p for p in installed if fallback_key in p.lower() and p not in known]
            hinted.sort(key=lambda p: (fallback_key not in p.rsplit(".", 1)[-1].lower(), p))
            for pkg in hinted[:3]:
                if self._launch_package(pkg):
                    return True
            self._emit(
                logging.WARNING,
                f"no {fallback_key} app is installed on this device "
                f"(checked {len(installed)} packages) — nothing to open",
            )
            return False

        return self._guard(label, _do, safe=safe)

    def open_camera(self, *, safe: Optional[bool] = None):
        return self._open_app(
            "open_camera", ["-a", "android.media.action.STILL_IMAGE_CAMERA"], "camera", safe=safe
        )

    def open_gallery(self, *, safe: Optional[bool] = None):
        return self._open_app(
            "open_gallery",
            ["-a", "android.intent.action.MAIN", "-c", "android.intent.category.APP_GALLERY"],
            "gallery",
            safe=safe,
        )

    def open_calculator(self, *, safe: Optional[bool] = None):
        return self._open_app(
            "open_calculator",
            ["-a", "android.intent.action.MAIN", "-c", "android.intent.category.APP_CALCULATOR"],
            "calculator",
            safe=safe,
        )

    def close_apps(self, *, safe: Optional[bool] = None):
        """Actually close apps: ``am kill-all`` (background) **and** force-stop
        every third-party app (so they really close, not just cache-trim)."""

        def _do():
            cmd = (
                "am kill-all >/dev/null 2>&1; "
                "n=0; for p in $(pm list packages -3 | tr -d '\\r' | cut -d: -f2); do "
                'am force-stop "$p"; n=$((n+1)); done; echo "closed $n apps"'
            )
            r = self._run(["shell", cmd], timeout=120, check=False)
            return r.text.strip() or "closed apps"

        return self._guard("close_apps", _do, safe=safe)

    # The one list of build/identity properties. build_report shows every
    # group; build_info shows the concise subset (identity + build groups minus
    # the low-level identifiers in _BUILD_INFO_OMIT).
    _BUILD_KEY_GROUPS = (
        ("Device identity", (
            "ro.product.manufacturer", "ro.product.brand", "ro.product.model",
            "ro.product.name", "ro.product.device", "ro.serialno",
            "ro.boot.serialno", "ro.product.cpu.abi", "ro.product.cpu.abilist",
            "ro.board.platform", "ro.hardware", "ro.build.characteristics",
        )),
        ("Android build", (
            "ro.build.version.release", "ro.build.version.sdk",
            "ro.build.version.incremental", "ro.build.version.security_patch",
            "ro.build.id", "ro.build.display.id", "ro.build.type",
            "ro.build.tags", "ro.build.date", "ro.build.version.base_os",
            "ro.build.fingerprint",
        )),
        ("Boot and radio", (
            "ro.boot.bootloader", "ro.boot.hardware", "ro.boot.verifiedbootstate",
            "ro.boot.vbmeta.device_state", "gsm.version.baseband",
        )),
    )
    _BUILD_INFO_OMIT = frozenset((
        "ro.serialno", "ro.boot.serialno", "ro.product.cpu.abilist", "ro.board.platform",
        "ro.hardware", "ro.build.version.incremental", "ro.build.id", "ro.build.tags",
        "ro.build.version.base_os",
    ))

    def build_info(self, *, safe: Optional[bool] = None):
        """A readable block of the key build/identity properties — the printed
        form of :meth:`build_properties` (one query, one key list)."""

        def _do():
            props = self.build_properties(safe=False)  # raw dict even when safe
            return "\n".join(f"{key:32} {value}" for key, value in props.items())

        return self._guard("build_info", _do, safe=safe)

    def build_properties(self, *, safe: Optional[bool] = None):
        """The :meth:`build_info` properties as a ``{name: value}`` dict, in the
        order :attr:`_BUILD_KEY_GROUPS` lists them."""

        def _do():
            p = self.getprop(safe=False)
            return {
                k: p.get(k, "")
                for _heading, group in self._BUILD_KEY_GROUPS[:2]
                for k in group
                if k not in self._BUILD_INFO_OMIT
            }

        return self._guard("build_properties", _do, safe=safe)

    def build_report(self, *, safe: Optional[bool] = None):
        """Detailed build identity followed by the complete ``getprop`` dump.

        This deliberately keeps :meth:`build_info` concise for CLI callers,
        while the GUI can present/export a support-ready report without asking
        the device a second time for the same properties.
        """

        key_groups = dict(self._BUILD_KEY_GROUPS)

        def _do():
            props = self.getprop(safe=False)
            kernel = self._run(["shell", "uname", "-a"], timeout=15, check=False).text.strip()
            lines = [
                "TurboADB build report",
                f"Captured: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"ADB serial: {self._serial or props.get('ro.serialno') or 'unknown'}",
            ]
            for heading, keys in key_groups.items():
                lines.extend(("", heading, "-" * len(heading)))
                lines.extend(f"{key:34} {props.get(key, '')}" for key in keys)
            lines.extend(("", "Kernel", "------", kernel or "unavailable"))
            lines.extend(("", "--- RAW DIAGNOSTICS ---", "", "Complete Android property dump (getprop)", "-------------------------------------------"))
            lines.extend(f"[{key}]: [{props[key]}]" for key in sorted(props))
            return "\n".join(lines)

        return self._guard("build_report", _do, safe=safe)

    def battery(self, *, safe: Optional[bool] = None):
        """Battery status (``dumpsys battery``) as the device prints it.

        check=True: a failed call used to hand callers a blank report."""
        return self._guard(
            "battery",
            lambda: self._run(["shell", "dumpsys", "battery"], timeout=20, check=True).text,
            safe=safe,
        )

    @staticmethod
    def _parse_battery(text: str) -> dict:
        info = {}
        for line in (text or "").splitlines():
            key, sep, value = line.strip().partition(":")
            key, value = key.strip(), value.strip()
            if not sep or not key or not value:
                continue
            if re.fullmatch(r"-?\d+", value):
                value = int(value)
            elif value in ("true", "false"):
                value = value == "true"
            info[key.replace(" ", "_")] = value
        return info

    def battery_status(self, *, safe: Optional[bool] = None):
        """``dumpsys battery`` as a dict (``level``, ``temperature``, ``status`` …;
        numbers become ints and true/false become booleans).

        The parsed view of :meth:`battery` — one query, one place to change."""
        return self._guard(
            "battery_status",
            lambda: self._parse_battery(self.battery(safe=False)),
            safe=safe,
        )

    @staticmethod
    def _parse_health(batt: str, meminfo: str, top: str, uptime: str) -> dict:
        """Parse the raw command output into the health snapshot dict — split
        out so it's unit-testable without a device."""
        info = {
            "battery_level": None,
            "battery_temp_c": None,
            "battery_status": None,
            "mem_total_kb": None,
            "mem_available_kb": None,
            "cpu": None,
            "uptime": None,
        }
        m = re.search(r"^\s*level:\s*(\d+)", batt, re.M)
        if m:
            info["battery_level"] = int(m.group(1))
        m = re.search(r"^\s*temperature:\s*(\d+)", batt, re.M)
        if m:
            info["battery_temp_c"] = int(m.group(1)) / 10.0
        m = re.search(r"^\s*status:\s*(\d+)", batt, re.M)
        if m:
            info["battery_status"] = {
                1: "unknown",
                2: "charging",
                3: "discharging",
                4: "not charging",
                5: "full",
            }.get(int(m.group(1)))
        m = re.search(r"^MemTotal:\s*(\d+)\s*kB", meminfo, re.M)
        if m:
            info["mem_total_kb"] = int(m.group(1))
        m = re.search(r"^MemAvailable:\s*(\d+)\s*kB", meminfo, re.M)
        if m:
            info["mem_available_kb"] = int(m.group(1))
        # toybox top -bn1 header: "400%cpu  12%user ... 380%idle"
        m = re.search(r"(\d+)%cpu.*?(\d+)%idle", top)
        if m:
            total, idle = int(m.group(1)), int(m.group(2))
            if total > 0:
                info["cpu"] = f"{max(0, total - idle) * 100 // total}%"
        up = uptime.strip().splitlines()
        if up:
            info["uptime"] = up[0].strip()
        return info

    def _health_raw(self) -> tuple:
        """(battery, meminfo, top, uptime) text — the shared health queries."""

        def out(args, timeout):
            return self._run(["shell"] + args, timeout=timeout, check=False).text or ""

        return (
            out(["dumpsys", "battery"], 20),
            out(["cat", "/proc/meminfo"], 15),
            out(["top", "-bn1"], 20),
            out(["uptime"], 15),
        )

    @staticmethod
    def _memory_summary(info: dict) -> str:
        mt, ma = info.get("mem_total_kb"), info.get("mem_available_kb")
        return f"{ma // 1024} MB free of {mt // 1024} MB" if mt and ma is not None else "n/a"

    def health(self, *, safe: Optional[bool] = None):
        """A one-shot device health snapshot: battery level / temperature /
        status, memory total + available, rough CPU load, and uptime. All
        best-effort — a field a device doesn't report is simply ``None``."""
        return self._guard("health", lambda: self._parse_health(*self._health_raw()), safe=safe)

    def health_report(self, *, safe: Optional[bool] = None):
        """A detailed health summary plus raw dumps suitable for support export."""

        def _do():
            batt, mem, top, uptime = self._health_raw()
            info = self._parse_health(batt, mem, top, uptime)
            memory = self._memory_summary(info)
            lines = [
                "TurboADB health report",
                f"Captured: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"ADB serial: {self._serial or 'unknown'}",
                "",
                "Summary",
                "-------",
                f"Battery: {info.get('battery_level') if info.get('battery_level') is not None else 'n/a'}%"
                + (f" ({info['battery_status']})" if info.get("battery_status") else ""),
                f"Temperature: {info.get('battery_temp_c') if info.get('battery_temp_c') is not None else 'n/a'} °C",
                f"Memory: {memory}",
                f"CPU load: {info.get('cpu') or 'n/a'}",
                f"Uptime: {info.get('uptime') or 'n/a'}",
                "",
                "--- RAW DIAGNOSTICS ---",
                "",
                "Raw dumpsys battery",
                "-------------------",
                batt.strip() or "unavailable",
                "",
                "Raw /proc/meminfo",
                "-----------------",
                mem.strip() or "unavailable",
                "",
                "Raw top -bn1",
                "------------",
                top.strip() or "unavailable",
                "",
                "Raw uptime",
                "----------",
                uptime.strip() or "unavailable",
            ]
            return "\n".join(lines)

        return self._guard("health_report", _do, safe=safe)

    def health_text(self, *, safe: Optional[bool] = None):
        """The health snapshot as a readable block (for the GUI/CLI)."""

        def _do():
            h = self.health(safe=False)
            mem = self._memory_summary(h)
            batt_lvl = h.get("battery_level")
            batt_str = f"battery   {batt_lvl}%" if batt_lvl is not None else "battery   n/a"
            lines = [
                batt_str + (f"  ({h['battery_status']})" if h.get("battery_status") else ""),
                f"temp      {h['battery_temp_c']} °C"
                if h.get("battery_temp_c") is not None
                else "temp      n/a",
                f"memory    {mem}",
                f"cpu       {h.get('cpu') or 'n/a'}",
                f"uptime    {h.get('uptime') or 'n/a'}",
            ]
            return "\n".join(lines)

        return self._guard("health_text", _do, safe=safe)

    def bugreport(self, local_path: str, *, timeout: float = 900, safe: Optional[bool] = None):
        """Capture a full ``adb bugreport`` to *local_path* (a .zip on modern
        devices). Slow — a couple of minutes is normal; *timeout* is generous."""

        def _do():
            lp = os.path.expanduser(local_path)
            parent = os.path.dirname(lp)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._emit(logging.INFO, f"Capturing bugreport → {lp} (takes a few minutes)…")
            res = self._run(["bugreport", lp], timeout=timeout, check=False)
            if not res.ok or not os.path.exists(lp):
                raise ADBError(f"bugreport failed: {self._combined_output(res) or 'no output'}")
            self._emit(logging.INFO, f"Bugreport saved: {lp} ({os.path.getsize(lp)} bytes)")
            return lp

        return self._guard("bugreport", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Telephony / messaging — dialler, calls, call log, SMS
    # ------------------------------------------------------------------ #
    @staticmethod
    def _phone_uri(scheme: str, number: str) -> str:
        """``tel:``/``sms:`` URI for *number* (digits, + * # , ; kept). ``#``
        starts a URI fragment, so it must be percent-encoded — unencoded,
        ``dial("*#06#")`` reached the dialler as just ``*``."""
        from urllib.parse import quote

        clean = "".join(c for c in str(number) if c in "+*#0123456789,;")
        return f"{scheme}:{quote(clean, safe='+*,;')}"

    _PHONE_INTENTS = {
        "android.intent.action.DIAL": "the dialler",
        "android.intent.action.CALL": "phone calls",
        "android.intent.action.SENDTO": "text messages",
    }

    def _phone_intent(self, action: str, scheme: str, number: str, *extra) -> bool:
        uri = self._phone_uri(scheme, number)
        res = self._run(self._shell_args("am", "start", "-a", action, "-d", uri, *extra), timeout=15)
        output = self._combined_output(res)
        if "unable to resolve intent" in output.lower() or "no activity found" in output.lower():
            # Customised head units often have no standard dialler / messaging app.
            what = self._PHONE_INTENTS.get(action, action)
            raise ADBError(
                f"No app on this device handles {what} ({action}). Customised head "
                "units often have their own phone app instead — see phone_support()."
            )
        if not res.ok or re.search(r"(?m)^Error:", output):
            raise self._command_error(f"shell am start -a {action} -d {uri}", res)
        return True

    def dial(self, number: str, *, safe: Optional[bool] = None):
        """Open the dialler pre-filled with *number* (doesn't place the call)."""
        return self._guard(
            "dial",
            lambda: self._phone_intent("android.intent.action.DIAL", "tel", number),
            safe=safe,
        )

    def call(self, number: str, *, safe: Optional[bool] = None):
        """Place a call to *number* (needs CALL_PHONE; works from adb shell on
        most devices)."""
        return self._guard(
            "call",
            lambda: self._phone_intent("android.intent.action.CALL", "tel", number),
            safe=safe,
        )

    def end_call(self, *, safe: Optional[bool] = None):
        return self.keyevent(6, safe=safe)  # KEYCODE_ENDCALL

    def answer_call(self, *, safe: Optional[bool] = None):
        return self.keyevent(5, safe=safe)  # KEYCODE_CALL

    def call_state(self, *, safe: Optional[bool] = None):
        def _do():
            r = self._run(["shell", "dumpsys", "telephony.registry"], timeout=15)
            m = re.search(r"mCallState=(\d+)", r.text)
            return {0: "idle", 1: "ringing", 2: "in call"}.get(
                int(m.group(1)) if m else -1, "unknown"
            )

        return self._guard("call_state", _do, safe=safe)

    _PROVIDER_ERROR_RE = re.compile(
        r"Error while accessing provider|SecurityException|Permission Denial|IllegalArgumentException"
    )

    # The foreground user is asked again after this many seconds, so a driver
    # switch on a car is noticed without an extra round-trip before every query.
    _FOREGROUND_USER_TTL = 30.0
    # Only a hint for the query: a slow answer must not hold up the call log.
    _FOREGROUND_USER_TIMEOUT = 4.0
    _foreground_user_cache = None  # (serial, monotonic time, user id or None)

    @staticmethod
    def parse_current_user(text) -> Optional[int]:
        """The user id that ``am get-current-user`` printed, or None for
        anything else (an old Android's "Unknown command", usage text, an error)."""
        m = re.match(r"\s*(\d+)\s*\Z", str(text or ""))
        return int(m.group(1)) if m else None

    def _content_user(self) -> Optional[int]:
        """The ``--user`` for ``content query``, or None to keep the default.

        ``content query`` reads user 0 unless told otherwise. On Android
        Automotive user 0 is the headless system user and the driver is a
        secondary user (usually 10), so the call log and SMS must be read as
        the foreground user. A normal phone answers 0, or can't answer at all,
        and keeps the plain command.

        Never raises: a lookup that fails or times out means "no ``--user``",
        and that answer is cached like any other, so a slow device doesn't
        wait again for every query."""
        cached = self._foreground_user_cache
        now = time.monotonic()
        if cached and cached[0] == self._serial and now - cached[1] < self._FOREGROUND_USER_TTL:
            return cached[2]
        try:
            res = self._run(
                ["shell", "am", "get-current-user"], timeout=self._FOREGROUND_USER_TIMEOUT
            )
        except ADBError as exc:
            self._emit(logging.DEBUG, f"foreground user unknown, querying the default user: {exc}")
            user = None
        else:
            user = (self.parse_current_user(res.text) if res.ok else None) or None  # 0 = default
        self._foreground_user_cache = (self._serial, now, user)
        return user

    def _query_rows(self, uri, fields, limit, free_last=False):
        """Run ``content query`` and parse its ``Row: N k=v, ...`` records.

        Rows are counted as RECORDS (a multi-line SMS body is one row, not
        several lines to ``head``), and provider errors such as a
        SecurityException are raised instead of being hidden as "no rows".
        The query runs as the foreground user (see :meth:`_content_user`)."""
        limit = int(limit)
        user = self._content_user()
        r = self._run(
            self._shell_args(
                "content", "query", "--uri", uri,
                *(("--user", user) if user is not None else ()),
                "--projection", ":".join(fields), "--sort", "date DESC",
            ),
            timeout=60,
            check=False,
        )
        if not r.ok or self._PROVIDER_ERROR_RE.search(self._combined_output(r)):
            if user is not None:
                self._foreground_user_cache = None  # the user may be gone: ask again next time
            raise self._command_error(f"shell content query --uri {uri}", r)
        body_re = None
        if free_last:
            body_re = re.compile(
                ", ".join(f"{re.escape(f)}=(.*?)" for f in fields[:-1])
                + f", {re.escape(fields[-1])}=(.*)\\Z",
                re.S,
            )
        rows = []
        for record in _iter_records(r.stdout or ""):
            if len(rows) >= limit:
                break
            body = record.rstrip("\r\n")
            d = {}
            if body_re is not None:
                m = body_re.match(body)
                if m:
                    d = dict(zip(fields, m.groups()))
            else:
                for kv in body.strip().split(", "):
                    if "=" in kv:
                        k, _, v = kv.partition("=")
                        d[k.strip()] = v
            if d:
                rows.append(d)
        return rows

    def call_log(self, limit: int = 50, *, safe: Optional[bool] = None):
        """Recent calls: list of {type, date, duration, number}. type 1=in 2=out
        3=missed."""
        return self._guard(
            "call_log",
            lambda: self._query_rows(
                "content://call_log/calls", ["type", "date", "duration", "number"], limit
            ),
            safe=safe,
        )

    def sms_list(self, limit: int = 50, *, safe: Optional[bool] = None):
        """Recent SMS: list of {type, date, address, body}. type 1=inbox 2=sent."""
        return self._guard(
            "sms_list",
            lambda: self._query_rows(
                "content://sms", ["type", "date", "address", "body"], limit, free_last=True
            ),
            safe=safe,
        )

    def send_sms(self, number: str, body: str, *, safe: Optional[bool] = None):
        """Open the messaging app composing to *number* pre-filled with *body*
        (sending directly needs the default-SMS app or root, so we open it)."""
        return self._guard(
            "send_sms",
            lambda: self._phone_intent(
                "android.intent.action.SENDTO", "sms", number, "--es", "sms_body", body
            ),
            safe=safe,
        )

    _PHONE_APP_WORDS = ("dialer", "dialler", "phone", "telecom", "hfp", "handsfree", "call")

    @classmethod
    def _looks_like_phone_app(cls, component: str) -> bool:
        """True for ``com.oem.btphone/.HandsfreeActivity`` or ``…/com.android.dialer.X``,
        not for apps that merely contain "phone" (``com.phonepe.app``, a phonebook)."""
        for part in re.split(r"[./$_]", component.lower()):
            if part.endswith("activity"):
                part = part[: -len("activity")]
            if part.startswith("incall") or any(
                part == word or part.endswith(word) for word in cls._PHONE_APP_WORDS
            ):
                return True
        return False

    @staticmethod
    def _phone_sections(text: str) -> dict:
        """``{name: [lines]}`` of the ``@@name`` sections that :meth:`phone_support`
        prints (lines stripped, blank ones dropped)."""
        sections = {}
        current = None
        for raw in (text or "").splitlines():
            line = raw.strip()
            if line.startswith("@@"):
                current = line[2:]
                sections[current] = []
            elif current is not None and line:
                sections[current].append(line)
        return sections

    @classmethod
    def parse_phone_support(cls, text: str) -> dict:
        """The telephony and phone-app part of :meth:`phone_support` from its
        shell output."""
        sections = cls._phone_sections(text)

        def handler(key):
            lines = sections.get(key)
            if not lines:
                return None
            if any("no activity found" in line.lower() for line in lines):
                return ""
            components = [line for line in lines if "/" in line and " " not in line]
            return components[-1] if components else None  # e.g. an old Android's "Unknown command"

        features = sections.get("features") or []
        telephony = None
        if any(line.startswith("feature:") for line in features):
            telephony = any(line.startswith("feature:android.hardware.telephony") for line in features)
        apps = []
        for line in sections.get("apps") or []:
            if "/" in line and " " not in line and cls._looks_like_phone_app(line):
                package = line.split("/", 1)[0]
                if package not in apps:
                    apps.append(package)
        return {
            "telephony": telephony,
            "dialer": handler("dial"),
            "caller": handler("call"),
            "messages": handler("sms"),
            "phone_apps": apps,
        }

    def phone_support(self, *, safe: Optional[bool] = None):
        """What the device offers for calls and messages — found without calling anyone.

        Returns ``{"telephony", "dialer", "caller", "messages", "phone_apps",
        "kind", "automotive", "phone_connected"}``: whether it has the telephony
        feature (a SIM radio); the activity that handles the dialler (``DIAL``),
        placing calls (``CALL``) and composing SMS (``SENDTO``), each ``""`` when
        no app does and ``None`` when the device can't say; launchable apps that
        look like phone apps, such as a customised head unit's own Bluetooth
        phone app; the device kind (see :meth:`device_kind`) and whether it is a
        car head unit; and, on a car, whether a phone is connected over
        Bluetooth to call through (:meth:`phone_connected`). ``phone_connected``
        is ``None`` on any other device and when the car can't tell.
        """
        script = "; ".join(
            (
                "echo @@features",
                "pm list features 2>/dev/null",
                "echo @@dial",
                "cmd package resolve-activity --brief -a android.intent.action.DIAL -d tel:1 2>&1",
                "echo @@call",
                "cmd package resolve-activity --brief -a android.intent.action.CALL -d tel:1 2>&1",
                "echo @@sms",
                "cmd package resolve-activity --brief -a android.intent.action.SENDTO -d smsto:1 2>&1",
                "echo @@apps",
                "cmd package query-activities --brief -a android.intent.action.MAIN "
                "-c android.intent.category.LAUNCHER 2>&1",
                "echo @@kind",
                "echo __turboadb_ch__; getprop ro.build.characteristics; "
                "echo __turboadb_wm__; wm size 2>/dev/null",
            )
        )

        def _do():
            text = self._run(["shell", script], timeout=30).text
            info = self.parse_phone_support(text)
            sections = self._phone_sections(text)
            kind = self.classify_device(
                "\n".join((sections.get("features") or []) + (sections.get("kind") or []))
            )
            info.update(kind=kind["kind"], automotive=kind["automotive"], phone_connected=None)
            if kind["automotive"]:
                try:
                    info["phone_connected"] = self.phone_connected(safe=False)
                except ADBError as exc:  # the rest of the answer still stands
                    self._emit(logging.DEBUG, f"phone connection unknown: {exc}")
            return info

        return self._guard("phone_support", _do, safe=safe)

    # HeadsetClientStateMachine states in which the phone link is up.
    _HFP_CONNECTED_STATES = ("Connected", "AudioOn")

    @classmethod
    def parse_phone_connection(cls, telecom: str = "", bluetooth: str = "") -> Optional[bool]:
        """:meth:`phone_connected` from ``dumpsys telecom`` and, optionally,
        ``dumpsys bluetooth_manager`` output.

        * True: Telecom lists an enabled PhoneAccount of the Bluetooth HFP
          client (``…hfpclient…ConnectionService``), or the HFP client profile
          (``Profile: HeadsetClientService``) has a device ``Connected`` or
          ``AudioOn``.
        * False: that profile runs with no phone connected, or the HFP account
          is registered but disabled.
        * None: neither dump says — it is unreadable, Android's Bluetooth is
          off (a head unit may pair phones through its own Bluetooth module,
          which these dumps never show), or it links phones some other way.
        """
        telecom = telecom or ""
        hfp_accounts = []  # enabled flag of each HFP client PhoneAccount
        for line in telecom.splitlines():
            if "PhoneAccount:" not in line:
                continue
            low = line.lower()
            if "hfpclient" in low:
                # "[[X] PhoneAccount: …" is enabled, "[[ ] PhoneAccount: …" is not.
                mark = re.search(r"\[\[(.)\]\s*PhoneAccount:", line)
                hfp_accounts.append(mark is None or mark.group(1) != " ")
        if any(hfp_accounts):
            return True

        # The HFP client profile's block: its indented lines up to the next
        # profile or top-level block. The state machine prints
        # "name=HeadsetClientStateMachine state=Connected" / "curState=AudioOn".
        section = None
        for raw in (bluetooth or "").splitlines():
            line = raw.strip()
            if line.startswith("Profile:"):
                if section is not None:
                    break
                if line[len("Profile:"):].strip() == "HeadsetClientService":
                    section = []
                continue
            if section is None:
                continue
            if line and not raw[:1].isspace() and not line.startswith(("HeadsetClient", "curState=")):
                break
            section.append(line)
        if section is not None:
            states = re.findall(r"(?<!\w)(?:state|curState)=(\w+)", "\n".join(section))
            return any(state in cls._HFP_CONNECTED_STATES for state in states)
        if hfp_accounts:
            return False
        return None

    def phone_connected(self, *, safe: Optional[bool] = None):
        """Whether a phone is connected to this car head unit to call through:
        True, False, or None when the device can't tell.

        A car places calls through a phone paired over Bluetooth, whose HFP
        client registers an enabled PhoneAccount with Telecom while the phone
        is connected, so ``dumpsys telecom`` usually answers on its own.
        ``dumpsys bluetooth_manager`` (the HFP client's connection state, or
        Bluetooth being off) is read only when it doesn't. Meant for car head
        units: :meth:`phone_support` asks it only there. See
        :meth:`parse_phone_connection` for the rules.
        """

        def _do():
            telecom = self._run(["shell", "dumpsys", "telecom"], timeout=20).text
            if self.parse_phone_connection(telecom):
                return True
            bluetooth = self._run(["shell", "dumpsys", "bluetooth_manager"], timeout=20).text
            return self.parse_phone_connection(telecom, bluetooth)

        return self._guard("phone_connected", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Properties / device info
    # ------------------------------------------------------------------ #
    def getprop(self, name: Optional[str] = None, *, safe: Optional[bool] = None):
        """Return one property's value, or a ``{name: value}`` dict of all of
        them when *name* is omitted."""

        def _do():
            if name:
                return self._run(self._shell_args("getprop", name), timeout=15, check=True).text
            # check=True: a failed call used to return {} silently, so every
            # caller (build_info, device_info, the GUI report) showed blanks
            # instead of the error.
            res = self._run(["shell", "getprop"], timeout=20, check=True)
            props = {}
            for line in res.stdout.splitlines():
                m = re.match(r"\[(.+?)\]:\s*\[(.*)\]", line.strip())
                if m:
                    props[m.group(1)] = m.group(2)
            return props

        return self._guard("getprop", _do, safe=safe)

    def device_info(self, *, safe: Optional[bool] = None):
        """A tidy dict of the most useful identity/build properties, plus an
        ``automotive`` flag (Android Automotive OS / IVI head-unit detection)."""

        def _do():
            p = self.getprop(safe=False)  # always the raw dict, even in safe mode
            info = {
                "serial": self._serial or p.get("ro.serialno", ""),
                "model": p.get("ro.product.model", ""),
                "brand": p.get("ro.product.brand", ""),
                "name": p.get("ro.product.name", ""),
                "device": p.get("ro.product.device", ""),
                "manufacturer": p.get("ro.product.manufacturer", ""),
                "android_version": p.get("ro.build.version.release", ""),
                "sdk": p.get("ro.build.version.sdk", ""),
                "build_id": p.get("ro.build.display.id", ""),
                "abi": p.get("ro.product.cpu.abi", ""),
                "characteristics": p.get("ro.build.characteristics", ""),
                "automotive": "automotive" in p.get("ro.build.characteristics", ""),
            }
            try:
                kind = self.device_kind(safe=False)
            except Exception:  # identity is still useful without the classification
                kind = None
            if kind:
                self.merge_kind(info, kind)
            return info

        return self._guard("device_info", _do, safe=safe)

    @staticmethod
    def merge_kind(info: dict, kind: dict) -> dict:
        """Fold a :meth:`classify_device` result into a device-info dict.

        The engine and the GUI's quick-scan both build the same summary, and
        the same five keys were copied across in two places -- adding a field
        to classify_device() meant remembering both."""
        info.update(
            kind=kind["kind"],
            kind_label=kind["label"],
            kind_reason=kind["reason"],
            telephony=kind["telephony"],
            display_size=kind["display_size"],
        )
        info["automotive"] = bool(info["automotive"] or kind["automotive"])
        return info

    _QUICK_IDENTITY_KEYS = ("manufacturer", "model", "android_version", "sdk", "device", "abi")

    def quick_identity(self, timeout: float = 1.5) -> dict:
        """Manufacturer, model, Android version, SDK, device and CPU ABI in one
        short shell call, for a friendly first line right after connecting.

        Best effort and quiet: returns ``{}`` when the device doesn't answer
        within *timeout* seconds, and logs that only at DEBUG. It is an
        optional nicety, so a slow first reply must not look like an error.
        """
        script = "; ".join(
            f"getprop {prop}"
            for prop in (
                "ro.product.manufacturer",
                "ro.product.model",
                "ro.build.version.release",
                "ro.build.version.sdk",
                "ro.product.device",
                "ro.product.cpu.abi",
            )
        )
        try:
            res = self._run(["shell", script], timeout=timeout)
        except ADBError as exc:
            self._emit(logging.DEBUG, f"quick identity unavailable: {exc}")
            return {}
        # res.text strips the WHOLE output, so a device that doesn't have the
        # first property lost its empty line and shifted every field up by one
        # (the model was reported as the manufacturer, and so on). The raw
        # stdout keeps one line per getprop; a short tail is padded out.
        keys = self._QUICK_IDENTITY_KEYS
        lines = [line.strip() for line in (res.stdout or "").splitlines()]
        lines += [""] * (len(keys) - len(lines))
        if not res.ok or not (lines[0] or lines[1]):
            return {}
        return {key: lines[index] for index, key in enumerate(keys)}

    _KIND_SCRIPT = (
        "pm list features 2>/dev/null; echo __turboadb_ch__; "
        "getprop ro.build.characteristics; echo __turboadb_wm__; wm size 2>/dev/null"
    )

    def device_kind(self, *, safe: Optional[bool] = None):
        """Classify the target: what kind of Android device it is.

        Returns a dict with ``kind`` (``automotive``, ``headunit``, ``tv``,
        ``watch``, ``tablet`` or ``phone``), a readable ``label``, ``reason``
        (the evidence used), ``automotive`` (True for both car kinds),
        ``telephony`` and ``display_size`` (``"WxH"`` or ``""``).

        * ``automotive`` — Android Automotive OS: the
          ``android.hardware.type.automotive`` feature, or ``automotive`` in
          ``ro.build.characteristics``.
        * ``headunit`` — an infotainment unit running ordinary Android: no
          telephony feature and a natively landscape display.
        * ``tv`` / ``watch`` / ``tablet`` — the leanback or watch feature, or
          the ``tablet`` build characteristic.
        * ``phone`` — everything else.

        One shell round-trip reads the feature list, the build characteristics
        and ``wm size``.
        """

        def _do():
            res = self._run(["shell", self._KIND_SCRIPT], timeout=20, check=False)
            return self.classify_device(res.text)

        return self._guard("device_kind", _do, safe=safe)

    @staticmethod
    def classify_device(text: str) -> dict:
        """Pure classification of :meth:`device_kind`'s shell output."""
        features, characteristics, wm_lines = set(), "", []
        section = "features"
        for raw in (text or "").splitlines():
            line = raw.strip()
            if line == "__turboadb_ch__":
                section = "characteristics"
            elif line == "__turboadb_wm__":
                section = "wm"
            elif section == "features" and line.startswith("feature:"):
                features.add(line[len("feature:"):].split("=", 1)[0].strip())
            elif section == "characteristics" and line:
                characteristics = line.lower()
            elif section == "wm":
                wm_lines.append(line)
        wm = "\n".join(wm_lines)
        match = re.search(r"Override size:\s*(\d+)x(\d+)", wm) or re.search(
            r"Physical size:\s*(\d+)x(\d+)", wm
        )
        width, height = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
        size = f"{width}x{height}" if match else ""
        tags = {tag.strip() for tag in characteristics.split(",") if tag.strip()}
        telephony = any(
            name == "android.hardware.telephony" or name.startswith("android.hardware.telephony.")
            for name in features
        )
        if "android.hardware.type.automotive" in features or "automotive" in tags:
            kind, label = "automotive", "Android Automotive head unit"
            reason = (
                "feature android.hardware.type.automotive"
                if "android.hardware.type.automotive" in features
                else "ro.build.characteristics=automotive"
            )
        elif "android.software.leanback" in features or "tv" in tags:
            kind, label, reason = "tv", "Android TV", "leanback feature / tv characteristic"
        elif "android.hardware.type.watch" in features or "watch" in tags:
            kind, label, reason = "watch", "Wear OS watch", "watch feature / characteristic"
        elif "tablet" in tags:
            kind, label, reason = "tablet", "Tablet", "ro.build.characteristics=tablet"
        elif features and not telephony and width > height > 0:
            kind, label = "headunit", "Infotainment head unit"
            reason = f"no telephony feature and a landscape display ({size})"
        else:
            kind, label = "phone", "Phone"
            reason = "telephony feature" if telephony else "no automotive, TV or tablet markers"
        return {
            "kind": kind,
            "label": label,
            "reason": reason,
            "automotive": kind in ("automotive", "headunit"),
            "telephony": telephony,
            "display_size": size,
        }

    def is_automotive(self, *, safe: Optional[bool] = None):
        """True if the target is Android Automotive OS (has the automotive
        hardware feature). Useful before driving IVI-specific flows."""

        def _do():
            res = self._run(
                ["shell", "pm", "has-feature", "android.hardware.type.automotive"],
                timeout=15,
                check=False,
            )
            if res.text.strip().lower() in ("true", "false"):
                return res.text.strip().lower() == "true"
            # fall back to build characteristics
            ch = self._run(["shell", "getprop", "ro.build.characteristics"], timeout=15).text
            return "automotive" in ch

        return self._guard("is_automotive", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Shell — one-shot and interactive
    # ------------------------------------------------------------------ #
    def shell(
        self,
        command: str,
        *,
        timeout: Optional[float] = None,
        check: bool = False,
        su: bool = False,
        safe: Optional[bool] = None,
    ):
        """Run a one-shot ``adb shell`` command. Returns CommandResult.

        *su=True* wraps the command in ``su -c`` for rooted devices.
        """

        def _do():
            cmd = f"su -c {shlex.quote(command)}" if su else command
            return self._logged_run("shell", ["shell", cmd], timeout=timeout, check=check)

        return self._guard("shell", _do, safe=safe)

    def shell_many(
        self,
        commands: Sequence[str],
        *,
        stop_on_error: bool = True,
        timeout: Optional[float] = None,
        su: bool = False,
        check: bool = False,
        safe: Optional[bool] = None,
        **kwargs,
    ):
        """Run several one-shot :meth:`shell` commands in order and return the
        list of CommandResults (stopping at the first failure unless
        *stop_on_error* is False). *timeout*, *su* and *check* apply to every
        command; unknown keyword arguments raise TypeError."""

        def _do():
            if kwargs:
                raise TypeError(
                    "shell_many() got unexpected keyword argument(s): " + ", ".join(sorted(kwargs))
                )
            results = []
            for cmd in commands:
                res = self.shell(cmd, timeout=timeout, su=su, check=check, safe=False)
                results.append(res)
                if stop_on_error and not res.ok:
                    self._emit(logging.WARNING, f"Stopping batch: {cmd!r} exited {res.exit_code}.")
                    break
            return results

        return self._guard("shell_many", _do, safe=safe)

    def open_shell(self, *, tty: bool = True, safe: Optional[bool] = None):
        """Open a persistent interactive :class:`ShellSession` (``adb shell``).

        *tty=True* forces a device-side pseudo-terminal (``adb shell -t -t``), so
        you get a real interactive shell — prompt, character echo, line editing,
        and full-screen apps — exactly like a native console. Set it False for a
        raw, non-echoing pipe (e.g. when scripting a send/expect flow yourself).
        """

        def _do():
            args = ["shell"]
            if tty:
                args = ["shell", "-t", "-t"]  # force a PTY even over pipes
            proc = subprocess.Popen(
                self._base(target=True) + args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=NO_WINDOW,
            )
            return ShellSession(proc, encoding=self.config.encoding)

        return self._guard("open_shell", _do, safe=safe)

    def interactive_shell(self) -> int:
        """Run ``adb shell`` attached to this console (stdin, stdout and stderr
        inherited) until the user leaves it, and return its exit code — the
        interactive ``turboadb shell`` with no command."""
        return subprocess.call(self._base(target=True) + ["shell"])

    def wait_for_boot(self, timeout: float = 180, *, safe: Optional[bool] = None):
        """After a reboot: wait until adb sees the device again and Android
        reports ``sys.boot_completed=1``. Raises ADBTimeoutError after
        *timeout* seconds."""

        def _do():
            limit = float(timeout)
            deadline = time.monotonic() + limit
            try:  # the device is often still online for a moment after `reboot`
                self._run(["wait-for-disconnect"], timeout=min(60.0, limit), check=False)
            except ADBError:
                pass
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ADBTimeoutError(
                        f"{self._serial or 'the device'} did not finish booting within {limit:g} s"
                    )
                try:
                    self._run(["wait-for-device"], timeout=max(1.0, left), check=False)
                    res = self._run(["shell", "getprop", "sys.boot_completed"], timeout=10, check=False)
                    if res.ok and res.text.strip() == "1":
                        return True
                except ADBError:
                    pass
                time.sleep(2.0)

        return self._guard("wait_for_boot", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Streaming (logcat -f, live shell loops…)
    # ------------------------------------------------------------------ #
    def iter_lines(
        self,
        args: Sequence[str],
        *,
        timeout: Optional[float] = None,
        stop_event=None,
        encoding: str = "utf-8",
    ):
        """Run an adb command and yield its stdout **line by line, live**. Stop
        by breaking out, setting ``stop_event`` (a threading.Event), or after
        ``timeout`` seconds. The process is always terminated on exit."""
        cmd = self._base(target=True) + list(args)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=NO_WINDOW,
        )
        self._emit(logging.DEBUG, f"$ adb {' '.join(args)}  (streaming)")
        start = time.monotonic()
        buf = b""

        # A watcher thread terminates the process the instant a stop is requested
        # (or the timeout elapses) so a blocking read on a quiet stream — e.g.
        # idle logcat — unblocks immediately instead of waiting for the next line.
        watcher_done = threading.Event()

        def _watch():
            while not watcher_done.is_set():
                if stop_event is not None and stop_event.is_set():
                    break
                if timeout and (time.monotonic() - start) > timeout:
                    break
                watcher_done.wait(0.1)
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if timeout and (time.monotonic() - start) > timeout:
                    break
                chunk = (
                    proc.stdout.read1(65536)
                    if hasattr(proc.stdout, "read1")
                    else proc.stdout.read(4096)
                )
                if chunk == b"":
                    if proc.poll() is not None:
                        break  # process ended (or was terminated by the watcher)
                    time.sleep(0.03)
                    continue
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()
                for ln in parts:
                    yield ln.decode(encoding, errors="replace").rstrip("\r")
                if len(buf) > self._MAX_LINE_BYTES:
                    yield buf.decode(encoding, errors="replace").rstrip("\r")
                    buf = b""
            if buf:
                yield buf.decode(encoding, errors="replace").rstrip("\r")
        finally:
            watcher_done.set()
            try:
                if proc.poll() is None:
                    proc.terminate()
                if proc.stdout:
                    proc.stdout.close()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            try:
                watcher.join(timeout=1.0)
            except Exception:
                pass

    # Longest line iter_lines will assemble before flushing it as-is. A
    # stream that never sends a newline must not grow the buffer forever.
    _MAX_LINE_BYTES = 4 * 1024 * 1024

    def popen(self, args: Sequence[str]):
        """Spawn ``adb -s SERIAL <args>`` and return the raw Popen (stdout pipe,
        stderr merged). The caller owns it — read its stdout, and ``kill()`` +
        ``stdout.close()`` to stop a blocking read immediately. Used by the GUI
        logcat viewer for instant stop."""
        cmd = self._base(target=True) + list(args)
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=NO_WINDOW,
        )

    def stream(
        self,
        args: Sequence[str],
        *,
        on_line=None,
        on_match=None,
        match=None,
        stop_on_match: bool = False,
        save_to: Optional[str] = None,
        append: bool = True,
        clean: bool = True,
        keep=None,
        timeout: Optional[float] = None,
        stop_event=None,
        encoding: str = "utf-8",
        safe: Optional[bool] = None,
    ):
        """Consume a streaming adb command with built-in matching + file logging.
        Returns a :class:`StreamResult`. See :meth:`logcat` for the common case.

        *keep* is an optional predicate applied first: a line it rejects is
        dropped entirely — not counted, not written to *save_to*, not matched
        and not passed to *on_line*. *save_to* is expanded (``~``) and its
        folder created, as for :meth:`screenshot`."""

        def _do():
            pat = re.compile(match) if isinstance(match, str) else match
            matches, count = [], 0
            path = self._prepare_local_path(save_to)
            fh = open(path, "a" if append else "w", encoding=encoding) if path else None
            try:
                for line in self.iter_lines(
                    args, timeout=timeout, stop_event=stop_event, encoding=encoding
                ):
                    if clean:
                        line = strip_ansi(line)
                    if keep is not None and not keep(line):
                        continue
                    count += 1
                    if fh:
                        fh.write(line + "\n")
                        # every line, at once: someone may be reading the
                        # file while the stream runs (tail -f, a test)
                        fh.flush()
                    if on_line:
                        on_line(line)
                    if pat and pat.search(line):
                        matches.append(line)
                        if on_match:
                            on_match(line)
                        if stop_on_match:
                            break
                return StreamResult(count, matches, path)
            finally:
                if fh:
                    fh.close()

        return self._guard("stream", _do, safe=safe)

    def logcat(
        self,
        *,
        buffers: Optional[Sequence[str]] = None,
        fmt: str = "threadtime",
        tag: Optional[str] = None,
        priority: Optional[str] = None,
        filterspecs: Optional[Sequence[str]] = None,
        dump: bool = False,
        tail: Optional[int] = None,
        grep: Optional[str] = None,
        crashes: bool = False,
        on_line=None,
        on_match=None,
        match=None,
        stop_on_match: bool = False,
        save_to: Optional[str] = None,
        append: bool = True,
        clean: bool = True,
        timeout: Optional[float] = None,
        stop_event=None,
        clear_first: bool = False,
        safe: Optional[bool] = None,
    ):
        """
        Stream ``adb logcat`` LIVE, line by line, cleanly formatted, with regex
        matching, match callbacks, stop-on-match, and tee-to-file.

        :param buffers:    logcat buffers, e.g. ``["main","system","crash"]``.
        :param fmt:        logcat ``-v`` format (default ``threadtime``).
        :param tag:        single tag filter; pairs with *priority*. Implies
                           ``*:S`` (silence everything else).
        :param priority:   minimum priority letter (V/D/I/W/E/F). With *tag* it
                           filters that tag; alone it sets ``*:PRIORITY``.
        :param filterspecs: explicit ``TAG:LEVEL`` specs (overrides tag/priority).
        :param dump:       ``-d`` — dump the current buffer and exit (no live).
        :param tail:       ``-T N`` — start from only the last *N* buffered
                           lines, then stream live. Without it, adb dumps the
                           device's ENTIRE in-memory log buffer first (often
                           hundreds of thousands of cached lines) before
                           following — pass ``tail=1`` for live-only output.
        :param grep:       case-insensitive regex kept CLIENT-side: only the
                           lines it matches reach *on_line* and *save_to* (and
                           only they are counted). Unlike *filterspecs* it can
                           search the message text, not just tag and level.
        :param crashes:    crashes and ANRs: defaults *buffers* to
                           crash/main/system and *priority* to ``E`` when the
                           caller set neither.
        :param match:      regex; matching lines collected + trigger on_match.
        :param clear_first: run ``logcat -c`` before streaming (fresh start).

        >>> dev.logcat(tag="ActivityManager", priority="I",
        ...            match=r"ANR|FATAL", on_line=print, save_to="boot.log")
        """

        def _do():
            if clear_first:
                self.logcat_clear(safe=False)
            use_buffers, use_priority = buffers, priority
            if crashes:
                use_buffers = use_buffers or ["crash", "main", "system"]
                use_priority = use_priority or "E"
            pat = re.compile(grep, re.I) if isinstance(grep, str) else grep
            args = ["logcat"]
            if dump:
                args.append("-d")
            if tail:
                # -T N starts the live stream at the last N lines; when dumping
                # the matching flag is -t N (only the last N lines, then exit)
                args += ["-t" if dump else "-T", str(int(tail))]
            if fmt:
                args += ["-v", fmt]
            for b in use_buffers or []:
                args += ["-b", b]
            if filterspecs:
                args += list(filterspecs)
            elif tag and use_priority:
                args += [f"{tag}:{use_priority}", "*:S"]
            elif tag:
                args += [f"{tag}:V", "*:S"]
            elif use_priority:
                args += [f"*:{use_priority}"]
            return self.stream(
                args,
                on_line=on_line,
                on_match=on_match,
                match=match,
                stop_on_match=stop_on_match,
                save_to=save_to,
                append=append,
                clean=clean,
                keep=(pat.search if pat is not None else None),
                timeout=timeout,
                stop_event=stop_event,
                safe=False,
            )

        return self._guard("logcat", _do, safe=safe)

    def logcat_clear(self, *, safe: Optional[bool] = None):
        """Clear (flush) the logcat buffers (``adb logcat -c``)."""
        return self._guard(
            "logcat_clear", lambda: self._run(["logcat", "-c"], timeout=15).ok, safe=safe
        )

    # ------------------------------------------------------------------ #
    # Device files — list, create, move, copy, delete (the Files tab's device
    # logic, shared with the CLI; the shell helpers live in remotefs)
    # ------------------------------------------------------------------ #
    def _file_command(self, label: str, command: str, timeout: float = 60) -> CommandResult:
        res = self.shell(command, timeout=timeout, safe=False)
        if not res.ok:
            raise self._command_error(label, res)
        return res

    def _probe_paths(self, paths) -> dict:
        """``{path: (kind, is_link, resolved)}``; kind is d, f, b (broken link) or n."""
        from . import remotefs

        try:
            return remotefs._probe_remote(self, list(paths))
        except RuntimeError as exc:
            raise ADBError(str(exc)) from None

    def list_dir(self, path: str = "/sdcard", *, safe: Optional[bool] = None):
        """The entries of a device folder, folders first, as dicts with ``name``,
        ``is_dir``, ``size``, ``size_text``, ``type``, ``modified``,
        ``permissions``, ``owner`` and ``exact`` (False when ``ls`` couldn't return
        the name unambiguously). Folder symlinks such as ``/sdcard`` count as
        folders. Raises ADBError when the folder can't be listed."""

        def _do():
            from . import remotefs

            target = remotefs._normalize_remote_path(path)
            rows, error = remotefs._list_remote_dir(self, target)
            if error and not rows:
                raise ADBError(f"could not list {target}: {error}")
            entries = [
                {
                    "name": name,
                    "is_dir": bool(is_dir),
                    "size": raw_size,
                    "size_text": size_text,
                    "type": ftype,
                    "modified": mtime,
                    "permissions": perms,
                    "owner": owner,
                    "exact": name not in rows.uncertain,
                }
                for name, raw_size, size_text, ftype, mtime, perms, owner, is_dir in rows
            ]
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
            return entries

        return self._guard("list_dir", _do, safe=safe)

    def make_dir(self, path: str, *, safe: Optional[bool] = None):
        """Create a device folder, with any missing parents (``mkdir -p``).
        Returns the normalised path."""

        def _do():
            from . import remotefs

            target = remotefs._normalize_remote_path(path)
            self._file_command(f"mkdir {target}", remotefs._mkdir_cmd(target))
            return target

        return self._guard("make_dir", _do, safe=safe)

    def touch(self, path: str, *, safe: Optional[bool] = None):
        """Create an empty device file (or update an existing file's time)."""

        def _do():
            from . import remotefs

            target = remotefs._normalize_remote_path(path)
            self._file_command(f"touch {target}", remotefs._touch_cmd(target))
            return target

        return self._guard("touch", _do, safe=safe)

    def remove(self, paths, *, recursive: bool = False, safe: Optional[bool] = None):
        """Delete device files, and folders too when *recursive* is True. Every
        path is checked first: a missing path or a folder without *recursive*
        raises ADBError and nothing is deleted. Returns the deleted paths."""

        def _do():
            from . import remotefs

            items = [paths] if isinstance(paths, str) else list(paths)
            targets = list(dict.fromkeys(remotefs._normalize_remote_path(p) for p in items))
            if not targets:
                return []
            info = self._probe_paths(targets)
            for target in targets:
                kind, is_link, _resolved = info[target]
                if target == "/":
                    raise ADBError("refusing to delete /")
                if kind == "n":
                    raise ADBError(f"{target}: no such file or folder")
                if kind == "d" and not is_link and not recursive:
                    raise ADBError(f"{target} is a folder — pass recursive=True (CLI: rm -r)")
            for chunk in remotefs._chunks(targets):
                self._file_command("rm " + " ".join(chunk), remotefs._rm_cmd(chunk), timeout=300)
            return targets

        return self._guard("remove", _do, safe=safe)

    def move(self, src: str, dst: str, *, overwrite: bool = False, safe: Optional[bool] = None):
        """Move or rename *src* to *dst* — into *dst* when it is an existing
        folder. Without *overwrite* an existing target is never replaced.
        Returns the new path."""

        def _do():
            from . import remotefs

            source = remotefs._normalize_remote_path(src)
            target = remotefs._normalize_remote_path(dst)
            info = self._probe_paths([source, target])
            if info[source][0] == "n":
                raise ADBError(f"{source}: no such file or folder")
            if info[target][0] == "d" and target != source:
                target = posixpath.join(target, posixpath.basename(source))
            if target == source:
                return target
            self._file_command(
                f"mv {source} {target}", remotefs._rename_cmd(source, target, overwrite=overwrite)
            )
            return target

        return self._guard("move", _do, safe=safe)

    def copy(self, src: str, dst: str, *, safe: Optional[bool] = None):
        """Copy a device file or folder to *dst* — into *dst* when it is an
        existing folder, merging into a folder of the same name that is already
        there. A folder is never copied into itself (also not through a
        symlinked path). Returns the new path."""

        def _do():
            from . import remotefs

            source = remotefs._normalize_remote_path(src)
            target = remotefs._normalize_remote_path(dst)
            info = self._probe_paths([source, target])
            kind, _is_link, resolved = info[source]
            if kind == "n":
                raise ADBError(f"{source}: no such file or folder")
            if info[target][0] == "d":
                target = posixpath.join(target, posixpath.basename(source))
                info.update(self._probe_paths([target]))
            if target == source:
                raise ADBError(f"{source}: the source and destination are the same")
            if kind == "d":
                real_src = posixpath.normpath(resolved or source)
                parent = posixpath.dirname(target)
                real_parent = posixpath.normpath(self._probe_paths([parent])[parent][2] or parent)
                real_target = posixpath.join(real_parent, posixpath.basename(target))
                if real_target == real_src or real_target.startswith(real_src.rstrip("/") + "/"):
                    raise ADBError(f"can't copy the folder {source} into itself")
            merge = kind == "d" and info[target][0] == "d"
            self._file_command(
                f"cp {source} {target}", remotefs._copy_into_cmd(source, target, merge), timeout=600
            )
            return target

        return self._guard("copy", _do, safe=safe)

    def stat_path(self, path: str, *, safe: Optional[bool] = None):
        """``{"path", "real_path", "size", "mode", "type"}`` for what *path* points
        to (symlinks followed; *mode* is octal such as ``644``, *type* for
        example ``regular file`` or ``directory``)."""

        def _do():
            from . import remotefs

            target = remotefs._normalize_remote_path(path)
            res = self.shell(remotefs._edit_stat_cmd(target), timeout=30, safe=False)
            parsed = remotefs._parse_edit_stat(res.stdout, target) if res.ok else None
            if parsed is None:
                raise ADBError(f"{target}: {self._combined_output(res) or 'no such file or folder'}")
            size, mode, ftype, real = parsed
            return {"path": target, "real_path": real, "size": size, "mode": mode, "type": ftype}

        return self._guard("stat_path", _do, safe=safe)

    def chmod(self, path: str, mode: str, *, safe: Optional[bool] = None):
        """Set a device file's permission bits (octal, for example ``644``)."""

        def _do():
            from . import remotefs

            if not re.fullmatch(r"[0-7]{3,4}", str(mode)):
                raise ValueError(f"mode must be octal such as 644, got {mode!r}")
            target = remotefs._normalize_remote_path(path)
            self._file_command(f"chmod {mode} {target}", remotefs._chmod_cmd(str(mode), target))
            return True

        return self._guard("chmod", _do, safe=safe)

    def edit_file(self, path: str, opener, *, editor: Optional[str] = None,
                  safe: Optional[bool] = None):
        """Edit a device text file on this machine and save it back.

        The file is pulled to a temporary copy, *opener* is called with that
        local path and returns the editor's exit code, and the copy is pushed
        back ONLY when its bytes changed — restoring the octal mode, which
        ``adb push`` resets. A non-zero editor exit leaves the device untouched.
        *editor* is the command name, used only in the log line.

        Returns ``{"changed": bool, "path": str, "editor_exit": int}``, where
        *path* is the resolved device file. Raises ADBError when *path* is not
        a regular file. The temporary copy is always removed.
        """

        def _do():
            import tempfile

            info = self.stat_path(path, safe=False)
            if not str(info["type"]).startswith("regular"):
                raise ADBError(f"{info['path']} is not a regular file ({info['type']})")
            real = info["real_path"]
            # keep the extension so the editor picks the right syntax mode
            handle, tmp = tempfile.mkstemp(
                prefix="turboadb-edit-", suffix=os.path.splitext(real)[1]
            )
            os.close(handle)
            try:
                self.pull(real, tmp, safe=False)
                with open(tmp, "rb") as fh:
                    before = fh.read()
                self._emit(logging.INFO, f"Editing {real}" + (f" with {editor}" if editor else ""))
                code = int(opener(tmp) or 0)
                changed = False
                if code == 0:
                    with open(tmp, "rb") as fh:
                        changed = fh.read() != before
                if changed:
                    self.push(tmp, real, safe=False)
                    if info.get("mode"):
                        self.chmod(real, info["mode"], safe=False)
                    self._emit(logging.INFO, f"Saved {real}")
                return {"changed": changed, "path": real, "editor_exit": code}
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        return self._guard("edit_file", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # File transfer (push / pull) with progress
    # ------------------------------------------------------------------ #
    def push(
        self,
        local_path: str,
        remote_path: str,
        *,
        on_progress=None,
        timeout: Optional[float] = None,
        cancel_event=None,
        safe: Optional[bool] = None,
    ):
        """Upload a file or directory to the device. Returns TransferResult.
        *on_progress* receives an int percent (0-100) as adb reports it."""
        return self._guard(
            "push", self._transfer, "push", local_path, remote_path, on_progress, timeout,
            cancel_event, safe=safe
        )

    def pull(
        self,
        remote_path: str,
        local_path: str,
        *,
        on_progress=None,
        timeout: Optional[float] = None,
        cancel_event=None,
        safe: Optional[bool] = None,
    ):
        """Download a file or directory from the device. Returns TransferResult."""
        return self._guard(
            "pull", self._transfer, "pull", remote_path, local_path, on_progress, timeout,
            cancel_event, safe=safe
        )

    _PCT_RE = re.compile(r"\[\s*(\d+)%\]")

    @staticmethod
    def _stop_process(proc) -> None:
        """Terminate an ADB child promptly, escalating only when necessary."""
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def _transfer(self, direction, a, b, on_progress, timeout, cancel_event) -> TransferResult:
        # direction push: a=local, b=remote ; pull: a=remote, b=local
        if direction == "push":
            local, remote = a, b
            measure = os.path.expanduser(a)
            if not os.path.exists(measure):
                raise ADBTransferError(f"Local path does not exist: {a}")
            args = ["push", measure, b]
        else:
            local, remote = b, a
            lp = os.path.expanduser(b)
            args = ["pull", a, lp]
            # `adb pull X existing_dir` writes existing_dir/basename(X); measure
            # that, not the size/count of everything already in the directory.
            name = posixpath.basename(str(a).rstrip("/"))
            measure = os.path.join(lp, name) if name and os.path.isdir(lp) else lp
        cmd = self._base(target=True) + args
        self._emit(logging.DEBUG, f"$ adb {' '.join(args)}")
        self._emit(logging.INFO, f"{direction} {a} -> {b}")
        start = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            # adb prints device paths as UTF-8; the locale codec (e.g. cp1252)
            # in strict mode raised UnicodeDecodeError and killed the reader
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
        )
        lines = queue.Queue()

        def _read_output():
            try:
                for line in iter(proc.stdout.readline, ""):
                    lines.put(line)
            except (OSError, ValueError):
                pass  # pipe closed underneath us during cancel/timeout
            finally:
                lines.put(None)

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()
        eff_timeout = timeout if timeout is not None else self.config.transfer_timeout
        deadline = time.monotonic() + eff_timeout if eff_timeout is not None else None
        last = -1
        tail = ""
        stream_closed = False
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._stop_process(proc)
                    raise ADBTransferError(f"adb {direction} cancelled")
                if deadline is not None and time.monotonic() >= deadline:
                    self._stop_process(proc)
                    raise ADBTimeoutError(f"adb {direction} timed out after {eff_timeout}s")
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    if stream_closed and proc.poll() is not None:
                        break
                    continue
                if line is None:
                    stream_closed = True
                    if proc.poll() is not None:
                        break
                    continue
                tail = line.strip()
                m = self._PCT_RE.search(line)
                if m and on_progress:
                    pct = int(m.group(1))
                    if pct != last:
                        last = pct
                        try:
                            on_progress(pct)
                        except Exception:
                            pass
            try:
                rc = proc.wait(timeout=2)
            except subprocess.TimeoutExpired as exc:
                self._stop_process(proc)
                raise ADBTimeoutError(
                    f"adb {direction} did not exit after finishing"
                ) from exc
        finally:
            if proc.poll() is None:
                self._stop_process(proc)
            reader.join(timeout=2.0)
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
        if rc != 0:
            raise ADBTransferError(f"adb {direction} failed (exit {rc}): {tail or '(no output)'}")
        if on_progress:
            try:
                on_progress(100)
            except Exception:
                pass
        # size best-effort from the local side
        lp = measure
        size, files = 0, 1
        if os.path.isdir(lp):
            files = 0
            for root, _dirs, fnames in os.walk(lp):
                for f in fnames:
                    try:
                        size += os.path.getsize(os.path.join(root, f))
                        files += 1
                    except OSError:
                        pass
        elif os.path.isfile(lp):
            size = os.path.getsize(lp)
        src, dst = (local, remote) if direction == "push" else (remote, local)
        return TransferResult(src, dst, direction, size, time.monotonic() - start, files)

    # ------------------------------------------------------------------ #
    # App management
    # ------------------------------------------------------------------ #
    def install(
        self,
        apk: str,
        *,
        replace: bool = True,
        downgrade: bool = False,
        grant_perms: bool = False,
        allow_test: bool = False,
        extra_args: Optional[Sequence[str]] = None,
        safe: Optional[bool] = None,
    ):
        """Install a single APK (``adb install``)."""

        def _do():
            args = ["install"]
            if replace:
                args.append("-r")
            if downgrade:
                args.append("-d")
            if grant_perms:
                args.append("-g")
            if allow_test:
                args.append("-t")
            args += list(extra_args or [])
            args.append(os.path.expanduser(apk))
            self._emit(logging.INFO, f"Installing {apk}…")
            res = self._run(args, timeout=300)
            out = self._combined_output(res)
            if "Success" not in out:
                # stdout is often just "Performing Streamed Install"; the real
                # reason (Failure [INSTALL_FAILED_…]) is on stderr
                raise ADBInstallError(f"Install failed: {out or f'exit {res.exit_code}'}")
            return res.text or "Success"

        return self._guard("install", _do, safe=safe)

    def install_multiple(
        self,
        apks: Sequence[str],
        *,
        replace: bool = True,
        grant_perms: bool = False,
        downgrade: bool = False,
        allow_test: bool = False,
        safe: Optional[bool] = None,
    ):
        """Install split APKs together (``adb install-multiple``)."""

        def _do():
            args = ["install-multiple"]
            if replace:
                args.append("-r")
            if downgrade:
                args.append("-d")
            if grant_perms:
                args.append("-g")
            if allow_test:
                args.append("-t")
            args += [os.path.expanduser(a) for a in apks]
            self._emit(logging.INFO, f"Installing {len(apks)} split APK(s)…")
            res = self._run(args, timeout=600)
            out = self._combined_output(res)
            if "Success" not in out:
                raise ADBInstallError(f"install-multiple failed: {out or f'exit {res.exit_code}'}")
            return res.text or "Success"

        return self._guard("install_multiple", _do, safe=safe)

    def uninstall(self, package: str, *, keep_data: bool = False, safe: Optional[bool] = None):
        """Uninstall a package. *keep_data=True* keeps app data/cache (``-k``)."""

        def _do():
            args = ["uninstall"] + (["-k"] if keep_data else []) + [package]
            res = self._run(args, timeout=120)
            out = self._combined_output(res)
            if "Success" not in out:
                raise ADBInstallError(f"Uninstall failed: {out or f'exit {res.exit_code}'}")
            return res.text or "Success"

        return self._guard("uninstall", _do, safe=safe)

    def list_packages(
        self,
        *,
        filter_text: Optional[str] = None,
        third_party: bool = False,
        system: bool = False,
        disabled: bool = False,
        enabled: bool = False,
        include_path: bool = False,
        safe: Optional[bool] = None,
    ):
        """Return a list of installed package names (``pm list packages``)."""

        def _do():
            args = ["pm", "list", "packages"]
            if include_path:
                args.append("-f")
            if third_party:
                args.append("-3")
            if system:
                args.append("-s")
            if disabled:
                args.append("-d")
            if enabled:
                args.append("-e")
            if filter_text:
                args.append(filter_text)
            # An offline/unauthorised transport previously produced an empty
            # stdout list, which the GUI displayed as a successful "0 packages".
            res = self._run(self._shell_args(*args), timeout=30, check=True)
            pkgs = []
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("package:"):
                    pkgs.append(line[len("package:") :])
            return sorted(pkgs)

        return self._guard("list_packages", _do, safe=safe)

    def clear_app(self, package: str, *, safe: Optional[bool] = None):
        """Clear an app's data and cache (``pm clear``)."""
        def _do():
            res = self._run(self._shell_args("pm", "clear", package), timeout=60, check=True)
            text = self._combined_output(res)
            if "success" not in text.lower():
                raise self._command_error("shell pm clear " + package, res)
            return text

        return self._guard("clear_app", _do, safe=safe)

    def start_app(self, package: str, *, safe: Optional[bool] = None):
        """Launch an app by package — resolves its launcher activity and starts
        it explicitly, so it works even on automotive/IVI where ``monkey`` is
        often blocked (falls back to monkey / a MAIN-LAUNCHER intent)."""
        return self._guard("start_app", lambda: self._launch_package(package), safe=safe)

    def start_activity(
        self,
        component: str,
        *,
        action: Optional[str] = None,
        data: Optional[str] = None,
        extras: Optional[Sequence[str]] = None,
        safe: Optional[bool] = None,
    ):
        """Start an explicit activity/component (``am start -n pkg/.Activity``).

        Returns ``am``'s output. Raises ADBCommandError when the activity did
        not start: ``am`` prints "Error: Activity class … does not exist" and
        still exits 0, so the exit code alone cannot be trusted."""

        def _do():
            args = ["am", "start", "-n", component]
            if action:
                args += ["-a", action]
            if data:
                args += ["-d", data]
            args += list(extras or [])
            res = self._run(self._shell_args(*args), timeout=30)
            if self._output_failed(res):
                raise self._command_error("shell " + " ".join(args), res)
            return res.text

        return self._guard("start_activity", _do, safe=safe)

    def stop_app(self, package: str, *, safe: Optional[bool] = None):
        """Force-stop an app (``am force-stop``). Like every ``am`` call site,
        the output decides — a refusal exits 0."""

        def _do():
            res = self._run(self._shell_args("am", "force-stop", package), timeout=30, check=True)
            if self._output_failed(res):
                raise self._command_error("shell am force-stop " + package, res)
            return True

        return self._guard("stop_app", _do, safe=safe)

    def grant(self, package: str, permission: str, *, safe: Optional[bool] = None):
        """Grant a runtime permission (``pm grant``)."""
        return self._guard(
            "grant",
            lambda: (
                self._run(
                    self._shell_args("pm", "grant", package, permission), timeout=30, check=True
                ).ok
            ),
            safe=safe,
        )

    def revoke(self, package: str, permission: str, *, safe: Optional[bool] = None):
        """Revoke a runtime permission (``pm revoke``)."""
        return self._guard(
            "revoke",
            lambda: (
                self._run(
                    self._shell_args("pm", "revoke", package, permission), timeout=30, check=True
                ).ok
            ),
            safe=safe,
        )

    def current_activity(self, *, safe: Optional[bool] = None):
        """Best-effort current foreground activity (handy on IVI to see what's up)."""

        def _do():
            res = self._run(["shell", "dumpsys", "activity", "activities"], timeout=30)
            for line in res.stdout.splitlines():
                if "ResumedActivity" in line:  # also matches mResumedActivity
                    m = re.search(r"\{[^}]*\s(\S+/\S+)", line)
                    if m:
                        return m.group(1)
            return ""

        return self._guard("current_activity", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Media — screenshot / screen record
    # ------------------------------------------------------------------ #
    @staticmethod
    def _bytes(stdout):
        return stdout if isinstance(stdout, (bytes, bytearray)) else b""

    _PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
    # the same 8-byte signature after a PTY translated every LF into CRLF
    _PNG_SIGNATURE_CRLF = b"\x89PNG\r\r\n\x1a\r\n"

    @classmethod
    def _as_png(cls, data):
        """*data* as bytes when it carries the full PNG signature, else None."""
        return bytes(data) if data[:8] == cls._PNG_SIGNATURE else None

    def _cap_exec_out(self, display_id=None):
        args = ["exec-out", "screencap", *self._display_flag(display_id), "-p"]
        r = self._run(args, timeout=60, binary=True, check=False)
        d = self._bytes(r.stdout)
        return self._as_png(d), r, len(d)

    def _cap_shell(self, display_id=None):
        args = ["shell", "screencap", *self._display_flag(display_id), "-p"]
        r = self._run(args, timeout=60, binary=True, check=False)
        d = self._bytes(r.stdout)
        # Undo CRLF translation ONLY when the stream was actually translated;
        # on a binary-clean stream it would corrupt genuine \r\n bytes.
        if d.startswith(self._PNG_SIGNATURE_CRLF):
            d = d.replace(b"\r\n", b"\n")
        return self._as_png(d), r, len(d)

    def _cap_file(self, display_id=None):
        # unique per call: concurrent captures (live view + a screenshot, two
        # tabs on one device) must not overwrite or delete each other's file
        remote = f"/data/local/tmp/_turboadb_cap_{_unique_token()}.png"
        try:
            self._run(
                ["shell", "screencap", *self._display_flag(display_id), "-p", remote],
                timeout=60,
                check=False,
            )
            r = self._run(["exec-out", "cat", remote], timeout=60, binary=True, check=False)
        finally:
            try:
                self._run(["shell", "rm", "-f", remote], timeout=15, check=False)
            except ADBError:
                pass
        d = self._bytes(r.stdout)
        return self._as_png(d), r, len(d)

    def capture_png(
        self,
        *,
        display_id: Optional[Union[int, str]] = None,
        safe: Optional[bool] = None,
    ):
        """Capture the screen as PNG bytes, trying several methods so it works on
        locked-down / automotive devices and over remote adb servers:

        1. ``exec-out screencap -p`` (binary-safe, fastest)
        2. ``shell screencap -p`` with CRLF de-translation (some servers/devices
           mangle the binary stream)
        3. write a PNG on the device, then read it back with ``exec-out cat``

        The method that works is REMEMBERED and tried first next time — so Live
        View (which captures many frames/second) doesn't re-probe the two failing
        methods on every frame (and, for method 3, doesn't do the failed probes'
        round-trips before the file write). Raises :class:`ADBError` with the
        actual device output if none yield a valid PNG. (Not annotated
        ``-> bytes``: in safe mode it returns an OperationResult like every
        other public method.)"""
        methods = [self._cap_exec_out, self._cap_shell, self._cap_file]

        def _do():
            order = list(range(len(methods)))
            cached = self._cap_method
            if cached is not None and cached in order:
                order.remove(cached)
                order.insert(0, cached)  # try the known-good one first
            # A requested display is never silently swapped for another one:
            # every candidate names the same display (its physical id, then
            # the logical id older screencap builds take); only display 0 is
            # captured without -d, as the default display it is.
            displays = self._screencap_candidates(display_id)
            last, errors = {}, []
            for disp in displays:
                for i in order:
                    try:
                        png, res, nbytes = methods[i](disp)
                    except ADBError as exc:  # one failing method must not abort the rest
                        errors.append(f"m{i + 1}: {exc}")
                        continue
                    last[i] = (res, nbytes)
                    if png is not None:
                        self._cap_method = i  # remember for next frame
                        return png
            self._cap_method = None  # nothing worked — re-probe next time
            if display_id is not None:
                # the display may have been re-plugged under a new physical id
                self._screencap_ids.pop(int(display_id), None)
            errtxt = ""
            for i in (0, 1):
                if i in last and last[i][0].stderr:
                    errtxt = last[i][0].stderr.strip()
                    break
            if not errtxt and errors:
                errtxt = "; ".join(errors)
            sizes = "; ".join(
                f"m{i + 1}: {last.get(i, (None, 0))[1]} bytes" for i in range(len(methods))
            )
            raise ADBError(
                "screencap did not produce a PNG on this device "
                f"({sizes}; stderr: {errtxt[:160] or 'none'}). The screen may be "
                "a secure/automotive surface that blocks screen capture."
            )

        return self._guard("capture_png", _do, safe=safe)

    @staticmethod
    def screencap_display_arg(display) -> Optional[int]:
        """The id ``screencap -d`` takes for *display* (an entry from
        :meth:`list_displays`), or None for the default display.

        ``input -d`` and ``wm size -d`` take the LOGICAL display id, but on
        Android 10+ ``screencap -d`` takes the PHYSICAL one (``uniqueId
        "local:<id>"``). Display 0 is captured without ``-d``; a display with
        no physical id (older Android, a virtual display) keeps its logical id.
        """
        display = display or {}
        logical = int(display.get("id", 0) or 0)
        if logical == 0:
            return None
        physical = display.get("physical_id")
        return int(physical) if physical is not None else logical

    def _screencap_candidates(self, display_id) -> list:
        """``screencap -d`` ids to try for logical *display_id*, best first.

        The id is validated before any adb call (a caller's ``"1; reboot"``
        must never reach the device shell). The physical id is looked up once
        per handler with ``cmd display get-displays`` / ``dumpsys display``.
        """
        if display_id is None:
            return [None]
        logical = int(str(display_id).strip())
        if logical == 0:
            return [None]
        cache = self._screencap_ids
        if logical not in cache:
            physical = None
            try:
                for display in self._list_displays_adb():
                    if int(display.get("id", -1)) == logical:
                        physical = display.get("physical_id")
                        break
            except (ADBError, ValueError, TypeError):
                physical = None
            cache[logical] = physical
        physical = cache[logical]
        return [physical, logical] if physical is not None and physical != logical else [logical]

    SCREENCAP_STREAM_FORMATS = ("auto", "gzip", "raw", "png")
    # printed (then the device's own error text) when the capture loop gives up
    SCREENCAP_ERROR_MARKER = b"TURBOADB-SCREENCAP-ERROR:"

    @classmethod
    def screencap_stream_script(
        cls, capture_id: Optional[int] = None, *, interval: float = 0.2, fmt: str = "auto"
    ) -> str:
        """The device-side loop behind :meth:`open_screencap_stream`.

        One shell writes a capture every *interval* seconds (a capture that
        takes longer simply starts the next one straight away): raw frames
        (``fmt="raw"``), raw frames through ``gzip -1`` (``"gzip"``), PNG
        (``"png"``), or ``"auto"`` — gzip when the device has it, else PNG.
        Measured on a 1080x2400 phone over USB: gzip ~3.1 fps, PNG ~2.5 fps,
        plain raw ~1.1-1.5 fps (10 MB a frame saturates the adb link). The
        reader tells the formats apart by their first bytes. After three failed
        captures in a row the loop prints :attr:`SCREENCAP_ERROR_MARKER` and
        screencap's own error text, then exits; a capture killed by a signal
        (the reader went away) ends it at once, so no loop outlives the reader.
        """
        fmt = str(fmt or "auto").lower()
        if fmt not in cls.SCREENCAP_STREAM_FORMATS:
            raise ValueError(f"unknown screencap stream format: {fmt!r}")
        target = "" if capture_id is None else f" -d {int(capture_id)}"
        cap = f"screencap{target}"
        pause = f"{max(0.0, float(interval)):.3f}".rstrip("0").rstrip(".") or "0"
        marker = cls.SCREENCAP_ERROR_MARKER.decode("ascii")

        def loop(command):
            return (
                f"while :; do sleep {pause} 2>/dev/null & "
                f"{command} && n=0 || fail $?; wait; done"
            )

        raw = loop(f"{cap} 2>/dev/null")
        png = loop(f"{cap} -p 2>/dev/null")
        packed = loop(f"{cap} 2>/dev/null | gzip -1")
        parts = [
            "n=0",
            "fail() { r=$1; [ $r -gt 128 ] && exit $r; n=$((n+1)); [ $n -lt 3 ] && return 0; "
            f'echo "{marker} $({cap} 2>&1 >/dev/null)"; exit 3; }}',
        ]
        if fmt in ("gzip", "auto"):
            # a failed capture must fail the pipeline, not gzip's clean exit
            parts.append("(set -o pipefail) 2>/dev/null && set -o pipefail")
        if fmt == "auto":
            parts.append(f"if command -v gzip >/dev/null 2>&1; then {packed}; else {png}; fi")
        else:
            parts.append({"gzip": packed, "raw": raw, "png": png}[fmt])
        return "; ".join(parts)

    def open_screencap_stream(
        self,
        *,
        capture_id: Optional[int] = None,
        max_fps: float = 5.0,
        fmt: str = "auto",
        safe: Optional[bool] = None,
    ):
        """Start ONE long-lived ``adb exec-out`` process that streams screen
        captures of *capture_id* (see :meth:`screencap_display_arg`) at up to
        *max_fps* frames per second, and return its Popen (binary ``stdout``,
        adb's own messages on ``stderr``).

        A process per frame is both slow and a pile of adb processes; this
        keeps one for the whole session. The caller reads and splits the
        stream and owns the process: ``kill()`` it to stop. A reader that stops
        reading throttles the device-side loop (its writes block), so a paused
        view costs no capture work.
        """

        def _do():
            fps = max(0.1, float(max_fps or 5.0))
            script = self.screencap_stream_script(capture_id, interval=1.0 / fps, fmt=fmt)
            args = ["exec-out", script]
            self._emit(logging.DEBUG, f"$ adb exec-out '{script}'  (screencap stream)")
            proc = subprocess.Popen(
                self._base(target=True) + args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=NO_WINDOW,
            )
            self._drain_stderr(proc)
            return proc

        return self._guard("open_screencap_stream", _do, safe=safe)

    # A long-lived `adb exec-out` writes to stderr as it goes (device offline,
    # reconnects, daemon notes).  Nothing read that pipe until the stdout loop
    # ended, so once ~64 KB filled the OS pipe buffer adb blocked on the write,
    # stopped producing frames, and the live view froze for good with no error.
    # Drain it in the background instead and keep a bounded copy for the caller.
    _STDERR_KEEP_BYTES = 64 * 1024

    @classmethod
    def _drain_stderr(cls, proc):
        """Consume *proc*'s stderr in a daemon thread so it can never fill.

        The text collected so far is always available from
        :meth:`collected_stderr`, whether or not the process has exited."""
        pipe = getattr(proc, "stderr", None)
        if pipe is None:
            return None
        proc._turboadb_stderr = bytearray()
        keep = cls._STDERR_KEEP_BYTES

        def _pump():
            try:
                for chunk in iter(lambda: pipe.read(4096), b""):
                    buf = proc._turboadb_stderr
                    if len(buf) < keep:
                        buf.extend(chunk[: keep - len(buf)])
            except (OSError, ValueError):
                pass  # the pipe was closed under us while stopping

        thread = threading.Thread(
            target=_pump, name="turboadb-stderr-drain", daemon=True
        )
        thread.start()
        proc._turboadb_stderr_thread = thread
        return thread

    @staticmethod
    def collected_stderr(proc, wait: float = 1.0) -> bytes:
        """*proc*'s stderr text, however it was captured.

        Returns the background drain's bounded copy when one is running;
        for a process nobody drained, falls back to reading the pipe, so a
        caller that builds its own Popen still gets adb's error text.

        Once the process has ended, the drain gets up to *wait* seconds to
        read what is left: adb's last message is usually the reason it
        stopped, and it can still be in the pipe when the caller asks."""
        drained = getattr(proc, "_turboadb_stderr", None)
        if drained is not None:
            thread = getattr(proc, "_turboadb_stderr_thread", None)
            poll = getattr(proc, "poll", None)
            ended = poll is None or poll() is not None
            if thread is not None and wait and ended:
                thread.join(wait)
            return bytes(drained)
        pipe = getattr(proc, "stderr", None)
        if pipe is None:
            return b""
        try:
            return pipe.read() or b""
        except Exception:
            return b""

    def screenshot(
        self,
        local_path: Optional[str] = None,
        *,
        display_id: Optional[Union[int, str]] = None,
        safe: Optional[bool] = None,
    ):
        """Capture the screen as PNG (robust multi-method, see :meth:`capture_png`).
        Saves to *local_path* if given and returns the path; otherwise returns
        the raw PNG bytes."""

        def _do():
            data = self.capture_png(display_id=display_id, safe=False)
            if local_path:
                lp = self._prepare_local_path(local_path)
                with open(lp, "wb") as fh:
                    fh.write(data)
                self._emit(logging.INFO, f"Screenshot saved: {lp} ({len(data)} bytes)")
                return lp
            return data

        return self._guard("screenshot", _do, safe=safe)

    def screen_record(
        self,
        local_path: str,
        *,
        time_limit: int = 180,
        size: Optional[str] = None,
        bit_rate: Optional[str] = None,
        display_id: Optional[Union[int, str]] = None,
        remote_tmp: Optional[str] = None,
        stop_event=None,
        safe: Optional[bool] = None,
    ):
        """Record the screen on-device (``screenrecord``), then pull it to
        *local_path* — ``~`` expanded and its folder created, like
        :meth:`screenshot`; the saved path is returned. Stops at *time_limit*
        seconds (max 180 per adb) or when *stop_event* is set. *size* like
        ``1280x720``, *bit_rate* like ``8M``.

        The default remote file and PID marker are unique to this invocation,
        so concurrent recordings cannot overwrite or interrupt one another.
        A *time_limit* above 180 is clamped (screenrecord rejects it); 0 means
        screenrecord's own default (180 s).

        *display_id* is the LOGICAL display id (as for screenshots and input);
        Android 10+ ``screenrecord --display-id`` takes the PHYSICAL one, so it
        is looked up the same way :meth:`capture_png` does, and the logical id
        is tried next when the device rejects the physical one at once.
        """

        def _record(record_display):
            limit = int(time_limit or 0)
            if limit < 0:
                raise ValueError("time_limit must be 0 or a positive number of seconds")
            if limit > 180:
                self._emit(logging.WARNING, f"screenrecord allows at most 180 s; clamping {limit} s")
                limit = 180
            token = _unique_token()
            remote_path = remote_tmp or f"/sdcard/turboadb_rec_{token}.mp4"
            pid_path = f"/data/local/tmp/turboadb_screenrecord_{token}.pid"
            record_args = ["screenrecord"]
            if limit:
                record_args += ["--time-limit", str(limit)]
            if size:
                record_args += ["--size", size]
            if bit_rate:
                record_args += ["--bit-rate", bit_rate]
            if record_display is not None:
                record_args += ["--display-id", str(record_display)]
            record_args.append(remote_path)
            self._run(
                self._shell_args("rm", "-f", remote_path, pid_path),
                check=False,
                timeout=15,
            )
            shell_command = (
                " ".join(shlex.quote(part) for part in record_args)
                + f" & recorder=$!; echo $recorder > {shlex.quote(pid_path)}; wait $recorder"
            )
            cmd = self._base(target=True) + ["shell", shell_command]
            self._emit(logging.INFO, f"Recording screen -> {remote_path} (limit {limit or 180}s)…")
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW
            )
            stage = "record"
            try:
                start = time.monotonic()
                stopped_by_user = False
                while proc.poll() is None:
                    if stop_event is not None and stop_event.is_set():
                        stopped_by_user = True
                        # Signal only the PID owned by this invocation. The old
                        # pidof loop interrupted every recorder on the device.
                        stop_script = (
                            f"pid=$(cat {shlex.quote(pid_path)} 2>/dev/null) || exit 1; "
                            "case $pid in ''|*[!0-9]*) exit 1;; esac; "
                            'kill -INT "$pid"'
                        )
                        for _attempt in range(5):
                            try:
                                result = self._run(
                                    ["shell", stop_script],
                                    check=False,
                                    timeout=5,
                                )
                                if getattr(result, "exit_code", 1) == 0:
                                    break
                            except Exception:
                                pass
                            time.sleep(0.1)
                        break
                    if time.monotonic() - start > ((limit or 180) + 5):
                        break
                    time.sleep(0.2)
                if stopped_by_user:
                    # Let the remote shell observe the interrupt and exit;
                    # terminate adb only when it remains stuck afterwards.
                    deadline = time.monotonic() + 5.0
                    while proc.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.1)
                forced = False
                if proc.poll() is None:
                    forced = True
                    proc.terminate()
                    try:
                        proc.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        pass
                # ``stop_event`` is already set when a user stops recording;
                # it must not cancel the *subsequent* pull of that final clip.
                exit_code = proc.poll()
                # A recorder we had to terminate exits non-zero by definition —
                # that is not a recording failure.
                if not stopped_by_user and not forced and exit_code not in (None, 0):
                    output = ""
                    try:
                        output = (proc.stdout.read() if proc.stdout else b"") or b""
                        if isinstance(output, bytes):
                            output = output.decode("utf-8", errors="replace")
                    except Exception:
                        pass
                    result = CommandResult(" ".join(cmd), int(exit_code), "", str(output), 0.0)
                    raise ADBCommandError(" ".join(cmd), result)
                if forced:
                    # the device may still be finalising the MP4 index
                    self._wait_remote_file_stable(remote_path)
                stage = "pull"
                saved_path = self._prepare_local_path(local_path)
                self._transfer("pull", remote_path, saved_path, None, None, None)
                if not os.path.isfile(saved_path) or os.path.getsize(saved_path) <= 0:
                    raise ADBTransferError(
                        f"adb reported success but the recording is empty; "
                        f"the device copy remains at {remote_path}"
                    )
                self._run(self._shell_args("rm", "-f", remote_path), check=False, timeout=15)
                self._emit(logging.INFO, f"Recording saved: {saved_path}")
                return saved_path
            finally:
                cleanup = [pid_path]
                if stage == "record" and not remote_tmp:
                    # interrupted/failed before the pull (e.g. Ctrl+C): don't
                    # leave our partial temp MP4 behind on the device
                    cleanup.append(remote_path)
                try:
                    self._run(self._shell_args("rm", "-f", *cleanup), check=False, timeout=15)
                except Exception:
                    pass
                try:
                    if proc.poll() is None:
                        proc.terminate()
                    if proc.stdout:
                        proc.stdout.close()
                    proc.wait(timeout=3.0)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass

        def _do():
            candidates = self._screencap_candidates(display_id)
            for index, candidate in enumerate(candidates):
                started = time.monotonic()
                try:
                    return _record(candidate)
                except ADBCommandError:
                    stopped = stop_event is not None and stop_event.is_set()
                    if index + 1 >= len(candidates) or stopped or time.monotonic() - started > 8.0:
                        raise
                    self._emit(
                        logging.INFO,
                        f"screenrecord rejected display id {candidate}; "
                        f"trying {candidates[index + 1]}",
                    )

        return self._guard("screen_record", _do, safe=safe)

    def screen_record_continuous(
        self,
        local_path: str,
        *,
        part_seconds: int = 180,
        size: Optional[str] = None,
        bit_rate: Optional[str] = None,
        display_id: Optional[Union[int, str]] = None,
        stop_event=None,
        on_part=None,
        safe: Optional[bool] = None,
    ):
        """Record back-to-back parts until *stop_event* is set, so a session can
        run past ``screenrecord``'s three-minute cap.

        Parts are named after *local_path*: ``drive.mp4``, ``drive-part02.mp4``,
        ``drive-part03.mp4`` … Each one is pulled and handed to *on_part* as
        soon as it is saved, so a long recording is never lost to a later
        failure. Returns the list of saved paths.

        Without a *stop_event* this records exactly one part. A failure in the
        FIRST part is raised as-is; a later one stops the recording and is
        re-raised after the parts already saved have been reported.
        """

        def _do():
            base, ext = os.path.splitext(self._prepare_local_path(local_path))
            ext = ext or ".mp4"
            paths = []
            while True:
                part = len(paths)
                target = local_path if part == 0 else f"{base}-part{part + 1:02d}{ext}"
                try:
                    saved = self.screen_record(
                        target,
                        time_limit=part_seconds,
                        size=size,
                        bit_rate=bit_rate,
                        display_id=display_id,
                        stop_event=stop_event,
                        safe=False,
                    )
                except Exception:
                    if paths:
                        # the earlier parts are already reported through on_part
                        self._emit(
                            logging.ERROR,
                            f"recording stopped after part {part} of "
                            f"{local_path}; {len(paths)} part(s) were saved",
                        )
                    raise
                paths.append(saved)
                if on_part:
                    on_part(saved)
                if stop_event is None or stop_event.is_set():
                    return paths

        return self._guard("screen_record_continuous", _do, safe=safe)

    def _wait_remote_file_stable(self, remote_path: str, attempts: int = 20, interval: float = 0.25):
        """Poll the device file size until two reads agree (bounded), instead of
        sleeping a fixed time and hoping the writer has finished."""
        last = None
        for _ in range(attempts):
            try:
                r = self._run(self._shell_args("stat", "-c", "%s", remote_path), timeout=10)
            except ADBError:
                return
            size = r.text.strip()
            if not size.isdigit():
                time.sleep(1.0)  # can't observe the file here; give it a moment
                return
            if size == last and int(size) > 0:
                return
            last = size
            time.sleep(interval)

    # ------------------------------------------------------------------ #
    # Port forwarding
    # ------------------------------------------------------------------ #
    def forward(self, local: str, remote: str, *, safe: Optional[bool] = None):
        """``adb forward`` — expose a device socket on the host. Specs look like
        ``tcp:8080`` / ``localabstract:name``. Returns a stoppable ForwardHandle.

        >>> fwd = dev.forward("tcp:9222", "localabstract:chrome_devtools_remote")
        >>> ...; fwd.close()
        """

        def _do():
            self._run(["forward", local, remote], timeout=15, check=True)
            self._emit(logging.INFO, f"forward {local} -> {remote}")
            return ForwardHandle(self, "forward", local, remote)

        return self._guard("forward", _do, safe=safe)

    def reverse(self, remote: str, local: str, *, safe: Optional[bool] = None):
        """``adb reverse`` — expose a host socket on the device. Returns a
        stoppable ForwardHandle.

        >>> rev = dev.reverse("tcp:8000", "tcp:8000")   # device reaches your PC
        """

        def _do():
            self._run(["reverse", remote, local], timeout=15, check=True)
            self._emit(logging.INFO, f"reverse {remote} <- {local}")
            return ForwardHandle(self, "reverse", local, remote)

        return self._guard("reverse", _do, safe=safe)

    def _own_forwards(self) -> list:
        """``forward --list`` lines ("SERIAL LOCAL REMOTE") for the active serial
        — adb lists EVERY device's rules regardless of ``-s``."""
        lines = self._run(["forward", "--list"], timeout=15, check=True).lines
        if not self._serial:
            return lines
        return [ln for ln in lines if ln.split() and ln.split()[0] == self._serial]

    def list_forwards(self, *, safe: Optional[bool] = None):
        """This device's forward rules as ``"SERIAL LOCAL REMOTE"`` lines."""
        return self._guard("list_forwards", self._own_forwards, safe=safe)

    def remove_all_forwards(self, *, safe: Optional[bool] = None):
        """Remove this device's forward rules. ``adb forward --remove-all`` is
        server-wide (it also drops other devices' rules, e.g. another tab's
        scrcpy tunnel), so with a known serial each own rule is removed
        individually. Returns True when every removal succeeded."""

        def _do():
            if not self._serial:
                return self._run(["forward", "--remove-all"], timeout=15).ok
            ok = True
            for line in self._own_forwards():
                parts = line.split()
                if len(parts) >= 2:
                    ok = self._run(["forward", "--remove", parts[1]], timeout=15).ok and ok
            return ok

        return self._guard("remove_all_forwards", _do, safe=safe)

    def remove_forward(self, local: str, *, safe: Optional[bool] = None):
        """Remove one of this device's forward rules by its local spec (``tcp:8080``)."""
        return self._guard(
            "remove_forward",
            lambda: self._run(["forward", "--remove", local], timeout=15, check=True).ok,
            safe=safe,
        )

    def list_reverses(self, *, safe: Optional[bool] = None):
        """This device's reverse rules (``adb reverse --list``)."""
        return self._guard(
            "list_reverses",
            lambda: self._run(["reverse", "--list"], timeout=15, check=True).lines,
            safe=safe,
        )

    def remove_reverse(self, remote: str, *, safe: Optional[bool] = None):
        """Remove one reverse rule by its device-side spec (``tcp:3000``)."""
        return self._guard(
            "remove_reverse",
            lambda: self._run(["reverse", "--remove", remote], timeout=15, check=True).ok,
            safe=safe,
        )

    def remove_all_reverses(self, *, safe: Optional[bool] = None):
        """Remove this device's reverse rules (reverse rules are per device)."""
        return self._guard(
            "remove_all_reverses",
            lambda: self._run(["reverse", "--remove-all"], timeout=15, check=True).ok,
            safe=safe,
        )

    # ------------------------------------------------------------------ #
    # scrcpy — visual mirroring/control session
    # ------------------------------------------------------------------ #
    _DISPLAY_INFO_RE = re.compile(
        r'DisplayInfo\{"(?P<name>[^"]*?)(?:, displayId (?P<quoted_id>\d+))?"'
        r"(?:, displayId (?P<id>\d+))?(?P<body>[^}]*)"
    )

    _UNIQUE_ID_RE = re.compile(r'\buniqueId[ =]"(?P<unique>[^"]*)"')

    @classmethod
    def parse_display_info(cls, text: str) -> list:
        """Displays in ``cmd display get-displays`` / ``dumpsys display`` output.

        Returns ``[{"id": int, "size": "WxH", "name": str}, …]`` sorted by id. A
        display listed more than once keeps its last entry (the current,
        override size). Android 9 entries carry no id; they take it from the
        ``mDisplayId=`` line above them.

        An entry that carries a ``uniqueId`` also gets ``"unique_id"`` (e.g.
        ``"local:4630946650788219010"`` or ``"virtual:…"``) and, for a physical
        (``local:``) display, ``"physical_id"``: the id ``screencap -d`` takes
        on Android 10+, where ``input -d`` / ``wm size -d`` keep the logical
        ``"id"``.
        """
        text = text or ""
        headers = [(m.start(), int(m.group(1))) for m in re.finditer(r"mDisplayId=\s*(\d+)", text)]
        found = {}
        matches = list(cls._DISPLAY_INFO_RE.finditer(text))
        for index, match in enumerate(matches):
            raw_id = match.group("quoted_id") or match.group("id")
            if raw_id is None:
                before = [did for pos, did in headers if pos < match.start()]
                if not before:
                    continue
                display_id = before[-1]
            else:
                display_id = int(raw_id)
            real = re.search(r"\breal (\d+) x (\d+)", match.group("body") or "")
            size = f"{real.group(1)}x{real.group(2)}" if real else ""
            if not size and display_id in found:
                size = found[display_id]["size"]
            entry = {"id": display_id, "size": size, "name": match.group("name").strip()}
            # ``uniqueId`` sits after nested ``{…}`` lists (supportedModes), past
            # where the body group stops: look through the rest of this entry.
            end = text.find("\n", match.end())
            end = len(text) if end < 0 else end
            if index + 1 < len(matches):
                end = min(end, matches[index + 1].start())
            unique = cls._UNIQUE_ID_RE.search(text, match.start(), end)
            if unique is None and display_id in found and "unique_id" in found[display_id]:
                for key in ("unique_id", "physical_id"):
                    if key in found[display_id]:
                        entry[key] = found[display_id][key]
            elif unique is not None:
                entry["unique_id"] = unique.group("unique")
                local = re.fullmatch(r"local:(\d+)", entry["unique_id"])
                if local:
                    entry["physical_id"] = int(local.group(1))
            found[display_id] = entry
        return [found[key] for key in sorted(found)]

    def _list_displays_adb(self) -> list:
        """Ask Android itself for its displays; ``[]`` when it can't say."""
        for args in (["shell", "cmd", "display", "get-displays"], ["shell", "dumpsys", "display"]):
            try:
                res = self._run(args, timeout=15)
            except ADBError:
                continue
            displays = self.parse_display_info(res.text)
            if displays:
                return displays
        return []

    def list_displays(self, *, method: str = "auto", safe: Optional[bool] = None):
        """Enumerate the device's displays — use an id with ``mirror(display_id=...)``
        to mirror a specific IVI display (cluster, centre stack, passenger).

        Returns ``[{"id": int, "size": "WxH", "name": str}, …]``. *method*
        ``"adb"`` asks Android itself (``cmd display get-displays``, else
        ``dumpsys display``): fast, and it never starts scrcpy. ``"scrcpy"`` runs
        ``scrcpy --list-displays`` (no names). ``"auto"`` (the default) tries adb
        first and falls back to scrcpy.
        """
        def _do():
            from .scrcpy import list_displays as _ld

            if method not in ("auto", "adb", "scrcpy"):
                raise ValueError(f"unknown display listing method: {method!r}")
            if method != "scrcpy":
                displays = self._list_displays_adb()
                if displays or method == "adb":
                    return displays
            return _ld(
                self._serial,
                scrcpy_path=self.config.scrcpy_path,
                adb_server_host=self.config.adb_server_host,
                adb_server_port=self.config.adb_server_port,
                adb_path=self.adb_path,
            )

        return self._guard("list_displays", _do, safe=safe)

    def mirror(
        self,
        options: Optional[ScrcpyOptions] = None,
        *,
        compat: bool = False,
        log_path: Optional[str] = None,
        safe: Optional[bool] = None,
        **kwargs,
    ):
        """Launch scrcpy to mirror & control this device. Extra keyword args are
        forwarded to :class:`ScrcpyOptions` (e.g. ``max_size=1024, bit_rate="8M",
        display_id=2, record="drive.mp4"``). Returns a ScrcpySession handle.

        *compat=True* applies an Android-Automotive/IVI **compatibility profile**
        for head units whose encoders choke on scrcpy's defaults: forces the
        widely-supported H.264 codec, caps the size/fps, and disables audio. Try
        it first when "scrcpy won't work" on a head unit.
        """
        def _do():
            from .scrcpy import launch_scrcpy

            if options is not None:
                # a copy: compat mode must never mutate the caller's options,
                # and keyword overrides apply on top of the given options
                opts = dataclasses.replace(options, **kwargs)
                opts.extra_args = list(opts.extra_args or [])
            else:
                opts = ScrcpyOptions(**kwargs)
            if compat:
                opts.video_codec = opts.video_codec or "h264"
                opts.max_size = opts.max_size or 1280
                opts.max_fps = opts.max_fps or 30
                opts.no_audio = True
                # IVIs/head units commonly block `adb reverse`; a forward tunnel
                # is what lets scrcpy connect to its server on them
                opts.force_adb_forward = True
            adb = self.adb_path  # pin scrcpy to OUR adb (avoids server clashes)
            # Do not synchronously probe or restart adb here.  TurboADB already
            # maintains its bundled adb server while discovering the device; an
            # extra ``get-state`` / ``start-server`` sequence delayed every
            # screen launch by up to ten seconds and could race scrcpy's own
            # server startup.  scrcpy is launched immediately with that exact
            # same bundled adb binary and reports any genuine connection error.
            # If the "remote" adb server is actually THIS machine (device is local,
            # just addressed by its LAN IP), run scrcpy LOCALLY — no network video
            # tunnel — which is exactly how running scrcpy directly there works.
            from .scrcpy import is_local_host, resolve_host, TUNNEL_PORT_FIREWALL_RANGE

            server_host = self.config.adb_server_host
            if server_host and is_local_host(server_host):
                self._emit(
                    logging.INFO,
                    f"adb server {server_host} is THIS machine → running "
                    f"scrcpy locally (no network tunnel)",
                )
                server_host = None
            elif server_host:
                ip = resolve_host(server_host)
                self._emit(
                    logging.INFO,
                    f"scrcpy will stream the device's video over the "
                    f"network from {ip} via tunnel ports TCP "
                    f"{TUNNEL_PORT_FIREWALL_RANGE} — that range must be "
                    f"open in {ip}'s firewall",
                )
            self._emit(
                logging.INFO,
                f"Launching scrcpy for {self._serial or '(only device)'}"
                f"{' (compat mode)' if compat else ''} with adb={adb}…",
            )
            return launch_scrcpy(
                self._serial,
                opts,
                scrcpy_path=self.config.scrcpy_path,
                adb_server_host=server_host,
                adb_server_port=self.config.adb_server_port,
                log_path=log_path,
                adb_path=adb,
            )

        return self._guard("mirror", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Context manager
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "ADBHandler":
        result = self.connect()
        if isinstance(result, OperationResult) and not result.success:
            raise result.error or ADBConnectionError("connect failed")
        return self

    def __exit__(self, *exc) -> None:
        """Release the handler. A network target is disconnected only when THIS
        handler created the connection; one that was already connected (by the
        GUI, another script, or a previous session) stays connected."""
        if self._owned_target and self._owned_target == self._serial:
            self.disconnect()
        self._connected = False

    def __repr__(self) -> str:
        state = "connected" if self._connected else "idle"
        return f"<ADBHandler target={self._serial!r} {state}>"


# A friendly alias: think of one handler as one device you drive end-to-end.
ADBDevice = ADBHandler
