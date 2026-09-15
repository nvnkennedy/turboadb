"""Headless tests for PyQt GUI components, dialogs, and panels."""
import os
import sys

import os
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys.path[0] != repo_root:
    sys.path.insert(0, repo_root)

import pytest

pytest.importorskip("PyQt5")  # CI's [test] extra doesn't install the GUI
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _contrast(fg, bg):
    def luminance(colour):
        channels = [int(colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_theme_text_tokens_are_readable():
    from turboadb.gui import theme

    for name, c in theme.THEMES.items():
        for fg in ("text", "dim", "accent_text", "section_text"):
            for bg in ("win", "panel", "raised", "input", "ribbon", "chrome"):
                assert _contrast(c[fg], c[bg]) >= 4.5, (name, fg, bg)
        assert _contrast(c["sel_text"], c["sel"]) >= 4.5, name
        assert _contrast(c["on_accent"], c["accent"]) >= 4.5, name
        assert _contrast(c["on_danger"], c["danger"]) >= 4.5, name
        assert _contrast(c["danger_text"], c["input"]) >= 4.5, name
        # control outlines must be visible (WCAG non-text contrast)
        for bg in ("win", "input"):
            assert _contrast(c["frame"], c[bg]) >= 3.0, (name, "frame", bg)
    assert _contrast(theme.TERM_FG, theme.TERM_BG) >= 7


def test_theme_stylesheet(qapp):
    from turboadb.gui import theme

    names = theme.theme_names()
    assert names[:2] == ("dark", "light") and len(names) == 10
    for name in names:
        # every theme belongs to a dark/light pair the ribbon toggle flips between
        pair = theme.counterpart(name)
        assert pair in names and theme.counterpart(pair) == name
        assert theme.is_light(name) != theme.is_light(pair)
    assert theme.section_text("dark") == theme.THEMES["dark"]["section_text"]
    assert theme.section_text("light") == theme.THEMES["light"]["section_text"]
    for name in theme.theme_names():
        css = theme.stylesheet(name)
        assert "QWidget" in css
        assert theme.THEMES[name]["accent"] in css
        assert theme.theme_description(name)
    assert theme.THEMES["dark"]["accent"] not in theme.stylesheet("light")
    # the old saturated sky-cyan accent and pure-white headings are gone
    assert "#38bdf8" not in theme.stylesheet("dark")
    assert "#ffffff" not in theme.stylesheet("dark")
    assert "QTabWidget::pane { border: 0" in theme.stylesheet("dark")
    assert "QFrame#animatedTabIndicator" in theme.stylesheet("dark")
    assert "QPushButton#latestFollowButton:checked" in theme.stylesheet("dark")


def test_animated_tabs_keep_normal_qtabwidget_api(qapp):
    from PyQt5.QtWidgets import QWidget
    from turboadb.gui.qtutil import AnimatedTabWidget

    tabs = AnimatedTabWidget(transition_ms=0)
    first, second = QWidget(), QWidget()
    tabs.addTab(first, "First")
    tabs.addTab(second, "Second")
    tabs.setCurrentWidget(second)
    assert tabs.currentWidget() is second
    assert tabs.count() == 2
    # Nested device/terminal tabs must stay with Qt's normal paint path.  A
    # graphics opacity effect here caused repeated QPainter-not-active errors
    # when a second terminal for the same device was opened on Windows.
    assert first.graphicsEffect() is None
    assert second.graphicsEffect() is None
    assert tabs._tab_indicator.parent() is tabs.tabBar()
    tabs.close()


def test_animated_tabs_ignore_deferred_work_after_deletion(qapp, monkeypatch):
    """Deferred indicator/page animation must not run on a deleted tab widget.

    PyQt aborts the whole process on an unhandled slot exception, so the
    excepthook is captured here to turn a regression into a plain failure.
    """
    from PyQt5 import sip
    from PyQt5.QtWidgets import QWidget
    from turboadb.gui.qtutil import AnimatedTabWidget

    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc))
    tabs = AnimatedTabWidget(transition_ms=50)
    tabs.addTab(QWidget(), "First")
    tabs.addTab(QWidget(), "Second")
    tabs.setCurrentIndex(1)  # schedules indicator + page-entry work
    sip.delete(tabs)
    for _ in range(5):
        qapp.processEvents()
    assert errors == []


