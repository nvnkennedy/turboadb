"""Another program drives TurboADB through its control API.

The API answers on 127.0.0.1 with the token only, refuses web pages, runs
window work on the window's thread and device commands from a fixed list.
The window and its device tabs are stand-ins here: no device or adb is used.
"""
import json
import os
import sys
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QTabWidget, QWidget  # noqa: E402

from turboadb import embed  # noqa: E402
from turboadb.gui import control_api, device_tab, embedding  # noqa: E402
from turboadb.results import CommandResult, OperationResult  # noqa: E402


class _Tab(QWidget):
    """A device tab: its pages, its connection, its screen."""

    screen_active = pyqtSignal(bool)

    def __init__(self, serial, *, connected=True, terminal_only=False):
        super().__init__()
        self.session = {"serial": serial}
        self.handler = types.SimpleNamespace(config=types.SimpleNamespace(target=serial)) \
            if connected else None
        self._terminal_only = terminal_only
        self.inner = QTabWidget()
        self.shown = []
        self.mirror_tab = types.SimpleNamespace(
            starts=[], stops=[], start=lambda **kw: self.mirror_tab.starts.append(kw),
            stop=lambda: self.mirror_tab.stops.append(True))
        if connected:
            self.make_pages()

    def make_pages(self):
        self._subtabs = {}
        for key in ("shell", "logcat", "files", "controls", "apps", "phone"):
            page = QWidget()
            self.inner.addTab(page, key)
            self._subtabs[key] = page
        self._subtabs["terminal"] = self._subtabs["shell"]
        self._subtabs["webcam"] = None

    def has_webcam_page(self):
        return self._subtabs.get("webcam") is not None

    def show_subtab(self, key):
        self.shown.append(key)
        self.inner.setCurrentWidget(self._subtabs[key])


class _Window(QWidget):
    def __init__(self):
        super().__init__()
        self.tabs = QTabWidget()
        self._live_devices = []
        self.calls = []
        self.parts = {part: True for part in embedding.HIDE_PARTS}

    def add_tab(self, tab, title):
        self.tabs.setCurrentIndex(self.tabs.addTab(tab, title))

    def open_live_device(self, serial):
        self.calls.append(("open", serial))
        self.add_tab(_Tab(serial), serial)

    def _open_session(self, session, name):
        self.calls.append(("session", session))
        self.add_tab(_Tab(f"{session['host']}:{session['port']}", connected=False), name)

    def _close_tab(self, index):
        self.calls.append(("close", index))
        self.tabs.removeTab(index)

    def ui_parts(self):
        return dict(self.parts)

    def set_ui_parts(self, **shown):
        self.parts.update(shown)
        return self.ui_parts()

    def set_webcam_pages(self, shown):
        self.calls.append(("webcam_page", shown))

    def _apply_theme(self, name):
        self.calls.append(("theme", name))


def _device(serial, state="device", model="Pixel"):
    from turboadb.devices import Device

    return Device(serial=serial, state=state, model=model)


@pytest.fixture
def api(qapp, monkeypatch):
    monkeypatch.setattr(device_tab, "DeviceTab", _Tab)
    window = _Window()
    made = control_api.ControlApi(window, token="secret")
    made.start()
    yield made
    made.stop()
    window.deleteLater()
    qapp.processEvents()


def _ask(qapp, api, method, path, body=None, *, token="secret", headers=None):
    """One request from another thread, while the window's thread runs."""
    out = {}

    def go():
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{api.port}{path}", data=data,
                                     method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=15) as resp:
                out["status"], out["body"] = resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            out["status"], out["body"] = exc.code, json.loads(exc.read())

    worker = threading.Thread(target=go)
    worker.start()
    deadline = time.monotonic() + 20
    while worker.is_alive() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.003)
    worker.join(1)
    return out["status"], out["body"]


# ---- who may ask ------------------------------------------------------------------
def test_it_listens_on_this_pc_only(api):
    assert api._server.server_address[0] == "127.0.0.1"


