"""Device Control page: the control-centre panel and the idle screen frame."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt5")

from turboadb.results import OperationResult  # noqa: E402


class _SyncDispatcher:
    """Runs each submitted command at once (stands in for the FIFO thread)."""

    def __init__(self):
        self.submitted = 0

    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        self.submitted += 1
        try:
            result = fn()
        except Exception as exc:
            if on_fail:
                on_fail(str(exc))
            return True
        if on_done:
            on_done(result)
        return True

    def stop(self):
        pass


class _Handler:
    """Records every engine call and reports success."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return OperationResult(True, name, value=True)

        return call


def _pump(app, rounds=10):
    for _ in range(rounds):
        app.processEvents()


def _controls(**kwargs):
    from turboadb.gui.controls_panel import ControlsPanel

    handler = _Handler()
    dispatcher = _SyncDispatcher()
    panel = ControlsPanel(handler, dispatcher=dispatcher, **kwargs)
    logs = []
    panel.log.connect(logs.append)
    return panel, handler, dispatcher, logs


def test_quick_setting_tiles_submit_on_and_off_through_the_dispatcher(qapp):
    from PyQt5.QtWidgets import QAbstractButton

    panel, handler, dispatcher, logs = _controls(compact=True)
    try:
        expected = {
            "wifi": "set_wifi",
            "bluetooth": "set_bluetooth",
            "mobile_data": "set_mobile_data",
            "airplane": "set_airplane",
            "hotspot": "set_hotspot",
        }
        for key, method in expected.items():
            tile = panel.quick_tiles[key]
            handler.calls.clear()
            tile.btn_on.click()
            tile.btn_off.click()
            assert handler.calls == [
                (method, (True,), {"safe": True}),
                (method, (False,), {"safe": True}),
            ], key
        handler.calls.clear()
        panel.quick_tiles["screen"].btn_on.click()
        panel.quick_tiles["screen"].btn_off.click()
        assert handler.calls == [("screen_on", (), {"safe": True}),
                                 ("screen_off", (), {"safe": True})]
        assert dispatcher.submitted == 12
        assert "[OK] Wi-Fi on" in logs and "[OK] Wi-Fi off" in logs
        # one tile per setting instead of separate "X on" / "X off" buttons
        texts = [b.text() for b in panel.findChildren(QAbstractButton)]
        assert not [t for t in texts if t.endswith(" on") or t.endswith(" off")]
        assert texts.count("On") == len(panel.quick_tiles) == 6
    finally:
        panel.close_panel()
        panel.deleteLater()


def test_control_buttons_have_icons_tooltips_and_keep_their_actions(qapp):
    from PyQt5.QtWidgets import QToolButton

    reboots = []
    panel, handler, _dispatcher, logs = _controls(
        compact=True, on_reboot=lambda: reboots.append(True)
    )
    try:
        # the panel's own controls (a QLineEdit action adds an internal tool button)
        buttons = [b for b in panel.findChildren(QToolButton)
                   if b.objectName().startswith("ctl")]
        assert len(buttons) > 30
        for button in buttons:
            assert button.toolTip(), button.text()
            if button.objectName() != "ctlSeg":
                assert not button.icon().isNull(), button.text()
        by_name = {b.accessibleName(): b for b in buttons}
        for name in ("Back", "Home", "Recents"):
            assert by_name[name].toolButtonStyle() == 0  # Qt.ToolButtonIconOnly
        by_name["Back"].click()
        by_name["Play / pause"].click()
        by_name["Volume up"].click()
        by_name["Settings"].click()
        by_name["YouTube"].click()
        by_name["Reboot"].click()
        assert handler.calls == [
            ("keyevent", ("back",), {"safe": True}),
            ("media", ("play-pause",), {"safe": True}),
            ("keyevent", ("vol_up",), {"safe": True}),
            ("open_settings", (), {"safe": True}),
            ("open_url", ("https://www.youtube.com",), {"safe": True}),
        ]
        assert reboots == [True]  # the device tab's reboot workflow, not a raw call
        assert logs[0] == "[OK] Back"
        panel.url.setText("example.com")
        panel._open_url()
        panel.text.setText("hi")
        panel._send_text()
        assert handler.calls[-2:] == [
            ("open_url", ("example.com",), {"safe": True}),
            ("input_text", ("hi",), {"safe": True}),
        ]
    finally:
        panel.close_panel()
        panel.deleteLater()