class _FakeUser32:
    """Just enough Win32 window semantics for ``_reparent``.

    Like the real API, ``GetParent`` of a WS_POPUP window reports its owner
    (none), while ``GetAncestor(GA_PARENT)`` reports the actual parent.
    """

    DESKTOP = 0x10010

    def __init__(self, style, accept=True):
        from turboadb.gui import mirror_panel as mp

        self._popup = mp._WS_POPUP
        self.style = style
        self.parent = self.DESKTOP
        self.accept = accept
        self.calls = []

    def GetWindowLongPtrW(self, _hwnd, _index):
        return self.style

    GetWindowLongW = GetWindowLongPtrW

    def SetWindowLongPtrW(self, _hwnd, _index, value):
        self.calls.append("style")
        self.style = value
        return 1

    SetWindowLongW = SetWindowLongPtrW

    def SetParent(self, _hwnd, parent):
        self.calls.append("parent")
        if self.accept:
            self.parent = parent
        return 1

    def GetParent(self, _hwnd):
        return 0 if self.style & self._popup else self.parent

    def GetAncestor(self, _hwnd, _flag):
        return self.parent

    def SetWindowPos(self, *_args):
        return 1

    def ShowWindow(self, *_args):
        return 1


def test_reparent_adopts_scrcpys_borderless_popup_window(monkeypatch):
    """The embedded mirror used to always fall back to a separate window."""
    from turboadb.gui import mirror_panel as mp

    fake = _FakeUser32(style=mp._WS_POPUP | 0x10000000)  # WS_POPUP | WS_VISIBLE
    monkeypatch.setattr(mp, "_win_api", lambda: (None, fake))
    mp._reparent(1234, 5678, 400, 300)
    assert fake.parent == 5678
    assert fake.style & mp._WS_CHILD and not fake.style & mp._WS_POPUP
    assert fake.calls[:2] == ["style", "parent"]  # WS_CHILD before SetParent


def test_reparent_still_reports_a_real_rejection(monkeypatch):
    from turboadb.gui import mirror_panel as mp

    fake = _FakeUser32(style=mp._WS_POPUP, accept=False)
    monkeypatch.setattr(mp, "_win_api", lambda: (None, fake))
    with pytest.raises(OSError, match="rejected"):
        mp._reparent(1234, 5678, 400, 300)


def test_window_ready_ignores_sdls_hidden_placeholder(monkeypatch):
    from turboadb.gui import mirror_panel as mp

    class User32:
        visible = False

        def IsWindowVisible(self, _hwnd):
            return self.visible

    fake = User32()
    monkeypatch.setattr(mp, "_win_api", lambda: (None, fake))
    assert not mp._window_ready(None)
    assert not mp._window_ready(42)  # created but still hidden
    fake.visible = True
    assert mp._window_ready(42)


def test_embed_waits_until_scrcpy_shows_its_window(qapp, monkeypatch):
    """Adopting SDL's hidden placeholder killed a cold-start scrcpy, so the
    first Start click always failed and only the automatic retry worked."""
    from turboadb.gui import mirror_panel as mp

    class Handler:
        serial = "dev"
        config = None

    class Session:
        running = True

    panel = mp.MirrorPanel(Handler(), session=None)
    try:
        panel._scrcpy = Session()
        panel._win_title = "turboadb-embed-test"
        panel._embed_tries = 0
        panel._win_first_seen = 0.0
        panel._EMBED_SETTLE = 0.0
        panel.container.resize(400, 400)
        adopted = []
        shown = {"value": False}
        monkeypatch.setattr(mp, "_find_window", lambda _title: 777)
        monkeypatch.setattr(mp, "_window_ready", lambda _hwnd: shown["value"])
        monkeypatch.setattr(mp, "_reparent", lambda child, _parent, _w, _h: adopted.append(child))
        monkeypatch.setattr(panel, "_fit", lambda: None)
        for _ in range(3):
            panel._try_embed()
        assert adopted == []  # the hidden placeholder is never adopted
        shown["value"] = True
        panel._try_embed()  # first sighting of the shown window starts the settle
        panel._try_embed()
        assert adopted == [777]
        assert panel._child_hwnd == 777
    finally:
        panel._scrcpy = None
        panel._child_hwnd = None
        panel.close_panel()


def test_light_console_keeps_a_dark_terminal(qapp, monkeypatch):
    import turboadb.gui.console as console_mod
    from turboadb.gui import theme

    real_get = console_mod.settings_mod.get
    monkeypatch.setattr(
        console_mod.settings_mod,
        "get",
        lambda key: "light" if key == "theme" else real_get(key),
    )
    console = console_mod.AnsiConsole()
    try:
        assert f"background: {theme.TERM_BG}" in console.styleSheet()
        assert f"selection-background-color: {theme.TERM_SELECTION}" in console.styleSheet()
        console._sgr("96")  # cyan is used by the regular shell prompt
        assert console._fmt.foreground().color().name() == theme.ANSI_FG[96]
    finally:
        console.close()


