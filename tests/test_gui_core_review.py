"""Regression tests for the review fixes in the large GUI modules: the device
tab, the mirror panel and the main window.  Headless (Qt offscreen platform);
no device, adb or network is used."""

from __future__ import annotations

import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytest.importorskip("PyQt5")


class _Session:
    """A scrcpy session stand-in that is still running."""

    log_path = None

    def __init__(self):
        self.running = True

    def read_log(self):
        return ""

    def stop(self):
        self.running = False

    def wait(self, timeout=None):
        self.running = False
        return 0


class _Handler:
    serial = "mock"
    config = None


def _panel(**kwargs):
    from turboadb.gui.mirror_panel import MirrorPanel

    return MirrorPanel(_Handler(), {"name": "mock"}, **kwargs)


def _close_panel(panel):
    panel._scrcpy = None
    panel._recording = False
    panel.close_panel()
    panel.close()


# --------------------------------------------------------------------------- #
# mirror_panel.py
# --------------------------------------------------------------------------- #
def test_closing_during_integrated_recording_finalizes_through_rec_wait(app, monkeypatch):
    """B12: a tab closed mid-recording must use the 15 s WM_CLOSE path."""
    import turboadb.gui.mirror_panel as mp_mod

    created = []

    class _FakeCloseThread:
        def __init__(self, session, window_title=None, window_handle=None):
            created.append((type(self).__name__, session))
            self.done = types.SimpleNamespace(connect=lambda _slot: None)

        def start(self):
            pass

        def isRunning(self):
            return False

        def isFinished(self):
            return True

    class RecWait(_FakeCloseThread):
        pass

    class MirrorStop(_FakeCloseThread):
        pass

    monkeypatch.setattr(mp_mod, "_RecWaitThread", RecWait)
    monkeypatch.setattr(mp_mod, "_MirrorStopThread", MirrorStop)
    panel = _panel()
    try:
        session = _Session()
        panel._scrcpy = session
        panel._recording = True
        panel._rec_mode = "integrated"
        panel._rec_path = "clip.mp4"
        panel.close_panel()
        assert created == [("RecWait", session)]
        assert panel._scrcpy is None
        assert panel._record_finalizing is True
    finally:
        panel.close()


def test_failed_embed_does_not_keep_a_child_handle(app, monkeypatch):
    """B13: _child_hwnd is only set once Windows accepted the reparent."""
    import turboadb.gui.mirror_panel as mp_mod
    from PyQt5.QtCore import QTimer

    def refuse(*_args):
        raise OSError("Windows rejected the scrcpy child-window attachment")

    monkeypatch.setattr(mp_mod, "_find_window", lambda _title: 4242)
    # the fake window has already been shown by SDL, so embedding is attempted
    monkeypatch.setattr(mp_mod, "_window_ready", lambda _hwnd: True)
    monkeypatch.setattr(mp_mod, "_reparent", refuse)
    panel = _panel()
    try:
        rescued = []
        panel._rescue_offscreen = lambda hwnd=None: rescued.append(hwnd) or False
        panel._scrcpy = _Session()
        panel._embed_on = True
        panel._win_title = "turboadb-embed-test"
        panel._win_first_seen = time.time() - 5
        panel.container.resize(400, 300)
        panel._embed_timer = QTimer(panel)
        panel._try_embed()
        assert rescued == [4242]
        assert panel._child_hwnd is None
        assert panel._embed_on is False
        assert panel._separate_hwnd == 4242
        assert panel._embed_timer is None
    finally:
        _close_panel(panel)


def test_ready_session_poll_does_not_reread_the_log(app):
    """B14: the 500 ms UI-thread monitor stops reading the log once ready."""
    panel = _panel()

    class Session(_Session):
        def read_log(self):
            raise AssertionError("a ready session must not re-read its log every tick")

    try:
        panel._scrcpy = Session()
        panel._became_ready = True
        panel._start_t = time.time()
        panel._check_alive()
        assert panel._scrcpy is not None
    finally:
        _close_panel(panel)


def test_log_tail_reads_only_the_end(tmp_path):
    from turboadb.gui.mirror_panel import _read_log_tail

    log = tmp_path / "scrcpy.log"
    log.write_bytes(b"x" * 5000 + b"INFO: Renderer: direct3d\n")
    tail = _read_log_tail(types.SimpleNamespace(log_path=str(log)), limit=64)
    assert tail.endswith("Renderer: direct3d\n")
    assert len(tail) <= 64


def test_window_lookup_never_matches_an_empty_title():
    """FindWindowW(None, None) returns an arbitrary window; never post WM_CLOSE to it."""
    from turboadb.gui.mirror_panel import _find_window, _post_close

    assert _find_window(None) is None
    assert _find_window("") is None
    assert _post_close(None) is False


def _wall_with_fake_starts(displays):
    from turboadb.gui.display_wall import DisplayWall

    wall = DisplayWall(types.SimpleNamespace(serial="ivi", config=None), {"name": "ivi"}, automotive=True)
    wall.set_displays(displays)
    started, stopped = [], []
    for tile in wall._tiles:
        tile.panel.start = lambda t=tile: started.append(t.display_id)
        tile.panel.stop = lambda t=tile: stopped.append(t.display_id)
        tile.panel.is_active = lambda t=tile: t.display_id in started and t.display_id not in stopped
    return wall, started, stopped