def test_controls_panel_fits_the_side_panel_without_horizontal_scroll(qapp):
    panel, _handler, _dispatcher, _logs = _controls(compact=True)
    try:
        panel.resize(340, 900)
        panel.show()
        _pump(qapp)
        assert panel._ncols == 1
        viewport = panel._scroll.viewport().width()
        assert panel._scroll.horizontalScrollBar().maximum() == 0
        assert panel._scroll.widget().minimumSizeHint().width() <= viewport
        panel.resize(1100, 900)
        _pump(qapp)
        assert panel._ncols == 3
        assert panel._scroll.horizontalScrollBar().maximum() == 0
        assert len(panel._groups) == 6
    finally:
        panel.hide()
        panel.close_panel()
        panel.deleteLater()


def test_tile_grid_uses_the_widest_column_count_that_fits(qapp):
    from PyQt5.QtWidgets import QToolButton
    from turboadb.gui.controls_panel import _TileGrid

    buttons = []
    for text in ("Browser", "Calculator", "Play Store", "Camera"):
        button = QToolButton()
        button.setText(text)
        buttons.append(button)
    grid = _TileGrid(buttons, (4, 2))
    try:
        need = grid._needed_width()
        assert need >= max(b.fontMetrics().horizontalAdvance(b.text()) for b in buttons)
        spacing = grid._grid.horizontalSpacing()
        assert grid.columns_for(4 * need + 3 * spacing) == 4
        assert grid.columns_for(4 * need + 3 * spacing - 1) == 2
        assert grid.columns_for(10) == 2  # never an odd, gappy count
    finally:
        grid.deleteLater()


class _MirrorHandler:
    serial = "mock"
    config = None


class _Signal:
    """A pyqtSignal stand-in for hand-built worker doubles."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


def test_the_scrcpy_log_dialog_never_opens_from_inside_a_timer_slot(qapp, monkeypatch):
    """Issue: _show_scrcpy_log ran a modal exec_() straight from the
    _check_alive timer slot, re-entering the timer that started the chain."""
    from turboadb.gui.mirror_panel import MirrorPanel

    panel = MirrorPanel(_MirrorHandler(), {"name": "mock"})
    try:
        opened = []
        monkeypatch.setattr(panel, "_open_scrcpy_log", lambda err: opened.append(err))
        panel._show_scrcpy_log("boom")
        assert opened == []  # not from the caller's stack frame
        qapp.processEvents()
        assert opened == ["boom"]  # from the event loop instead
    finally:
        panel.close_panel()
        panel.close()


def test_a_locked_tile_reports_results_on_its_status_line_not_in_dialogs(
    qapp, monkeypatch, tmp_path
):
    """Issue: a recording or screenshot result per display stacked modal
    dialogs in front of the displays tab's running start chain."""
    import turboadb.gui.mirror_panel as mp_mod
    from turboadb.gui.mirror_panel import MirrorPanel

    class _NoModal:
        @staticmethod
        def warning(*_args, **_kwargs):
            raise AssertionError("a locked tile must not open a modal dialog")

    panel = MirrorPanel(_MirrorHandler(), {"name": "ivi"}, automotive=True)
    try:
        panel.set_fixed_display({"id": 2, "size": "1920x720", "name": "Cluster"})
        monkeypatch.setattr(mp_mod, "QMessageBox", _NoModal)
        monkeypatch.setattr(
            mp_mod, "saved_dialog", lambda *a, **k: pytest.fail("no dialog for a tile")
        )
        logs = []
        panel.log.connect(logs.append)

        panel._record_failed("no encoder")
        assert "Recording failed: no encoder" in panel.status.text()
        panel._shot_fail("secure surface")
        assert "secure surface" in panel.status.text()
        video = tmp_path / "cluster.mp4"
        video.write_bytes(b"video data")
        panel._record_finished([str(video)])
        assert "cluster.mp4" in panel.status.text()
        assert panel.status.isVisible() or panel.isHidden()  # shown with the panel
        assert any(line.startswith("[ERROR] recording:") for line in logs)
    finally:
        panel.close_panel()
        panel.close()