def test_settings_dialog(qapp):
    from turboadb.gui import theme
    from turboadb.gui.settings_dialog import SettingsDialog

    dlg = SettingsDialog()
    dlg._select_theme("light")
    res = dlg.result_settings()
    assert isinstance(res, dict)
    assert res["theme"] == "light"
    assert "term_font" in res
    assert "scrcpy_bit_rate" in res
    assert res["scrcpy_audio"] is True
    assert res["scrcpy_audio_source"] in {"output", "playback", "mic", "voice-call-downlink", "voice-performance"}
    assert hasattr(dlg, "audio_enabled")
    assert hasattr(dlg, "font_combo")
    assert set(dlg._theme_buttons) == set(theme.theme_names())
    assert dlg.duplicate_device_action.currentData() in {"ask", "terminal", "focus"}
    pages = [dlg.nav.item(index).text() for index in range(dlg.nav.count())]
    assert pages[pages.index("Themes") + 1] == "Startup"
    dlg.reject()  # restores the live preview instead of leaking it into later tests


def test_deploy_dialog(qapp):
    from turboadb.gui.deploy_dialog import DeployDialog

    dlg = DeployDialog()
    assert hasattr(dlg, "chk_update")
    dlg.hosts.setPlainText("192.168.1.100")
    dlg.user.setText("admin")
    dlg.pw.setText("secret")
    v = dlg.values()
    assert v["hosts"] == ["192.168.1.100"]
    assert v["user"] == "admin"
    assert v["password"] == "secret"
    assert v["update"] is True
    assert dlg._problem() is None
    dlg.close()


def test_session_dialog(qapp):
    from turboadb.gui.session_dialog import SessionDialog

    dlg = SessionDialog()
    dlg.name.setText("My Device")
    dlg.serial.setEditText("10BE330KG9000AF")
    res = dlg.result_session()
    assert res["name"] == "My Device"
    assert res["type"] == "usb"
    assert res["serial"] == "10BE330KG9000AF"
    dlg.close()


def test_log_panel(qapp):
    from turboadb.gui.log_panel import LogPanel

    panel = LogPanel()
    panel.append("[OK] connected to device")
    panel.append("[ERROR] adb error")
    panel.append("[DEBUG] $ adb shell getprop")
    assert len(panel._entries) == 3
    panel._clear()
    assert len(panel._entries) == 0
    panel.close()


def test_console_local_shell(qapp):
    from turboadb.gui.console import AnsiConsole

    sent = []
    console = AnsiConsole(send_fn=lambda data: sent.append(data))
    console.set_emulate_prompt(False)
    assert console._need_prompt is False
    console._line = "Get-Process"
    console.feed(b"Windows PowerShell\r\nPS C:\\> ")
    console._drain_tick()
    assert "Windows PowerShell" in console.toPlainText()
    console.close_archive()
    console.close()


def test_console_jump_to_latest_is_explicit(qapp):
    """New output must not pull a user out of older scrollback."""
    from turboadb.gui.console import AnsiConsole

    console = AnsiConsole()
    console.resize(360, 100)
    console.show()
    console._echo("line\n" * 300)
    qapp.processEvents()
    bar = console.verticalScrollBar()
    assert bar.maximum() > 0

    bar.setValue(bar.minimum())
    console.feed(b"new output\n")
    console._drain_tick()
    assert bar.value() == bar.minimum()

    console.scroll_to_latest()
    assert bar.value() == bar.maximum()

    console.set_follow_latest(True)
    assert console._follow_latest is True
    bar.setValue(bar.minimum())
    qapp.processEvents()
    # Latest is a tail preference, not a scroll lock: older output remains
    # inspectable until the user explicitly returns to the terminal/tail.
    assert bar.value() == bar.minimum()
    console.feed(b"follow mode still permits review\n")
    console._drain_tick()
    assert bar.value() == bar.minimum()
    console._pin_to_latest_if_following()
    assert bar.value() == bar.maximum()
    console.set_follow_latest(False)
    bar.setValue(bar.minimum())
    console.feed(b"manual position stays put\n")
    console._drain_tick()
    assert bar.value() == bar.minimum()
    console.close_archive()
    console.close()