def test_display_wall_starts_every_display_one_after_another(app):
    """Issue: each IVI display had to be started by hand, one by one."""
    wall, started, stopped = _wall_with_fake_starts(
        [{"id": 2, "size": "1920x720"}, {"id": 0, "size": "1920x720"}, {"id": 3, "size": "800x480"}]
    )
    try:
        assert [tile.display_id for tile in wall._tiles] == [0, 2, 3]
        wall.start_all()
        assert started == [0]  # never several scrcpy servers starting at once
        wall._on_tile_ready(wall._tiles[1])  # not the one being waited on: ignored
        assert started == [0] and wall._next_timer.remainingTime() > 5000
        wall._on_tile_ready(wall._tiles[0])  # display 0 is up: the next follows shortly
        # the interval asked for, not remainingTime(): Qt may run a normal
        # timer up to 5 % late (525 ms on Linux) to group wake-ups
        assert wall._next_timer.isActive()
        assert wall._next_timer.interval() == wall.START_GAP_MS
        wall._start_next()
        assert started == [0, 2]
        wall._start_next()  # display 2 never came up (timeout): move on
        assert started == [0, 2, 3]
        wall.stop_all()
        assert stopped == [0, 2, 3]
        wall.set_displays([{"id": 0}, {"id": 2}, {"id": 3}, {"id": 4}])
        assert started == [0, 2, 3]  # stopped by the user: a rescan doesn't restart them
    finally:
        wall.close_panel()
        wall.close()


def test_display_wall_focus_shows_one_display_then_all(app):
    wall, _started, _stopped = _wall_with_fake_starts([{"id": 0}, {"id": 1}])
    try:
        first, second = wall._tiles
        second.btn_focus.setChecked(True)
        assert wall._focused is second and first.isHidden() and not second.isHidden()
        first.btn_focus.setChecked(True)  # focusing another display switches to it
        assert wall._focused is first and second.isHidden() and not second.btn_focus.isChecked()
        first.btn_focus.setChecked(False)
        assert wall._focused is None and not first.isHidden() and not second.isHidden()
    finally:
        wall.close_panel()
        wall.close()


def test_options_popover_restyles_on_theme_refresh(app):
    """B22: the popover follows live theme switches through the application
    stylesheet (#screenOptions rules), not per-widget stylesheets."""
    from turboadb.gui import theme

    panel = _panel()
    try:
        panel.refresh_theme()  # MainWindow._apply_theme still calls it
        assert panel._opts_panel.objectName() == "screenOptions"
        assert not panel._opts_panel.styleSheet()
        names = {label.objectName() for label, _is_title in panel._opts_headings}
        assert names == {"screenOptionsTitle", "screenOptionsSection"}
        assert not [label for label, _is_title in panel._opts_headings if label.styleSheet()]
        light = theme.stylesheet("light")
        raised = theme.palette("light")["raised"]
        assert f"QWidget#screenOptions {{ background: {raised}; }}" in light
        assert f"QLabel#screenOptionsSection {{ color: {theme.accent_text('light')};" in light
        assert f"QLabel#screenOptionsTitle {{ color: {theme.accent_text('light')};" in light
    finally:
        _close_panel(panel)


def test_keyboard_bar_goes_straight_to_the_shared_dispatcher_in_order(app):
    """B23: one queue (the dispatcher) and ordered, batched text."""
    from turboadb.gui.mirror_panel import MirrorPanel

    order = []

    class Dispatcher:
        def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
            result = fn()
            order.append(result)
            if on_done:
                on_done(result)
            return True

        def stop(self):
            pass

        def isRunning(self):
            return False

    class Handler(_Handler):
        def input_text(self, text, safe=False):
            return ("text", text, safe)

        def keyevent(self, code, safe=False):
            return ("key", code, safe)

    panel = MirrorPanel(Handler(), {"name": "mock"}, dispatcher=Dispatcher())
    try:
        panel._send_key("text", "a")
        panel._send_key("text", "b")
        panel._send_key("key", 66)
        assert order == [("text", "ab", True), ("key", 66, True)]
        panel.close_panel()
        panel._send_key("text", "late")
        panel._key_batcher.flush()
        assert order == [("text", "ab", True), ("key", 66, True)]
    finally:
        panel.close()