@pytest.mark.parametrize("token", [None, "wrong", "secre"])
def test_no_token_or_a_wrong_one_is_refused(qapp, api, token):
    status, body = _ask(qapp, api, "GET", "/v1/info", token=token)
    assert status == 401 and body == {"ok": False, "error": "the token is missing or wrong"}


def test_a_web_page_is_refused_even_with_the_token(qapp, api):
    status, _body = _ask(qapp, api, "GET", "/v1/info", headers={"Origin": "https://evil.example"})
    assert status == 403


def test_the_token_is_made_when_the_host_chose_none(qapp):
    made = control_api.ControlApi(_Window())
    try:
        assert len(made.token) >= 32 and made.authorized(f"Bearer {made.token}")
        assert not made.authorized(made.token) and not made.authorized("Basic x")
    finally:
        made._server.server_close()


# ---- requests -----------------------------------------------------------------------
def test_info_says_what_it_offers(qapp, api):
    status, body = _ask(qapp, api, "GET", "/v1/info")
    info = body["result"]
    assert status == 200 and info["api_version"] == 1 and info["pid"] == os.getpid()
    assert "files" in info["pages"] and "shell" in info["device_calls"]
    assert info["ui_parts"] == list(embedding.HIDE_PARTS)


@pytest.mark.parametrize("method, path, status", [
    ("GET", "/v1/nothing", 404), ("POST", "/v1/info", 405), ("GET", "/v1/open", 405),
])
def test_what_is_not_there(qapp, api, method, path, status):
    assert _ask(qapp, api, method, path, {} if method == "POST" else None)[0] == status


def test_a_body_that_is_not_json(qapp, api):
    out = {}

    def go():
        req = urllib.request.Request(f"http://127.0.0.1:{api.port}/v1/open", data=b"{nope",
                                     method="POST", headers={"Authorization": "Bearer secret"})
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10)
        except urllib.error.HTTPError as exc:
            out["status"] = exc.code

    worker = threading.Thread(target=go)
    worker.start()
    while worker.is_alive():
        qapp.processEvents()
        time.sleep(0.003)
    assert out["status"] == 400


def test_devices_say_which_are_open(qapp, api):
    api.window._live_devices = [_device("R58M1"), _device("10.0.0.5:5555", model="IVI")]
    api.window.add_tab(_Tab("R58M1"), "Pixel")
    devices = _ask(qapp, api, "GET", "/v1/devices")[1]["result"]
    assert [(d["serial"], d["open"], d["network"]) for d in devices] == [
        ("R58M1", True, False), ("10.0.0.5:5555", False, True)]


def test_open_a_device_on_a_page(qapp, api):
    api.window._live_devices = [_device("R58M1")]
    status, body = _ask(qapp, api, "POST", "/v1/open", {"serial": "R58M1", "page": "files"})
    tab = body["result"]
    assert status == 200 and api.window.calls == [("open", "R58M1")]
    assert tab["serial"] == "R58M1" and tab["page"] == "files" and tab["page_state"] == "shown"
    # opened again: brought up, not a second tab
    _ask(qapp, api, "POST", "/v1/open", {"serial": "R58M1", "page": "logcat"})
    assert api.window.calls == [("open", "R58M1")] and api.window.tabs.count() == 1


def test_a_network_device_is_opened_by_host_and_port(qapp, api):
    status, body = _ask(qapp, api, "POST", "/v1/open", {"host": "10.0.0.9", "port": 5555,
                                                         "page": "logcat"})
    assert status == 200 and body["result"]["page_state"] == "waiting for the device to connect"
    assert api.window.calls == [("session", {"name": "10.0.0.9", "type": "network",
                                             "host": "10.0.0.9", "port": 5555})]
    tab = api.window.tabs.widget(0)
    tab.make_pages()  # it has connected
    api._notice()
    assert tab.shown == ["logcat"]


