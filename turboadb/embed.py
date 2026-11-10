"""Put TurboADB inside your own program's window, and drive it.

    from turboadb.embed import launch

    tadb = launch(parent=int(my_widget.winId()))   # TurboADB fills that window
    tadb.devices()                                 # what adb reports
    tadb.open("R58M12345", page="files")           # a device tab, on its Files page
    tadb.call("R58M12345", "shell", "getprop ro.product.model")
    for event in tadb.events():                    # devices, tabs, pages, screens, log
        print(event["type"], event)
    tadb.quit()

TurboADB runs as a process of its own inside the window you give it, so it
never mixes with your program's toolkit: PyQt5, PySide6, Tk, wx or no GUI at
all.  Only the standard library is used here.  :mod:`turboadb.embed_qt` wraps
this as a Qt widget, and programs in other languages (Delphi, C#, …) speak
the same HTTP API: see EMBEDDING.md.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Iterator, Optional, Sequence

TOKEN_ENV = "TURBOADB_API_TOKEN"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


class TurboADBError(RuntimeError):
    """TurboADB refused a request, or could not be reached or started.
    :attr:`status` is the HTTP status (0 when there was no answer)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Client:
    """The control API of a TurboADB that runs (see :func:`launch`)."""

    def __init__(self, port: int, token: str, *, host: str = "127.0.0.1", timeout: float = 30.0):
        self.base = f"http://{host}:{int(port)}/v1"
        self.token = token
        self.timeout = timeout
        # no proxy: the API is on this PC, and an office proxy would refuse it
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    def request(self, method: str, path: str, body: Optional[dict] = None,
                timeout: Optional[float] = None):
        """Send one request; returns its ``result``, raises TurboADBError."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._open(req, timeout=self.timeout if timeout is None else timeout) as resp:
                answer = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                answer = json.loads(exc.read().decode("utf-8"))
            except Exception:
                raise TurboADBError(f"{method} {path}: HTTP {exc.code}", exc.code) from None
            raise TurboADBError(answer.get("error") or f"HTTP {exc.code}", exc.code) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TurboADBError(f"TurboADB did not answer {method} {path}: {exc}") from None
        if not answer.get("ok"):
            raise TurboADBError(answer.get("error") or "the request failed")
        return answer.get("result")

    # ---- what TurboADB shows ----
    def info(self) -> dict:
        return self.request("GET", "/info")

    def devices(self) -> list:
        return self.request("GET", "/devices")

    def tabs(self) -> list:
        return self.request("GET", "/tabs")

    def open(self, serial: Optional[str] = None, *, page: Optional[str] = None,
             host: Optional[str] = None, port: int = 5555) -> dict:
        """Open a device tab (or bring the open one up): a connected device by
        *serial*, or a network device by *host* and *port*; *page* is one of
        terminal, logcat, files, controls, apps, phone, webcam, displays."""
        body = {"serial": serial} if serial else {"host": host, "port": port}
        if page:
            body["page"] = page
        return self.request("POST", "/open", body)

    def show(self, serial: Optional[str] = None, page: Optional[str] = None, *,
             tab: Optional[int] = None) -> dict:
        """Bring a device tab up, on *page* when given."""
        body = {"tab": tab} if tab is not None else {"serial": serial}
        if page:
            body["page"] = page
        return self.request("POST", "/show", body)

    def close_tab(self, serial: Optional[str] = None, *, tab: Optional[int] = None) -> dict:
        return self.request("POST", "/close", {"tab": tab} if tab is not None else {"serial": serial})

    def screen(self, serial: str, start: bool = True, *, display_id: Optional[int] = None) -> dict:
        """Start (or stop) the device's screen in its Device Control page."""
        body = {"serial": serial, "action": "start" if start else "stop"}
        if display_id is not None:
            body["display_id"] = int(display_id)
        return self.request("POST", "/screen", body)

    def ui(self) -> dict:
        return self.request("GET", "/ui")

    def set_ui(self, **parts) -> dict:
        """Show or hide parts of the window (menubar, ribbon, sidebar,
        statusbar, log), the Webcam page (``webcam_page``), or set ``theme``."""
        return self.request("POST", "/ui", parts)

    def focus(self) -> dict:
        return self.request("POST", "/focus", {})

    def quit(self) -> dict:
        return self.request("POST", "/quit", {})

    # ---- the device itself ----
    def call(self, serial: str, method: str, *args, timeout: Optional[float] = None, **kwargs):
        """Run one device command (an ``ADBHandler`` method TurboADB allows,
        listed by ``info()["device_calls"]``) and return its result:
        ``{"success", "value", "error", …}``."""
        body = {"serial": serial, "method": method, "args": list(args), "kwargs": kwargs}
        return self.request("POST", "/device", body, timeout=timeout or max(self.timeout, 600.0))

    def shell(self, serial: str, command: str, *, timeout: Optional[float] = None) -> str:
        """The output of an ``adb shell`` *command*; TurboADBError when it failed."""
        kwargs = {} if timeout is None else {"timeout": timeout}
        result = self.call(serial, "shell", command, **kwargs)
        if not result.get("success"):
            raise TurboADBError(str(result.get("error") or "the command failed"))
        value = result.get("value") or {}
        return value.get("stdout", "") if isinstance(value, dict) else str(value)

    # ---- what happens ----
    def poll_events(self, after: int = 0, wait: float = 20.0):
        """``(events after *after*, the newest id)``, waiting up to *wait* s."""
        found = self.request("GET", f"/events?after={int(after)}&wait={float(wait):g}",
                             timeout=float(wait) + 15.0)
        return found["events"], found["last"]

    def events(self, after: Optional[int] = None, wait: float = 20.0) -> Iterator[dict]:
        """Every event from now on (from *after* when given), as it happens.
        Ends when TurboADB has closed."""
        if after is None:
            after = self.info()["events"]
        while True:
            try:
                found, after = self.poll_events(after, wait)
            except TurboADBError:
                return
            for event in found:
                yield event
                if event.get("type") == "closing":
                    return


