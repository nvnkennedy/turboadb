"""Headless tests for PyQt GUI components, dialogs, and panels."""
import os
import sys

import os
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys.path[0] != repo_root:
    sys.path.insert(0, repo_root)

import pytest
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_theme_stylesheet(qapp):
    from turboadb.gui import theme

    dark_css = theme.stylesheet("dark")
    light_css = theme.stylesheet("light")
    assert "QWidget" in dark_css
    assert "QWidget" in light_css


def test_settings_dialog(qapp):
    from turboadb.gui.settings_dialog import SettingsDialog

    dlg = SettingsDialog()
    res = dlg.result_settings()
    assert isinstance(res, dict)
    assert "theme" in res
    assert "term_font" in res
    assert "scrcpy_bit_rate" in res
    assert hasattr(dlg, "font_combo")
    dlg.close()


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


def test_mirror_panel_display_actions(qapp):
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
    # Test that _got_displays preserves act_mirror_all enabled even with 1 display
    mp._got_displays([{"id": 0, "size": "1080x2400"}])
    assert mp.act_mirror_all.isEnabled() is True

    # Test _select_and_mirror calls start() without attribute errors
    started_display = []
    mp.start = lambda display_id="__use_combo__", **kwargs: started_display.append(display_id)
    mp._select_and_mirror(1)
    assert started_display == [1]

    # Test _record_scrcpy passes no_playback=True
    recorded_opts = []
    def mock_mirror(opts, compat=False, safe=False):
        recorded_opts.append(opts)
        class MockSession:
            running = True
            def stop(self): pass
        return MockSession()
    mp.handler.mirror = mock_mirror
    mp._record_scrcpy("test_rec.mp4", display_id=1)
    assert len(recorded_opts) == 1
    assert recorded_opts[0].no_playback is True
    assert recorded_opts[0].record == "test_rec.mp4"
    assert recorded_opts[0].display_id == 1
    mp.close()


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

    # Test MobaXterm-style banner
    h1 = "\x1b[1;92m•  TurboADB Professional v1.1.16  •\x1b[0m"
    h2 = "\x1b[93m(ADB client, Screen mirror and device tools)\x1b[0m"
    sess = "\x1b[37mADB session to \x1b[1;35mvivo V2318 [USB]\x1b[0m  \x1b[37m(\x1b[91m@Android 16\x1b[37m)\x1b[0m"
    items = [
        ("File-browser", "", True),
        ("Screen-mirror", "\x1b[90m(remote display is forwarded)\x1b[0m", True),
    ]
    mob_banner = _render_mobaxterm_banner(h1, h2, sess, items, min_width=60, cwd="/")
    mob_lines = [l for l in mob_banner.splitlines() if l.strip()]
    assert len(mob_lines) == 9  # 8 box lines + 1 prompt bar line
    mob_target_w = _str_width(mob_lines[0])
    assert mob_target_w >= 60

    # 8 lines of the box must have exact uniform width
    for line in mob_lines[:8]:
        plain = ansi_re.sub("", line)
        assert plain.startswith(("┌", "│", "└"))
        assert plain.endswith(("┐", "│", "┘"))
        assert _str_width(line) == mob_target_w

    # 9th line is the MobaXterm powerline status bar
    assert "📅" in mob_lines[8]
    assert "📁" in mob_lines[8]


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