@pytest.mark.parametrize("body, status", [
    ({"serial": "GONE"}, 404),  # not connected
    ({"serial": "R58M1", "page": "garden"}, 400),  # no such page: nothing opens
    ({}, 400),
    ({"host": "10.0.0.9", "port": 70000}, 400),
])
def test_what_cannot_be_opened(qapp, api, body, status):
    api.window._live_devices = [_device("R58M1")]
    assert _ask(qapp, api, "POST", "/v1/open", body)[0] == status
    assert api.window.calls == []


def test_show_and_close_a_tab(qapp, api):
    api.window.add_tab(_Tab("A"), "A")
    api.window.add_tab(_Tab("B"), "B")
    status, body = _ask(qapp, api, "POST", "/v1/show", {"serial": "A", "page": "apps"})
    assert status == 200 and api.window.tabs.currentIndex() == 0 and body["result"]["page"] == "apps"
    assert _ask(qapp, api, "POST", "/v1/close", {"serial": "B"})[0] == 200
    assert api.window.calls == [("close", 1)]


def test_a_webcam_page_that_is_off_is_said(qapp, api):
    api.window.add_tab(_Tab("A"), "A")
    status, body = _ask(qapp, api, "POST", "/v1/show", {"serial": "A", "page": "webcam"})
    assert status == 409 and "webcam_page" in body["error"]


def test_the_screen_starts_and_stops(qapp, api):
    tab = _Tab("A")
    api.window.add_tab(tab, "A")
    _ask(qapp, api, "POST", "/v1/screen", {"serial": "A", "action": "start", "display_id": 2})
    _ask(qapp, api, "POST", "/v1/screen", {"serial": "A", "action": "stop"})
    assert tab.mirror_tab.starts == [{"display_id": 2}] and tab.mirror_tab.stops == [True]
    assert tab.shown == ["controls"]
    assert _ask(qapp, api, "POST", "/v1/screen", {"serial": "A", "action": "dance"})[0] == 400


def test_parts_of_the_window(qapp, api, monkeypatch):
    from turboadb.gui import settings as settings_mod

    monkeypatch.setattr(settings_mod, "get", lambda key, *d: False if key == "show_webcam_page" else None)
    status, body = _ask(qapp, api, "POST", "/v1/ui", {"menubar": False, "ribbon": False,
                                                      "webcam_page": True, "theme": "light"})
    assert status == 200 and body["result"]["menubar"] is False and body["result"]["log"] is True
    assert ("webcam_page", True) in api.window.calls and ("theme", "light") in api.window.calls
    assert _ask(qapp, api, "POST", "/v1/ui", {"toolbar": False})[0] == 400
    assert _ask(qapp, api, "POST", "/v1/ui", {"menubar": "no"})[0] == 400
    assert _ask(qapp, api, "POST", "/v1/ui", {"theme": "neon"})[0] == 400


# ---- device commands ------------------------------------------------------------------
class _Handler:
    def __init__(self):
        self.calls = []

    def shell(self, command, *, timeout=None, safe=None):
        self.calls.append((command, timeout, safe))
        value = CommandResult(command, 0, "Pixel 7\n", "", 0.1)
        return OperationResult(success=True, action="shell", value=value)

    def reboot(self, mode=None, *, safe=None):
        raise AssertionError("not on the list")


def test_a_device_command_runs_and_comes_back_as_json(qapp, api, monkeypatch):
    handler = _Handler()
    monkeypatch.setattr(api, "_handler_for", lambda serial: handler)
    status, body = _ask(qapp, api, "POST", "/v1/device", {
        "serial": "A", "method": "shell", "args": ["getprop ro.product.model"],
        "kwargs": {"timeout": 5}})
    result = body["result"]
    assert status == 200 and result["success"] is True and result["value"]["stdout"] == "Pixel 7\n"
    assert handler.calls == [("getprop ro.product.model", 5, True)]