def test_max_view_restores_docks_after_repeated_resume(app):
    """Resuming max view twice used to forget which docks it had hidden."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QDockWidget, QMainWindow, QWidget

    win = QMainWindow()
    dock = QDockWidget("Log", win)
    dock.setWidget(QWidget())
    win.addDockWidget(Qt.BottomDockWidgetArea, dock)
    panel = _panel()
    win.setCentralWidget(panel)
    win.show()
    app.processEvents()
    try:
        panel.act_max.setChecked(True)
        assert not dock.isVisible()
        panel._resume_max()  # e.g. a tab switch and split view both resume
        panel._suspend_max()
        assert dock.isVisible()
        panel._resume_max()
        assert not dock.isVisible()
        panel.act_max.setChecked(False)
        assert dock.isVisible()
    finally:
        panel.close_panel()
        win.close()


def test_split_view_reattaches_embedded_screen_when_container_changes(app, monkeypatch):
    """B21 safety net: re-adopt scrcpy only when the container handle changed."""
    import turboadb.gui.mirror_panel as mp_mod

    reparented = []
    panel = _panel()
    try:
        current = int(panel.container.winId())
        parent_of = {"hwnd": current}
        monkeypatch.setattr(mp_mod, "_IS_WIN", True)
        monkeypatch.setattr(
            mp_mod,
            "_win_api",
            lambda: (None, types.SimpleNamespace(GetParent=lambda _hwnd: parent_of["hwnd"])),
        )
        monkeypatch.setattr(
            mp_mod, "_reparent", lambda child, parent, _w, _h: reparented.append((child, parent))
        )
        panel._fit = lambda: None
        panel._scrcpy = _Session()
        panel._child_hwnd = 77
        panel._embed_parent_hwnd = current
        panel.ensure_embedded()
        assert reparented == []
        panel._embed_parent_hwnd = 1  # Device Control moved to a new native parent
        panel.ensure_embedded()
        assert reparented == [(77, current)]
        assert panel._embed_parent_hwnd == current
    finally:
        panel._child_hwnd = None
        _close_panel(panel)


def test_display_switch_is_blocked_while_scrcpy_records(app):
    panel = _panel()
    try:
        panel._got_displays([{"id": 0, "size": "1080x2400"}, {"id": 1, "size": "1920x720"}])
        starts = []
        panel.start = lambda **kwargs: starts.append(kwargs)
        panel._scrcpy = _Session()
        panel._recording = True
        panel._rec_mode = "integrated"
        panel._launch_spec = {"display_id": 0}
        panel._on_display_combo_changed(panel.cmb_display.findData(1))
        assert starts == []
        assert panel.cmb_display.currentData() is None  # display 0 = the default display

        panel._recording = False
        panel._rec_mode = None
        panel._on_display_combo_changed(panel.cmb_display.findData(1))
        assert starts == [{"display_id": 1}]
        assert panel.cmb_display.currentData() == 1
    finally:
        _close_panel(panel)


# --------------------------------------------------------------------------- #
# device_tab.py
# --------------------------------------------------------------------------- #
def _device_tab(**kwargs):
    from turboadb.gui.device_tab import DeviceTab

    return DeviceTab({"name": "mock", "serial": "123"}, **kwargs)


def _close_tab(tab):
    tab.handler = None
    tab.close_session()
    tab.close()


def test_device_tab_mirror_keeps_device_control_defaults(app):
    """B10: the ribbon/menu Mirror follows the selected display and IVI compat."""
    tab = _device_tab()
    try:
        calls = []
        tab.handler = object()
        tab.mirror_tab = types.SimpleNamespace(start=lambda **kwargs: calls.append(kwargs))
        tab.mirror()
        assert calls == [{"display_id": "__use_combo__", "compat": None, "embed": None}]
    finally:
        _close_tab(tab)


def test_connect_result_after_close_builds_nothing(app, monkeypatch):
    """B19: a queued connect result must not build panels on a closed tab."""
    from turboadb.gui.device_tab import DeviceTab

    released = []
    monkeypatch.setattr(DeviceTab, "_release_handler", staticmethod(released.append))
    tab = _device_tab()
    tab.close_session()
    handler = object()
    tab._on_connected(handler, None)
    assert released == [handler]
    assert tab.handler is None
    assert not hasattr(tab, "shell")
    tab.close()


def test_failed_connect_and_reconnect_timeout_offer_a_retry(app, monkeypatch):
    """B20: failures leave a Reconnect path and a usable Reboot button."""
    from turboadb.gui import device_tab as dt_mod

    monkeypatch.setattr(dt_mod.QMessageBox, "warning", lambda *_args, **_kwargs: None)
    tab = _device_tab()
    try:
        tab._started_connect = True
        tab._on_fail("device offline")
        assert tab._started_connect is False
        assert not tab.btn_reconnect.isHidden()

        started = []
        monkeypatch.setattr(tab, "start_connect", lambda: started.append(True))
        tab.reconnect_device()
        assert started == [True]
        assert tab.btn_reconnect.isHidden()

        tab.handler = object()
        tab._enable_actions(False)
        tab._reconnecting = True
        tab._on_reconnected(False)
        assert not tab._reconnecting
        assert not tab.btn_reconnect.isHidden()
        assert tab.btn_reboot.isEnabled()
    finally:
        _close_tab(tab)


def test_closing_before_connect_does_not_park_unstarted_workers(app):
    """A never-started QThread never emits finished and would stay parked forever."""
    from turboadb.gui import qtutil

    tab = _device_tab()
    connect = tab._ct
    tab.close_session()
    assert connect not in qtutil._parked
    tab.close()


def test_hidden_device_tab_releases_max_view(app):
    from PyQt5.QtWidgets import QWidget

    calls = []
    tab = _device_tab()
    try:
        tab.mirror_tab = types.SimpleNamespace(
            act_max=object(),
            _suspend_max=lambda: calls.append("suspend"),
            _resume_max=lambda: calls.append("resume"),
            yield_keyboard=lambda: calls.append("yield"),
        )
        tab.combo_view = QWidget()
        tab.inner.addTab(tab.combo_view, "Device Control")
        tab.inner.setCurrentWidget(tab.combo_view)
        calls.clear()
        tab.set_workspace_active(True)
        assert calls == ["resume"]
        calls.clear()
        tab.set_workspace_active(False)
        assert calls == ["suspend", "yield"]
    finally:
        _close_tab(tab)


def test_android_shell_stop_never_kills_device_wide_processes(app):
    """B16: Stop ends this shell only; no pkill/killall of logcat or top."""
    from turboadb.gui.device_tab import _AndroidShellWidget

    class Handler:
        serial = "123"

        def __init__(self):
            self.shell_commands = []
            self.opened = 0

        def open_shell(self, tty=True, safe=None):
            self.opened += 1
            return None

        def shell(self, command, **_kwargs):
            self.shell_commands.append(command)

    handler = Handler()
    widget = _AndroidShellWidget(handler, device_name="mock")
    try:
        widget.interrupt()
        time.sleep(0.3)  # the old reaper ran on a background thread
        assert handler.opened == 2
        assert not any(
            "pkill" in command or "killall" in command for command in handler.shell_commands
        )
    finally:
        widget.close_panel()
        widget.close()


def test_prompt_probe_reads_the_device_user_and_host_in_safe_mode(app):
    """B7: the probe copies the device's own user@host and root state, in safe mode."""
    from turboadb.gui.device_tab import _PromptThread
    from turboadb.results import CommandResult, OperationResult

    class Handler:
        def shell(self, command, **kwargs):
            assert command == _PromptThread.COMMAND
            assert kwargs.get("safe") is True
            return OperationResult(
                True, "shell", value=CommandResult(command, 0, "0|root|adelegg\n", "", 0.0)
            )

    probe = _PromptThread(Handler())
    results = []
    probe.ready.connect(lambda root, identity: results.append((root, identity)))
    probe.run()
    assert results == [(True, "root@adelegg")]


