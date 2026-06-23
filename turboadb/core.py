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
import logging
import threading
import subprocess
from typing import Callable, Optional, Sequence, Union

from .config import ADBConfig, ScrcpyOptions
from .devices import list_devices
from .results import (CommandResult, TransferResult, StreamResult,
                      OperationResult, strip_ansi)
from .tools import find_adb, find_scrcpy, NO_WINDOW
from .exceptions import (
    ADBError,
    ADBConnectionError,
    ADBTimeoutError,
    ADBCommandError,
    ADBTransferError,
    ADBInstallError,
    ADBNotConnectedError,
)


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

    @property
    def proc(self) -> subprocess.Popen:
        return self._proc

    @property
    def running(self) -> bool:
        return self._proc.poll() is None

    def send(self, data: Union[str, bytes]) -> None:
        if isinstance(data, str):
            data = data.encode(self.encoding, "replace")
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except Exception:
            pass

    def send_line(self, line: str) -> None:
        self.send(line + "\n")

    def read(self, size: int = 65536) -> bytes:
        """Read available bytes (blocking up to one chunk). b'' at EOF."""
        try:
            return self._proc.stdout.read1(size)  # type: ignore[attr-defined]
        except AttributeError:
            return self._proc.stdout.read(size)
        except Exception:
            return b""

    def resize(self, cols: int, rows: int) -> None:
        # adb shell over a pipe has no PTY to resize; kept for API parity.
        pass

    def close(self) -> None:
        try:
            if self.running:
                self._proc.terminate()
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
        self.kind = kind            # "forward" or "reverse"
        self.local = local
        self.remote = remote
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.kind == "forward":
                self._h._run_global(["forward", "--remove", self.local], check=False)
            else:
                self._h._run_global(["reverse", "--remove", self.remote], check=False)
        except Exception:
            pass

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

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"turboadb.{self.config.target or 'device'}")
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
            logger.addHandler(handler)
        logger.setLevel(logging.WARNING if self._quiet else logging.INFO)
        return logger

    def _emit(self, level: int, msg: str) -> None:
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
            if not self.config.adb_path:
                try:
                    from . import toolsdl
                    toolsdl.ensure_tools(
                        notify=lambda m: self._emit(logging.INFO, m))
                except Exception:
                    pass
            self._adb = find_adb(self.config.adb_path)
        return self._adb

    @property
    def serial(self) -> Optional[str]:
        """The active device serial (``host:port`` for a network target)."""
        return self._serial

    def _base(self, target: bool = True) -> list:
        cmd = [self.adb_path]
        if self.config.adb_server_host:        # route through a remote adb server
            cmd += ["-H", self.config.adb_server_host,
                    "-P", str(self.config.adb_server_port)]
        if target and self._serial:
            cmd += ["-s", self._serial]
        return cmd

    def _exec(self, args: Sequence[str], *, timeout, target: bool, check: bool,
              binary: bool = False) -> CommandResult:
        cmd = self._base(target=target) + list(args)
        eff_timeout = timeout if timeout is not None else self.config.command_timeout
        start = time.time()
        try:
            out = subprocess.run(
                cmd, capture_output=True, timeout=eff_timeout,
                creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise ADBTimeoutError(
                f"adb command timed out after {eff_timeout}s: {' '.join(args)!r}"
            ) from exc
        except FileNotFoundError as exc:  # pragma: no cover
            raise ADBError(f"adb executable not runnable: {exc}") from exc
        if binary:
            stdout = out.stdout or b""
            stderr = (out.stderr or b"").decode("utf-8", "replace")
            result = CommandResult(" ".join(args), out.returncode, stdout, stderr,
                                   time.time() - start, device=self._serial or "")
        else:
            enc = self.config.encoding
            result = CommandResult(
                " ".join(args), out.returncode,
                (out.stdout or b"").decode(enc, "replace"),
                (out.stderr or b"").decode(enc, "replace"),
                time.time() - start, device=self._serial or "")
        if check and not result.ok:
            raise ADBCommandError(" ".join(args), result)
        return result

    def _run(self, args, *, timeout=None, check=False, binary=False) -> CommandResult:
        """Run an adb command scoped to this device (``adb -s SERIAL ...``)."""
        return self._exec(args, timeout=timeout, target=True, check=check,
                          binary=binary)

    def _run_global(self, args, *, timeout=None, check=False) -> CommandResult:
        """Run an adb command not scoped to a device (connect/devices/pair...)."""
        return self._exec(args, timeout=timeout, target=False, check=check)

    def _logged_run(self, label, args, *, timeout=None) -> CommandResult:
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
            self._emit(logging.WARNING,
                       f"  -> {label}: exit {res.exit_code} ({res.duration:.2f}s): "
                       f"{detail}")
        return res

    def adb(self, *args, timeout=None, check=False, safe: Optional[bool] = None):
        """Escape hatch: run an arbitrary ``adb -s SERIAL`` command and get a
        CommandResult. e.g. ``dev.adb("shell", "wm", "size")``."""
        return self._guard("adb", lambda: self._run(list(args), timeout=timeout,
                                                     check=check), safe=safe)

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
        if not remote:
            self._run_global(["start-server"], check=False, timeout=30)

        if cfg.host and cfg.auto_connect and not remote:
            target = f"{cfg.host}:{cfg.port}"
            self._emit(logging.INFO, f"adb connect {target}")
            res = self._run_global(["connect", target], timeout=cfg.connect_timeout)
            text = (res.stdout + res.stderr).lower()
            if "cannot" in text or "failed" in text or "unable" in text:
                raise ADBConnectionError(
                    f"adb connect {target} failed: {res.text or res.stderr.strip()}. "
                    f"Check the head unit's IP, that 'adb tcpip {cfg.port}' was run "
                    f"over USB first (or wireless debugging is on), and that you can "
                    f"reach it on the network.")
            self._serial = target

        if self._serial is None:
            # no explicit target: bind to the only online device on the (local or
            # remote) adb server
            try:
                devs = list_devices(cfg.adb_path, server_host=cfg.adb_server_host,
                                    server_port=cfg.adb_server_port)
            except ConnectionError as exc:
                raise ADBConnectionError(str(exc)) from exc
            online = [d for d in devs if d.is_online]
            if len(online) == 1:
                self._serial = online[0].serial
            elif len(online) > 1:
                raise ADBConnectionError(
                    f"Multiple devices on {'the remote server' if remote else 'USB'} "
                    f"({', '.join(d.serial for d in online)}); set serial=... to "
                    f"choose one.")
            elif remote:
                raise ADBConnectionError(
                    f"No online devices on the adb server at {cfg.adb_server_host}:"
                    f"{cfg.adb_server_port}. Plug a device into that machine and "
                    f"check 'adb devices' there.")

        if cfg.auto_wait:
            self._wait_for_device(cfg.connect_timeout)

        state = self.get_state()
        if state != "device":
            raise ADBConnectionError(
                f"Device {self._serial or '(any)'} is '{state}', not ready. "
                f"If 'unauthorized', accept the USB-debugging / wireless prompt on "
                f"the screen; if 'offline', replug or re-run adb connect.")
        self._connected = True
        self._emit(logging.INFO, f"Connected to {self._serial or 'device'} "
                                 f"({state}).")
        return self

    def _wait_for_device(self, timeout: float) -> None:
        args = ["wait-for-device"]
        try:
            self._run(args, timeout=timeout, check=False)
        except ADBTimeoutError as exc:
            raise ADBConnectionError(
                f"Timed out after {timeout}s waiting for "
                f"{self._serial or 'a device'}.") from exc

    def wait_for_device(self, timeout: Optional[float] = None, *,
                        safe: Optional[bool] = None):
        """Block until the device is online (``adb wait-for-device``)."""
        return self._guard(
            "wait_for_device",
            lambda: self._wait_for_device(timeout or self.config.connect_timeout)
            or True, safe=safe)

    def disconnect(self, *, safe: Optional[bool] = None):
        """Disconnect. For network targets this runs ``adb disconnect host:port``;
        USB targets are simply released (the device stays attached)."""
        def _do():
            if self._serial and ":" in self._serial:
                self._run_global(["disconnect", self._serial], check=False)
                self._emit(logging.INFO, f"Disconnected {self._serial}.")
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
        """``device`` | ``offline`` | ``unauthorized`` | ``unknown``."""
        try:
            res = self._run(["get-state"], timeout=10, check=False)
        except ADBError:
            return "unknown"
        out = res.text or res.stderr.strip()
        return out.split()[0] if out else "unknown"

    def get_serialno(self) -> str:
        return self._run(["get-serialno"], timeout=10, check=False).text

    @staticmethod
    def devices(adb_path: Optional[str] = None, server_host: Optional[str] = None,
                server_port: int = 5037) -> list:
        """List devices on the local (or a remote) adb server."""
        return list_devices(adb_path, server_host=server_host,
                            server_port=server_port)

    # ------------------------------------------------------------------ #
    # TCP/IP + pairing (Android 11+ wireless debugging)
    # ------------------------------------------------------------------ #
    def tcpip(self, port: int = 5555, *, safe: Optional[bool] = None):
        """Restart adbd on the USB-attached device listening on TCP *port*, so it
        can be reached over the network. Run this while on USB; afterwards use
        ``ADBConfig(host=..., port=port)`` to connect wirelessly."""
        def _do():
            res = self._run(["tcpip", str(port)], timeout=20)
            self._emit(logging.INFO, f"adbd now listening on tcp:{port}.")
            return res.text or res.stderr.strip()
        return self._guard("tcpip", _do, safe=safe)

    def connect_tcp(self, host: str, port: int = 5555, *,
                    safe: Optional[bool] = None):
        """``adb connect host:port`` and switch this handler to that target."""
        def _do():
            res = self._run_global(["connect", f"{host}:{port}"], timeout=20)
            text = (res.stdout + res.stderr).lower()
            if "cannot" in text or "failed" in text:
                raise ADBConnectionError(f"connect failed: {res.text or res.stderr}")
            self._serial = f"{host}:{port}"
            self.config.host, self.config.port = host, port
            return self._serial
        return self._guard("connect_tcp", _do, safe=safe)

    def pair(self, host: str, port: int, code: str, *,
             safe: Optional[bool] = None):
        """Pair with an Android 11+ device using its Wireless-debugging code
        (``adb pair host:port code``). Pairing port differs from the connect port."""
        def _do():
            proc = subprocess.run(
                self._base(target=False) + ["pair", f"{host}:{port}"],
                input=(code + "\n").encode(), capture_output=True, timeout=30,
                creationflags=NO_WINDOW)
            out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
            if "Successfully paired" not in out:
                raise ADBConnectionError(f"Pairing failed: {out.strip()}")
            self._emit(logging.INFO, f"Paired with {host}:{port}.")
            return out.strip()
        return self._guard("pair", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Reboot / root / device state
    # ------------------------------------------------------------------ #
    def reboot(self, mode: Optional[str] = None, *, safe: Optional[bool] = None):
        """Reboot the device. *mode*: None (normal), ``recovery``, ``bootloader``,
        or ``sideload``."""
        args = ["reboot"] + ([mode] if mode else [])
        return self._guard("reboot",
                           lambda: self._run(args, timeout=30, check=False).ok,
                           safe=safe)

    def root(self, *, safe: Optional[bool] = None):
        """Restart adbd as root (``adb root``). No-op on production/secure builds."""
        def _do():
            res = self._run(["root"], timeout=30, check=False)
            time.sleep(1.0)
            return res.text or res.stderr.strip()
        return self._guard("root", _do, safe=safe)

    def unroot(self, *, safe: Optional[bool] = None):
        def _do():
            res = self._run(["unroot"], timeout=30, check=False)
            time.sleep(1.0)
            return res.text or res.stderr.strip()
        return self._guard("unroot", _do, safe=safe)

    def remount(self, *, safe: Optional[bool] = None):
        """Remount /system (and friends) read-write (needs root)."""
        return self._guard("remount",
                           lambda: self._run(["remount"], timeout=30,
                                             check=False).text, safe=safe)

    def disable_verity(self, *, safe: Optional[bool] = None):
        """``adb disable-verity`` — disable dm-verity (needs root; reboot after)."""
        return self._guard("disable_verity",
                           lambda: self._run(["disable-verity"], timeout=30,
                                             check=False).text or "ok", safe=safe)

    def enable_verity(self, *, safe: Optional[bool] = None):
        """``adb enable-verity`` — re-enable dm-verity (reboot after)."""
        return self._guard("enable_verity",
                           lambda: self._run(["enable-verity"], timeout=30,
                                             check=False).text or "ok", safe=safe)

    def mount_rw(self, path: str = "/", *, safe: Optional[bool] = None):
        """Remount a partition read-write via the shell (needs root).
        Default ``/`` (system-as-root); pass ``/system`` on older devices."""
        return self._guard(
            "mount_rw",
            lambda: self._run(["shell", "mount", "-o", "remount,rw", path],
                              timeout=30, check=False).text or "ok", safe=safe)

    # ------------------------------------------------------------------ #
    # Device input & controls (keys, text, connectivity) — handy for IVI /
    # head units with no keyboard: type from the PC into the focused field.
    # ------------------------------------------------------------------ #
    # common Android keycodes
    KEYS = {
        "home": 3, "back": 4, "call": 5, "menu": 82, "search": 84,
        "recents": 187, "power": 26, "sleep": 223, "wake": 224,
        "vol_up": 24, "vol_down": 25, "vol_mute": 164,
        "play_pause": 85, "stop": 86, "next": 87, "prev": 88,
        "rewind": 89, "fast_forward": 90, "media_play": 126, "media_pause": 127,
        "up": 19, "down": 20, "left": 21, "right": 22, "center": 23,
        "enter": 66, "del": 67, "tab": 61, "space": 62, "esc": 111,
        "brightness_up": 221, "brightness_down": 220, "notifications": 83,
    }

    @staticmethod
    def _dq(s: str) -> str:
        """Single-quote a string for the on-device shell."""
        return "'" + s.replace("'", "'\\''") + "'"

    def keyevent(self, key, *, longpress: bool = False, safe: Optional[bool] = None):
        """Send a key. *key* is a keycode int or a name from :attr:`KEYS`
        (e.g. ``"home"``, ``"vol_up"``, ``"play_pause"``)."""
        code = self.KEYS.get(key, key) if isinstance(key, str) else key
        args = ["shell", "input", "keyevent"] + (["--longpress"] if longpress else []) \
            + [str(code)]
        label = f"key {key}" if isinstance(key, str) else f"keyevent {code}"
        return self._guard("keyevent",
                           lambda: self._logged_run(label, args, timeout=15).ok, safe=safe)

    def input_text(self, text: str, *, safe: Optional[bool] = None):
        """Type *text* into the currently focused field on the device — works as
        a keyboard for head units / devices with no on-screen keyboard."""
        payload = text.replace(" ", "%s")        # 'input text' uses %s for space
        cmd = "input text " + self._dq(payload)
        return self._guard("input_text",
                           lambda: self._logged_run(f"type {text!r}", ["shell", cmd],
                                                    timeout=15).ok, safe=safe)

    def tap(self, x: int, y: int, *, safe: Optional[bool] = None):
        return self._guard("tap",
                           lambda: self._run(["shell", "input", "tap", str(x), str(y)],
                                             timeout=15).ok, safe=safe)

    def swipe(self, x1, y1, x2, y2, ms: int = 200, *, safe: Optional[bool] = None):
        return self._guard("swipe",
                           lambda: self._run(["shell", "input", "swipe", str(x1),
                                              str(y1), str(x2), str(y2), str(ms)],
                                             timeout=15).ok, safe=safe)

    def _screen_size(self):
        import re
        try:
            txt = self._run(["shell", "wm", "size"], timeout=10).text
            m = re.search(r"(\d+)x(\d+)", txt)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        return 1080, 1920

    def scroll(self, direction: str, *, safe: Optional[bool] = None):
        """Scroll the screen with a swipe gesture — works on **any touch screen**
        (unlike D-pad keys, which only work on D-pad/IVI launchers). *direction*:
        up | down | left | right."""
        def _do():
            w, h = self._screen_size()
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
            return self._logged_run(f"scroll {d}",
                                    ["shell", "input", "swipe", *map(str, pts), "200"],
                                    timeout=12).ok
        return self._guard("scroll", _do, safe=safe)

    def tap_center(self, *, safe: Optional[bool] = None):
        def _do():
            w, h = self._screen_size()
            return self._logged_run("tap center",
                                    ["shell", "input", "tap", str(w // 2), str(h // 2)],
                                    timeout=10).ok
        return self._guard("tap_center", _do, safe=safe)

    def set_wifi(self, on: bool, *, safe: Optional[bool] = None):
        st = "enable" if on else "disable"
        return self._guard("set_wifi",
                           lambda: self._logged_run(f"wifi {st}",
                                                    ["shell", "svc", "wifi", st],
                                                    timeout=15).text or "ok", safe=safe)

    def set_bluetooth(self, on: bool, *, safe: Optional[bool] = None):
        st = "enable" if on else "disable"
        return self._guard("set_bluetooth",
                           lambda: self._logged_run(f"bluetooth {st}",
                                                    ["shell", "svc", "bluetooth", st],
                                                    timeout=15).text or "ok", safe=safe)

    def set_airplane(self, on: bool, *, safe: Optional[bool] = None):
        """Toggle airplane mode (Android 11+ via ``cmd connectivity``)."""
        st = "enable" if on else "disable"
        return self._guard("set_airplane",
                           lambda: self._logged_run(
                               f"airplane {st}",
                               ["shell", "cmd", "connectivity", "airplane-mode", st],
                               timeout=15).text or "ok", safe=safe)

    def _open_tether_settings(self) -> bool:
        """Open the Hotspot/Tethering settings screen. Tries AOSP, OEM and
        Android-Automotive (car settings) components/actions in turn."""
        intents = [
            ["am", "start", "-a", "android.settings.TETHER_SETTINGS"],
            ["am", "start", "-n", "com.android.settings/.TetherSettings"],
            ["am", "start", "-n",
             "com.android.settings/.Settings\\$TetherSettingsActivity"],
            ["am", "start", "-n",
             "com.android.settings/.Settings\\$WifiTetherSettingsActivity"],
            # Android Automotive (car) settings
            ["am", "start", "-n",
             "com.android.car.settings/.wifi.WifiTetherActivity"],
            ["am", "start", "-n",
             "com.android.car.settings/.wifi.WifiSettingsActivity"],
            ["am", "start", "-a", "android.settings.WIRELESS_SETTINGS"],
            ["am", "start", "-a", "android.settings.SETTINGS"],   # last resort
        ]
        for it in intents:
            r = self._run(["shell"] + it, timeout=15, check=False)
            out = (r.stdout + r.stderr).lower()
            if r.ok and "unable to resolve" not in out and "error" not in out:
                return True
        return False

    def set_hotspot(self, on: bool, *, safe: Optional[bool] = None):
        """Toggle the Wi-Fi hotspot. No single adb command works across every
        Android version / OEM / automotive build, so we try the modern
        ``cmd wifi`` soft-AP commands and, if they aren't supported, fall back to
        opening the Hotspot/Tethering settings screen so it can still be toggled
        — making the button do something useful on every device."""
        def _bad(out):
            out = out.lower()
            return any(x in out for x in ("unknown command", "no such", "usage:",
                       "error", "exception", "not allowed", "permission",
                       "denied", "invalid"))

        def _do():
            if on:
                attempts = [
                    ["cmd", "wifi", "start-softap", "TurboADB", "wpa2", "turboadb123"],
                    ["cmd", "wifi", "start-softap", "TurboADB", "open"],
                    ["cmd", "wifi", "start-tethering"],
                ]
            else:
                attempts = [["cmd", "wifi", "stop-softap"],
                            ["cmd", "wifi", "stop-tethering"]]
            for a in attempts:
                r = self._logged_run("hotspot " + ("on" if on else "off"),
                                     ["shell"] + a, timeout=20)
                if r.ok and not _bad(r.stdout + r.stderr):
                    return "ok"
            # nothing worked via adb (on locked-down/automotive builds the shell
            # user lacks the TETHER_PRIVILEGED permission) — open the settings UI
            if self._open_tether_settings():
                return ("the hotspot can't be switched via adb on this device "
                        "(the shell user is permission-denied) - opened the "
                        "Hotspot/Tethering settings so you can toggle it there")
            return "hotspot can't be controlled via adb on this device"
        return self._guard("set_hotspot", _do, safe=safe)

    def screen_on(self, *, safe: Optional[bool] = None):
        return self.keyevent("wake", safe=safe)

    def screen_off(self, *, safe: Optional[bool] = None):
        return self.keyevent("sleep", safe=safe)

    def media(self, action: str, *, safe: Optional[bool] = None):
        """Control media via the active session (more reliable than keyevents).
        *action*: play-pause | play | pause | next | previous | stop |
        fast-forward | rewind | mute."""
        return self._guard("media",
                           lambda: self._logged_run(
                               f"media {action}",
                               ["shell", "cmd", "media_session", "dispatch", action],
                               timeout=15).ok, safe=safe)

    def get_brightness(self, *, safe: Optional[bool] = None):
        def _do():
            r = self._run(["shell", "settings", "get", "system",
                           "screen_brightness"], timeout=15)
            try:
                return int(r.text.strip())
            except ValueError:
                return 128
        return self._guard("get_brightness", _do, safe=safe)

    def set_brightness(self, value: int, *, safe: Optional[bool] = None):
        """Set screen brightness 0-255 (turns off auto-brightness first)."""
        v = max(0, min(255, int(value)))

        def _do():
            self._run(["shell", "settings", "put", "system",
                       "screen_brightness_mode", "0"], timeout=15, check=False)
            self._run(["shell", "settings", "put", "system",
                       "screen_brightness", str(v)], timeout=15, check=False)
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
        f = max(0.0, min(1.0, float(fraction)))
        return self._guard(
            "display_brightness",
            lambda: self._logged_run(f"brightness {int(f*100)}%",
                                     ["shell", "cmd", "display", "set-brightness",
                                      f"{f:.3f}"], timeout=12).ok, safe=safe)

    # A URL/scheme hint -> the app packages that can handle it. On head units /
    # IVIs there's often no browser, so a VIEW intent fails; we then launch the
    # matching app directly if it's installed.
    _URL_APPS = {
        "youtube": ["com.google.android.youtube",
                    "com.google.android.apps.youtube.music",
                    "com.google.android.youtube.tv"],
        "maps.google": ["com.google.android.apps.maps"],
        "google.com/maps": ["com.google.android.apps.maps"],
        "spotify": ["com.spotify.music", "com.spotify.tv.android"],
        "play.google": ["com.android.vending"],
        "market:": ["com.android.vending"],
    }

    def open_url(self, url: str, *, safe: Optional[bool] = None):
        """Open a URL/URI with a VIEW intent — routes to the matching app
        (e.g. a youtube.com URL opens the YouTube app) or the browser. If that
        fails (common on IVIs with no browser), launch the matching app
        package directly."""
        if not url.startswith(("http://", "https://")) and "://" not in url:
            url = "https://" + url

        def _do():
            r = self._logged_run(f"open {url}",
                                 ["shell", "am", "start", "-a",
                                  "android.intent.action.VIEW", "-d", url],
                                 timeout=20)
            out = (r.stdout + r.stderr).lower()
            if r.ok and not any(x in out for x in ("unable to resolve",
                    "no activities", "does not exist", "error")):
                return True
            # VIEW failed — try launching the matching app directly
            try:
                installed = set(self.list_packages(safe=False))
            except Exception:
                installed = set()
            low = url.lower()
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
        return self.open_url("https://www.google.com/search?q=" + quote_plus(query),
                            safe=safe)

    def open_settings(self, *, safe: Optional[bool] = None):
        return self._guard(
            "open_settings",
            lambda: self._run(["shell", "am", "start", "-a",
                               "android.settings.SETTINGS"], timeout=15).ok,
            safe=safe)

    # Known package names per app, so launching works on ANY OEM (Vivo/Samsung/
    # Xiaomi/Oppo/OnePlus/stock…) when the standard intent isn't honoured.
    _APP_PACKAGES = {
        "calculator": ["com.google.android.calculator", "com.android.calculator2",
                       "com.sec.android.app.popupcalculator", "com.miui.calculator",
                       "com.coloros.calculator", "com.oneplus.calculator",
                       "com.vivo.calculator", "com.transsion.calculator"],
        "gallery": ["com.google.android.apps.photos", "com.android.gallery3d",
                    "com.sec.android.gallery3d", "com.miui.gallery",
                    "com.coloros.gallery3d", "com.vivo.gallery",
                    "com.oneplus.gallery"],
        "camera": ["com.android.camera2", "com.android.camera",
                   "com.sec.android.app.camera", "com.google.android.GoogleCamera",
                   "com.oppo.camera", "com.vivo.camera", "com.oneplus.camera"],
    }

    def _launch_package(self, package: str) -> bool:
        """Launch an installed app as robustly as possible across phones AND
        Android Automotive head units. Strategy, in order:

        1. Resolve the package's LAUNCHER activity and start it by explicit
           component (``am start -n pkg/Activity``) — works where ``monkey`` is
           blocked, which is common on automotive/IVI.
        2. ``monkey -p pkg`` (the usual launcher shortcut).
        3. A bare MAIN/LAUNCHER intent.
        """
        def _bad(out):
            out = out.lower()
            return any(x in out for x in ("error", "no activities", "unable to "
                       "resolve", "does not exist", "not found", "exception",
                       "permission denial"))
        self._emit(logging.INFO, f"launching {package}…")
        # 1) resolve the launcher activity, then start that component
        try:
            r = self._run(["shell", "cmd", "package", "resolve-activity",
                           "--brief", "-c", "android.intent.category.LAUNCHER",
                           package], timeout=15, check=False)
            comp = ""
            for line in r.text.splitlines():
                line = line.strip()
                if "/" in line and " " not in line:
                    comp = line
            if comp and "/" in comp:
                a = self._logged_run(f"start {comp}",
                                     ["shell", "am", "start", "-n", comp],
                                     timeout=20)
                if a.ok and not _bad(a.stdout + a.stderr):
                    return True
        except Exception:
            pass
        # 2) monkey
        m = self._logged_run(f"monkey {package}",
                             ["shell", "monkey", "-p", package, "-c",
                              "android.intent.category.LAUNCHER", "1"], timeout=20)
        if m.ok and "No activities found" not in (m.stdout + m.stderr):
            return True
        # 3) bare MAIN/LAUNCHER intent scoped to the package
        b = self._logged_run(f"intent {package}",
                             ["shell", "am", "start", "-a",
                              "android.intent.action.MAIN", "-c",
                              "android.intent.category.LAUNCHER", package],
                             timeout=20)
        ok = b.ok and not _bad(b.stdout + b.stderr)
        if not ok:
            self._emit(logging.WARNING, f"could not launch {package} "
                       "(blocked or no launcher activity on this device)")
        return ok

    def _open_app(self, label, intent_args, fallback_key, *, safe):
        """Try a standard intent; if it doesn't resolve, launch the first
        installed known package for this app type; finally, fall back to any
        installed package whose name hints the type — so it works on any device,
        including automotive head units with OEM-specific package names."""
        def _do():
            r = self._run(["shell", "am", "start"] + intent_args, timeout=15,
                          check=False)
            text = (r.stdout + r.stderr).lower()
            if r.ok and not any(x in text for x in ("unable to resolve",
                    "not started", "no activities", "does not exist", "error")):
                return True
            try:
                installed = set(self.list_packages(safe=False))
            except Exception:
                installed = set()
            for pkg in self._APP_PACKAGES.get(fallback_key, []):
                if pkg in installed and self._launch_package(pkg):
                    return True
            # last resort: any installed package whose name hints the app type
            for pkg in sorted(installed):
                if fallback_key in pkg.lower() and self._launch_package(pkg):
                    return True
            self._emit(logging.WARNING,
                       f"no {fallback_key} app is installed on this device "
                       f"(checked {len(installed)} packages) — nothing to open")
            return False
        return self._guard(label, _do, safe=safe)

    def open_camera(self, *, safe: Optional[bool] = None):
        return self._open_app("open_camera",
                              ["-a", "android.media.action.STILL_IMAGE_CAMERA"],
                              "camera", safe=safe)

    def open_gallery(self, *, safe: Optional[bool] = None):
        return self._open_app("open_gallery",
                              ["-a", "android.intent.action.MAIN", "-c",
                               "android.intent.category.APP_GALLERY"],
                              "gallery", safe=safe)

    def open_calculator(self, *, safe: Optional[bool] = None):
        return self._open_app("open_calculator",
                              ["-a", "android.intent.action.MAIN", "-c",
                               "android.intent.category.APP_CALCULATOR"],
                              "calculator", safe=safe)

    def close_apps(self, *, safe: Optional[bool] = None):
        """Actually close apps: ``am kill-all`` (background) **and** force-stop
        every third-party app (so they really close, not just cache-trim)."""
        def _do():
            cmd = ("am kill-all >/dev/null 2>&1; "
                   "n=0; for p in $(pm list packages -3 | cut -d: -f2); do "
                   "am force-stop \"$p\"; n=$((n+1)); done; echo \"closed $n apps\"")
            r = self._run(["shell", cmd], timeout=120, check=False)
            return r.text.strip() or "closed apps"
        return self._guard("close_apps", _do, safe=safe)

    def build_info(self, *, safe: Optional[bool] = None):
        """A readable block of the key build/identity properties."""
        keys = ["ro.product.manufacturer", "ro.product.brand", "ro.product.model",
                "ro.product.name", "ro.product.device", "ro.build.version.release",
                "ro.build.version.sdk", "ro.build.version.security_patch",
                "ro.build.display.id", "ro.build.type", "ro.build.date",
                "ro.product.cpu.abi", "ro.build.characteristics",
                "ro.build.fingerprint"]

        def _do():
            p = self.getprop(safe=False)        # raw dict even when handler is safe
            return "\n".join(f"{k:32} {p.get(k, '')}" for k in keys)
        return self._guard("build_info", _do, safe=safe)

    def battery(self, *, safe: Optional[bool] = None):
        """Battery status (``dumpsys battery``)."""
        return self._guard("battery",
                           lambda: self._run(["shell", "dumpsys", "battery"],
                                             timeout=20).text, safe=safe)

    # ------------------------------------------------------------------ #
    # Telephony / messaging — dialler, calls, call log, SMS
    # ------------------------------------------------------------------ #
    def dial(self, number: str, *, safe: Optional[bool] = None):
        """Open the dialler pre-filled with *number* (doesn't place the call)."""
        return self._guard("dial",
                           lambda: self._run(["shell", "am", "start", "-a",
                                              "android.intent.action.DIAL", "-d",
                                              f"tel:{number}"], timeout=15).ok,
                           safe=safe)

    def call(self, number: str, *, safe: Optional[bool] = None):
        """Place a call to *number* (needs CALL_PHONE; works from adb shell on
        most devices)."""
        return self._guard("call",
                           lambda: self._run(["shell", "am", "start", "-a",
                                              "android.intent.action.CALL", "-d",
                                              f"tel:{number}"], timeout=15).ok,
                           safe=safe)

    def end_call(self, *, safe: Optional[bool] = None):
        return self.keyevent(6, safe=safe)        # KEYCODE_ENDCALL

    def answer_call(self, *, safe: Optional[bool] = None):
        return self.keyevent(5, safe=safe)        # KEYCODE_CALL

    def call_state(self, *, safe: Optional[bool] = None):
        def _do():
            r = self._run(["shell", "dumpsys", "telephony.registry"], timeout=15)
            m = re.search(r"mCallState=(\d+)", r.text)
            return {0: "idle", 1: "ringing", 2: "in call"}.get(
                int(m.group(1)) if m else -1, "unknown")
        return self._guard("call_state", _do, safe=safe)

    def _query_rows(self, uri, fields, limit, free_last=False):
        proj = ":".join(fields)
        cmd = (f"content query --uri {uri} --projection '{proj}' "
               f"--sort 'date DESC' 2>/dev/null | head -n {int(limit)}")
        r = self._run(["shell", cmd], timeout=25, check=False)
        rows = []
        for line in r.text.splitlines():
            line = line.strip()
            if not line.startswith("Row:"):
                continue
            parts = line.split(" ", 2)
            if len(parts) < 3:
                continue
            body = parts[2]
            d = {}
            if free_last:
                simple = ", ".join(f"{f}=(.*?)" for f in fields[:-1])
                pat = simple + f", {fields[-1]}=(.*)$"
                m = re.match(pat, body)
                if m:
                    for f, v in zip(fields, m.groups()):
                        d[f] = v
            else:
                for kv in body.split(", "):
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
            lambda: self._query_rows("content://call_log/calls",
                                     ["type", "date", "duration", "number"], limit),
            safe=safe)

    def sms_list(self, limit: int = 50, *, safe: Optional[bool] = None):
        """Recent SMS: list of {type, date, address, body}. type 1=inbox 2=sent."""
        return self._guard(
            "sms_list",
            lambda: self._query_rows("content://sms",
                                     ["type", "date", "address", "body"], limit,
                                     free_last=True),
            safe=safe)

    def send_sms(self, number: str, body: str, *, safe: Optional[bool] = None):
        """Open the messaging app composing to *number* pre-filled with *body*
        (sending directly needs the default-SMS app or root, so we open it)."""
        return self._guard(
            "send_sms",
            lambda: self._run(["shell", "am", "start", "-a",
                               "android.intent.action.SENDTO", "-d",
                               f"sms:{number}", "--es", "sms_body", body],
                              timeout=15).ok, safe=safe)

    # ------------------------------------------------------------------ #
    # Properties / device info
    # ------------------------------------------------------------------ #
    def getprop(self, name: Optional[str] = None, *, safe: Optional[bool] = None):
        """Return one property's value, or a ``{name: value}`` dict of all of
        them when *name* is omitted."""
        def _do():
            if name:
                return self._run(["shell", "getprop", name], timeout=15).text
            res = self._run(["shell", "getprop"], timeout=20)
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
            p = self.getprop(safe=False)        # always the raw dict, even in safe mode
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
            return info
        return self._guard("device_info", _do, safe=safe)

    def is_automotive(self, *, safe: Optional[bool] = None):
        """True if the target is Android Automotive OS (has the automotive
        hardware feature). Useful before driving IVI-specific flows."""
        def _do():
            res = self._run(
                ["shell", "pm", "has-feature",
                 "android.hardware.type.automotive"], timeout=15, check=False)
            if res.text.strip().lower() in ("true", "false"):
                return res.text.strip().lower() == "true"
            # fall back to build characteristics
            ch = self._run(["shell", "getprop", "ro.build.characteristics"],
                           timeout=15).text
            return "automotive" in ch
        return self._guard("is_automotive", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Shell — one-shot and interactive
    # ------------------------------------------------------------------ #
    def shell(self, command: str, *, timeout: Optional[float] = None,
              check: bool = False, su: bool = False, safe: Optional[bool] = None):
        """Run a one-shot ``adb shell`` command. Returns CommandResult.

        *su=True* wraps the command in ``su -c`` for rooted devices.
        """
        def _do():
            cmd = command
            if su:
                cmd = f"su -c {shlex.quote(command)}"
            self._emit(logging.DEBUG, f"$ adb shell {cmd}")
            res = self._run(["shell", cmd], timeout=timeout, check=check)
            if res.ok:
                self._emit(logging.DEBUG, f"  -> ok ({res.duration:.2f}s)")
            else:
                detail = (res.stderr.strip() or res.text or "").splitlines()
                detail = " ".join(detail)[:300] if detail else "no output"
                self._emit(logging.WARNING,
                           f"  -> exit {res.exit_code} ({res.duration:.2f}s): {detail}")
            return res
        return self._guard("shell", _do, safe=safe)

    def shell_many(self, commands: Sequence[str], *, stop_on_error: bool = True,
                   **kwargs) -> list:
        results = []
        for cmd in commands:
            res = self._run(["shell", cmd], timeout=kwargs.get("timeout"))
            results.append(res)
            if stop_on_error and not res.ok:
                self._emit(logging.WARNING,
                           f"Stopping batch: {cmd!r} exited {res.exit_code}.")
                break
        return results

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
                args = ["shell", "-t", "-t"]   # force a PTY even over pipes
            proc = subprocess.Popen(
                self._base(target=True) + args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0, creationflags=NO_WINDOW)
            return ShellSession(proc, encoding=self.config.encoding)
        return self._guard("open_shell", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Streaming (logcat -f, live shell loops…)
    # ------------------------------------------------------------------ #
    def iter_lines(self, args: Sequence[str], *, timeout: Optional[float] = None,
                   stop_event=None, encoding: str = "utf-8"):
        """Run an adb command and yield its stdout **line by line, live**. Stop
        by breaking out, setting ``stop_event`` (a threading.Event), or after
        ``timeout`` seconds. The process is always terminated on exit."""
        cmd = self._base(target=True) + list(args)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=0,
                                creationflags=NO_WINDOW)
        self._emit(logging.INFO, f"$ adb {' '.join(args)}  (streaming)")
        start = time.time()
        buf = b""

        # A watcher thread terminates the process the instant a stop is requested
        # (or the timeout elapses) so a blocking read on a quiet stream — e.g.
        # idle logcat — unblocks immediately instead of waiting for the next line.
        watcher_done = threading.Event()

        def _watch():
            while not watcher_done.is_set():
                if stop_event is not None and stop_event.is_set():
                    break
                if timeout and (time.time() - start) > timeout:
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
                if timeout and (time.time() - start) > timeout:
                    break
                chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") \
                    else proc.stdout.read(4096)
                if chunk == b"":
                    if proc.poll() is not None:
                        break       # process ended (or was terminated by the watcher)
                    time.sleep(0.03)
                    continue
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()
                for ln in parts:
                    yield ln.decode(encoding, errors="replace").rstrip("\r")
            if buf:
                yield buf.decode(encoding, errors="replace").rstrip("\r")
        finally:
            watcher_done.set()
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

    def popen(self, args: Sequence[str]):
        """Spawn ``adb -s SERIAL <args>`` and return the raw Popen (stdout pipe,
        stderr merged). The caller owns it — read its stdout, and ``kill()`` +
        ``stdout.close()`` to stop a blocking read immediately. Used by the GUI
        logcat viewer for instant stop."""
        cmd = self._base(target=True) + list(args)
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=0,
                                creationflags=NO_WINDOW)

    def stream(self, args: Sequence[str], *, on_line=None, on_match=None,
               match=None, stop_on_match: bool = False,
               save_to: Optional[str] = None, append: bool = True,
               clean: bool = True, timeout: Optional[float] = None,
               stop_event=None, encoding: str = "utf-8",
               safe: Optional[bool] = None):
        """Consume a streaming adb command with built-in matching + file logging.
        Returns a :class:`StreamResult`. See :meth:`logcat` for the common case."""
        def _do():
            pat = re.compile(match) if isinstance(match, str) else match
            matches, count = [], 0
            fh = open(save_to, "a" if append else "w", encoding=encoding) \
                if save_to else None
            try:
                for line in self.iter_lines(args, timeout=timeout,
                                            stop_event=stop_event, encoding=encoding):
                    if clean:
                        line = strip_ansi(line)
                    count += 1
                    if fh:
                        fh.write(line + "\n")
                        fh.flush()
                    if on_line:
                        on_line(line)
                    if pat and pat.search(line):
                        matches.append(line)
                        if on_match:
                            on_match(line)
                        if stop_on_match:
                            break
                return StreamResult(count, matches, save_to)
            finally:
                if fh:
                    fh.close()
        return self._guard("stream", _do, safe=safe)

    def logcat(self, *, buffers: Optional[Sequence[str]] = None,
               fmt: str = "threadtime", tag: Optional[str] = None,
               priority: Optional[str] = None,
               filterspecs: Optional[Sequence[str]] = None,
               dump: bool = False, on_line=None, on_match=None, match=None,
               stop_on_match: bool = False, save_to: Optional[str] = None,
               append: bool = True, clean: bool = True,
               timeout: Optional[float] = None, stop_event=None,
               clear_first: bool = False, safe: Optional[bool] = None):
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
        :param match:      regex; matching lines collected + trigger on_match.
        :param clear_first: run ``logcat -c`` before streaming (fresh start).

        >>> dev.logcat(tag="ActivityManager", priority="I",
        ...            match=r"ANR|FATAL", on_line=print, save_to="boot.log")
        """
        if clear_first:
            self.logcat_clear()
        args = ["logcat"]
        if dump:
            args.append("-d")
        if fmt:
            args += ["-v", fmt]
        for b in (buffers or []):
            args += ["-b", b]
        if filterspecs:
            args += list(filterspecs)
        elif tag and priority:
            args += [f"{tag}:{priority}", "*:S"]
        elif tag:
            args += [f"{tag}:V", "*:S"]
        elif priority:
            args += [f"*:{priority}"]
        return self.stream(args, on_line=on_line, on_match=on_match, match=match,
                           stop_on_match=stop_on_match, save_to=save_to,
                           append=append, clean=clean, timeout=timeout,
                           stop_event=stop_event, safe=safe)

    def logcat_clear(self, *, safe: Optional[bool] = None):
        """Clear (flush) the logcat buffers (``adb logcat -c``)."""
        return self._guard("logcat_clear",
                           lambda: self._run(["logcat", "-c"], timeout=15).ok,
                           safe=safe)

    # ------------------------------------------------------------------ #
    # File transfer (push / pull) with progress
    # ------------------------------------------------------------------ #
    def push(self, local_path: str, remote_path: str, *, on_progress=None,
             safe: Optional[bool] = None):
        """Upload a file or directory to the device. Returns TransferResult.
        *on_progress* receives an int percent (0-100) as adb reports it."""
        return self._guard("push", self._transfer, "push", local_path,
                           remote_path, on_progress, safe=safe)

    def pull(self, remote_path: str, local_path: str, *, on_progress=None,
             safe: Optional[bool] = None):
        """Download a file or directory from the device. Returns TransferResult."""
        return self._guard("pull", self._transfer, "pull", remote_path,
                           local_path, on_progress, safe=safe)

    _PCT_RE = re.compile(r"\[\s*(\d+)%\]")

    def _transfer(self, direction, a, b, on_progress) -> TransferResult:
        # direction push: a=local, b=remote ; pull: a=remote, b=local
        if direction == "push":
            local, remote, args = a, b, ["push", os.path.expanduser(a), b]
            if not os.path.exists(os.path.expanduser(a)):
                raise ADBTransferError(f"Local path does not exist: {a}")
        else:
            local, remote, args = b, a, ["pull", a, os.path.expanduser(b)]
        cmd = self._base(target=True) + args
        self._emit(logging.INFO, f"$ adb {' '.join(args)}")
        start = time.time()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=1,
                                universal_newlines=True, creationflags=NO_WINDOW)
        last = -1
        tail = ""
        for line in proc.stdout:
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
        rc = proc.wait()
        if rc != 0:
            raise ADBTransferError(
                f"adb {direction} failed (exit {rc}): {tail or '(no output)'}")
        if on_progress:
            try:
                on_progress(100)
            except Exception:
                pass
        # size best-effort from the local side
        lp = os.path.expanduser(local)
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
        return TransferResult(src, dst, direction, size, time.time() - start, files)

    # ------------------------------------------------------------------ #
    # App management
    # ------------------------------------------------------------------ #
    def install(self, apk: str, *, replace: bool = True, downgrade: bool = False,
                grant_perms: bool = False, allow_test: bool = False,
                extra_args: Optional[Sequence[str]] = None,
                safe: Optional[bool] = None):
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
            if "Success" not in (res.stdout + res.stderr):
                raise ADBInstallError(
                    f"Install failed: {res.text or res.stderr.strip()}")
            return res.text or "Success"
        return self._guard("install", _do, safe=safe)

    def install_multiple(self, apks: Sequence[str], *, replace: bool = True,
                         grant_perms: bool = False,
                         safe: Optional[bool] = None):
        """Install split APKs together (``adb install-multiple``)."""
        def _do():
            args = ["install-multiple"]
            if replace:
                args.append("-r")
            if grant_perms:
                args.append("-g")
            args += [os.path.expanduser(a) for a in apks]
            self._emit(logging.INFO, f"Installing {len(apks)} split APK(s)…")
            res = self._run(args, timeout=600)
            if "Success" not in (res.stdout + res.stderr):
                raise ADBInstallError(
                    f"install-multiple failed: {res.text or res.stderr.strip()}")
            return res.text or "Success"
        return self._guard("install_multiple", _do, safe=safe)

    def uninstall(self, package: str, *, keep_data: bool = False,
                  safe: Optional[bool] = None):
        """Uninstall a package. *keep_data=True* keeps app data/cache (``-k``)."""
        def _do():
            args = ["uninstall"] + (["-k"] if keep_data else []) + [package]
            res = self._run(args, timeout=120)
            if "Success" not in (res.stdout + res.stderr):
                raise ADBInstallError(
                    f"Uninstall failed: {res.text or res.stderr.strip()}")
            return res.text or "Success"
        return self._guard("uninstall", _do, safe=safe)

    def list_packages(self, *, filter_text: Optional[str] = None,
                      third_party: bool = False, system: bool = False,
                      disabled: bool = False, enabled: bool = False,
                      include_path: bool = False, safe: Optional[bool] = None):
        """Return a list of installed package names (``pm list packages``)."""
        def _do():
            args = ["shell", "pm", "list", "packages"]
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
            res = self._run(args, timeout=30)
            pkgs = []
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("package:"):
                    pkgs.append(line[len("package:"):])
            return sorted(pkgs)
        return self._guard("list_packages", _do, safe=safe)

    def clear_app(self, package: str, *, safe: Optional[bool] = None):
        """Clear an app's data and cache (``pm clear``)."""
        return self._guard("clear_app",
                           lambda: self._run(["shell", "pm", "clear", package],
                                             timeout=60).text, safe=safe)

    def start_app(self, package: str, *, safe: Optional[bool] = None):
        """Launch an app by package — resolves its launcher activity and starts
        it explicitly, so it works even on automotive/IVI where ``monkey`` is
        often blocked (falls back to monkey / a MAIN-LAUNCHER intent)."""
        return self._guard("start_app",
                           lambda: self._launch_package(package), safe=safe)

    def start_activity(self, component: str, *, action: Optional[str] = None,
                       data: Optional[str] = None,
                       extras: Optional[Sequence[str]] = None,
                       safe: Optional[bool] = None):
        """Start an explicit activity/component (``am start -n pkg/.Activity``)."""
        def _do():
            args = ["shell", "am", "start", "-n", component]
            if action:
                args += ["-a", action]
            if data:
                args += ["-d", data]
            args += list(extras or [])
            return self._run(args, timeout=30).text
        return self._guard("start_activity", _do, safe=safe)

    def stop_app(self, package: str, *, safe: Optional[bool] = None):
        """Force-stop an app (``am force-stop``)."""
        return self._guard("stop_app",
                           lambda: self._run(["shell", "am", "force-stop", package],
                                             timeout=30).ok, safe=safe)

    def grant(self, package: str, permission: str, *, safe: Optional[bool] = None):
        """Grant a runtime permission (``pm grant``)."""
        return self._guard("grant",
                           lambda: self._run(["shell", "pm", "grant", package,
                                              permission], timeout=30, check=True).ok,
                           safe=safe)

    def revoke(self, package: str, permission: str, *, safe: Optional[bool] = None):
        """Revoke a runtime permission (``pm revoke``)."""
        return self._guard("revoke",
                           lambda: self._run(["shell", "pm", "revoke", package,
                                              permission], timeout=30, check=True).ok,
                           safe=safe)

    def current_activity(self, *, safe: Optional[bool] = None):
        """Best-effort current foreground activity (handy on IVI to see what's up)."""
        def _do():
            res = self._run(["shell", "dumpsys", "activity", "activities"],
                            timeout=30)
            for line in res.stdout.splitlines():
                if "mResumedActivity" in line or "ResumedActivity" in line:
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

    def capture_png(self, *, safe: Optional[bool] = None) -> bytes:
        """Capture the screen as PNG bytes, trying several methods so it works on
        locked-down / automotive devices and over remote adb servers:

        1. ``exec-out screencap -p`` (binary-safe, fastest)
        2. ``shell screencap -p`` with CRLF de-translation (some servers/devices
           mangle the binary stream)
        3. write a PNG on the device, then read it back with ``exec-out cat``

        Raises :class:`ADBError` with the actual device output if none yield a
        valid PNG (e.g. a secure/automotive surface that blocks screencap)."""
        def _do():
            # 1) exec-out (no translation)
            r = self._run(["exec-out", "screencap", "-p"], timeout=60,
                          binary=True, check=False)
            d = self._bytes(r.stdout)
            if d[:4] == b"\x89PNG":
                return bytes(d)
            # 2) shell + undo CRLF translation
            r2 = self._run(["shell", "screencap", "-p"], timeout=60,
                           binary=True, check=False)
            d2 = self._bytes(r2.stdout).replace(b"\r\n", b"\n")
            if d2[:4] == b"\x89PNG":
                return bytes(d2)
            # 3) write on device, read back
            remote = "/data/local/tmp/_turboadb_live.png"
            self._run(["shell", "screencap", "-p", remote], timeout=60, check=False)
            r3 = self._run(["exec-out", "cat", remote], timeout=60,
                           binary=True, check=False)
            d3 = self._bytes(r3.stdout)
            self._run(["shell", "rm", "-f", remote], timeout=15, check=False)
            if d3[:4] == b"\x89PNG":
                return bytes(d3)
            # nothing worked — report exactly what the device gave us
            errtxt = (r.stderr or r2.stderr or "").strip()
            raise ADBError(
                "screencap did not produce a PNG on this device "
                f"(exec-out: {len(d)} bytes starting {bytes(d[:8])!r}; "
                f"shell: {len(d2)} bytes; file: {len(d3)} bytes; "
                f"stderr: {errtxt[:160] or 'none'}). The screen may be a "
                "secure/automotive surface that blocks screen capture.")
        return self._guard("capture_png", _do, safe=safe)

    def screenshot(self, local_path: Optional[str] = None, *,
                   safe: Optional[bool] = None):
        """Capture the screen as PNG (robust multi-method, see :meth:`capture_png`).
        Saves to *local_path* if given and returns the path; otherwise returns
        the raw PNG bytes."""
        def _do():
            data = self.capture_png(safe=False)
            if local_path:
                lp = os.path.expanduser(local_path)
                parent = os.path.dirname(lp)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(lp, "wb") as fh:
                    fh.write(data)
                self._emit(logging.INFO, f"Screenshot saved: {lp} "
                                         f"({len(data)} bytes)")
                return lp
            return data
        return self._guard("screenshot", _do, safe=safe)

    def screen_record(self, local_path: str, *, time_limit: int = 180,
                      size: Optional[str] = None, bit_rate: Optional[str] = None,
                      remote_tmp: str = "/sdcard/turboadb_rec.mp4",
                      stop_event=None, safe: Optional[bool] = None):
        """Record the screen on-device (``screenrecord``), then pull it to
        *local_path*. Stops at *time_limit* seconds (max 180 per adb) or when
        *stop_event* is set. *size* like ``1280x720``, *bit_rate* like ``8M``."""
        def _do():
            args = ["shell", "screenrecord"]
            if time_limit:
                args += ["--time-limit", str(time_limit)]
            if size:
                args += ["--size", size]
            if bit_rate:
                args += ["--bit-rate", bit_rate]
            args.append(remote_tmp)
            cmd = self._base(target=True) + args
            self._emit(logging.INFO, f"Recording screen -> {remote_tmp} "
                                     f"(limit {time_limit}s)…")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    creationflags=NO_WINDOW)
            start = time.time()
            while proc.poll() is None:
                if stop_event is not None and stop_event.is_set():
                    proc.terminate()        # SIGINT finalizes the mp4 cleanly
                    break
                if time.time() - start > (time_limit + 5):
                    break
                time.sleep(0.2)
            # give the device a moment to flush the file
            time.sleep(1.5)
            self._transfer("pull", remote_tmp, local_path, None)
            self._run(["shell", "rm", "-f", remote_tmp], check=False, timeout=15)
            self._emit(logging.INFO, f"Recording saved: {local_path}")
            return local_path
        return self._guard("screen_record", _do, safe=safe)

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
            res = self._run(["forward", local, remote], timeout=15, check=True)
            self._emit(logging.INFO, f"forward {local} -> {remote}")
            return ForwardHandle(self, "forward", local, remote)
        return self._guard("forward", _do, safe=safe)

    def reverse(self, remote: str, local: str, *, safe: Optional[bool] = None):
        """``adb reverse`` — expose a host socket on the device. Returns a
        stoppable ForwardHandle.

        >>> rev = dev.reverse("tcp:8000", "tcp:8000")   # device reaches your PC
        """
        def _do():
            res = self._run(["reverse", remote, local], timeout=15, check=True)
            self._emit(logging.INFO, f"reverse {remote} <- {local}")
            return ForwardHandle(self, "reverse", local, remote)
        return self._guard("reverse", _do, safe=safe)

    def list_forwards(self, *, safe: Optional[bool] = None):
        return self._guard("list_forwards",
                           lambda: self._run(["forward", "--list"], timeout=15).lines,
                           safe=safe)

    def remove_all_forwards(self, *, safe: Optional[bool] = None):
        return self._guard("remove_all_forwards",
                           lambda: self._run(["forward", "--remove-all"],
                                             timeout=15).ok, safe=safe)

    # ------------------------------------------------------------------ #
    # scrcpy — visual mirroring/control session
    # ------------------------------------------------------------------ #
    def list_displays(self, *, safe: Optional[bool] = None):
        """Enumerate the device's displays (id + size) via scrcpy — use the id
        with ``mirror(display_id=...)`` to mirror a specific IVI display."""
        from .scrcpy import list_displays as _ld
        return self._guard(
            "list_displays",
            lambda: _ld(self._serial, scrcpy_path=self.config.scrcpy_path,
                        adb_server_host=self.config.adb_server_host,
                        adb_server_port=self.config.adb_server_port,
                        adb_path=self.adb_path),
            safe=safe)

    def mirror(self, options: Optional[ScrcpyOptions] = None, *,
               compat: bool = False, log_path: Optional[str] = None,
               safe: Optional[bool] = None, **kwargs):
        """Launch scrcpy to mirror & control this device. Extra keyword args are
        forwarded to :class:`ScrcpyOptions` (e.g. ``max_size=1024, bit_rate="8M",
        display_id=2, record="drive.mp4"``). Returns a ScrcpySession handle.

        *compat=True* applies an Android-Automotive/IVI **compatibility profile**
        for head units whose encoders choke on scrcpy's defaults: forces the
        widely-supported H.264 codec, caps the size/fps, and disables audio. Try
        it first when "scrcpy won't work" on a head unit.
        """
        from .scrcpy import launch_scrcpy

        def _do():
            opts = options or ScrcpyOptions(**kwargs)
            if compat:
                opts.video_codec = opts.video_codec or "h264"
                opts.max_size = opts.max_size or 1280
                opts.max_fps = opts.max_fps or 30
                opts.no_audio = True
                # IVIs/head units commonly block `adb reverse`; a forward tunnel
                # is what lets scrcpy connect to its server on them
                opts.force_adb_forward = True
            adb = self.adb_path     # pin scrcpy to OUR adb (avoids server clashes)
            # Preflight: make sure OUR adb server is up and the device is visible
            # to it BEFORE scrcpy runs. Otherwise scrcpy tries to (re)start a
            # server itself and, if a different-version one is around (common over
            # RDP), dies with "adb start-server exited unexpectedly".
            try:
                self._run_global(["start-server"], check=False, timeout=30)
                state = self._run(["get-state"], timeout=10, check=False)
                self._emit(logging.INFO,
                           f"scrcpy preflight: adb={adb}  device "
                           f"{self._serial or '(only)'} state="
                           f"{state.text.strip() or state.stderr.strip() or '?'}")
            except Exception as exc:
                self._emit(logging.WARNING, f"scrcpy preflight warning: {exc}")
            # If the "remote" adb server is actually THIS machine (device is local,
            # just addressed by its LAN IP), run scrcpy LOCALLY — no network video
            # tunnel — which is exactly how running scrcpy directly there works.
            from .scrcpy import is_local_host, resolve_host, TUNNEL_PORT
            server_host = self.config.adb_server_host
            if server_host and is_local_host(server_host):
                self._emit(logging.INFO,
                           f"adb server {server_host} is THIS machine → running "
                           f"scrcpy locally (no network tunnel)")
                server_host = None
            elif server_host:
                ip = resolve_host(server_host)
                self._emit(logging.INFO,
                           f"scrcpy will stream the device's video over the "
                           f"network from {ip} via tunnel port TCP {TUNNEL_PORT} — "
                           f"that port must be open in {ip}'s firewall")
            self._emit(logging.INFO, f"Launching scrcpy for "
                                     f"{self._serial or 'device'}"
                                     f"{' (compat mode)' if compat else ''}…")
            return launch_scrcpy(self._serial, opts,
                                 scrcpy_path=self.config.scrcpy_path,
                                 adb_server_host=server_host,
                                 adb_server_port=self.config.adb_server_port,
                                 log_path=log_path, adb_path=adb)
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
        self.disconnect()

    def __repr__(self) -> str:
        state = "connected" if self._connected else "idle"
        return f"<ADBHandler target={self._serial!r} {state}>"


# A friendly alias: think of one handler as one device you drive end-to-end.
ADBDevice = ADBHandler