@pytest.mark.parametrize("body, status", [
    ({"serial": "A", "method": "popen"}, 400),  # streams: not on the list
    ({"serial": "A", "method": "_run"}, 400),
    ({"serial": "A", "method": "shell", "kwargs": {"safe": False}}, 400),
    ({"serial": "A", "method": "shell", "args": "ls"}, 400),
    ({"serial": "A", "method": "shell", "kwargs": {"nonsense": 1}}, 400),  # bad arguments
])
def test_device_commands_that_are_refused(qapp, api, monkeypatch, body, status):
    monkeypatch.setattr(api, "_handler_for", lambda serial: _Handler())
    assert _ask(qapp, api, "POST", "/v1/device", body)[0] == status


def test_every_listed_command_is_an_engine_method():
    from turboadb.core import ADBHandler

    assert all(callable(getattr(ADBHandler, name, None)) for name in control_api.DEVICE_CALLS)


def test_a_device_that_is_not_connected(qapp, api):
    status, body = _ask(qapp, api, "POST", "/v1/device", {"serial": "GONE", "method": "shell",
                                                          "args": ["ls"]})
    assert status == 404 and "not connected" in body["error"]


# ---- events -----------------------------------------------------------------------------
def test_events_tell_what_changed(qapp, api):
    _status, body = _ask(qapp, api, "GET", "/v1/events?after=0")
    assert [e["type"] for e in body["result"]["events"]] == ["ready"]
    last = body["result"]["last"]
    api.window._live_devices = [_device("R58M1")]
    api.window.add_tab(_Tab("R58M1"), "Pixel")
    api._notice()
    api.window._live_devices = []
    api._notice()
    found = _ask(qapp, api, "GET", f"/v1/events?after={last}")[1]["result"]["events"]
    kinds = [(e["type"], e.get("serial"), e.get("state")) for e in found]
    assert ("device", "R58M1", "device") in kinds and ("device", "R58M1", "gone") in kinds
    assert any(e["type"] == "tab_opened" and e["serial"] == "R58M1" for e in found)


def test_a_wait_for_events_ends_when_one_comes(qapp, api):
    last = api.events.last
    threading.Timer(0.3, lambda: api.events.publish("log", text="hello")).start()
    started = time.monotonic()
    found = _ask(qapp, api, "GET", f"/v1/events?after={last}&wait=10")[1]["result"]["events"]
    assert found[0]["text"] == "hello" and time.monotonic() - started < 5


def test_a_screen_that_starts_is_an_event(qapp, api):
    tab = _Tab("A")
    api.window.add_tab(tab, "A")
    api._notice()
    last = api.events.last
    tab.screen_active.emit(True)
    found, _last = api.events.after(last)
    assert found[0]["type"] == "screen" and found[0]["serial"] == "A" and found[0]["active"]


def test_the_event_log_keeps_the_newest():
    log = control_api.EventLog(keep=3)
    for n in range(5):
        log.publish("x", n=n)
    found, last = log.after(0)
    assert [e["n"] for e in found] == [2, 3, 4] and last == 5


# ---- results as JSON --------------------------------------------------------------------
def test_results_become_json():
    value = control_api.jsonable(OperationResult(success=False, action="pull",
                                                 error=OSError("gone")))
    assert value == {"success": False, "action": "pull", "value": None, "error": "OSError: gone"}
    assert control_api.jsonable(b"\x89PNG") == {"base64": "iVBORw=="}
    json.dumps(control_api.jsonable({"a": (1, {2}), "b": object()}))


# ---- the host side: turboadb.embed --------------------------------------------------------
def test_the_python_client_speaks_the_api(qapp, api):
    client = embed.Client(api.port, "secret")
    out = {}
    worker = threading.Thread(target=lambda: out.update(info=client.info(), ui=client.set_ui(
        sidebar=False)))
    worker.start()
    while worker.is_alive():
        qapp.processEvents()
        time.sleep(0.003)
    assert out["info"]["api_version"] == 1 and out["ui"]["sidebar"] is False
    with pytest.raises(embed.TurboADBError) as refused:
        embed.Client(api.port, "wrong").request("GET", "/info")
    assert refused.value.status == 401