def test_prompt_probe_parse_falls_back_safely():
    from turboadb.gui.device_tab import _PromptThread

    assert _PromptThread.parse("2000|shell|V2318\n") == (False, "shell@V2318")
    assert _PromptThread.parse("0||adelegg") == (True, "root@adelegg")
    assert _PromptThread.parse("0|root|") == (True, "")
    assert _PromptThread.parse("sh: id: not found") == (False, "")
    assert _PromptThread.parse("") == (False, "")


def test_console_prompt_shows_the_device_user_and_host(app):
    """The prompt reads like the device's shell (root@adelegg), never adb@<model>."""
    from turboadb.gui.console import AnsiConsole
    from turboadb.results import strip_ansi

    con = AnsiConsole()
    con.set_prompt("root@adelegg", root=True)
    assert strip_ansi(con._prompt_text()).startswith("root@adelegg:/ #")
    con.set_prompt("V2318", root=False)
    assert strip_ansi(con._prompt_text()).startswith("shell@V2318:/ $")
    con.set_prompt("", root=False)
    assert "adb@" not in strip_ansi(con._prompt_text())


def test_device_actions_treat_safe_mode_failures_as_errors():
    from turboadb.gui.device_tab import DeviceTab
    from turboadb.results import OperationResult

    assert DeviceTab._action_error(OperationResult(False, "reboot", error="offline")) == "offline"
    assert DeviceTab._action_error(OperationResult(True, "reboot", value=False))
    assert DeviceTab._action_error(OperationResult(True, "root", value="adbd is root")) is None
    assert DeviceTab._action_value(OperationResult(True, "root", value="adbd is root")) == (
        "adbd is root"
    )


# --------------------------------------------------------------------------- #
# main_window.py
# --------------------------------------------------------------------------- #
def _fake_log_window(status, *, dock_visible=False, silent=False):
    from turboadb.gui.main_window import MainWindow

    return types.SimpleNamespace(
        log_panel=types.SimpleNamespace(
            append=lambda _text: None,
            chk_silent=types.SimpleNamespace(isChecked=lambda: silent),
        ),
        _log_dock=types.SimpleNamespace(isVisible=lambda: dock_visible),
        _LOG_LEVEL_RE=MainWindow._LOG_LEVEL_RE,
        _LEVEL_ALIASES=MainWindow._LEVEL_ALIASES,
        _show_log_dock=lambda: None,
        statusBar=lambda: types.SimpleNamespace(showMessage=lambda text, _ms: status.append(text)),
    )


def test_log_messages_reach_status_bar_and_toasts(app, monkeypatch):
    from turboadb.gui import fileutil
    from turboadb.gui.main_window import MainWindow

    activity, errors, status = [], [], []
    monkeypatch.setattr(
        fileutil, "activity_toast",
        lambda _parent, message, **kw: activity.append((kw.get("level"), message)),
    )
    monkeypatch.setattr(fileutil, "error_toast", lambda _parent, message, **_kw: errors.append(message))
    # "Silent" is on by default; with the log closed it must not mute anything.
    fake = _fake_log_window(status, silent=True)
    MainWindow._log(fake, "[WARNING] first")
    MainWindow._log(fake, "[ERROR] second")
    MainWindow._log(fake, "[OK] Saved screenshot")
    MainWindow._log(fake, "Rebooting to recovery…")
    MainWindow._log(fake, "$ adb -s x shell ls")
    MainWindow._log(fake, "[DEBUG] noise")
    assert activity == [
        ("warning", "first"), ("ok", "Saved screenshot"), ("info", "Rebooting to recovery…"),
    ]
    assert errors == ["second"]
    assert status == [
        "Warning: first", "Error: second", "Saved screenshot", "Rebooting to recovery…",
    ]