class Embedded(Client):
    """A TurboADB this program started (see :func:`launch`)."""

    def __init__(self, port: int, token: str, *, pid: int, window: int, parent: Optional[int],
                 process: Optional[subprocess.Popen] = None, timeout: float = 30.0):
        super().__init__(port, token, timeout=timeout)
        self.pid = pid
        self.window = window  # TurboADB's own window handle
        self.parent = parent
        self.process = process

    def running(self) -> bool:
        try:
            self.request("GET", "/info", timeout=3.0)
            return True
        except TurboADBError:
            return False

    def close(self, timeout: float = 15.0) -> None:
        """Close TurboADB as File > Exit does, and wait for it to go."""
        try:
            self.quit()
        except TurboADBError:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.running():
            time.sleep(0.2)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def command(exe: Optional[str] = None) -> list:
    """What starts TurboADB: *exe* (``TurboADB-x.y.z-win64.exe``), else the
    TurboADB of this Python, with no console window."""
    if exe:
        return [exe]
    python = sys.executable
    if os.name == "nt":
        windowless = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.isfile(windowless):
            python = windowless
    return [python, "-m", "turboadb.gui.app"]


def launch(parent: Optional[int] = None, *, exe: Optional[str] = None, hide: Sequence[str] = (),
           api_port: int = 0, timeout: float = 90.0, extra_args: Sequence[str] = ()) -> Embedded:
    """Start TurboADB inside the window *parent* (a window handle: ``int(
    widget.winId())`` in Qt), or as a window of its own when None, and wait
    until its control API answers.

    *hide* leaves out parts of its window (menubar, ribbon, sidebar,
    statusbar, log).  Raises TurboADBError when it did not start within
    *timeout* seconds (its first start may fetch adb and scrcpy)."""
    token = secrets.token_urlsafe(32)
    folder = tempfile.mkdtemp(prefix="turboadb-embed-")
    handshake = os.path.join(folder, "handshake.json")
    args = command(exe) + ["--api-port", str(int(api_port)), "--handshake-file", handshake]
    if parent is not None:
        args += ["--embed-into", str(int(parent))]
    if hide:
        args += ["--hide", ",".join(hide)]
    args += [str(a) for a in extra_args]
    env = dict(os.environ, **{TOKEN_ENV: token})
    try:
        process = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=_NO_WINDOW)
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise TurboADBError(f"TurboADB could not be started ({args[0]}): {exc}") from None
    try:
        found = _wait_for_handshake(handshake, process, timeout)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if found.get("error"):
        raise TurboADBError(f"TurboADB did not start: {found['error']}")
    if found.get("token") != token:
        raise TurboADBError("TurboADB answered with another token")
    return Embedded(found["port"], token, pid=found.get("pid", process.pid),
                    window=found.get("window", 0), parent=parent, process=process)


def _wait_for_handshake(path: str, process: subprocess.Popen, timeout: float) -> dict:
    """The handshake TurboADB writes once its window is up."""
    deadline = time.monotonic() + timeout
    ended = None
    while time.monotonic() < deadline:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass  # (being replaced this instant: read it again)
        code = process.poll()
        if code not in (None, 0):
            raise TurboADBError(f"TurboADB ended with exit code {code} before its window was up")
        if code == 0:
            # A launcher that hands over to another process ends with 0 at
            # once; a TurboADB that ended with 0 and wrote nothing is gone.
            ended = ended or time.monotonic()
            if time.monotonic() - ended > 5.0:
                raise TurboADBError("TurboADB closed before its window was up")
        time.sleep(0.1)
    raise TurboADBError(f"TurboADB was not up within {timeout:g} s")