_FAKE_TURBOADB = r'''
import json, os, sys
args = sys.argv[1:]
path = args[args.index("--handshake-file") + 1]
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "crash":
    sys.exit(7)
info = {"port": 1, "token": os.environ.get("TURBOADB_API_TOKEN"), "pid": 42, "window": 99}
if mode == "token":
    info["token"] = "another"
if mode == "error":
    info = {"error": "there is no window 5 to go into"}
with open(path + ".tmp", "w") as fh:
    json.dump(dict(info, args=args), fh)
os.replace(path + ".tmp", path)
'''


@pytest.fixture
def fake_turboadb(tmp_path, monkeypatch):
    script = tmp_path / "fake_turboadb.py"
    script.write_text(_FAKE_TURBOADB, encoding="utf-8")
    monkeypatch.setattr(embed, "command", lambda exe=None: [sys.executable, str(script)])
    return monkeypatch


def test_launch_waits_for_the_handshake(fake_turboadb):
    started = embed.launch(1234, hide=("menubar", "log"), timeout=20)
    assert started.pid == 42 and started.window == 99 and started.parent == 1234
    assert started.base == "http://127.0.0.1:1/v1"


@pytest.mark.parametrize("mode, said", [
    ("crash", "exit code 7"), ("token", "another token"), ("error", "no window 5"),
])
def test_launch_says_why_it_failed(fake_turboadb, mode, said):
    fake_turboadb.setenv("FAKE_MODE", mode)
    with pytest.raises(embed.TurboADBError) as failed:
        embed.launch(5, timeout=20)
    assert said in str(failed.value)


def test_launch_passes_the_token_outside_the_command_line(fake_turboadb, tmp_path):
    seen = {}
    real = embed.subprocess.Popen

    def popen(args, **kw):
        seen["args"], seen["env"] = args, kw["env"]
        return real(args, **kw)

    fake_turboadb.setattr(embed.subprocess, "Popen", popen)
    started = embed.launch(77, hide=("ribbon",), timeout=20)
    assert started.token not in " ".join(seen["args"])
    assert seen["env"][embed.TOKEN_ENV] == started.token
    assert seen["args"][-4:] == ["--embed-into", "77", "--hide", "ribbon"]


# ---- the command line and the handshake file ------------------------------------------------
def test_the_embedding_options_are_read():
    found = embedding.parse(["--embed-into", "0x1A2B", "--api-port", "0", "--hide",
                             "menubar,Log", "--handshake-file", "h.json", "--other"])
    assert (found.parent, found.api_port, found.hide, found.handshake) == (
        0x1A2B, 0, ("menubar", "log"), "h.json")
    assert found.embedded and found.api
    assert not embedding.parse([]).api and not embedding.parse([]).embedded


@pytest.mark.parametrize("argv", [
    ["--embed-into", "0"], ["--embed-into", "window"], ["--api-port", "70000"],
    ["--hide", "menubar,toolbar"],
])
def test_bad_embedding_options_are_refused(argv, capsys):
    with pytest.raises(SystemExit):
        embedding.parse(argv)


def test_the_token_is_taken_out_of_the_environment():
    environ = {embedding.TOKEN_ENV: " abc "}
    assert embedding.take_token(environ) == "abc" and embedding.TOKEN_ENV not in environ
    assert embedding.take_token({}) is None


def test_the_handshake_file_is_written_whole(tmp_path):
    path = tmp_path / "deep" / "handshake.json"
    embedding.write_handshake(str(path), {"port": 5, "token": "t"})
    assert json.loads(path.read_text()) == {"port": 5, "token": "t"}
    assert os.listdir(path.parent) == ["handshake.json"]


def test_the_handshake_says_how_to_reach_the_api(api):
    shake = api.handshake()
    assert shake["api"] == f"http://127.0.0.1:{api.port}/v1" and shake["token"] == "secret"
    assert shake["pid"] == os.getpid() and shake["api_version"] == 1


def test_dpi_is_left_alone_when_set_on_purpose():
    environ = {"QT_QPA_PLATFORM": "windows:dpiawareness=0"}
    assert embedding.match_host_dpi(1, environ) is None
    assert environ == {"QT_QPA_PLATFORM": "windows:dpiawareness=0"}