def test_silent_mutes_toasts_only_while_the_log_is_open(app, monkeypatch):
    from turboadb.gui import fileutil
    from turboadb.gui.main_window import MainWindow

    toasts, status = [], []
    monkeypatch.setattr(fileutil, "activity_toast", lambda *_a, **_k: toasts.append("activity"))
    monkeypatch.setattr(fileutil, "error_toast", lambda *_a, **_k: toasts.append("error"))
    MainWindow._log(_fake_log_window(status, dock_visible=True, silent=True), "[ERROR] boom")
    assert toasts == [] and status == ["Error: boom"]
    MainWindow._log(_fake_log_window(status, dock_visible=True, silent=False), "[ERROR] boom")
    assert toasts == ["error"]


def test_device_poll_stays_socket_only_while_adb_restarts(app, monkeypatch):
    """`adb devices` in the kill→start gap would auto-start a competing daemon."""
    import turboadb.gui.main_window as mw_mod

    created = []

    class Poll:
        def __init__(self, adb_path=None, *, socket_only=False, parent=None):
            created.append(socket_only)
            self.result = types.SimpleNamespace(connect=lambda _slot: None)
            self.failed = types.SimpleNamespace(connect=lambda _slot: None)

        def start(self):
            pass

    monkeypatch.setattr(mw_mod, "_DevicesPoll", Poll)
    monkeypatch.setattr(mw_mod, "gui_adb_path", lambda: None)
    fake = types.SimpleNamespace(
        _poll=None,
        _server_task_running=lambda: True,
        _on_devices=None,
        _on_device_poll_error=None,
    )
    mw_mod.MainWindow._poll_devices(fake)
    assert created == [True]


def test_changed_adb_path_offers_a_server_restart(app, monkeypatch):
    """B25: a new adb binary must take over the daemon."""
    import turboadb.gui.main_window as mw_mod

    changes = {"adb_path": "C:/new/adb.exe"}

    class Dialog:
        Accepted = 1

        def __init__(self, _parent):
            pass

        def exec_(self):
            return 1

        def changed_settings(self):
            return dict(changes)

    monkeypatch.setattr(mw_mod, "SettingsDialog", Dialog)
    offered = []
    fake = types.SimpleNamespace(
        _apply_theme=lambda *_args, **_kwargs: None,
        _log=lambda _text: None,
        _offer_adb_restart_for_new_binary=lambda: offered.append(True),
    )
    mw_mod.MainWindow.show_settings(fake)
    assert offered == [True]

    changes.clear()  # e.g. only the font changed
    mw_mod.MainWindow.show_settings(fake)
    assert offered == [True]


def test_only_device_usb_target_matches_the_live_device_tab(app):
    """An 'only device' USB target and the live device must not open two tabs."""
    from turboadb.gui.main_window import MainWindow

    fake = types.SimpleNamespace(
        _live_devices=[types.SimpleNamespace(serial="ZX1G22", is_online=True)],
        _session_identity=MainWindow._session_identity,
    )
    fake._resolve_identity = lambda identity: MainWindow._resolve_identity(fake, identity)
    live_tab = types.SimpleNamespace(session={"type": "usb", "serial": "ZX1G22"}, handler=None)

    only_device = fake._resolve_identity(MainWindow._session_identity({"type": "usb", "serial": ""}))
    assert only_device == MainWindow._tab_identity(fake, live_tab)

    fake._live_devices.append(types.SimpleNamespace(serial="ABC123", is_online=True))
    assert fake._resolve_identity(("usb", "")) == ("usb", "")


def test_a_burst_of_failures_updates_one_toast_in_place(app, monkeypatch):
    """Ten failures in a row are still one popup and one alert sound, not ten —
    but no longer by skipping toasts: every failure is shown, in place, and
    the one error toast counts them. A per-severity time limit used to drop
    every message within 4 s of the previous one ("Wi-Fi on" then
    "Bluetooth off" showed only the first)."""
    from PyQt5.QtWidgets import QFrame, QMainWindow

    from turboadb.gui import fileutil
    from turboadb.gui.main_window import MainWindow

    beeps = []
    monkeypatch.setattr(fileutil, "_play_error_sound", lambda: beeps.append(1))
    monkeypatch.setattr(fileutil, "_LAST_ERROR_SOUND", [float("-inf")])
    host = QMainWindow()
    host.resize(760, 520)
    status = []
    fake = _fake_log_window(status)
    fake.window = lambda: host  # the toasts attach to a real window
    assert not hasattr(MainWindow, "_TOAST_INTERVAL_S")
    try:
        for i in range(10):
            MainWindow._log(fake, f"[ERROR] share devices: host {i} refused")
            assert host._turboadb_error_toast.message == f"share devices: host {i} refused"
        error = host._turboadb_error_toast
        assert error.title.text() == "Error (10)"
        assert beeps == [1]
        assert len(status) == 10  # every failure also reaches the log + status bar
        assert len(host.findChildren(QFrame, "errorToast")) == 1
        # Another severity right away is shown too, in the one activity toast:
        # "Wi-Fi on" then "Bluetooth off" both appear, in place.
        MainWindow._log(fake, "[OK] Wi-Fi on")
        activity = host._turboadb_activity_toast
        assert activity.message == activity.text.text() == "Wi-Fi on"
        MainWindow._log(fake, "[INFO] Bluetooth off")
        assert activity.message == activity.text.text() == "Bluetooth off"
        assert len(host.findChildren(QFrame, "activityToast")) == 1
        assert not activity.isHidden() and not error.isHidden()
        assert beeps == [1]  # updating a visible error toast never beeps again
    finally:
        for toast in (getattr(host, "_turboadb_error_toast", None),
                      getattr(host, "_turboadb_activity_toast", None)):
            if toast is not None:
                toast.hide()
        host.deleteLater()


