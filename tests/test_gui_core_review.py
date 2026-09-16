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


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    return QApplication.instance() or QApplication(["test-gui-core-review"])


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
        assert wall._next_timer.remainingTime() <= wall.START_GAP_MS
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
        _last_toast=float("-inf"),
        _TOAST_INTERVAL_S=4.0,
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
        log_panel=types.SimpleNamespace(append=lambda _text: None),
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
