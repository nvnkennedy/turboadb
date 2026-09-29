"""The main window's own actions: a command broadcast to every device at once,
progress dialogs that are freed when their task ends, the restart after a
self-update tearing the window down properly, the welcome tile opening a
device as the sidebar does, device scans that never replace a newer tracker
report, saved-target and theme changes that fail with a clear message, and
the remote-deploy and share summaries. No test runs adb: the window's
workers are inert stand-ins."""

import os
import threading
import types

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, QThread, Qt, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QProgressDialog, QWidget  # noqa: E402

import turboadb.gui.main_window as mw  # noqa: E402
from turboadb.devices import Device  # noqa: E402


class _Tracker(QThread):
    result = pyqtSignal(list)
    stopped = []

    def __init__(self, adb_path=None, parent=None):
        super().__init__(parent)

    def stop(self):
        _Tracker.stopped.append(self)

    def run(self):
        pass


class _Poll(QThread):
    result = pyqtSignal(list)
    failed = pyqtSignal(str)
    made = []

    def __init__(self, adb_path=None, *, socket_only=False, parent=None):
        super().__init__(parent)
        _Poll.made.append(self)

    def run(self):
        pass


class _Init(QThread):
    ready = pyqtSignal(bool, str)
    note = pyqtSignal(str)

    def __init__(self, adb_path=None, parent=None):
        super().__init__(parent)

    def run(self):
        pass


class _Tab(QWidget):
    """An open device tab: its target, and whether it was closed."""

    def __init__(self, session=None):
        super().__init__()
        self.session = session
        self.closed = 0

    def close_session(self):
        self.closed += 1