def test_closing_disconnects_workers_before_parking_them(app, monkeypatch):
    """A completion slot running during teardown restarted the poll timer, the
    tracker and the ADB init thread on a window that is going away."""
    import inspect

    import turboadb.gui.main_window as mw_mod

    order = []
    monkeypatch.setattr(
        mw_mod, "disconnect_signals",
        lambda worker, names: order.append(("disconnect", worker, names)),
    )
    monkeypatch.setattr(mw_mod, "park_thread", lambda worker: order.append(("park", worker)))
    poll, upgrade = object(), object()
    fake = types.SimpleNamespace(_poll=poll, _upd_run=upgrade)
    mw_mod.MainWindow._release_workers(fake)
    # every worker is detached first, then parked — never the other way round
    assert order[:2] == [("disconnect", poll, mw_mod._WORKER_SIGNALS), ("park", poll)]
    assert ("disconnect", upgrade, mw_mod._WORKER_SIGNALS) in order
    assert order.index(("disconnect", upgrade, mw_mod._WORKER_SIGNALS)) < order.index(
        ("park", upgrade)
    )
    for name in ("result", "failed", "ready", "done", "finished"):
        assert name in mw_mod._WORKER_SIGNALS
    for attr in ("_poll", "_tracker", "_adb_init", "_as", "_upd_run", "_deploy"):
        assert attr in mw_mod._WORKER_ATTRS
    body = inspect.getsource(mw_mod.MainWindow.closeEvent)
    assert body.index("self._closing = True") < body.index("self._release_workers()")


def test_closing_flag_stops_the_timer_tracker_and_adb_init(app):
    from turboadb.gui.main_window import MainWindow

    started = []
    fake = types.SimpleNamespace(
        _closing=True,
        _timer=types.SimpleNamespace(isActive=lambda: False,
                                     start=lambda _ms: started.append("timer")),
        _tracker=None,
        _adb_init=None,
        _poll=None,
        _DEVICE_POLL_INTERVAL_MS=15_000,
    )
    MainWindow._start_poll_timer(fake)
    MainWindow._start_device_tracker(fake)
    MainWindow._begin_adb_startup(fake)
    MainWindow._poll_devices(fake)
    assert started == []
    assert fake._tracker is None and fake._adb_init is None and fake._poll is None


def test_teardown_failures_are_logged_not_swallowed(app):
    """An orphaned scrcpy surviving a closed tab used to have zero diagnostics."""
    from turboadb.gui.main_window import MainWindow

    # closing a single tab: the user gets the usual notification
    notified = []
    MainWindow._warn_teardown(
        types.SimpleNamespace(_closing=False, _log=notified.append),
        "close 'Pixel'", RuntimeError("scrcpy is still running"),
    )
    assert notified == ["[WARNING] could not close 'Pixel': scrcpy is still running"]

    # closing the window: nothing to toast over, so it goes straight to the log
    lines = []
    MainWindow._warn_teardown(
        types.SimpleNamespace(_closing=True,
                              log_panel=types.SimpleNamespace(append=lines.append)),
        "close 'Pixel'", RuntimeError("scrcpy is still running"),
    )
    assert lines == ["[WARNING] could not close 'Pixel': scrcpy is still running"]

    # A panel that is already gone must not turn a warning into a crash.
    def boom(_text):
        raise RuntimeError("the log panel is deleted")

    MainWindow._warn_teardown(
        types.SimpleNamespace(_closing=True, log_panel=types.SimpleNamespace(append=boom)),
        "close a tab", OSError("nope"),
    )


def test_network_serials_are_split_by_the_shared_parser():
    """rpartition(':') read the IPv6 serial fe80::1 as host 'fe80:', port 1."""
    from turboadb.gui.main_window import _network_target

    assert _network_target("192.168.1.50:5555") == ("192.168.1.50", 5555)
    assert _network_target("[fe80::1]:5555") == ("fe80::1", 5555)
    assert _network_target("fe80::1") == (None, None)  # a bare literal, not host:port
    assert _network_target("ZX1G22") == (None, None)
    assert _network_target("") == (None, None)


def test_connect_dialog_uses_the_shared_parser_and_tunnel_constant():
    from turboadb import scrcpy
    from turboadb.config import parse_host_port
    from turboadb.gui import connect_dialog

    assert connect_dialog._split_host_port is parse_host_port
    assert not hasattr(connect_dialog, "_tunnel_port_range")
    assert connect_dialog.TUNNEL_PORT_FIREWALL_RANGE == scrcpy.TUNNEL_PORT_FIREWALL_RANGE


# --------------------------------------------------------------------------- #
# theme.py
# --------------------------------------------------------------------------- #
def test_stylesheet_declares_each_reviewed_selector_once(app):
    """Two declarations of one selector meant the first was silently dead."""
    from turboadb.gui import theme

    selectors = (
        "QLabel#sidebarSection",
        "QTabWidget#terminalTabs QTabBar::tab",
        "QTabWidget#terminalTabs QTabBar::tab:hover",
        "QTabWidget#terminalTabs QTabBar::tab:selected",
    )
    for name in theme.theme_names():
        starts = [line.strip().split("{")[0].strip() for line in theme.stylesheet(name).splitlines()]
        for selector in selectors:
            assert starts.count(selector) == 1, f"{selector} is declared twice in {name}"
    dark = theme.stylesheet("dark")
    # the kept declarations: section headings in section_text, shell tabs as chips
    assert f"QLabel#sidebarSection {{\n        color: {theme.palette('dark')['section_text']};" in dark
    assert "border-radius: 6px; margin: 6px 2px 4px 2px;" in dark


