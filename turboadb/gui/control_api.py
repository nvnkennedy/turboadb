"""The control API: another program drives TurboADB, on this PC.

HTTP with JSON, on 127.0.0.1 only, so any language can use it (Python,
Delphi, C#, a shell with curl).  Every request carries the token:
``Authorization: Bearer <token>``.  Answers are ``{"ok": true, "result": …}``
or ``{"ok": false, "error": "…"}`` with a matching HTTP status.

    GET  /v1/info                  this TurboADB: version, window, what it offers
    GET  /v1/devices               the devices adb reports, and which are open
    GET  /v1/tabs                  the open tabs, and the page each shows
    GET  /v1/ui                    which parts of the window show
    GET  /v1/events?after=N&wait=S what happened since event N (waits up to S s)
    POST /v1/open                  {"serial"} or {"host", "port"}, optional "page"
    POST /v1/show                  {"serial" or "index", "page"}: bring a tab and page up
    POST /v1/close                 {"serial" or "index"}: close a tab
    POST /v1/screen                {"serial", "action": "start"|"stop", "display_id"}
    POST /v1/device                {"serial", "method", "args", "kwargs"}: a device command
    POST /v1/ui                    {"menubar", "ribbon", "sidebar", "statusbar", "log",
                                    "webcam_page": true/false, "theme": name}
    POST /v1/focus                 give TurboADB the keyboard
    POST /v1/quit                  close TurboADB

Requests that touch the window run on its thread; device commands run on the
request's own thread, so a long ``shell`` never holds the window up.  A
browser can't use the API: a request with an ``Origin`` header is refused, and
it never knows the token.  EMBEDDING.md has the details and examples.
"""

from __future__ import annotations

import base64
import dataclasses
import hmac
import inspect
import json
import os
import secrets
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal

from .. import __version__
from . import embedding

API_VERSION = 1
_MAX_BODY = 1 << 20  # a request is a few hundred bytes; never read a flood
_WAIT_MAX_S = 60.0  # the longest /v1/events waits for something to happen

# The pages of a device tab, by the names the API gives them, and DeviceTab's own.
PAGES = {
    "terminal": "shell", "logcat": "logcat", "files": "files", "controls": "controls",
    "screen": "controls", "apps": "apps", "phone": "phone", "webcam": "webcam",
    "displays": "ivi",
}

# Device commands the API runs (ADBHandler methods), each a request that ends.
# Streaming ones (logcat, an interactive shell, a mirror) are left out: they
# belong in the window, where they have a page.
DEVICE_CALLS = frozenset((
    "shell", "shell_many", "getprop", "get_state", "get_serialno", "device_info",
    "build_info", "battery", "battery_status", "health", "is_automotive", "quick_identity",
    "keyevent", "keyevents", "input_text", "tap", "swipe", "scroll", "tap_center",
    "screen_on", "screen_off", "media", "expand_notifications", "collapse_notifications",
    "get_brightness", "set_brightness", "open_url", "open_settings",
    "set_wifi", "set_bluetooth", "set_airplane",
    "reboot", "root", "unroot", "remount", "wait_for_device", "wait_for_boot",
    "list_dir", "make_dir", "remove", "move", "copy", "stat_path", "chmod",
    "push", "pull", "install", "install_multiple", "uninstall", "list_packages",
    "clear_app", "start_app", "start_activity", "stop_app", "grant", "revoke",
    "current_activity", "screenshot", "list_displays",
    "forward", "reverse", "list_forwards", "remove_forward",
    "dial", "end_call", "call_state", "call_log", "sms_list", "send_sms",
))