def test_a_normal_panel_still_reports_a_failure_in_a_dialog(qapp, monkeypatch):
    """The tile-only routing must not silence the device tab's own screen."""
    import turboadb.gui.mirror_panel as mp_mod
    from turboadb.gui.mirror_panel import MirrorPanel

    shown = []

    class _Recorder:
        @staticmethod
        def warning(_parent, title, text):
            shown.append((title, text))

    panel = MirrorPanel(_MirrorHandler(), {"name": "mock"})
    try:
        monkeypatch.setattr(mp_mod, "QMessageBox", _Recorder)
        panel._record_failed("no encoder")
        panel._shot_fail("secure surface")
        assert [title for title, _text in shown] == ["Recording", "Screenshot"]
    finally:
        panel.close_panel()
        panel.close()


def test_each_embedded_start_gets_its_own_scrcpy_window_title(qapp, monkeypatch):
    """Issue: turboadb-embed-<pid>-<id(panel)> repeated across a quick
    Stop/Start (and across tiles reusing a freed address), so the watchdog
    could adopt the previous, dying window."""
    import turboadb.gui.mirror_panel as mp_mod
    from turboadb.gui.mirror_panel import MirrorPanel

    class _FakeLaunch:
        def __init__(self, _handler, opts, _compat, _log_path):
            self.opts = opts
            self.done, self.fail = _Signal(), _Signal()

        def isFinished(self):
            return True

        def isRunning(self):
            return False

        def start(self):
            pass

    monkeypatch.setattr(mp_mod, "_IS_WIN", True)
    monkeypatch.setattr(mp_mod, "_MirrorLaunchThread", _FakeLaunch)
    panel = MirrorPanel(_MirrorHandler(), {"name": "mock"})
    try:
        titles = []
        for _attempt in range(2):
            panel._launch_pending = False  # the previous launch is done with
            panel.start(embed=True)
            titles.append(panel._win_title)
        assert all(title.startswith("turboadb-embed-") for title in titles)
        assert len(set(titles)) == 2
    finally:
        panel._launch_pending = False
        panel.close_panel()
        panel.close()


def test_idle_screen_start_runs_the_toolbar_action_and_follows_the_session(qapp):
    from turboadb.gui.mirror_panel import MirrorPanel

    panel = MirrorPanel(_MirrorHandler(), {"name": "mock"})
    try:
        idle = panel.empty_state
        assert idle.title.text() == "Screen is off"
        assert idle.btn_start.property("role") == "ok"
        assert not idle.btn_start.icon().isNull()
        for button in (panel.btn_mirror, panel.btn_popout, panel.btn_stop, panel.btn_shot,
                       panel.btn_record, panel.btn_audio, panel.btn_opts, panel.btn_max):
            assert not button.icon().isNull()
            assert button.toolTip()

        starts = []
        panel.start = lambda **kwargs: starts.append(kwargs)
        idle.btn_start.click()
        panel.btn_mirror.click()
        assert starts == [{}, {}]  # the same call as the toolbar Start
        popouts = []
        panel._popout_mirror = lambda: popouts.append(True)
        idle.btn_window.click()
        assert popouts == [True]

        panel.act_embed.setChecked(False)
        assert "own scrcpy window" in idle.hint.text()
        panel.act_embed.setChecked(True)
        assert "here" in idle.hint.text()

        # hidden while a session launches or runs, back once it stops
        assert not idle.isHidden()
        panel._launch_pending = True
        panel._refresh_buttons()
        assert idle.isHidden()
        panel._launch_pending = False
        panel._refresh_buttons()
        assert not idle.isHidden()
    finally:
        panel.close()