def test_turboadb_gui_passes_the_options_on(monkeypatch):
    from turboadb import cli

    seen = []
    monkeypatch.setattr(cli, "launch_gui", lambda argv=None: seen.append(argv) or 0)
    assert cli.main(["gui", "--embed-into", "4660", "--api-port", "0", "--hide", "menubar,log",
                     "--handshake-file", "h.json"]) == 0
    assert seen == [["--embed-into", "4660", "--api-port", "0", "--handshake-file", "h.json",
                     "--hide", "menubar,log"]]
    seen.clear()
    assert cli.main(["gui"]) == 0 and seen == [[]]


# ---- a busy window never holds a read up ---------------------------------------------
def _ask_without_the_window(api, path, method="GET"):
    """A request while the window's thread does not answer (busy, or waiting
    on the host): nothing pumps Qt's events meanwhile."""
    out = {}

    def go():
        req = urllib.request.Request(f"http://127.0.0.1:{api.port}{path}", method=method,
                                     data=b"{}" if method == "POST" else None,
                                     headers={"Authorization": "Bearer secret"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=15) as resp:
                out["status"], out["body"] = resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            out["status"], out["body"] = exc.code, json.loads(exc.read())

    worker = threading.Thread(target=go)
    started = time.monotonic()
    worker.start()
    worker.join(15)
    return out["status"], out["body"], time.monotonic() - started


def test_a_read_answers_from_the_last_look_when_the_window_is_busy(qapp, api):
    api.window._live_devices = [_device("R58M1")]
    api._notice()  # the last look
    status, body, took = _ask_without_the_window(api, "/v1/devices")
    assert status == 200 and [d["serial"] for d in body["result"]] == ["R58M1"]
    assert took < control_api.ControlApi.READ_WAIT_S + 2
    qapp.processEvents()  # (the call that was queued meanwhile runs, and is dropped)


def test_a_change_still_waits_for_the_window(qapp, api, monkeypatch):
    monkeypatch.setattr(control_api.ControlApi, "UI_TIMEOUT_S", 0.3)
    status, body, _took = _ask_without_the_window(api, "/v1/focus", "POST")
    assert status == 504 and "did not answer" in body["error"]
    qapp.processEvents()


def test_dialogs_open_after_the_answer(qapp, api):
    opened = []
    api.window.show_settings = lambda: opened.append("settings")
    status, body = _ask(qapp, api, "POST", "/v1/dialog", {"name": "settings"})
    assert status == 200 and body["result"] == {"opening": "settings"}
    end = time.monotonic() + 2
    while not opened and time.monotonic() < end:
        qapp.processEvents()
    assert opened == ["settings"]
    assert _ask(qapp, api, "POST", "/v1/dialog", {"name": "format_disk"})[0] == 400


def test_every_dialog_is_a_window_method():
    from turboadb.gui.main_window import MainWindow

    assert all(callable(getattr(MainWindow, method, None))
               for method in control_api.DIALOGS.values())


def test_a_client_on_a_window_thread_keeps_answering(monkeypatch):
    """Its wait takes what other threads send its windows (embed._answering_windows)."""
    seen = []
    monkeypatch.setattr(embed, "_thread_has_windows", lambda: True)
    monkeypatch.setattr(embed, "_answering_windows", lambda work: seen.append(1) or work())
    client = embed.Client(1, "t")
    monkeypatch.setattr(client, "_request", lambda *a: {"ok": 1})
    assert client.request("GET", "/info") == {"ok": 1} and seen == [1]


@pytest.mark.skipif(os.name != "nt", reason="Windows' messages")
def test_the_answering_wait_returns_the_answer_and_the_error():
    assert embed._answering_windows(lambda: 42) == 42
    with pytest.raises(embed.TurboADBError):
        embed._answering_windows(lambda: (_ for _ in ()).throw(embed.TurboADBError("no")))