class ApiError(Exception):
    """A request the API answers with *status* and *message*."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def jsonable(value, depth: int = 0):
    """*value* as JSON can carry it: results and records as objects, bytes
    as ``{"base64": …}``, anything else as its text."""
    if depth > 8:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, depth + 1) for v in value]
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {value}"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name), depth + 1)
                for f in dataclasses.fields(value)}
    if hasattr(value, "_asdict"):
        return jsonable(value._asdict(), depth + 1)
    return str(value)


class EventLog:
    """What happened, numbered, for ``GET /v1/events``: the last *keep* events,
    and a wait for the next one."""

    def __init__(self, keep: int = 1000):
        self._items = deque(maxlen=keep)
        self._last = 0
        self._closed = False
        self._cond = threading.Condition()

    @property
    def last(self) -> int:
        return self._last

    def publish(self, event_type: str, /, **data) -> None:
        with self._cond:
            self._last += 1
            self._items.append({**data, "id": self._last, "time": round(time.time(), 3),
                                "type": event_type})
            self._cond.notify_all()

    def after(self, last_id: int, wait: float = 0.0):
        """``(events after *last_id*, the newest id)``; waits up to *wait*
        seconds for one when there is none yet."""
        deadline = time.monotonic() + max(0.0, min(float(wait), _WAIT_MAX_S))
        with self._cond:
            while True:
                found = [e for e in self._items if e["id"] > last_id]
                left = deadline - time.monotonic()
                if found or left <= 0 or self._closed:
                    return found, self._last
                self._cond.wait(left)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows lets a second server take a port in use with SO_REUSEADDR: the
    # API's port must be its own
    allow_reuse_address = False
    api = None


class _Handler(BaseHTTPRequestHandler):
    server_version = "TurboADB"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # (TurboADB's log has what matters)
        pass

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def do_PUT(self):
        self._serve("PUT")

    def do_DELETE(self):
        self._serve("DELETE")

    def _serve(self, method: str) -> None:
        api = self.server.api
        try:
            if self.headers.get("Origin"):
                raise ApiError(403, "the control API is not for web pages")
            if not api.authorized(self.headers.get("Authorization", "")):
                raise ApiError(401, "the token is missing or wrong")
            url = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(url.query))
            body = self._body() if method == "POST" else {}
            result = api.dispatch(method, url.path.rstrip("/") or "/", body, query)
            self._reply(200, {"ok": True, "result": result})
        except ApiError as exc:
            self._reply(exc.status, {"ok": False, "error": exc.message})
        except Exception as exc:  # a bug, or a device command that raised
            self._reply(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length is not a number")
        if length > _MAX_BODY:
            raise ApiError(413, "the request is too large")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "the body is not JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "the body must be a JSON object")
        return data

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # the client went away


def _text(body: dict, key: str, *, required: bool = True) -> str:
    value = body.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ApiError(400, f'"{key}" is missing')
        return ""
    if not isinstance(value, (str, int)):
        raise ApiError(400, f'"{key}" must be text')
    return str(value).strip()


class ControlApi(QObject):
    """The control API of one TurboADB window (see the module's notes)."""

    _call = pyqtSignal(object)
    UI_TIMEOUT_S = 30.0

    def __init__(self, window, *, port: int = 0, token: str | None = None, parent=None):
        super().__init__(parent)
        self.window = window
        self.token = token or secrets.token_urlsafe(32)
        self.events = EventLog()
        self._call.connect(self._run_call, Qt.QueuedConnection)
        self._server = _Server(("127.0.0.1", int(port)), _Handler)
        self._server.api = self
        self._thread = None
        self._tab_ids = {}  # tab widget -> its number in the API
        self._next_tab = 1
        self._seen = {"devices": {}, "tabs": {}, "current": None, "pages": {}}
        self._watch = QTimer(self)
        self._watch.setInterval(400)
        self._watch.timeout.connect(self._notice)
        log = getattr(getattr(window, "log_panel", None), "line_added", None)
        if log is not None:
            log.connect(lambda text: self.events.publish("log", text=text))

    # ---- life ----
    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="turboadb-control-api", daemon=True)
        self._thread.start()
        self._notice()
        self._watch.start()
        self.events.publish("ready", version=__version__)

    def stop(self) -> None:
        self._watch.stop()
        self.events.publish("closing")
        self.events.close()
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass

    def handshake(self) -> dict:
        """What the handshake file holds: how a host reaches this API."""
        return {"api": self.url, "port": self.port, "token": self.token, "pid": os.getpid(),
                "version": __version__, "api_version": API_VERSION,
                "window": self._window_handle(), "embedded": embedding.is_embedded()}

    def authorized(self, header: str) -> bool:
        scheme, _sep, value = str(header or "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            value.strip().encode("utf-8"), self.token.encode("utf-8"))

    # ---- the window's thread ----
    def on_ui(self, fn, timeout: float | None = None):
        """Run *fn* on the window's thread and return what it returns."""
        if threading.current_thread() is threading.main_thread():
            return fn()
        box = {"done": threading.Event()}
        self._call.emit((fn, box))
        if not box["done"].wait(self.UI_TIMEOUT_S if timeout is None else timeout):
            raise ApiError(504, "TurboADB's window did not answer in time (a dialog may be open)")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _run_call(self, item) -> None:
        fn, box = item
        try:
            box["result"] = fn()
        except BaseException as exc:  # handed to the request's thread
            box["error"] = exc
        finally:
            box["done"].set()

    # ---- routes ----
    def dispatch(self, method: str, path: str, body: dict, query: dict):
        routes = {
            ("GET", "/v1/info"): lambda: self.on_ui(self._info),
            ("GET", "/v1/devices"): lambda: self.on_ui(self._devices),
            ("GET", "/v1/tabs"): lambda: self.on_ui(self._tabs),
            ("GET", "/v1/ui"): lambda: self.on_ui(self._ui),
            ("GET", "/v1/events"): lambda: self._events(query),
            ("POST", "/v1/open"): lambda: self.on_ui(lambda: self._open(body)),
            ("POST", "/v1/show"): lambda: self.on_ui(lambda: self._show(body)),
            ("POST", "/v1/close"): lambda: self.on_ui(lambda: self._close(body)),
            ("POST", "/v1/screen"): lambda: self.on_ui(lambda: self._screen(body)),
            ("POST", "/v1/ui"): lambda: self.on_ui(lambda: self._set_ui(body)),
            ("POST", "/v1/device"): lambda: self._device(body),
            ("POST", "/v1/focus"): lambda: self.on_ui(self._focus),
            ("POST", "/v1/quit"): lambda: self.on_ui(self._quit),
        }
        route = routes.get((method, path))
        if route is None:
            if any(p == path for _m, p in routes):
                raise ApiError(405, f"{path} does not take {method}")
            raise ApiError(404, f"no such request: {method} {path}")
        return route()

    def _window_handle(self) -> int:
        try:
            return int(self.window.winId())
        except Exception:
            return 0

    def _info(self) -> dict:
        options = embedding.active()
        return {
            "version": __version__, "api_version": API_VERSION, "pid": os.getpid(),
            "window": self._window_handle(), "embedded": options.embedded,
            "parent": options.parent, "pages": sorted(PAGES), "ui_parts": list(embedding.HIDE_PARTS),
            "device_calls": sorted(DEVICE_CALLS), "events": self.events.last,
        }

    # devices and tabs
    def _device_tabs(self):
        from .device_tab import DeviceTab

        tabs = self.window.tabs
        return [(i, tabs.widget(i)) for i in range(tabs.count())
                if isinstance(tabs.widget(i), DeviceTab)]

    @staticmethod
    def tab_serial(tab) -> str:
        """The serial a device tab works with (``host:port`` for a network one)."""
        handler = getattr(tab, "handler", None)
        config = getattr(handler, "config", None)
        target = getattr(config, "target", None)
        if target:
            return str(target)
        session = getattr(tab, "session", None) or {}
        if session.get("serial"):
            return str(session["serial"])
        if session.get("host"):
            return f"{session['host']}:{session.get('port') or 5555}"
        return ""

    def _tab_id(self, tab) -> int:
        if tab not in self._tab_ids:
            self._tab_ids[tab] = self._next_tab
            self._next_tab += 1
            screen = getattr(tab, "screen_active", None)
            if screen is not None:
                screen.connect(lambda active, t=tab: self.events.publish(
                    "screen", tab=self._tab_ids.get(t), serial=self.tab_serial(t),
                    active=bool(active)))
        return self._tab_ids[tab]

    @staticmethod
    def page_of(tab) -> str:
        """The API's name of the page *tab* shows ("" when it can't say)."""
        subtabs = getattr(tab, "_subtabs", None) or {}
        inner = getattr(tab, "inner", None)
        current = inner.currentWidget() if inner is not None else None
        if current is None:
            return ""
        for name, key in PAGES.items():
            if name != "screen" and subtabs.get(key) is current:
                return name
        return ""

    def _tab_record(self, index, tab) -> dict:
        from .device_tab import DeviceTab

        window = self.window
        if isinstance(tab, DeviceTab):
            kind = "terminal" if getattr(tab, "_terminal_only", False) else "device"
        elif tab is getattr(window, "_webcam_tab", None):
            kind = "webcam"
        else:
            kind = "other"
        return {
            "tab": self._tab_id(tab), "index": index, "title": window.tabs.tabText(index),
            "kind": kind, "serial": self.tab_serial(tab) if kind in ("device", "terminal") else "",
            "page": self.page_of(tab) if kind == "device" else "",
            "connected": bool(getattr(tab, "handler", None)) if kind != "other" else False,
            "current": index == window.tabs.currentIndex(),
        }

    def _tabs(self) -> list:
        tabs = self.window.tabs
        return [self._tab_record(i, tabs.widget(i)) for i in range(tabs.count())]

    def _devices(self) -> list:
        open_serials = {self.tab_serial(tab) for _i, tab in self._device_tabs()}
        return [{
            "serial": d.serial, "state": d.state, "online": d.is_online,
            "network": d.is_network, "model": d.model, "product": d.product,
            "device": d.device, "open": d.serial in open_serials,
        } for d in list(getattr(self.window, "_live_devices", None) or [])]

    def _find_tab(self, body: dict, *, full: bool = True):
        """``(index, tab)`` of the device tab *body* names by ``serial`` (or
        ``tab``, or ``index``); ApiError 404 when none is open."""
        tabs = self._device_tabs()
        if full:
            tabs = [(i, t) for i, t in tabs if not getattr(t, "_terminal_only", False)]
        if body.get("tab") is not None:
            for i, tab in tabs:
                if self._tab_ids.get(tab) == body.get("tab"):
                    return i, tab
            raise ApiError(404, f"no device tab {body.get('tab')}")
        if body.get("index") is not None:
            for i, tab in tabs:
                if i == body.get("index"):
                    return i, tab
            raise ApiError(404, f"no device tab at index {body.get('index')}")
        serial = _text(body, "serial")
        for i, tab in tabs:
            if self.tab_serial(tab) == serial:
                return i, tab
        raise ApiError(404, f"{serial} is not open (POST /v1/open opens it)")

    @staticmethod
    def _page_key(page) -> str:
        key = PAGES.get(str(page or "").strip().lower())
        if key is None:
            raise ApiError(400, f"no page called {page!r}: the pages are {', '.join(sorted(PAGES))}")
        return key

    def _show_page(self, tab, page: str) -> str:
        """Show *page* in *tab*, or once the tab has connected (its pages are
        made then).  The webcam page is there only when it is turned on."""
        key = self._page_key(page)
        if key == "webcam" and not getattr(tab, "has_webcam_page", lambda: True)():
            if "_subtabs" in tab.__dict__:
                raise ApiError(409, "the webcam page is off: POST /v1/ui {\"webcam_page\": true}")
        if "_subtabs" in tab.__dict__ and tab._subtabs.get(key) is not None:
            tab.show_subtab(key)
            return "shown"
        tab._api_page = key  # shown by _notice once the tab has its pages
        return "waiting for the device to connect"

    def _open(self, body: dict) -> dict:
        window = self.window
        page = body.get("page")
        if page is not None:
            self._page_key(page)  # checked before anything opens
        serial = _text(body, "serial", required=False)
        host = _text(body, "host", required=False)
        if not serial and not host:
            raise ApiError(400, 'give "serial" (a connected device) or "host" (and "port")')
        if not serial:
            from ..config import validate_port

            try:
                port = validate_port(body.get("port") or 5555)
            except ValueError as exc:
                raise ApiError(400, str(exc))
            serial = f"{host}:{port}"
        try:
            index, tab = self._find_tab({"serial": serial})
        except ApiError:
            index = None
        if index is None:
            live = {d.serial for d in getattr(window, "_live_devices", None) or []}
            if serial in live:
                window.open_live_device(serial)
            elif host:
                window._open_session({"name": host, "type": "network", "host": host,
                                      "port": int(serial.rsplit(":", 1)[1])}, host)
            else:
                raise ApiError(404, f"{serial} is not connected (GET /v1/devices lists what is)")
            index, tab = self._find_tab({"serial": serial})
        window.tabs.setCurrentIndex(index)
        shown = self._show_page(tab, page) if page is not None else ""
        return dict(self._tab_record(index, tab), page_state=shown)

    def _show(self, body: dict) -> dict:
        index, tab = self._find_tab(body)
        self.window.tabs.setCurrentIndex(index)
        shown = self._show_page(tab, body["page"]) if body.get("page") is not None else ""
        return dict(self._tab_record(index, tab), page_state=shown)

    def _close(self, body: dict) -> dict:
        index, tab = self._find_tab(body, full=False)
        self.window._close_tab(index)
        return {"closed": self._tab_ids.pop(tab, None)}

    def _screen(self, body: dict) -> dict:
        _index, tab = self._find_tab(body)
        action = _text(body, "action")
        panel = getattr(tab, "mirror_tab", None)
        if panel is None:
            raise ApiError(409, "the device has not connected yet")
        if action == "start":
            tab.show_subtab("controls")
            if body.get("display_id") is not None:
                panel.start(display_id=int(body["display_id"]))
            else:
                panel.start()
        elif action == "stop":
            panel.stop()
        else:
            raise ApiError(400, 'the action is "start" or "stop"')
        return {"action": action, "serial": self.tab_serial(tab)}

    # the window
    def _ui(self) -> dict:
        from . import settings as settings_mod, theme

        parts = self.window.ui_parts()
        parts["webcam_page"] = bool(settings_mod.get("show_webcam_page", False))
        parts["theme"] = theme.current_name()
        return parts

    def _set_ui(self, body: dict) -> dict:
        from . import theme

        parts = {k: body[k] for k in embedding.HIDE_PARTS if k in body}
        unknown = set(body) - set(parts) - {"webcam_page", "theme"}
        if unknown:
            raise ApiError(400, f"no part called {', '.join(sorted(unknown))}")
        for key, value in parts.items():
            if not isinstance(value, bool):
                raise ApiError(400, f'"{key}" must be true or false')
        if "theme" in body:
            name = str(body["theme"] or "")
            if name not in theme.THEMES:
                raise ApiError(400, f"no theme called {name!r}: {', '.join(theme.THEMES)}")
            self.window._apply_theme(name)
        if parts:
            self.window.set_ui_parts(**parts)
        if "webcam_page" in body:
            if not isinstance(body["webcam_page"], bool):
                raise ApiError(400, '"webcam_page" must be true or false')
            self.window.set_webcam_pages(body["webcam_page"])
        return self._ui()

    def _focus(self) -> dict:
        window = self.window
        window.activateWindow()
        window.setFocus()
        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.user32.SetFocus(self._window_handle())
            except Exception:
                pass
        return {"focused": True}

    def _quit(self) -> dict:
        from PyQt5.QtWidgets import QApplication

        def close():
            self.window.close()
            QApplication.instance().quit()

        QTimer.singleShot(0, close)  # after this answer has gone
        return {"quitting": True}

    # events
    def _events(self, query: dict) -> dict:
        try:
            after = int(query.get("after", 0))
            wait = float(query.get("wait", 0))
        except ValueError:
            raise ApiError(400, '"after" and "wait" are numbers')
        found, last = self.events.after(after, wait)
        return {"events": found, "last": last}

    def _notice(self) -> None:
        """Publish what changed since the last look (the window's thread)."""
        seen = self._seen
        devices = {d.serial: d.state for d in getattr(self.window, "_live_devices", None) or []}
        for serial, state in devices.items():
            if seen["devices"].get(serial) != state:
                self.events.publish("device", serial=serial, state=state)
        for serial in set(seen["devices"]) - set(devices):
            self.events.publish("device", serial=serial, state="gone")
        seen["devices"] = devices

        tabs = self.window.tabs
        now = {}
        for index in range(tabs.count()):
            tab = tabs.widget(index)
            now[self._tab_id(tab)] = tab
            pending = tab.__dict__.get("_api_page") if hasattr(tab, "__dict__") else None
            if pending and tab.__dict__.get("_subtabs") and tab._subtabs.get(pending) is not None:
                tab._api_page = None
                tab.show_subtab(pending)
        for number, tab in now.items():
            if number not in seen["tabs"]:
                record = self._tab_record(tabs.indexOf(tab), tab)
                self.events.publish("tab_opened", **record)
        for number in set(seen["tabs"]) - set(now):
            self.events.publish("tab_closed", tab=number)
            gone = seen["tabs"][number]
            self._tab_ids.pop(gone, None)
        seen["tabs"] = now
        current = tabs.currentWidget()
        if current is not None and seen["current"] is not current:
            seen["current"] = current
            self.events.publish("tab_selected", **self._tab_record(tabs.currentIndex(), current))
        pages = {}
        for number, tab in now.items():
            page = self.page_of(tab)
            pages[number] = page
            if page and seen["pages"].get(number) not in (None, page):
                self.events.publish("page", tab=number, serial=self.tab_serial(tab), page=page)
        seen["pages"] = pages

    # devices
    def _handler_for(self, serial: str):
        """The tab's own connection to *serial* when it is open, else a new one."""
        for _index, tab in self._device_tabs():
            if self.tab_serial(tab) == serial and getattr(tab, "handler", None) is not None:
                return tab.handler
        from ..config import ADBConfig
        from ..core import ADBHandler
        from .adb_path import gui_adb_path

        live = {d.serial for d in getattr(self.window, "_live_devices", None) or []}
        if serial not in live:
            raise ApiError(404, f"{serial} is not connected (GET /v1/devices lists what is)")
        return ADBHandler(ADBConfig(serial=serial, adb_path=gui_adb_path()), quiet=True)

    def _device(self, body: dict):
        serial = _text(body, "serial")
        method = _text(body, "method")
        if method not in DEVICE_CALLS:
            raise ApiError(400, f"{method!r} is not a device command the API runs "
                                "(GET /v1/info lists them)")
        args = body.get("args") or []
        kwargs = body.get("kwargs") or {}
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ApiError(400, '"args" is a list and "kwargs" an object')
        if "safe" in kwargs:
            raise ApiError(400, '"safe" is set by the API: a failure comes back as the result')
        handler = self.on_ui(lambda: self._handler_for(serial))
        fn = getattr(handler, method)
        try:
            takes_safe = "safe" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            takes_safe = False
        if takes_safe:
            kwargs = dict(kwargs, safe=True)
        try:
            result = fn(*args, **kwargs)
        except TypeError as exc:  # wrong arguments for the command
            raise ApiError(400, f"{method}: {exc}")
        return jsonable(result)