def test_idle_screen_frame_is_portrait_for_phones_and_wide_for_automotive(qapp):
    from PyQt5.QtCore import QRectF
    from turboadb.gui.mirror_panel import MirrorPanel

    panel = MirrorPanel(_MirrorHandler(), {"name": "mock"})
    try:
        idle = panel.empty_state
        idle.resize(900, 700)
        idle._place()
        frame = idle.frame_rect()
        assert frame.height() > frame.width()
        assert idle.glyph.icon_name == "smartphone"
        assert frame.contains(QRectF(idle._content.geometry()))

        panel.set_automotive_default(True)
        frame = idle.frame_rect()
        assert idle.landscape
        assert frame.width() > frame.height()
        assert idle.glyph.icon_name == "car"  # a head unit, not a monitor or phone
        assert frame.contains(QRectF(idle._content.geometry()))

        # a short area never squeezes the frame below its content
        idle.resize(900, 260)
        idle._place()
        frame = idle.frame_rect()
        assert frame.height() <= 260
        assert idle._content.geometry().height() <= frame.height()
    finally:
        panel.close()


def test_control_keys_and_typing_follow_the_shown_display(qapp):
    """Device Control's keys and text go to the display its screen shows; the
    default display keeps the plain command."""
    from PyQt5.QtWidgets import QToolButton
    from turboadb.gui.controls_panel import ControlsPanel

    import types

    shown = {"display": 2}
    handler = types.SimpleNamespace(calls=[])

    def record(name):
        def method(*args, **kwargs):
            handler.calls.append((name, args, kwargs))
            return True
        return method

    handler.keyevent = record("keyevent")
    handler.input_text = record("input_text")

    class _Now:
        """Runs a submitted command at once, like a drained dispatcher."""

        def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
            result = fn()
            if on_done is not None:
                on_done(result)
            return True

    panel = ControlsPanel(handler, compact=True, dispatcher=_Now(),
                          display_provider=lambda: shown["display"])
    try:
        back = next(b for b in panel.findChildren(QToolButton) if b.accessibleName() == "Back")
        back.click()
        shown["display"] = None
        back.click()
        assert handler.calls == [
            ("keyevent", ("back",), {"safe": True, "display_id": 2}),
            ("keyevent", ("back",), {"safe": True}),
        ]
    finally:
        panel.close_panel()
        panel.deleteLater()


# --------------------------------------------------------------------------- #
# Device Control layout: the screen as large as the height allows, controls in
# comfortable columns, and Maximize view
# --------------------------------------------------------------------------- #
# ControlsPanel.layout_options() as measured on Windows (card sizes): columns,
# narrowest and widest card width, card height.
_OPTIONS = [(1, 344, 418, 1283), (2, 644, 818, 706), (3, 964, 1218, 511)]
_TOOLBAR = 653  # the screen toolbar on one row (live screencap screen)
_CHROME = 47  # its height above the picture, with the card frame
_PHONE = 1080 / 2400
_HEAD_UNIT = 1920 / 720


@pytest.mark.parametrize(
    "screen_size, width, height, columns",
    [
        # (laptop screen, Device Control's row minus the splitter handle)
        ("1366x768", 1324, 544, 2),
        ("1536x864", 1494, 634, 2),
        ("1920x1080", 1878, 784, 2),
        ("2560x1440", 2518, 1134, 2),
    ],
)
def test_a_portrait_phone_gets_the_height_and_the_controls_comfortable_columns(
    screen_size, width, height, columns
):
    from turboadb.gui.device_tab import plan_side_layout

    screen, controls, cols = plan_side_layout(
        width, height, _PHONE, _CHROME, _TOOLBAR, _OPTIONS, screen_min=340
    )
    assert screen + controls == width, screen_size  # nothing overlaps, nothing is lost
    assert cols == columns, screen_size
    narrowest, widest = _OPTIONS[cols - 1][1:3]
    assert narrowest <= controls <= widest, screen_size  # never stretched past the cap
    # the whole picture fits at full height, beside a one-row toolbar
    picture = (height - _CHROME) * _PHONE
    assert screen >= max(picture, _TOOLBAR), screen_size
    # the screen card takes whatever the capped controls leave
    if controls == widest:
        assert screen == width - widest


def test_the_controls_take_more_columns_only_to_avoid_scrolling():
    from turboadb.gui.device_tab import plan_side_layout

    # a short, wide window: three columns show every control without scrolling
    _screen, controls, cols = plan_side_layout(2400, 560, _PHONE, _CHROME, _TOOLBAR, _OPTIONS)
    assert cols == 3 and controls == 1218
    # a tall one: two fuller columns rather than a wide panel with empty space below
    _screen, controls, cols = plan_side_layout(2400, 900, _PHONE, _CHROME, _TOOLBAR, _OPTIONS)
    assert cols == 2 and controls == 818
    # a narrow window still keeps one column of controls
    screen, controls, cols = plan_side_layout(900, 700, _PHONE, _CHROME, _TOOLBAR, _OPTIONS)
    assert cols == 1 and controls >= 344 and screen + controls == 900