def test_scrollback(qapp):
    from PyQt5.QtWidgets import QPlainTextEdit
    from turboadb.gui.scrollback import Scrollback

    edit = QPlainTextEdit()
    sb = Scrollback(edit, display_cap=5000)
    sb.archive("line 1\nline 2\n")
    assert sb.full_text() == "line 1\nline 2\n"
    sb.reset()
    assert sb.full_text() == ""
    sb.close()


def test_flowlayout(qapp):
    from PyQt5.QtWidgets import QWidget, QPushButton
    from turboadb.gui.flowlayout import FlowLayout

    w = QWidget()
    lay = FlowLayout(w)
    b1 = QPushButton("Btn1")
    b2 = QPushButton("Btn2")
    lay.addWidget(b1)
    lay.addWidget(b2)
    assert lay.count() == 2
    w.close()


def test_welcome_screen(qapp):
    from PyQt5.QtWidgets import QMainWindow
    from turboadb.gui.welcome import WelcomeScreen

    win = QMainWindow()
    win.new_session = lambda: None
    win.open_selected = lambda: None
    win.open_webcam_tab = lambda: None
    win.upgrade_tools_gui = lambda: None
    ws = WelcomeScreen(win)
    ws.refresh_status([])
    assert "No device connected" in ws._foot.text()
    ws.close()
    win.close()


def test_park_thread(qapp):
    from PyQt5.QtCore import QThread
    from turboadb.gui.qtutil import park_thread, _parked

    t = QThread()
    park_thread(t)
    # A thread that hasn't started is not finished, so it is parked
    assert t in _parked
    t.deleteLater()


def test_settings_set_and_mute_popups(qapp):
    from turboadb.gui import settings as settings_mod
    from turboadb.gui.log_panel import LogPanel

    orig = settings_mod.get("mute_popups_with_log")
    try:
        settings_mod.set("mute_popups_with_log", False)
        assert settings_mod.get("mute_popups_with_log") is False
        settings_mod.set("mute_popups_with_log", True)
        assert settings_mod.get("mute_popups_with_log") is True

        panel = LogPanel()
        panel.chk_silent.setChecked(False)
        assert settings_mod.get("mute_popups_with_log") is False
        panel.chk_silent.setChecked(True)
        assert settings_mod.get("mute_popups_with_log") is True
        panel.close()
    finally:
        settings_mod.set("mute_popups_with_log", orig)


def test_local_terminal_startup_banners(qapp):
    import time
    from turboadb.gui.local_terminal import LocalShellSession

    ps = LocalShellSession("powershell")
    out_ps = ""
    for _ in range(40):
        time.sleep(0.1)
        chunk = ps.read().decode("utf-8", "replace")
        if chunk:
            out_ps += chunk
            if "PS" in out_ps:
                break
    ps.close()
    assert "PS" in out_ps

    cmd = LocalShellSession("cmd")
    out_cmd = ""
    for _ in range(40):
        time.sleep(0.1)
        chunk = cmd.read().decode("utf-8", "replace")
        if chunk:
            out_cmd += chunk
            if ">" in out_cmd:
                break
    cmd.close()
    assert ">" in out_cmd


def test_local_shell_adb_shell_force_pty(qapp):
    from turboadb.gui.device_tab import _LocalShellWidget

    w = _LocalShellWidget("cmd")
    sent = []
    class MockSession:
        running = True
        def send(self, data):
            sent.append(data)
    w.session = MockSession()
    w._started = True
    w._send(b"adb shell\r\n")
    assert sent[-1] == b"adb shell -t -t\r\n"
    assert w.term._pending_echo == "adb shell -t -t"
    assert w._in_adb_shell is True

    # Inside adb shell, ls must NOT be rewritten to dir
    w._send(b"ls\r\n")
    assert sent[-1] == b"ls\r\n"

    # Exiting returns to cmd
    w._send(b"exit\r\n")
    assert w._in_adb_shell is False

    w._send(b"adb -s 12345 shell\r\n")
    assert sent[-1] == b"adb -s 12345 shell -t -t\r\n"
    w.close()