@pytest.fixture
def make_window(qapp, tmp_path, monkeypatch):
    """Build MainWindows with inert workers, in a profile of the test's own."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _Tracker.stopped, _Poll.made = [], []
    monkeypatch.setattr(mw, "_DeviceTracker", _Tracker)
    monkeypatch.setattr(mw, "_DevicesPoll", _Poll)
    monkeypatch.setattr(mw, "_AdbInitThread", _Init)
    for name in ("_check_tools", "_ensure_shortcuts", "_quiet_update_check"):
        monkeypatch.setattr(mw.MainWindow, name, lambda self: None)
    logged = []
    monkeypatch.setattr(mw.MainWindow, "_log", lambda self, text: logged.append(text))
    boxes = []
    for kind in ("warning", "information"):
        monkeypatch.setattr(mw.QMessageBox, kind, staticmethod(
            lambda *args, _kind=kind, **_kw: boxes.append((_kind, args[1], args[2]))))
    built = []

    def make():
        win = mw.MainWindow()
        win.logged, win.boxes = logged, boxes
        built.append(win)
        return win

    yield make
    for win in built:
        win.close()
        win.deleteLater()


@pytest.fixture
def window(make_window):
    return make_window()


def _free_deleted(qapp):
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def _rows(win):
    return [win.live_list.item(i).data(Qt.UserRole) for i in range(win.live_list.count())]


# --------------------------------------------------------------------------- #
# Run a command on ALL devices
# --------------------------------------------------------------------------- #
def test_a_broadcast_runs_on_every_device_at_once(qapp, monkeypatch):
    from turboadb import core
    from turboadb.results import CommandResult

    # Each device's command waits until all three are running: one device at
    # a time, the first one waited alone until the barrier gave up.
    together = threading.Barrier(3, timeout=5)
    timeouts = []

    def shell(self, command, *, timeout=None, safe=None, **_kw):
        timeouts.append(timeout)
        together.wait()
        return CommandResult(command, 0, f"13 on {self.serial}", "", 0.1)

    monkeypatch.setattr(core.ADBHandler, "shell", shell)
    worker = mw._BroadcastThread(["A", "B", "C"], "getprop ro.build.version.release")
    events = []
    worker.line.connect(lambda text: events.append(text))
    worker.done.connect(lambda: events.append("done"))
    worker.run()  # in this thread: its signals are delivered at once
    assert events[-1] == "done"
    assert sorted(events[:-1]) == [f"[OK] {s}: OK · 13 on {s}" for s in ("A", "B", "C")]
    assert timeouts == [mw._BroadcastThread.TIMEOUT_S] * 3


def test_a_broadcast_reports_each_device_as_it_finishes(qapp, monkeypatch):
    from turboadb import core
    from turboadb.results import CommandResult

    slow_may_finish = threading.Event()

    def shell(self, command, *, timeout=None, safe=None, **_kw):
        if self.serial == "slow":
            assert slow_may_finish.wait(10)
        return CommandResult(command, 0, "ok", "", 0.1)

    monkeypatch.setattr(core.ADBHandler, "shell", shell)
    worker = mw._BroadcastThread(["slow", "fast"], "true")
    lines = []

    def on_line(text):
        lines.append(text)
        slow_may_finish.set()  # the fast device was reported before the slow one ended

    worker.line.connect(on_line)
    worker.run()
    assert [line.split(":")[0] for line in lines] == ["[OK] fast", "[OK] slow"]


# --------------------------------------------------------------------------- #
# progress dialogs
# --------------------------------------------------------------------------- #
class _Download(QThread):
    progress = pyqtSignal(int)
    stage = pyqtSignal(str)
    done = pyqtSignal(dict)

    def __init__(self, mode="fetch", force=False):
        super().__init__()

    def run(self):
        pass


def test_tool_downloads_free_their_progress_dialogs(qapp, window, monkeypatch):
    monkeypatch.setattr(mw, "_ToolsDownloadThread", _Download)
    for _ in range(3):
        assert window._start_tools_task("fetch", "Downloading…", "TurboADB — tools",
                                        window._tools_done)
        assert window._dl.wait(5000)
        window._tools_done({"adb": "adb", "scrcpy": "scrcpy"})
    for _ in range(2):
        assert window._start_tools_task("upgrade", "Checking…", "TurboADB — upgrade",
                                        window._upgrade_done)
        assert window._dl.wait(5000)
        window._upgrade_done({"updated": {}, "checks": {}})
    _free_deleted(qapp)
    assert window.findChildren(QProgressDialog) == []
    assert window._dlg is None


def test_a_failed_self_update_frees_its_progress_dialog(qapp, window, monkeypatch):
    class Upgrade(QThread):
        progress = pyqtSignal(str)
        done = pyqtSignal(dict)

        def __init__(self, expected=None):
            super().__init__()

        def run(self):
            pass

    monkeypatch.setattr(mw, "_AppUpgradeThread", Upgrade)
    monkeypatch.setattr(mw.QMessageBox, "question",
                        staticmethod(lambda *a, **k: mw.QMessageBox.Ok))
    window._do_self_update("99.0.0")
    assert window._upd_run.wait(5000)
    window._on_self_update_done({"ok": False, "error": "pip failed"})
    _free_deleted(qapp)
    assert window.findChildren(QProgressDialog) == []


def test_a_remote_deploy_frees_its_dialog_and_survives_unsaved_hosts(qapp, window, monkeypatch):
    from turboadb.gui import deploy_dialog

    class Dialog:
        Accepted = 1

        def __init__(self, parent):
            pass

        def exec_(self):
            return self.Accepted

        def values(self):
            return {"hosts": ["pc1", "pc2"], "user": "LAB\\tester", "password": "test",
                    "port": 5037, "update": False, "use_ssl": False}

        def deleteLater(self):
            pass

    class Deploy(QThread):
        status = pyqtSignal(str)

        def __init__(self, *args, **kwargs):
            super().__init__()

        def run(self):
            pass

    def unwritable(*_args):
        raise OSError("settings.json could not be read (sharing violation)")

    monkeypatch.setattr(deploy_dialog, "DeployDialog", Dialog)
    monkeypatch.setattr(mw, "_DeployThread", Deploy)
    monkeypatch.setattr(mw.settings_mod, "add_recent", unwritable)
    window.deploy_serve_remote()  # the recent-hosts list can't be saved: it deploys anyway
    assert any(line.startswith("[WARNING] Could not remember the deploy hosts")
               for line in window.logged)
    assert window._deploy.wait(5000)
    _free_deleted(qapp)  # delivers `finished`
    assert window.boxes and window.boxes[-1][1] == "Remote deploy finished (with errors)"
    _free_deleted(qapp)
    assert window.findChildren(QProgressDialog) == []


# --------------------------------------------------------------------------- #
# the restart after a self-update
# --------------------------------------------------------------------------- #
def test_the_restart_after_an_update_tears_the_window_down(window, monkeypatch):
    from turboadb import update

    monkeypatch.setattr(update, "relaunch_on_exit", lambda: True)  # never a real relaunch
    quits = []
    monkeypatch.setattr(mw, "QApplication", types.SimpleNamespace(
        instance=lambda: types.SimpleNamespace(quit=lambda: quits.append(1))))
    tab = _Tab({"name": "Pixel", "type": "usb", "serial": "R58M123"})
    window.tabs.addTab(tab, "Pixel")
    tracker = window._tracker
    window._on_self_update_done({"ok": True, "new": "9.9.9"})
    # the same teardown as closing the window: tracker stopped, every tab closed
    assert window._closing
    assert tracker in _Tracker.stopped
    assert tab.closed == 1
    assert quits == [1]
    assert window.boxes[-1][1] == "Updated"


# --------------------------------------------------------------------------- #
# the theme is applied once at start-up
# --------------------------------------------------------------------------- #
def test_the_window_themes_the_app_only_when_nothing_has(qapp, make_window, monkeypatch):
    from turboadb.gui import settings

    applied = []
    monkeypatch.setattr(mw.theme, "apply_to_app", lambda app, name=None: applied.append(name))
    previous = qapp.styleSheet()
    try:
        qapp.setStyleSheet("QLabel { color: gray; }")  # app.main() has themed it
        make_window()
        assert applied == []
        qapp.setStyleSheet("")  # a window built on its own
        make_window()
        assert applied == [settings.get("theme")]
    finally:
        qapp.setStyleSheet(previous)


# --------------------------------------------------------------------------- #
# the welcome page's "Open target" tile
# --------------------------------------------------------------------------- #
def _record_opened(window, monkeypatch):
    opened, duplicates = [], []

    def add_tab(session, name, *, terminal_only=False):
        opened.append((dict(session), name))
        window.tabs.addTab(_Tab(dict(session)), name)

    monkeypatch.setattr(window, "_add_device_tab", add_tab)
    monkeypatch.setattr(window, "_open_duplicate_device",
                        lambda session, name, index: duplicates.append(name))
    return opened, duplicates


def test_the_welcome_tile_opens_a_wifi_device_as_the_sidebar_does(window, monkeypatch):
    opened, duplicates = _record_opened(window, monkeypatch)
    window._on_devices([Device(serial="192.168.1.5:5555", state="device", model="HU")])
    window.welcome._on_open_target()
    assert opened == [({"name": "HU", "type": "network", "host": "192.168.1.5", "port": 5555},
                       "HU")]
    # the sidebar now finds that tab instead of opening a second one
    window._open_live(window.live_list.item(0))
    assert duplicates == ["HU"] and len(opened) == 1


def test_the_welcome_tile_prefers_a_device_that_is_online(window, monkeypatch):
    opened, _duplicates = _record_opened(window, monkeypatch)
    window._on_devices([Device(serial="OLD1", state="offline", model="Old"),
                        Device(serial="R58M123", state="device", model="Pixel")])
    window.welcome._on_open_target()
    assert [session.get("serial") for session, _name in opened] == ["R58M123"]


def test_a_usb_target_with_a_network_serial_is_that_network_device(window, monkeypatch):
    _opened, duplicates = _record_opened(window, monkeypatch)
    window.tabs.addTab(_Tab({"name": "HU", "type": "network", "host": "192.168.1.5",
                             "port": 5555}), "HU")
    window._open_session({"name": "HU usb", "type": "usb", "serial": "192.168.1.5:5555"}, "HU usb")
    assert duplicates == ["HU usb"]


# --------------------------------------------------------------------------- #
# device scans and the tracker
# --------------------------------------------------------------------------- #
def test_a_scan_that_ends_after_a_newer_tracker_report_is_dropped(window):
    pixel = Device(serial="R58M123", state="device", model="Pixel")
    hu = Device(serial="HU77", state="device", model="HU")
    window._on_devices([pixel])
    window._poll_devices()
    scan = _Poll.made[-1]
    window._tracker.result.emit([pixel, hu])  # the head unit is plugged in meanwhile
    scan.result.emit([pixel])  # the scan's older answer
    scan.failed.emit("adb devices timed out after 3s")  # or its stale failure
    assert _rows(window) == ["R58M123", "HU77"]
    assert window._status_adb.text() == "ADB ready"
    # a scan started after that report is taken as usual
    window._poll_devices()
    _Poll.made[-1].result.emit([pixel])
    assert _rows(window) == ["R58M123"]


# --------------------------------------------------------------------------- #
# saved targets and settings that can't be written
# --------------------------------------------------------------------------- #
def _unreadable(*_args, **_kwargs):
    raise OSError("sessions.json could not be read (sharing violation); the change was not saved")


def test_a_target_that_cannot_be_saved_is_reported_plainly(window, monkeypatch):
    class Dialog:
        Accepted = 1

        def __init__(self, parent, existing=None):
            pass

        def exec_(self):
            return self.Accepted

        def result_session(self):
            return {"name": "Bench", "type": "usb", "serial": "ABC123"}

    monkeypatch.setattr(mw, "SessionDialog", Dialog)
    monkeypatch.setattr(window.store, "save", _unreadable)
    window.new_session()
    assert window.logged[-1].startswith("[ERROR] Could not save the target 'Bench': ")
    assert window.boxes[-1][:2] == ("warning", "Saved targets")
    assert "sharing violation" in window.boxes[-1][2]


def test_an_edit_that_cannot_be_saved_is_reported_plainly(window, monkeypatch):
    class Dialog:
        Accepted = 1

        def __init__(self, parent, existing=None):
            pass

        def exec_(self):
            return self.Accepted

        def result_session(self):
            return {"name": "Bench 2", "type": "usb", "serial": "ABC123",
                    "previous_name": "Bench"}

    window.store.save({"name": "Bench", "type": "usb", "serial": "ABC123"})
    window.refresh_sessions()
    window.session_list.setCurrentRow(0)
    monkeypatch.setattr(mw, "SessionDialog", Dialog)
    monkeypatch.setattr(window.store, "save", _unreadable)
    window.edit_session()
    assert window.logged[-1].startswith("[ERROR] Could not save the changes to 'Bench': ")
    assert window.boxes[-1][:2] == ("warning", "Saved targets")


def test_discovered_devices_that_cannot_be_saved_can_still_be_opened(window, monkeypatch):
    asked = []
    monkeypatch.setattr(mw.QMessageBox, "question", staticmethod(
        lambda *args, **_kw: asked.append(args[2]) or mw.QMessageBox.No))
    monkeypatch.setattr(window.store, "remember", _unreadable)
    window._on_discovered([{"service": "connect", "address": "10.0.0.7:41555",
                            "host": "10.0.0.7", "port": 41555, "name": "adb-HU"}])
    assert "[WARNING] Could not add 10.0.0.7:41555 to Saved targets: " in window.logged[-2]
    assert window.logged[-1] == "[OK] Found 1 Wireless-debugging device(s)"
    assert asked and "saved to the sidebar" not in asked[0]


def test_a_device_discovered_on_a_new_port_updates_its_saved_target(window, monkeypatch):
    """Wireless debugging picks a new port each time it starts, and every
    discovery saved another "IP:PORT" target."""
    monkeypatch.setattr(mw.QMessageBox, "question",
                        staticmethod(lambda *args, **_kw: mw.QMessageBox.No))
    for port in (41555, 37123, 37123):
        window._on_discovered([{"service": "connect", "address": f"10.0.0.7:{port}",
                                "host": "10.0.0.7", "port": port,
                                "name": "adb-R58M123-Xy1z"}])
    assert window.store.sessions == [{"name": "adb-R58M123-Xy1z", "type": "network",
                                      "host": "10.0.0.7", "port": 37123}]
    assert window.session_list.count() == 1


def test_no_discovery_runs_while_the_adb_server_is_busy(window, monkeypatch):
    """`adb mdns services` starts a server when none answers, in the middle of
    a restart, a share or a tools update."""
    started = []
    monkeypatch.setattr(mw, "_discover_worker", lambda adb_path: started.append(adb_path))
    monkeypatch.setattr(window, "_server_task_running", lambda: True)
    window.discover_wireless()
    assert started == [] and "busy" in window.logged[-1]


def test_a_target_that_cannot_be_deleted_stays_listed(window, monkeypatch):
    window.store.save({"name": "Bench", "type": "usb", "serial": "ABC123"})
    window.refresh_sessions()
    window.session_list.setCurrentRow(0)
    monkeypatch.setattr(mw.QMessageBox, "question",
                        staticmethod(lambda *a, **k: mw.QMessageBox.Yes))
    monkeypatch.setattr(window.store, "delete", _unreadable)
    window.delete_session()
    assert window.logged[-1].startswith("[ERROR] Could not delete 'Bench': ")
    assert window.store.names() == ["Bench"] and window.session_list.count() == 1


def test_connecting_goes_ahead_when_the_target_cannot_be_saved(window, monkeypatch):
    from turboadb.gui import connect_dialog

    class Dialog:
        Accepted = 1

        def __init__(self, parent):
            pass

        def exec_(self):
            return self.Accepted

        def session(self):
            return {"name": "Bench", "type": "usb", "serial": "ABC123"}

        def deleteLater(self):
            pass

    opened = []
    monkeypatch.setattr(connect_dialog, "ConnectDialog", Dialog)
    monkeypatch.setattr(window.store, "save", _unreadable)
    monkeypatch.setattr(window, "_open_session", lambda session, name: opened.append(name))
    window.open_connect()
    assert opened == ["Bench"]
    assert window.logged[-1].startswith("[ERROR] Could not save the target 'Bench': ")


def test_duplicating_twice_keeps_both_copies(window):
    window.store.save({"name": "HU", "type": "network", "host": "10.0.0.5", "port": 5555})
    window.refresh_sessions()
    for _ in range(2):
        window.session_list.setCurrentRow(0)  # the original
        window._duplicate_session()
    assert window.store.names() == ["HU", "HU (copy)", "HU (copy) (2)"]
    assert window.logged[-1] == "[OK] Duplicated 'HU' as 'HU (copy) (2)'"


def test_a_theme_that_cannot_be_saved_still_applies(window, monkeypatch):
    applied = []

    def unwritable(_changes):
        raise OSError("settings.json could not be read (sharing violation)")

    monkeypatch.setattr(mw.settings_mod, "update", unwritable)
    monkeypatch.setattr(mw.theme, "apply_to_app", lambda app, name=None: applied.append(name))
    window._apply_theme("light")
    assert applied == ["light"]
    assert window.logged[-1].startswith("[WARNING] Theme: ")
    assert "could not be saved" in window.logged[-1]


# --------------------------------------------------------------------------- #
# the remote-deploy summary and device sharing
# --------------------------------------------------------------------------- #
def test_a_deploy_that_never_reached_a_host_skipped_none(qapp, monkeypatch):
    boxes = []
    for kind in ("warning", "information"):
        monkeypatch.setattr(mw.QMessageBox, kind, staticmethod(
            lambda *args, _kind=kind, **_kw: boxes.append((_kind, args[1], args[2]))))

    def run(messages):
        logged = []
        fake = types.SimpleNamespace(
            _dep_results=[], _dep_hosts=3, _dep_host_names=["pc1", "pc2", "pc3"],
            _dep_skipped=0, _dep_dlg=None, _log=logged.append, _poll_devices=lambda: None,
            log_panel=types.SimpleNamespace(append=lambda _t: None),
        )
        for message in messages:
            mw.MainWindow._on_deploy_status(fake, message)
        mw.MainWindow._on_deploy_finished(fake)
        return logged

    logged = run(["[INFO] Installing pywinrm (one-time, for WinRM remoting)…",
                  "[ERROR] could not install pywinrm: no network  (try: pip install pywinrm)",
                  "[OK] Remote deploy finished."])
    assert logged[-1] == "[WARNING] Remote deploy finished: nothing was deployed"
    assert boxes[-1][:2] == ("warning", "Remote deploy failed")
    assert "could not install pywinrm" in boxes[-1][2] and "skipped" not in boxes[-1][2]
    # hosts that were reached are still counted one by one
    logged = run(["[OK] pc1: serve started — ok", "[ERROR] pc2: WinRM failed: timeout",
                  "[OK] Remote deploy finished."])
    assert logged[-1] == "[WARNING] Remote deploy finished: 1 host(s) deployed, 1 failed, 1 skipped"


def _share_messages(monkeypatch, install_startup):
    from turboadb import devices

    monkeypatch.setattr(devices, "start_shared_server", lambda adb_path=None: "shared server up")
    monkeypatch.setattr(devices, "open_firewall", lambda ports: "firewall: opened")
    monkeypatch.setattr(devices, "install_startup", install_startup)
    worker = mw._ShareThread(install_startup=True)
    messages = []
    worker.msg.connect(messages.append)
    worker.run()
    return messages


def test_a_failed_login_auto_start_does_not_fail_the_share(qapp, monkeypatch):
    def refuse(port=5037, adb_path=None):
        raise RuntimeError("the interpreter path cannot be written into a batch file")

    messages = _share_messages(monkeypatch, refuse)
    assert "[OK] shared server up" in messages
    assert ("[WARNING] Could not set up the login auto-start: the interpreter path cannot be "
            "written into a batch file") in messages
    assert messages[-1].startswith("[OK] Other machines can now connect")
    assert not any(message.startswith("[ERROR]") for message in messages)


class _ShareBox:
    """QMessageBox as share_devices uses it: records the offered buttons and
    answers Cancel."""
    Question = AcceptRole = YesRole = RejectRole = 0
    offered = []

    def __init__(self, parent):
        self.buttons = []

    def setWindowTitle(self, _title):
        pass

    def setIcon(self, _icon):
        pass

    def setText(self, text):
        self.text = text

    def addButton(self, text, _role):
        button = types.SimpleNamespace(text=text)
        self.buttons.append(button)
        return button

    def exec_(self):
        _ShareBox.offered = [button.text for button in self.buttons]

    def clickedButton(self):
        return self.buttons[-1]


@pytest.mark.skipif(os.name == "nt", reason="the login auto-start is a Windows launcher")
def test_run_at_login_is_not_offered_where_it_cannot_work(window, monkeypatch):
    monkeypatch.setattr(mw, "QMessageBox", _ShareBox)
    window.share_devices()
    assert _ShareBox.offered == ["Start once", "Cancel"]


@pytest.mark.skipif(os.name != "nt", reason="the login auto-start is a Windows launcher")
def test_run_at_login_is_offered_on_windows(window, monkeypatch):
    monkeypatch.setattr(mw, "QMessageBox", _ShareBox)
    window.share_devices()
    assert _ShareBox.offered == ["Start + run at login", "Start once", "Cancel"]


# --------------------------------------------------------------------------- #
# the adb server's tasks and the device tabs on it
# --------------------------------------------------------------------------- #
class _ServerTab:
    """A device tab on an adb server (host None: this PC's), recording what
    the window's server tasks ask of it."""

    def __init__(self, host=None, port=5037):
        config = types.SimpleNamespace(adb_server_host=host, adb_server_port=port)
        self.handler = types.SimpleNamespace(config=config)
        self.events = []

    def prepare_for_adb_restart(self):
        self.events.append("paused")

    def finish_adb_restart(self, success):
        self.events.append(("resumed", success))


def _deliver(qapp, thread):
    """Let *thread* end and its queued signals (done, finished) arrive."""
    assert thread.wait(5000)
    for _ in range(5):
        qapp.processEvents()


class _ToolsTask(QThread):
    """A tools download or upgrade that runs until released; the local adb
    server answers afterwards when ``server_up_after`` says so."""
    progress = pyqtSignal(int)
    stage = pyqtSignal(str)
    done = pyqtSignal(dict)
    release = threading.Event()
    server_up_after = False
    made = []

    def __init__(self, mode="fetch", force=False):
        super().__init__()
        self.mode = mode
        self.server_up = False
        _ToolsTask.made.append(self)

    def run(self):
        _ToolsTask.release.wait(10)
        self.server_up = _ToolsTask.server_up_after
        self.done.emit({"updated": {}, "checks": {}} if self.mode == "upgrade"
                       else {"adb": "C:/tools/adb.exe", "scrcpy": "C:/tools/scrcpy.exe"})


@pytest.fixture
def tools_task(monkeypatch):
    _ToolsTask.release, _ToolsTask.server_up_after, _ToolsTask.made = threading.Event(), False, []
    monkeypatch.setattr(mw, "_ToolsDownloadThread", _ToolsTask)
    yield _ToolsTask
    _ToolsTask.release.set()
    for task in _ToolsTask.made:
        task.wait(5000)


@pytest.mark.parametrize("action", ["Download and reinstall ADB", "Check for updates"])
def test_a_tools_update_pauses_the_device_tabs_as_a_restart_does(
        qapp, window, monkeypatch, tools_task, action):
    """It stops the adb server to replace adb.  The tabs went on: their shells
    ended, their shell-lost and reconnect paths ran plain adb commands, and
    adb started the server again itself, from the folder being replaced (the
    swap then failed), as a server TurboADB never recorded."""
    from turboadb import update

    local, remote = _ServerTab(), _ServerTab("lab-pc")
    monkeypatch.setattr(window, "_device_tabs", lambda: [local, remote])
    if action == "Download and reinstall ADB":
        window._run_tools("fetch", "Downloading…", force=True)
    else:
        monkeypatch.setattr(update, "can_self_update", lambda: False)  # adb and scrcpy only
        window.upgrade_tools_gui()
        _deliver(qapp, window._upd_chk)
    task = window._dl
    assert task in tools_task.made
    assert task.mode == ("fetch" if action.startswith("Download") else "upgrade")
    assert local.events == ["paused"] and remote.events == []  # its server is not touched
    assert window._server_task_running()
    tools_task.release.set()
    _deliver(qapp, task)
    # The server is down after the swap: the tab waits for the start that
    # follows, instead of starting one with adb's own commands.
    assert local.events == ["paused"]
    window._adb_init.ready.emit(True, "ADB server active (127.0.0.1:5037)")
    assert local.events == ["paused", ("resumed", True)] and remote.events == []


def test_tabs_resume_at_once_when_the_tools_update_left_the_server_running(
        qapp, window, monkeypatch, tools_task):
    local = _ServerTab()
    monkeypatch.setattr(window, "_device_tabs", lambda: [local])
    tools_task.server_up_after = True  # nothing was replaced: the server still answers
    window._run_tools("fetch", "Downloading…", force=True)
    tools_task.release.set()
    _deliver(qapp, window._dl)
    assert local.events == ["paused", ("resumed", True)]


def test_a_self_update_pauses_the_device_tabs_too(qapp, window, monkeypatch):
    release = threading.Event()

    class Upgrade(QThread):
        progress = pyqtSignal(str)
        done = pyqtSignal(dict)

        def __init__(self, expected=None):
            super().__init__()
            self.server_up = False

        def run(self):
            release.wait(10)
            self.server_up = True  # pip failed before adb was touched
            self.done.emit({"ok": False, "error": "pip failed"})

    local = _ServerTab()
    monkeypatch.setattr(window, "_device_tabs", lambda: [local])
    monkeypatch.setattr(mw, "_AppUpgradeThread", Upgrade)
    monkeypatch.setattr(mw.QMessageBox, "question",
                        staticmethod(lambda *a, **k: mw.QMessageBox.Ok))
    window._do_self_update("99.0.0")
    try:
        assert local.events == ["paused"]
    finally:
        release.set()
    _deliver(qapp, window._upd_run)
    assert local.events == ["paused", ("resumed", True)]


def test_a_restart_waits_for_another_adb_server_task(window, monkeypatch):
    """Restart ran on top of a share, its undoing, a tools download or an
    update (Share and Stop sharing already refused), from the ribbon, every
    terminal's Restart ADB button and the prompt after a new adb path.  A
    tools download or a check for updates now waits for a restart too."""
    local = _ServerTab()
    monkeypatch.setattr(window, "_device_tabs", lambda: [local])
    monkeypatch.setattr(window, "_server_task_running", lambda: True)
    started = []
    monkeypatch.setattr(mw, "_adb_restart_worker", lambda adb_path: started.append(adb_path))
    monkeypatch.setattr(mw, "_ToolsDownloadThread", lambda **kw: started.append(kw))
    monkeypatch.setattr(mw, "_upgrade_check_worker", lambda: started.append("check"))
    window.restart_adb_server()
    assert "busy" in window.logged[-1] and "restart it once" in window.logged[-1]
    assert not window._start_tools_task("fetch", "Downloading…", "TurboADB — tools",
                                        window._tools_done)
    assert "busy" in window.logged[-1]
    window.upgrade_tools_gui()
    assert "busy" in window.logged[-1]
    assert started == [] and local.events == []


def test_restart_and_share_pause_only_the_tabs_on_that_server(qapp, window, monkeypatch):
    """A tab on another machine's adb server lost its shell to a local
    restart or share for nothing, and stayed disconnected if it failed."""
    monkeypatch.delenv("ANDROID_ADB_SERVER_PORT", raising=False)  # this PC's server on 5037
    local, loopback = _ServerTab(), _ServerTab("127.0.0.1", 5037)
    remote, other_port = _ServerTab("lab-pc"), _ServerTab(None, 5038)
    tabs = [local, loopback, remote, other_port]
    monkeypatch.setattr(window, "_device_tabs", lambda: tabs)
    monkeypatch.setattr(mw, "_adb_restart_worker",
                        lambda adb_path: mw.FunctionThread(lambda: "[OK] ADB server restarted"))
    window.restart_adb_server()
    assert [tab.events for tab in tabs] == [["paused"], ["paused"], [], []]
    _deliver(qapp, window._as)
    assert local.events == loopback.events == ["paused", ("resumed", True)]
    assert remote.events == other_port.events == []

    class Share(QThread):
        server_up = True

        def run(self):
            pass

    for tab in tabs:
        tab.events = []
    share = Share()
    window.run_share_task(share, 5038)  # "Start shared server on THIS PC" on port 5038
    assert [tab.events for tab in tabs] == [[], [], [], ["paused"]]
    _deliver(qapp, share)
    assert other_port.events == ["paused", ("resumed", True)]
    assert local.events == loopback.events == remote.events == []


def test_restarting_a_shared_server_keeps_it_shared(qapp, monkeypatch):
    """The restart stopped the shared server and started a localhost-only one:
    every other machine lost this PC's devices while the log said
    "restarted"."""
    from turboadb import core, devices, tools

    port = tools.local_adb_port()
    done = []
    monkeypatch.setattr(devices, "server_is_shared", lambda port=5037, adb_path=None: True)
    monkeypatch.setattr(tools, "kill_adb_server",
                        lambda adb_path=None, port=5037, **kw: done.append(("stop", adb_path, port)))
    monkeypatch.setattr(devices, "restart_shared_server",
                        lambda port=5037, adb_path=None: done.append(("share", adb_path, port))
                        or f"shared adb server is listening on 0.0.0.0:{port}")
    monkeypatch.setattr(core.ADBHandler, "restart_server",
                        lambda self, safe=None: pytest.fail("a plain restart"))
    worker = mw._adb_restart_worker("C:/tools/adb.exe")
    results = []
    worker.done.connect(results.append)
    worker.run()  # the worker's body, on this thread
    assert done == [("stop", "C:/tools/adb.exe", port), ("share", "C:/tools/adb.exe", port)]
    assert results == ["[OK] ADB server restarted, still shared on the network: "
                       f"shared adb server is listening on 0.0.0.0:{port}"]


def test_check_for_updates_asks_whether_pip_can_update_off_the_ui_thread(
        qapp, window, monkeypatch):
    """update.can_self_update() runs a fresh Python the first time (up to
    20 s): asked by the button itself, it froze the window."""
    from turboadb import update

    asked_on = []
    monkeypatch.setattr(update, "can_self_update",
                        lambda: asked_on.append(threading.current_thread()) or True)
    monkeypatch.setattr(update, "check", lambda: "99.0.0")
    offered = []
    monkeypatch.setattr(window, "_do_self_update", offered.append)
    window.upgrade_tools_gui()
    _deliver(qapp, window._upd_chk)
    assert asked_on and threading.main_thread() not in asked_on
    assert offered == ["99.0.0"]