def test_a_head_unit_screen_keeps_the_width_and_the_controls_one_column():
    from turboadb.gui.device_tab import choose_control_layout, plan_side_layout

    for width, height in ((1324, 544), (1494, 634), (1878, 784)):
        assert choose_control_layout(width, height - _CHROME, _HEAD_UNIT, 360, 380) == "side"
        screen, controls, cols = plan_side_layout(
            width, height, _HEAD_UNIT, _CHROME, _TOOLBAR, _OPTIONS
        )
        assert cols == 1 and controls == 344  # the narrowest column: the screen is wide
        assert screen == width - 344


def test_controls_columns_stop_growing_at_a_comfortable_width(qapp):
    from turboadb.gui import controls_panel as cp_mod

    panel, _handler, _dispatcher, _logs = _controls(compact=True)
    try:
        panel.resize(1800, 900)
        panel.show()
        _pump(qapp)
        options = panel.layout_options()  # (sizes are known once shown and polished)
        assert [o[0] for o in options] == [1, 2, 3]
        heights = [o[3] for o in options]
        assert heights[0] > heights[1] > heights[2]  # more columns, shorter panel
        for columns, narrowest, widest, _height in options:
            # each range really lays the sections out in that many columns
            assert panel._columns_for(narrowest) == columns
            assert panel._columns_for(widest) == columns
        assert panel._ncols == 3
        column_cap = 3 * cp_mod._COL_MAX + 2 * cp_mod._COL_GAP
        assert panel._grid_host.width() <= column_cap  # buttons never stretch across 1800 px
        assert panel._scroll.horizontalScrollBar().maximum() == 0
        # the predicted height is what the laid-out sections really take
        _n, narrowest, _widest, predicted = options[1]
        panel.resize(narrowest, 2000)
        _pump(qapp)
        assert panel._ncols == 2
        laid_out = panel._grid_host.height() + cp_mod._HOST_MARGINS[1] + cp_mod._HOST_MARGINS[3]
        assert abs(laid_out - predicted) <= 8
    finally:
        panel.hide()
        panel.close_panel()
        panel.deleteLater()


def _control_view(qapp, width, height):
    """Device Control as the device tab builds it, off-screen, with no device."""
    from PyQt5.QtCore import Qt, pyqtSignal
    from PyQt5.QtWidgets import QWidget
    from turboadb.gui.device_tab import DeviceTab

    class _Tab(QWidget):
        log = pyqtSignal(str)
        screen_active = pyqtSignal(bool)
        _build_control_view = DeviceTab._build_control_view

        def __init__(self):
            super().__init__()
            self.session = {"name": "phone"}
            self._automotive = False
            self._device_dispatcher = _SyncDispatcher()

        def reboot(self, mode=None):
            pass

    tab = _Tab()
    view = tab._build_control_view(_MirrorHandler())
    tab.mirror_tab.set_default_display_size("1080x2400")
    view.setAttribute(Qt.WA_DontShowOnScreen, True)
    view.resize(width, height)
    view.show()
    _settle(qapp, view)
    return tab, view


def _settle(qapp, view):
    _pump(qapp, 20)
    view._apply()
    _pump(qapp, 20)


def _close_view(tab, view):
    view.hide()
    tab.mirror_tab.close_panel()
    view.controls.close_panel()
    view.deleteLater()
    tab.deleteLater()