def test_mirror_panel_display_actions(qapp, monkeypatch):
    from turboadb.gui.mirror_panel import MirrorPanel

    class MockHandler:
        serial = "mock_device"
        config = None
        def list_displays(self, safe=False):
            return [{"id": 0, "size": "1080x2400"}, {"id": 1, "size": "1920x720"}]
        def screenshot(self, path=None, display_id=None, safe=False):
            return b"\x89PNG\r\n\x1a\n"
        def mirror(self, opts, compat=False, safe=False):
            return None

    mp = MirrorPanel(MockHandler(), {"name": "mock"})
    assert mp.act_mirror_all.isEnabled() is True
    assert mp.btn_mirror.text() == "Start in this tab"
    mp.act_embed.setChecked(False)
    assert mp.btn_mirror.text() == "Start separate window"
    mp.act_embed.setChecked(True)
    assert mp.btn_mirror.text() == "Start in this tab"
    assert mp.btn_stop.isHidden()
    assert mp.btn_audio.isCheckable()
    assert mp.btn_audio.isChecked() is True
    assert mp.btn_ivi.isHidden()
    assert not hasattr(mp, "btn_live")
    assert mp._displays_loaded is True
    mp.set_automotive_default(True)
    assert not mp.btn_ivi.isHidden()
    # Test that _got_displays preserves act_mirror_all enabled even with 1 display
    mp._got_displays([{"id": 0, "size": "1080x2400"}])
    assert mp.act_mirror_all.isEnabled() is True

    # Test _select_and_mirror calls start() without attribute errors
    started_display = []
    mp.start = lambda display_id="__use_combo__", **kwargs: started_display.append(display_id)
    mp._select_and_mirror(1)
    assert started_display == [1]

    # Recording must not start a second hidden scrcpy session: that window
    # steals focus from the interactive mirror.  Device-side recording retains
    # keyboard control in the active display session.
    import turboadb.gui.mirror_panel as mirror_mod
    monkeypatch.setattr(
        mirror_mod.QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: ("test_rec.mp4", "MP4 video (*.mp4)"),
    )
    started = []
    mp._record_device = lambda path, display_id=None: started.append((path, display_id))
    mp._begin_record(display_id=1)
    assert started == [("test_rec.mp4", 1)]
    mp._recording = False

    # With an active mirror, a second windowless scrcpy records alongside it:
    # the live screen is never stopped, restarted or re-embedded.
    class Session:
        running = True

    mp._scrcpy = Session()
    mp._launch_spec = {"display_id": 1, "compat": False, "embed": True, "camera": None}
    stopped, parallel = [], []
    mp.stop = lambda: stopped.append(True)
    mp._start_parallel_record = lambda path, display_id: parallel.append((path, display_id))
    mp._begin_record(display_id=1)
    assert stopped == []
    assert parallel == [("test_rec.mp4", 1)]
    assert mp._queued_start is None
    mp._scrcpy = None
    mp._recording = False
    mp.close()


def test_live_thread_accepts_a_specific_display(qapp):
    from turboadb.gui.mirror_panel import _LiveThread

    live = _LiveThread(object(), max_fps=2, display_id=3)
    assert live.display_id == 3


def test_scrcpy_launch_worker_does_not_add_an_adb_preflight(qapp):
    """scrcpy should start immediately; it performs its own ADB handshake."""
    from turboadb.config import ScrcpyOptions
    from turboadb.gui.mirror_panel import _MirrorLaunchThread

    class Handler:
        def get_state(self):
            raise AssertionError("launch must not block on adb get-state")

        def mirror(self, *_args, **_kwargs):
            return "session"

    result = []
    worker = _MirrorLaunchThread(Handler(), ScrcpyOptions(), False, "test.log")
    worker.done.connect(result.append)
    worker.run()
    assert result == ["session"]


def test_popout_action_is_persistent_and_preserves_separate_mode(qapp):
    from turboadb.gui.mirror_panel import MirrorPanel

    class Handler:
        serial = "mock"
        config = None

    panel = MirrorPanel(Handler(), {"name": "mock"})
    try:
        panel._refresh_buttons()
        assert not panel.btn_popout.isHidden()
        panel._launch_pending = True
        panel._refresh_buttons()
        assert not panel.btn_popout.isHidden()
        assert not panel.btn_popout.isEnabled()

        panel._launch_pending = False
        starts = []
        panel.start = lambda **kwargs: starts.append(kwargs)
        panel._popout_mirror()
        assert starts == [{"embed": False}]

        panel._launch_spec = {"display_id": 2, "compat": False, "embed": False}
        panel._retry_start()
        assert starts[-1] == {
            "display_id": 2,
            "compat": False,
            "embed": False,
            "_retry": True,
        }
    finally:
        panel.close()


def test_mirror_start_queues_while_previous_session_is_stopping(qapp):
    from turboadb.gui.mirror_panel import MirrorPanel

    class Handler:
        serial = "mock"
        config = None

    panel = MirrorPanel(Handler(), {"name": "mock"})
    try:
        panel._stopping_mirror = True
        panel.start(display_id=2, compat=True, embed=False)
        assert panel._queued_start == {
            "display_id": 2,
            "compat": True,
            "embed": False,
            "record": None,
            "record_format": None,
            "camera": None,
        }
        assert "before restarting" in panel.status.text()
    finally:
        panel._stopping_mirror = False
        panel.close_panel()
        panel.close()


