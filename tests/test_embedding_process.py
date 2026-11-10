"""TurboADB started for another program: the real process, and the Qt widget.

The process runs off screen (Qt's offscreen platform) with downloads turned
off; it needs no device.  The widget is shown a TurboADB that only pretends.
"""
import json
import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("PyQt5")

from turboadb import embed  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(home):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", TURBOADB_AUTO_FETCH="0",
               HOME=str(home), USERPROFILE=str(home))
    env["PYTHONPATH"] = os.pathsep.join(p for p in (ROOT, env.get("PYTHONPATH")) if p)
    return env


def test_a_real_turboadb_answers_its_api_and_closes(tmp_path, monkeypatch):
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(embed, "command", lambda exe=None: [sys.executable, "-m", "turboadb.gui.app"])
    tadb = embed.launch(None, hide=("menubar", "log"), timeout=120)
    try:
        info = tadb.info()
        assert info["api_version"] == 1 and info["embedded"] is False and info["pid"] == tadb.pid
        assert tadb.ui()["menubar"] is False and tadb.set_ui(sidebar=False)["sidebar"] is False
        assert tadb.tabs() == []
        found, _last = tadb.poll_events(0, 0)
        assert found[0]["type"] == "ready"
    finally:
        tadb.close(timeout=60)
    assert tadb.process.wait(timeout=60) == 0 and not tadb.running()


@pytest.mark.skipif(os.name != "nt", reason="window handles are Windows'")
def test_a_window_that_is_not_there_is_said_in_the_handshake(tmp_path):
    handshake = tmp_path / "handshake.json"
    done = subprocess.run(
        [sys.executable, "-m", "turboadb.gui.app", "--embed-into", "0x7FFFFFF0",
         "--api-port", "0", "--handshake-file", str(handshake)],
        env=_env(tmp_path), capture_output=True, timeout=120)
    assert done.returncode == 4
    said = json.loads(handshake.read_text(encoding="utf-8"))
    assert said["error"] == f"there is no window {0x7FFFFFF0} to go into"


def test_bad_options_end_before_any_window(qapp):
    from turboadb.gui import app

    assert app.main(["--embed-into", "zero"]) == 2
    assert app.main(["--hide", "toolbar"]) == 2


# ---- the Qt widget ------------------------------------------------------------------------
class _Pretend:
    """What launch() gives: a TurboADB that only pretends."""

    pid, window = 4242, 99

    def __init__(self):
        self.closed = False
        self.quit_asked = False

    def events(self):
        yield {"id": 1, "type": "device", "serial": "R58M1", "state": "device"}
        yield {"id": 2, "type": "closing"}

    def close(self, timeout=15.0):
        self.closed = True

    def quit(self):
        self.quit_asked = True


def _pump(qapp, until, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not until():
        qapp.processEvents()
        time.sleep(0.005)
    return until()


def test_the_widget_starts_turboadb_inside_itself(qapp, monkeypatch):
    from turboadb import embed_qt

    pretend = _Pretend()
    asked = []
    monkeypatch.setattr(embed_qt, "launch", lambda parent, **kw: asked.append((parent, kw)) or pretend)
    view = embed_qt.TurboADBWidget(hide=("menubar",), autostart=False)
    seen = []
    view.started.connect(lambda: seen.append("started"))
    view.event_received.connect(lambda e: seen.append(e["type"]))
    view.finished.connect(lambda: seen.append("finished"))
    try:
        view.start()
        assert _pump(qapp, lambda: "finished" in seen)
        assert seen == ["started", "device", "closing", "finished"]
        assert asked == [(int(view.winId()), {"exe": None, "hide": ("menubar",), "timeout": 90.0})]
        assert view.client is None  # it has closed
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_the_widget_says_why_it_did_not_start(qapp, monkeypatch):
    from turboadb import embed_qt

    def refuse(parent, **kw):
        raise embed.TurboADBError("TurboADB was not up within 90 s")

    monkeypatch.setattr(embed_qt, "launch", refuse)
    view = embed_qt.TurboADBWidget(autostart=False)
    failed = []
    view.failed.connect(failed.append)
    try:
        view.start()
        assert _pump(qapp, lambda: failed)
        assert failed == ["TurboADB was not up within 90 s"]
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_the_widget_is_a_window_of_its_own_for_turboadb(qapp):
    from PyQt5.QtCore import Qt
    from turboadb import embed_qt

    view = embed_qt.TurboADBWidget(autostart=False)
    try:
        assert view.testAttribute(Qt.WA_NativeWindow)
        assert view.testAttribute(Qt.WA_DontCreateNativeAncestors)
        # (a signal called "event" hid QWidget.event and crashed the widget)
        assert callable(view.event) and not hasattr(type(view).event, "emit")
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_the_widget_closes_turboadb_when_the_program_quits(qapp, monkeypatch):
    from turboadb import embed_qt

    pretend = _Pretend()
    view = embed_qt.TurboADBWidget(autostart=False)
    view.client = pretend
    try:
        view._on_quit()
        assert pretend.quit_asked and view._closed
    finally:
        view.client = None
        view.deleteLater()
        qapp.processEvents()


def test_the_widget_uses_the_qt_the_program_has():
    from turboadb import embed_qt

    assert embed_qt.BINDING == "PyQt5"  # imported by these tests already
    assert embed_qt.QtWidgets.__name__ == "PyQt5.QtWidgets"


# ---- inside a host: TurboADB's own windows ------------------------------------------------
@pytest.mark.skipif(os.name != "nt", reason="window owners are Windows'")
def test_a_dialog_gets_the_host_as_owner_and_turboadb_keeps_its_notices():
    """A dialog with no owner went behind the host's window; a window made inside
    TurboADB told the host, and waited for it.  (Real windows: Qt's offscreen
    platform has no handles.)"""
    import ctypes
    from ctypes import wintypes

    from turboadb.gui import embedding

    u = ctypes.windll.user32
    u.CreateWindowExW.restype = wintypes.HWND
    u.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    u.GetWindow.restype = wintypes.HWND
    u.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    u.DestroyWindow.argtypes = (wintypes.HWND,)
    popup = 0x80000000  # WS_POPUP, never shown
    host = u.CreateWindowExW(0, "STATIC", "host", popup, 0, 0, 10, 10, None, None, None, None)
    box = u.CreateWindowExW(0, "STATIC", "box", popup, 0, 0, 10, 10, None, None, None, None)
    try:
        assert embedding.own_by(box, host) is True
        assert int(u.GetWindow(box, 4) or 0) == host  # GW_OWNER
        assert embedding.own_by(box, host) is False  # it has one now
        assert embedding.host_root(host) == host
        assert embedding.keep_to_itself(host) is True
        assert embedding.keep_to_itself(host) is False  # set already
    finally:
        u.DestroyWindow(box)
        u.DestroyWindow(host)