@pytest.mark.parametrize("width, height", [(1346, 560), (1516, 650), (1900, 800), (2540, 1150)])
def test_device_control_lays_out_the_screen_and_controls_side_by_side(qapp, width, height):
    from turboadb.gui.device_tab import plan_side_layout

    tab, view = _control_view(qapp, width, height)
    mirror = tab.mirror_tab
    try:
        assert view.mode == "side"
        screen, controls = view.split.sizes()
        assert screen + controls + view.split.handleWidth() == view.split.width()
        options = view._controls_options()
        assert controls <= max(o[2] for o in options)  # capped, not stretched
        card, side = view.screen_card.geometry(), view.controls_card.geometry()
        assert card.right() < side.left()  # no overlap
        # the screen toolbar stays on one row: its height is the one-row height
        one_row = mirror.chrome_height(mirror.toolbar_width())
        assert mirror.chrome_height() == one_row
        frame = view._card_frame()
        assert screen >= mirror.toolbar_width() + frame
        # the controls use the planned number of columns
        _s, _c, columns = plan_side_layout(
            view.split.width() - view.split.handleWidth(),
            view.split.height(),
            mirror.display_aspect(),
            one_row + frame,
            mirror.toolbar_width() + frame,
            options,
            screen_min=view.screen_card.minimumWidth(),
        )
        assert view.controls._ncols == columns
    finally:
        _close_view(tab, view)


def test_maximize_view_hides_the_controls_and_restore_brings_them_back(qapp):
    tab, view = _control_view(qapp, 1516, 650)
    mirror = tab.mirror_tab
    toggle = tab._device_controls_toggle
    try:
        sizes = view.split.sizes()
        assert not view.controls_card.isHidden() and toggle.isChecked()
        mirror.btn_max.click()
        _settle(qapp, view)
        assert mirror.act_max.isChecked() and mirror.btn_max.isChecked()
        assert mirror.btn_max.text() == "Restore view" and "Esc" in mirror.btn_max.toolTip()
        assert view.controls_card.isHidden()
        assert view.screen_card.width() == view.split.width()  # the screen fills the tab
        # the toggle stays usable and says what a click does
        assert toggle.isEnabled() and not toggle.isChecked()
        assert toggle.text() == "Show controls"
        # the lazily built options popover shows the same state
        mirror._ensure_options_menu()
        assert mirror.btn_toggle_max.text().endswith("Restore view")

        mirror.btn_max.click()
        _settle(qapp, view)
        assert not mirror.act_max.isChecked() and mirror.btn_max.text() == "Maximize view"
        assert mirror.btn_toggle_max.text().endswith("Maximize view")
        assert not view.controls_card.isHidden() and toggle.isEnabled() and toggle.isChecked()
        assert view.split.sizes() == sizes  # exactly as before

        # controls the user hid stay hidden through Maximize / Restore
        toggle.click()
        assert view.controls_card.isHidden() and toggle.text() == "Show controls"
        mirror.act_max.setChecked(True)
        mirror.act_max.setChecked(False)
        assert view.controls_card.isHidden() and not toggle.isChecked()
        toggle.click()
        _settle(qapp, view)
        assert not view.controls_card.isHidden() and view.split.sizes() == sizes
    finally:
        _close_view(tab, view)


def test_device_controls_can_be_shown_and_hidden_while_maximized(qapp):
    tab, view = _control_view(qapp, 1516, 650)
    mirror = tab.mirror_tab
    toggle = tab._device_controls_toggle
    try:
        mirror.act_max.setChecked(True)
        _settle(qapp, view)
        assert view.controls_card.isHidden()

        toggle.click()  # Show controls, still maximized
        _settle(qapp, view)
        assert mirror.act_max.isChecked()
        assert not view.controls_card.isHidden() and view.controls_visible()
        assert toggle.isChecked() and toggle.text() == "Hide controls"
        card, side = view.screen_card.geometry(), view.controls_card.geometry()
        assert card.right() < side.left()  # laid out beside the screen, no overlap

        toggle.click()  # and hidden again
        _settle(qapp, view)
        assert view.controls_card.isHidden() and toggle.text() == "Show controls"

        # Restore keeps the latest choice: hidden while maximized stays hidden
        mirror.act_max.setChecked(False)
        _settle(qapp, view)
        assert view.controls_card.isHidden() and not toggle.isChecked()

        # hidden before maximizing, shown while maximized: shown after Restore
        mirror.act_max.setChecked(True)
        toggle.click()
        mirror.act_max.setChecked(False)
        _settle(qapp, view)
        assert not view.controls_card.isHidden() and toggle.isChecked()

        # maximizing again hides them again, and the toggle still works
        mirror.act_max.setChecked(True)
        _settle(qapp, view)
        assert view.controls_card.isHidden() and toggle.isEnabled()
    finally:
        _close_view(tab, view)