def test_integrated_recording_failure_is_not_auto_retried(qapp):
    from turboadb.gui.mirror_panel import MirrorPanel

    class Handler:
        serial = "mock"
        config = None

    panel = MirrorPanel(Handler(), {"name": "mock"})
    try:
        failures, retries = [], []
        panel._recording = True
        panel._rec_mode = "integrated"
        panel._launch_spec = {"record": "one.mp4"}
        panel._record_failed = failures.append
        panel._retry_or_show_scrcpy_failure = retries.append
        panel._on_mirror_launch_failed("encoder unavailable")
        assert failures == ["scrcpy recording could not start: encoder unavailable"]
        assert retries == []
    finally:
        panel._recording = False
        panel.close_panel()
        panel.close()


def test_ivi_display_and_record_toolbar_keep_native_mirror_focus(qapp):
    from PyQt5.QtCore import Qt
    from turboadb.gui.mirror_panel import MirrorPanel

    class Handler:
        serial = "mock"
        config = None

    panel = MirrorPanel(Handler(), {"name": "mock"}, automotive=True)
    try:
        starts = []
        panel.start = lambda **kwargs: starts.append(kwargs)
        panel._open_display_from_ivi(2, maximize=False)
        assert starts == [{"display_id": 2, "embed": False}]

        class Session:
            running = True

        panel._scrcpy = Session()
        panel._embed_on = False
        focused = []
        panel._focus_separate_window = lambda: focused.append(True)
        panel._restore_mirror_focus()
        assert focused == [True]

        # A Record/Stop click must return keyboard ownership while the Windows
        # foreground-input grant is still active; a deferred callback is too
        # late to do so reliably.
        focus_args = []
        panel._focus_separate_window = lambda **kwargs: focus_args.append(kwargs)
        panel._restore_mirror_focus(direct=True)
        assert focus_args == [{"focus_keyboard": True}]

        # The typing bar is gone: the screen itself takes the PC keyboard and
        # Device Control has a Keyboard section.
        panel._refresh_buttons()
        assert not hasattr(panel, "kb_bar") and not hasattr(panel, "kb_input")

        # Auto-focus must never move the keyboard (a timer would steal typing
        # from wherever the user works); a direct Record/Stop click gives it
        # to the embedded screen container, which forwards keys to the device.
        panel._embed_on = True
        panel._child_hwnd = 1
        panel._fit = lambda: None  # the fake child HWND must never reach MoveWindow
        panel.resize(400, 300)
        panel.show()
        qapp.processEvents()
        claimed = []
        import turboadb.gui.mirror_panel as mirror_mod

        original_claim = mirror_mod._claim_native_focus
        mirror_mod._claim_native_focus = lambda hwnd: claimed.append(hwnd) or True
        try:
            embedded_focus = []
            panel.container.setFocus = lambda reason: embedded_focus.append(reason)
            panel._restore_mirror_focus()
            assert embedded_focus == [] and claimed == []
            panel._restore_mirror_focus(direct=True)
            assert embedded_focus == [Qt.OtherFocusReason]
            assert claimed == [int(panel.container.winId())]
        finally:
            mirror_mod._claim_native_focus = original_claim
            panel._child_hwnd = None
    finally:
        panel.close()


def test_stopping_display_does_not_stop_device_recording(qapp):
    from turboadb.gui.mirror_panel import MirrorPanel

    class Handler:
        serial = "mock"
        config = None

    class Session:
        running = True

        def stop(self):
            self.running = False

    class StopEvent:
        calls = 0

        def set(self):
            self.calls += 1

    panel = MirrorPanel(Handler(), {"name": "mock"})
    try:
        stop_event = StopEvent()
        panel._recording = True
        panel._rec_stop = stop_event
        panel._scrcpy = Session()
        panel.stop()
        assert stop_event.calls == 0
        assert "Recording continues" in panel.status.text()
    finally:
        # The test is about the display-stop action, not closing a recording.
        panel._recording = False
        panel.close()


def test_terminal_only_connection_skips_identity_probe(qapp):
    from turboadb.config import ADBConfig
    from turboadb.gui.device_tab import _ConnectThread

    full = _ConnectThread(ADBConfig(serial="one"))
    terminal_only = _ConnectThread(ADBConfig(serial="one"), fetch_identity=False)
    assert full.fetch_identity is True
    assert terminal_only.fetch_identity is False