def test_missing_arrow_png_degrades_instead_of_emitting_an_empty_url(app, monkeypatch):
    """An unwritable icon cache must drop the property, not write image: url()."""
    from turboadb.gui import theme

    def arrow_rule(css):
        return next(line for line in css.splitlines() if "QComboBox::down-arrow" in line)

    monkeypatch.setattr(theme, "_image", lambda _fn, _color: "chevron.png")
    assert "image: url(chevron.png);" in arrow_rule(theme.stylesheet("dark"))
    monkeypatch.setattr(theme, "_image", lambda _fn, _color: "")
    degraded = arrow_rule(theme.stylesheet("dark"))
    assert "url()" not in degraded and "image:" not in degraded
    assert "width: 12px" in degraded  # the rule itself is still there


# --------------------------------------------------------------------------- #
# settings_dialog.py / deploy_dialog.py
# --------------------------------------------------------------------------- #
def test_cancelling_settings_restores_the_active_theme_name(app):
    """Reverting only the stylesheet left theme._ACTIVE_NAME on the cancelled
    palette, so every icon and status_colors() lookup stayed wrong."""
    from turboadb.gui import theme
    from turboadb.gui.settings_dialog import SettingsDialog

    theme.apply_to_app(app, "dark")
    dlg = SettingsDialog()
    try:
        dlg._orig_theme = "dark"
        dlg._select_theme("light")
        assert theme._ACTIVE_NAME == "light"
        dlg.reject()
        assert theme._ACTIVE_NAME == "dark"
        assert app.styleSheet() == theme.stylesheet("dark")
    finally:
        theme.apply_to_app(app, "dark")
        dlg.deleteLater()


def test_deploy_dialog_is_the_only_validation_of_its_form():
    """main_window re-validated fields the dialog already refuses to accept."""
    import inspect

    from turboadb.gui.deploy_dialog import DeployDialog
    from turboadb.gui.main_window import MainWindow

    blank = types.SimpleNamespace(
        values=lambda: {"hosts": [], "user": "", "password": "", "port": 5037,
                        "update": True, "use_ssl": False}
    )
    assert DeployDialog._problem(blank) == "Enter at least one host."
    filled = types.SimpleNamespace(
        values=lambda: {"hosts": ["pc1"], "user": "EU\\me", "password": "x", "port": 5037,
                        "update": True, "use_ssl": False}
    )
    assert DeployDialog._problem(filled) is None
    body = inspect.getsource(MainWindow.deploy_serve_remote)
    assert "Enter at least one host" not in body
    assert "Enter the admin user and password" not in body


# --------------------------------------------------------------------------- #
# ffmpeg_tools.py / camera_widget.py / remote_webcam.py
# --------------------------------------------------------------------------- #
def test_ffmpeg_download_honours_cancel_and_an_elapsed_ceiling(tmp_path, monkeypatch):
    import io as _io

    from turboadb.gui import ffmpeg_tools

    payload = b"z" * (1024 * 1024)

    class Resp(_io.BytesIO):
        headers = {"Content-Length": str(len(payload))}

    monkeypatch.setattr(
        ffmpeg_tools.urllib.request, "urlopen", lambda req, timeout=0: Resp(payload)
    )
    dest = tmp_path / "part.zip"
    with pytest.raises(ffmpeg_tools.DownloadCancelled):
        ffmpeg_tools._download_zip("https://x/y.zip", str(dest), lambda _m: None,
                                   should_cancel=lambda: True)

    # a chunk loop that keeps running past the ceiling gives up with a reason
    clock = iter([0.0] + [ffmpeg_tools._DOWNLOAD_DEADLINE_S + 1] * 8)
    monkeypatch.setattr(ffmpeg_tools.time, "monotonic", lambda: next(clock))
    with pytest.raises(OSError) as err:
        ffmpeg_tools._download_zip("https://x/y.zip", str(dest), lambda _m: None)
    assert "still downloading" in str(err.value)


def test_ffmpeg_reuse_rechecks_the_recorded_checksum(tmp_path, monkeypatch):
    from turboadb.gui import ffmpeg_tools

    cache = tmp_path / "ffmpeg"
    cache.mkdir()
    exe = cache / "ffmpeg.exe"
    exe.write_bytes(b"MZ-real-ffmpeg")
    monkeypatch.setattr(ffmpeg_tools, "_CACHE", str(cache))
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(ffmpeg_tools, "_VERIFIED", {})
    monkeypatch.delenv("TURBOADB_FFMPEG_SHA256", raising=False)

    # first use adopts the copy and records its hash beside it
    assert ffmpeg_tools._usable_cached(lambda _m: None) == str(exe)
    recorded = (cache / "ffmpeg.exe.sha256").read_text().strip()
    assert recorded == ffmpeg_tools._sha256_file(str(exe))

    # a binary that changed underneath is dropped instead of being executed
    ffmpeg_tools._VERIFIED.clear()
    exe.write_bytes(b"MZ-something-else")
    assert ffmpeg_tools._usable_cached(lambda _m: None) is None
    assert not exe.exists() and not (cache / "ffmpeg.exe.sha256").exists()