def test_esc_restores_a_maximized_device_control(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest

    tab, view = _control_view(qapp, 1516, 650)
    mirror = tab.mirror_tab
    try:
        assert not mirror._esc_restore.isEnabled()  # Esc is left alone normally
        mirror.act_max.setChecked(True)
        assert mirror._esc_restore.isEnabled()
        qapp.setActiveWindow(view)
        _pump(qapp)
        QTest.keyClick(mirror.btn_opts, Qt.Key_Escape)
        _pump(qapp)
        assert not mirror.act_max.isChecked()
        assert not view.controls_card.isHidden() and not mirror._esc_restore.isEnabled()
    finally:
        _close_view(tab, view)


def test_leaving_the_tab_gives_the_docks_back_but_keeps_the_tab_maximized(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QDockWidget, QMainWindow, QWidget

    tab, view = _control_view(qapp, 1200, 600)
    mirror = tab.mirror_tab
    win = QMainWindow()
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    dock = QDockWidget("Log", win)
    dock.setWidget(QWidget())
    win.addDockWidget(Qt.BottomDockWidgetArea, dock)
    win.setCentralWidget(view)
    win.show()
    _pump(qapp)
    try:
        mirror.act_max.setChecked(True)
        assert dock.isHidden() and view.controls_card.isHidden()
        mirror._suspend_max()  # another tab shows
        assert not dock.isHidden() and view.controls_card.isHidden()
        assert mirror.act_max.isChecked()
        mirror._resume_max()  # back to Device Control
        assert dock.isHidden() and view.controls_card.isHidden()
        mirror.act_max.setChecked(False)
        assert not dock.isHidden() and not view.controls_card.isHidden()
    finally:
        win.hide()
        win.takeCentralWidget()
        _close_view(tab, view)
        win.close()


# --------------------------------------------------------------------------- #
# review regressions: the toolbar re-plans when (and only when) it must
# --------------------------------------------------------------------------- #
def _wait(qapp, ms):
    """Let queued layout requests and the view's 40 ms re-plan timer run."""
    import time

    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


class _FakeScreencap:
    """What the toolbar reads from a running screencap view."""

    def __init__(self, text):
        self.text = text

    def renderer_text(self):
        return self.text

    def __getattr__(self, name):  # geometry, focus…: nothing to do
        return lambda *args, **kwargs: None


def _one_row(view):
    """The screen toolbar is on one row — wherever the card has room for it
    (a window too narrow for the card and one controls column wraps it)."""
    mirror = view.mirror
    if view.split.sizes()[0] < mirror.toolbar_width() + view._card_frame():
        return True
    return mirror.chrome_height() == mirror.chrome_height(mirror.toolbar_width())


def test_the_embed_option_re_plans_the_screen_card_for_its_wider_start_label(qapp):
    """Issue: unticking "Show screen inside this tab" made Start read "Start
    separate window", wider than the card; the toolbar wrapped (costing the
    picture height) and nothing re-planned."""
    tab, view = _control_view(qapp, 1346, 600)
    mirror = tab.mirror_tab
    changes = []
    mirror.toolbar_changed.connect(lambda: changes.append(mirror.toolbar_width()))
    try:
        idle = mirror.toolbar_width()
        mirror._ensure_options_menu()
        mirror.act_embed.setChecked(False)
        _wait(qapp, 150)
        assert mirror.btn_mirror.text() == "Start separate window"
        assert mirror.toolbar_width() > idle and changes[-1] == mirror.toolbar_width()
        frame = view._card_frame()
        assert view.split.sizes()[0] >= mirror.toolbar_width() + frame
        assert _one_row(view)
        mirror.act_embed.setChecked(True)
        _wait(qapp, 150)
        assert mirror.toolbar_width() == idle and _one_row(view)
    finally:
        _close_view(tab, view)


def test_a_changing_frame_rate_never_moves_or_wraps_the_toolbar(qapp):
    """Issue: "9.8 fps" -> "10.2 fps" widened the label past the card, wrapped
    the toolbar and made the picture jump as the rate hovered around 10."""
    tab, view = _control_view(qapp, 1346, 600)
    mirror = tab.mirror_tab
    changes = []
    try:
        mirror.set_screen_backend("screencap")
        mirror._launch_pending = True
        mirror._refresh_buttons()
        mirror._screencap = _FakeScreencap("screencap · 9.8 fps")
        mirror._sync_renderer_chip()
        _wait(qapp, 150)
        mirror.toolbar_changed.connect(lambda: changes.append(True))
        sizes, chip_w = view.split.sizes(), mirror.lbl_renderer.width()
        container_h = mirror.container.height()
        assert _one_row(view)
        for text in ("screencap · 10.2 fps", "screencap · 9.9 fps", "screencap · 10.0 fps"):
            mirror._screencap.text = text
            mirror._sync_renderer_chip()
            _wait(qapp, 100)
            assert mirror.lbl_renderer.text() == text
            assert view.split.sizes() == sizes and mirror.lbl_renderer.width() == chip_w
            assert mirror.container.height() == container_h and _one_row(view)
        assert changes == []  # nothing to re-plan
    finally:
        mirror._screencap = None
        mirror._launch_pending = False
        _close_view(tab, view)


@pytest.mark.parametrize("width, height", [(1180, 600), (1500, 560)])
@pytest.mark.parametrize("backend", ["scrcpy", "screencap"])
def test_starting_and_stopping_a_screen_never_reflows_the_controls(qapp, width, height, backend):
    """Issue: the planned toolbar width followed Start/Stop and the frame-rate
    label, so the controls panel changed its column count on Start, again when
    the rate appeared, and on Stop."""
    tab, view = _control_view(qapp, width, height)
    mirror = tab.mirror_tab
    try:
        mirror.set_screen_backend(backend)
        _wait(qapp, 150)
        idle = (view.split.sizes(), view.controls._ncols)
        assert _one_row(view)

        mirror._launch_pending = True  # Start: Stop replaces it
        mirror._refresh_buttons()
        _wait(qapp, 150)
        assert (view.split.sizes(), view.controls._ncols) == idle and _one_row(view)
        if backend == "screencap":
            mirror._screencap = _FakeScreencap("screencap · 4.8 fps")
            mirror._sync_renderer_chip()
            _wait(qapp, 150)
            assert not mirror.lbl_renderer.isHidden()
            assert (view.split.sizes(), view.controls._ncols) == idle and _one_row(view)
            mirror._screencap = None
            mirror._sync_renderer_chip()
        mirror._launch_pending = False  # Stop
        mirror._refresh_buttons()
        _wait(qapp, 150)
        assert (view.split.sizes(), view.controls._ncols) == idle
    finally:
        mirror._screencap = None
        mirror._launch_pending = False
        _close_view(tab, view)


def test_the_display_picker_elides_only_a_name_that_does_not_fit(qapp):
    """Issue: "default display" painted as "default displ…" in a picker wide
    enough for it (4 px of slack where the style insets the label by 2)."""
    from PyQt5.QtWidgets import QStyle, QStyleOptionComboBox

    tab, view = _control_view(qapp, 1346, 600)
    combo = tab.mirror_tab.cmb_display
    try:
        text = combo.currentText()
        assert text == "default display"
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        field = combo.style().subControlRect(
            QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxEditField, combo
        )
        chrome = combo.width() - field.width()  # the arrow and frame around the label
        fits = combo.fontMetrics().horizontalAdvance(text) + 2  # the style's 1 px inset each side
        combo.setMaximumWidth(fits + chrome + 10)
        combo.resize(fits + chrome, combo.height())  # exactly wide enough
        assert combo.label_text() == "default display"
        combo.resize(fits + chrome - 1, combo.height())  # one pixel short
        assert combo.label_text().endswith("…")
        combo.setMaximumWidth(combo.MAX_WIDTH)
        combo.addItem("Display 3  ·  Rear seat entertainment left  ·  2560x1440", 3)
        combo.setCurrentIndex(1)
        _wait(qapp, 50)
        assert combo.width() == combo.MAX_WIDTH
        painted = combo.label_text()
        assert painted.endswith("…") and painted != combo.currentText()
        assert combo.toolTip().startswith(combo.currentText())  # the full name
    finally:
        _close_view(tab, view)