def test_device_tab_mirror_choose_display(qapp):
    from turboadb.gui.device_tab import DeviceTab
    called = []
    dt = DeviceTab({"name": "mock", "serial": "123"})
    dt.handler = object()
    class MockMirrorTab:
        def _show_display_manager(self):
            called.append(True)
    dt.mirror_tab = MockMirrorTab()
    dt.mirror_choose_display()
    assert called == [True]
    dt.close()


def test_device_tab_screen_actions_force_the_requested_view_mode(qapp):
    """Each labelled screen command must preserve its explicit launch target."""
    from turboadb.gui.device_tab import DeviceTab

    tab = DeviceTab({"name": "mock", "serial": "123"})
    try:
        called = []
        tab.mirror = lambda **kwargs: called.append(kwargs)
        actions = tab.btn_mirror.menu().actions()
        assert actions[0].text() == "Open screen in a separate window"
        actions[0].trigger()
        assert called == [{"embed": False}]
        assert actions[1].text() == "Show screen in this tab"
        actions[1].trigger()
        assert called[-1] == {"embed": True}
    finally:
        tab.close_session()
        tab.close()


def test_device_tab_split_view_reuses_existing_pages(qapp):
    """Split view must move persistent pages, never construct replacements."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QWidget
    from turboadb.gui.device_tab import DeviceTab

    tab = DeviceTab({"name": "mock", "serial": "123"})
    try:
        tab.resize(1280, 820)
        tab.show()
        qapp.processEvents()
        pages = {key: QWidget() for key in ("shell", "logcat", "files", "controls")}
        tab.handler = object()
        tab._subtabs = dict(pages)
        for key, page in pages.items():
            icon, label = {
                "shell": ("⌨", "Terminal"),
                "logcat": ("📜", "Logcat"),
                "files": ("📁", "Files"),
                "controls": ("🎛", "Device Control"),
            }[key]
            tab._add_subtab(page, icon, label)

        tab._activate_split(["shell", "controls"], Qt.Horizontal)
        assert tab._content_stack.currentWidget() is tab._split_workspace
        assert tab._split_root.count() == 2
        assert pages["shell"] is tab._split_panes["shell"].layout().itemAt(1).widget()
        assert pages["controls"] is tab._split_panes["controls"].layout().itemAt(1).widget()
        assert not pages["shell"].isHidden()
        assert not pages["controls"].isHidden()

        tab._activate_split(["shell", "controls", "files", "logcat"])
        assert tab._split_root.count() == 2
        assert tab._split_root.widget(0).count() == 2
        assert tab._split_root.widget(1).count() == 2
        assert all(page.parent() is not tab.inner for page in pages.values())
        tab._even_split_sizes()
        qapp.processEvents()
        rows = tab._split_root.sizes()
        assert abs(rows[0] - rows[1]) <= 8
        for row_index in range(2):
            columns = tab._split_root.widget(row_index).sizes()
            assert abs(columns[0] - columns[1]) <= 8

        tab._leave_split()
        assert tab._content_stack.currentWidget() is tab.inner
        assert tab.inner.count() == 4
        assert all(tab.inner.indexOf(page) >= 0 for page in pages.values())
    finally:
        tab.close_session()
        tab.close()


def test_report_png_contains_the_complete_text(qapp, tmp_path, monkeypatch):
    from turboadb.gui import report_dialog
    from turboadb.gui.report_dialog import render_report_png, report_html, split_report

    path = tmp_path / "device-report.png"
    report = "Device report\nSerial: one\n\n--- RAW DIAGNOSTICS ---\nsecret raw line"
    summary, raw = split_report(report)
    render_report_png(str(path), "Device report", summary)
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    html = report_html("Device report", summary, raw)
    assert "report-table" in html
    assert "secret raw line" in html

    from turboadb.gui import theme

    for name in ("dark", "light"):
        monkeypatch.setattr(report_dialog.settings_mod, "get", lambda *_, n=name: n)
        colours = theme.report_palette(name)
        html_for_theme = report_dialog.summary_html("Device report", summary)
        assert f"background:{colours['background']}" in html_for_theme
        assert f"color:{colours['foreground']}" in html_for_theme
    # light reports use an off-white canvas, never a pure white slab
    assert theme.report_palette("light")["background"] != "#ffffff"


def test_terminal_only_device_tab_has_no_full_workspace_actions(qapp):
    from turboadb.gui.device_tab import DeviceTab

    tab = DeviceTab({"name": "mock", "serial": "123"}, terminal_only=True)
    assert tab._terminal_only is True
    assert tab.btn_mirror.isHidden()
    assert tab.btn_shot.isHidden()
    tab.close_session()
    tab.close()


def test_automotive_device_gets_a_dedicated_ivi_tab(qapp):
    from turboadb.gui.device_tab import DeviceTab

    class MirrorStub:
        def open_ivi_view(self):
            pass

        def refresh_displays(self):
            pass

    tab = DeviceTab({"name": "ivi", "serial": "123"})
    tab.mirror_tab = MirrorStub()
    tab._subtabs = {}
    tab._add_ivi_tab()
    assert "ivi" in tab._subtabs
    assert tab.inner.tabText(tab.inner.indexOf(tab._subtabs["ivi"])) == "IVI Displays"
    tab.close_session()
    tab.close()


def test_webcam_defaults_to_fit_view(qapp):
    from turboadb.gui.camera_widget import CameraPanel

    panel = CameraPanel()
    try:
        assert panel.view_mode.currentData() == "fit"
        assert "Start camera" in panel.view.text()
        panel._stop_stream()
        assert "Camera stopped" not in panel.view.text()
        assert "Start camera" in panel.view.text()
    finally:
        panel.close()


def test_render_box_banner():
    import re
    from turboadb.gui.device_tab import _render_box_banner, _render_mobaxterm_banner, _str_width

    ansi_re = re.compile(r"\x1b\[[0-9;]*m")

    title = "⚡ TurboADB Shell"
    lines = [
        "\x1b[36mTarget\x1b[0m : \x1b[1;97mvivo V2318\x1b[0m \x1b[92m· Android 16\x1b[0m",
        "\x1b[36mLink\x1b[0m   : \x1b[93mvia USB\x1b[0m [10BE330KG9000AF]",
        "\x1b[36mMode\x1b[0m   : \x1b[37mInteractive cooked shell · Tab: complete · Ctrl+C: stop\x1b[0m",
    ]
    banner = _render_box_banner(title, lines, min_width=74)
    raw_lines = [l for l in banner.splitlines() if l.strip()]
    assert len(raw_lines) == 7  # top, title, blank, 3 body lines, bottom

    target_width = _str_width(raw_lines[0])
    assert target_width >= 74

    for line in raw_lines:
        plain = ansi_re.sub("", line)
        assert plain.startswith(("┌", "│", "└"))
        assert plain.endswith(("┐", "│", "┘"))
        assert _str_width(line) == target_width

    # Restore the framed welcome banner, but leave the redundant date/path bar
    # to the real terminal prompt below it.
    h1 = "\x1b[1;92m•  TurboADB Professional v1.1.16  •\x1b[0m"
    h2 = "\x1b[93m(ADB client, Screen mirror and device tools)\x1b[0m"
    sess = "\x1b[37mADB session to \x1b[1;35mvivo V2318 [USB]\x1b[0m  \x1b[37m(\x1b[91m@Android 16\x1b[37m)\x1b[0m"
    items = [
        ("File-browser", "", True),
        ("Screen-mirror", "\x1b[90m(remote display is forwarded)\x1b[0m", True),
    ]
    mob_banner = _render_mobaxterm_banner(h1, h2, sess, items, min_width=60, cwd="/")
    mob_lines = [l for l in mob_banner.splitlines() if l.strip()]
    assert len(mob_lines) == 8
    mob_target_w = _str_width(mob_lines[0])
    assert mob_target_w >= 60
    for line in mob_lines:
        plain = ansi_re.sub("", line)
        assert plain.startswith(("┌", "│", "└"))
        assert plain.endswith(("┐", "│", "┘"))
        assert _str_width(line) == mob_target_w
    assert "📅" not in mob_banner and "📁" not in mob_banner


def test_adb_init_thread(qapp):
    from turboadb.gui.main_window import _AdbInitThread

    init_t = _AdbInitThread()
    assert hasattr(init_t, "ready")
    assert init_t.adb_path is None


def test_welcome_screen_scanning_status(qapp):
    from turboadb.gui.welcome import WelcomeScreen

    class MockWin:
        _version = "1.2.0"
        def new_session(self): pass
        def open_selected(self): pass
        def open_webcam_tab(self): pass
        def upgrade_tools_gui(self): pass

    ws = WelcomeScreen(MockWin())
    assert "Detecting" in ws._foot.text()
    ws.refresh_status([])
    assert "No device connected" in ws._foot.text()
    ws.close()
