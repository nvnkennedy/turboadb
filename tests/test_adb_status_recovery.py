"""The main window's side of the adb server: an honest status that recovers,
a sidebar that notices the last device leaving, no device poll while adb.exe is
replaced by a self-update, and the exit setting that keeps scrcpy windows too.
No test here runs a real adb: the window's workers are inert stand-ins."""
import threading
import types

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QThread, Qt, pyqtSignal  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

import turboadb.gui.main_window as mw  # noqa: E402
from turboadb import tools  # noqa: E402
from turboadb.devices import Device  # noqa: E402


class _Tracker(QThread):
    result = pyqtSignal(list)

    def __init__(self, adb_path=None, parent=None):
        super().__init__(parent)

    def stop(self):
        pass

    def run(self):
        pass


class _Poll(QThread):
    result = pyqtSignal(list)
    failed = pyqtSignal(str)
    made = []

    def __init__(self, adb_path=None, *, socket_only=False, parent=None):
        super().__init__(parent)
        _Poll.made.append(self)
        self.socket_only = socket_only

    def run(self):
        pass


class _Init(QThread):
    ready = pyqtSignal(bool, str)
    note = pyqtSignal(str)
    made = []

    def __init__(self, adb_path=None, parent=None):
        super().__init__(parent)
        _Init.made.append(self)

    def run(self):
        pass


@pytest.fixture
def window(qapp, monkeypatch):
    _Poll.made, _Init.made = [], []
    monkeypatch.setattr(mw, "_DeviceTracker", _Tracker)
    monkeypatch.setattr(mw, "_DevicesPoll", _Poll)
    monkeypatch.setattr(mw, "_AdbInitThread", _Init)
    for name in ("_check_tools", "_ensure_shortcuts", "_quiet_update_check"):
        monkeypatch.setattr(mw.MainWindow, name, lambda self: None)
    logged = []
    monkeypatch.setattr(mw.MainWindow, "_log", lambda self, text: logged.append(text))
    win = mw.MainWindow()
    win.logged = logged
    yield win
    win.close()
    win.deleteLater()


def _pixel():
    return Device(serial="R58M123", state="device", model="Pixel_7")


def _rows(win):
    return [win.live_list.item(i).text() for i in range(win.live_list.count())]


def test_a_failed_start_recovers_when_the_server_answers(window):
    """A slow cold start left "ADB: unavailable" and "ADB unavailable" in the
    sidebar for the whole session, and the fallback poll never started."""
    window._on_devices([_pixel()])
    window._on_adb_ready(False, "ADB server did not start: did not become ready")
    assert window._status_adb.text() == "ADB unavailable"
    assert window._status_adb.tone() == "error"
    assert _rows(window) == ["⚠  ADB unavailable — see the log"]
    assert window._timer.isActive()  # the reconciliation poll runs anyway
    # the tracker reconnects and reports the SAME device: the list comes back
    window._on_devices([_pixel()])
    assert window._status_adb.text() == "ADB ready"
    assert window._status_adb.tone() == "ok"
    assert window.live_list.item(0).data(Qt.UserRole) == "R58M123"
    assert "[OK] ADB server is answering again" in window.logged


def test_a_scan_issue_clears_on_the_next_result(window):
    window._on_device_poll_error("adb devices timed out after 3s")
    assert window._status_adb.text() == "ADB scan issue"
    assert window._status_adb.tone() == "warn"
    window._on_devices([])
    assert window._status_adb.text() == "ADB ready"


def test_a_failed_start_is_retried_quietly(window):
    window._ADB_RETRY_MS = 10
    window._on_adb_ready(False, "ADB server did not start: boom")
    QTest.qWait(200)
    assert len(_Init.made) == 2  # the window's first check, then one retry
    assert _rows(window) == ["⚠  ADB unavailable — see the log"]  # not "Connecting…"
    window._on_adb_ready(False, "ADB server did not start: boom")
    assert window.logged.count("[WARNING] ADB server did not start: boom") == 1
    assert "[DEBUG] ADB server did not start: boom" in window.logged
    # the same poll failure every 15 s is one warning too
    window._on_device_poll_error("nothing answers")
    window._on_device_poll_error("nothing answers")
    assert sum("WARNING] Device scan failed" in line for line in window.logged) == 1


def test_retries_are_bounded(window):
    window._ADB_RETRY_MS = 0
    for _ in range(6):
        window._on_adb_ready(False, "ADB server did not start: boom")
        QTest.qWait(30)
    assert len(_Init.made) == 1 + window._ADB_RETRIES


def test_a_restart_that_fixes_a_failed_start_starts_the_fallback_poll(window):
    window._on_adb_ready(False, "ADB server did not start: boom")
    window._timer.stop()
    window._poll_timer_was_active = False  # as restart_adb_server records it
    window._on_adb_restarted("[OK] ADB server restarted")
    assert window._timer.isActive()
    assert window._status_adb.text() == "ADB ready"


def test_the_startup_check_logs_what_it_found_about_the_server(window):
    init = window._adb_init
    init.note.emit("[WARNING] The adb server on port 5037 runs another adb")
    assert "[WARNING] The adb server on port 5037 runs another adb" in window.logged


def test_unplugging_the_last_device_updates_the_sidebar_promptly(window):
    """adb's tracker reports the last device leaving once; the debounce waited
    for a second report that only the 15 s poll (or nothing) delivered."""
    window._EMPTY_CONFIRM_MS = 10
    window._on_devices([_pixel()])
    _Poll.made.clear()
    window._on_devices([])  # the tracker's one removal report
    assert window._live_devices and "Pixel" in _rows(window)[0]  # a hiccup is not a removal
    QTest.qWait(100)
    probes = [poll for poll in _Poll.made if poll.socket_only]
    assert probes, "no confirmation probe was asked for"
    probes[0].result.emit([])  # the server confirms: nothing attached
    assert _rows(window) == ["No devices connected"]
    assert window._live_devices == []
    assert window._status_devices.text() == "No devices"
    assert window._status_devices.tone() == "idle"