def test_ffmpeg_url_and_hash_can_be_pinned(monkeypatch):
    from turboadb.gui import ffmpeg_tools

    monkeypatch.delenv("TURBOADB_FFMPEG_URL", raising=False)
    monkeypatch.delenv("TURBOADB_FFMPEG_SHA256", raising=False)
    assert ffmpeg_tools.ffmpeg_url() == ffmpeg_tools._FFMPEG_URL
    assert ffmpeg_tools.expected_sha256() == ""
    monkeypatch.setenv("TURBOADB_FFMPEG_URL", "https://example/dated-build.zip")
    monkeypatch.setenv("TURBOADB_FFMPEG_SHA256", "  AABB  ")
    assert ffmpeg_tools.ffmpeg_url() == "https://example/dated-build.zip"
    assert ffmpeg_tools.expected_sha256() == "aabb"


def test_quitting_waits_for_the_remote_webcam_teardown(app, monkeypatch):
    """Closing the app used to leak the remote ffmpeg AND its firewall rule."""
    from turboadb.gui import camera_widget as cw

    class Job:
        """A teardown job that finishes when joined, unless it is stuck."""

        def __init__(self, stuck=False):
            self.waited = []
            self._stuck = stuck
            self._running = True

        def wait(self, msecs):
            self.waited.append(msecs)
            self._running = self._stuck
            return not self._stuck

        def isRunning(self):
            return self._running

    monkeypatch.setattr(cw, "_REMOTE_STOPS", [])
    finishes, stuck = Job(), Job(stuck=True)
    cw.track_remote_stop(finishes)
    cw.track_remote_stop(stuck)
    assert cw._REMOTE_STOPS == [finishes, stuck]
    assert cw.join_remote_stops(2.0) is False  # the stuck one is reported
    assert finishes.waited and finishes.waited[0] <= 2000
    assert stuck.waited  # the port/ffmpeg cleanup was given its chance
    assert cw._REMOTE_STOPS == [stuck]  # still running: kept for the next join


def test_remote_ffmpeg_push_is_bounded(monkeypatch):
    """An unbounded SMB copy left 'Scan cameras' greyed out for the session."""
    import inspect

    from turboadb.gui import remote_webcam

    assert remote_webcam._SMB_COPY_TIMEOUT_S > 0
    body = inspect.getsource(remote_webcam._smb_push)
    assert "_call_with_timeout(copy, _SMB_COPY_TIMEOUT_S)" in body
    assert "shutil.copyfile" in body


def test_selected_theme_card_is_outlined_with_the_line_accent(app):
    """``accent`` is fill-only: on Black, Slate and Night it barely showed as
    the selected card's outline. Lines and borders use ``accent_text``."""
    from turboadb.gui import theme
    from turboadb.gui.settings_dialog import SettingsDialog

    theme.apply_to_app(app, "dark")
    dlg = SettingsDialog()
    try:
        for name, button in dlg._theme_buttons.items():
            c = theme.THEMES[name]
            css = button.styleSheet()
            checked = css.split("QToolButton:checked {", 1)[1]
            hover = css.split("QToolButton:hover {", 1)[1].split("}", 1)[0]
            assert f"border-color:{c['accent_text']};" in checked, name
            assert f"border-color:{c['accent_text']};" in hover, name
    finally:
        dlg.reject()
        theme.apply_to_app(app, "dark")
        dlg.deleteLater()


def test_retired_theme_names_resolve_to_their_kind(app, monkeypatch, tmp_path):
    """A settings file or caller naming a removed theme (Rose is light) must
    open its kind's default, not always Graphite."""
    from turboadb.gui import settings, theme
    from turboadb.gui.main_window import MainWindow
    from turboadb.gui.settings_dialog import SettingsDialog

    monkeypatch.setattr(settings, "_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(theme, "_ACTIVE_NAME", theme._ACTIVE_NAME)
    applied, sheet = [], app.styleSheet()
    monkeypatch.setattr(theme, "apply_to_app", lambda _app, name=None: applied.append(name))
    fake = types.SimpleNamespace(
        findChildren=lambda _cls: [],
        _sync_theme_action=lambda: None,
        _sync_theme_menu=lambda: None,
        refresh_sessions=lambda: None,
        _log=lambda _text: None,
    )
    MainWindow._apply_theme(fake, "plum-light", persist=False, announce=False)
    MainWindow._apply_theme(fake, "forest-dark", persist=False, announce=False)
    MainWindow._apply_theme(fake, "no-such-theme", persist=False, announce=False)
    assert applied == ["light", "dark", "dark"]

    settings.save(dict(settings.load(), theme="plum-light"))
    dlg = SettingsDialog()
    try:
        assert dlg._orig_theme == "light" and dlg._selected_theme == "light"
    finally:
        dlg.done(0)
        dlg.deleteLater()
    app.setStyleSheet(sheet)


def test_main_tab_row_paints_its_styled_background():
    """The strip behind the "+" new-tab corner button showed the window colour
    instead of the tab-bar colour."""
    import inspect

    from turboadb.gui.main_window import MainWindow

    body = inspect.getsource(MainWindow._build_center)
    assert "self.tabs.setAttribute(Qt.WA_StyledBackground, True)" in body
