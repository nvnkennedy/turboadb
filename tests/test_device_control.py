"""Device Control page: the control-centre panel and the idle screen frame."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt5")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