def test_a_device_that_comes_back_is_not_removed(window):
    window._EMPTY_CONFIRM_MS = 10
    window._on_devices([_pixel()])
    window._on_devices([])
    window._on_devices([_pixel()])  # re-enumerated before the probe
    QTest.qWait(100)
    assert window.live_list.item(0).data(Qt.UserRole) == "R58M123"


def test_a_self_update_pauses_the_device_poll(window, monkeypatch):
    """Its `adb devices` restarted the server from the old folder while the
    update replaced adb.exe, and the swap failed as 'still in use'."""
    release = threading.Event()

    class Upgrade(QThread):
        progress = pyqtSignal(str)
        done = pyqtSignal(dict)

        def __init__(self, expected=None):
            super().__init__()

        def run(self):
            release.wait(10)

    monkeypatch.setattr(mw, "_AppUpgradeThread", Upgrade)
    monkeypatch.setattr(mw.QMessageBox, "question", staticmethod(lambda *a, **k: mw.QMessageBox.Ok))
    monkeypatch.setattr(mw.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    window._start_poll_timer()
    window._do_self_update("99.0.0")
    try:
        assert not window._timer.isActive()
        assert window._server_task_running()
        _Poll.made.clear()
        window._poll_devices()
        assert [poll.socket_only for poll in _Poll.made] == [True]  # can't start a server
    finally:
        release.set()
        window._upd_run.wait(5000)
    window._on_self_update_done({"ok": False, "error": "pip failed"})
    assert window._timer.isActive()


def test_the_exit_setting_keeps_separate_screen_windows(window, monkeypatch):
    """Unticking "Close ADB and scrcpy when TurboADB closes" still closed every
    scrcpy window: the tabs stopped their screens on the way out."""
    from turboadb.gui import settings as settings_mod
    from turboadb.gui.mirror_panel import MirrorPanel

    handler = types.SimpleNamespace(serial="dummy123", config=None)
    separate, embedded = MirrorPanel(handler, None), MirrorPanel(handler, None)
    stopped = []

    class Session:
        running = True

        def __init__(self, name):
            self.name = name

        def stop(self, timeout=5.0):
            stopped.append(self.name)

    separate._scrcpy, separate._embed_on = Session("separate"), False
    embedded._scrcpy, embedded._embed_on = Session("embedded"), True
    for panel in (separate, embedded):
        panel.setParent(window)
    window.close()  # the setting is on: nothing is released
    assert separate._scrcpy is not None
    monkeypatch.setattr(settings_mod, "get",
                        lambda key, default=None: False if key == "stop_adb_on_exit" else default)
    window.close()
    assert separate._scrcpy is None  # released: closing the panel leaves it open
    assert embedded._scrcpy is not None  # a screen inside the tab goes with it
    separate.close_panel()
    assert stopped == []


def test_a_recording_window_is_still_finished_on_exit(qapp):
    from turboadb.gui.mirror_panel import MirrorPanel

    panel = MirrorPanel(types.SimpleNamespace(serial="dummy123", config=None), None)
    panel._scrcpy = types.SimpleNamespace(running=True)
    panel._embed_on, panel._recording = False, True
    assert panel.keep_window_open() is False and panel._scrcpy is not None
    panel._recording = False
    panel.deleteLater()


def test_the_startup_check_waits_on_the_prewarm_launcher(qapp, monkeypatch, tmp_path):
    """The startup check gave up after 2.5 s and started a second launcher (or
    reported 'did not start' while the daemon was still scanning USB)."""
    adb = tmp_path / "adb.exe"
    adb.write_text("")
    made, state = [], {"alive": False}

    class Launcher:
        def __init__(self, cmd, **kw):
            made.append(cmd)

        def poll(self):
            return None

        def kill(self):
            pass

    monkeypatch.setattr(tools.subprocess, "Popen", Launcher)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: state["alive"])
    monkeypatch.setattr(tools, "find_adb", lambda path=None: str(adb))
    monkeypatch.setattr(tools, "describe_adb_server", lambda *a, **k: ("INFO", "another adb"))
    # the pre-warm launches, then gives up before the daemon answers
    assert tools.ensure_adb_server(str(adb), timeout=0.05) is False
    threading.Timer(0.3, lambda: state.update(alive=True)).start()
    init = mw._AdbInitThread(adb_path=str(adb))
    got, notes = [], []
    init.ready.connect(lambda ok, msg: got.append((ok, msg)))
    init.note.connect(notes.append)
    init.run()  # in this thread: the signals are delivered at once
    assert got == [(True, "ADB server active (127.0.0.1:5037)")]
    assert notes == ["[INFO] another adb"]
    assert len(made) == 1
    init.deleteLater()


def test_the_window_names_a_moved_server_port(window, monkeypatch):
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5099")
    window._log_adb_environment()
    assert any("port 5099" in line for line in window.logged)


def test_gui_app_prewarm_is_the_shared_one(monkeypatch):
    from turboadb.gui import adb_path
    from turboadb.gui import app as app_mod

    calls = []
    monkeypatch.setattr(adb_path, "prewarm_adb_server", lambda: calls.append(1))
    app_mod._prewarm_adb_server()
    assert calls == [1]
