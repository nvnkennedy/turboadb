"""Window-structure regressions: device state pill, theme-following glyph icons,
and the themed tab close button."""
import pytest

pytest.importorskip("PyQt5")


class _RecordingPainter:
    """Stands in for QPainter so glyph colour checks need no installed fonts."""

    def __init__(self):
        self.pens = []

    def setPen(self, colour):
        self.pens.append(colour.name())

    def save(self):
        pass

    def restore(self):
        pass

    def setRenderHint(self, *_args):
        pass

    def setFont(self, _font):
        pass

    def drawText(self, *_args):
        pass


def test_device_state_pill_shows_short_state_and_tints_itself(qapp):
    from turboadb.gui.device_tab import _StatePill

    pill = _StatePill("Connecting…")
    assert pill.property("state") == "warn"
    pill.setText("Connected — vivo V2318")
    # The device name is already the page title; the pill keeps the state only.
    assert pill.text() == "Connected"
    assert pill.toolTip() == "Connected — vivo V2318"
    assert pill.full_text() == "Connected — vivo V2318"
    assert pill.property("state") == "ok"
    pill.setText("Connect failed")
    assert pill.property("state") == "error"
    pill.setText("Device didn't come back (timed out)")
    assert pill.property("state") == "error"
    pill.setText("Rebooted to recovery — reconnect after Android is ready")
    assert pill.property("state") == "warn"


def test_glyph_icons_follow_the_active_theme(qapp):
    from PyQt5.QtCore import QRect
    from PyQt5.QtGui import QColor, QIcon
    from turboadb.gui import theme

    engine = theme._glyph_engine_class()("■")
    fixed = theme._glyph_engine_class()("■", "#ff0000")

    def pen(glyph_engine, mode=QIcon.Normal):
        painter = _RecordingPainter()
        glyph_engine.paint(painter, QRect(0, 0, 20, 20), mode, QIcon.Off)
        return painter.pens[-1]

    try:
        theme.apply_to_app(qapp, "dark")
        assert pen(engine) == QColor(theme.palette("dark")["dim"]).name()
        assert pen(engine, QIcon.Disabled) == QColor(theme.palette("dark")["frame"]).name()
        # The same icon repaints in whichever palette is applied at paint time.
        theme.apply_to_app(qapp, "light")
        assert pen(engine) == QColor(theme.palette("light")["dim"]).name()
        assert pen(fixed) == "#ff0000"
    finally:
        theme.apply_to_app(qapp, "dark")
    assert not theme.emoji_icon("■").isNull()


def test_tab_close_button_is_themed_not_qt_fallback(qapp):
    from turboadb.gui import theme

    css = theme.stylesheet("dark")
    assert "QTabBar::close-button" in css
    assert "turboadb-close-" in css


def _contrast(fg, bg):
    def lum(colour):
        channels = [int(colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_colour_tones_stay_readable_in_every_theme(qapp):
    from turboadb.gui import theme

    for name in theme.theme_names():
        tokens = theme.palette(name)
        for tone in theme.TONES:
            colour = theme.hue(tone, name)
            # Coloured icons and tone labels must stand out from panels…
            for layer in ("win", "panel", "raised"):
                assert _contrast(colour, tokens[layer]) >= 3.0, (name, tone, layer)
            # …and normal text must stay readable on the tinted tile wash.
            for extra in (0.0, 0.09, 0.18):
                wash = theme.tint(tone, name, extra)
                assert _contrast(tokens["text"], wash) >= 4.5, (name, tone, extra)


def test_every_vector_icon_is_valid_svg(qapp):
    from turboadb.gui import icons

    assert len(icons.icon_names()) > 80
    for name in icons.icon_names():
        assert icons._renderer(name, "#123456", 1.9) is not None, name
    for alias, target in icons.ALIASES.items():
        assert target in icons.ICONS, alias
    assert not icons.icon("wifi", "blue").isNull()
    assert icons.svg("does-not-exist", "#000000") == ""


def test_toolbar_flow_layout_wraps_instead_of_widening(qapp):
    from PyQt5.QtWidgets import QLineEdit, QPushButton, QWidget
    from turboadb.gui.flowlayout import ToolbarFlowLayout

    bar = QWidget()
    row = ToolbarFlowLayout(bar, hspacing=8, vspacing=6)
    row.setContentsMargins(0, 0, 0, 0)
    left = [QPushButton(f"Action {i}") for i in range(8)]
    for button in left:
        row.addWidget(button)
    field = QLineEdit()
    row.addWidget(field, 1)
    row.addSpacing(8)
    row.addStretch(1)
    right = QPushButton("Delete")
    row.addWidget(right)
    one_row = sum(w.sizeHint().width() for w in left + [field, right]) + 8 * 10 + 8
    # The minimum is one control wide, never the whole row.
    assert row.minimumSize().width() < 200
    assert row.heightForWidth(one_row + 40) < row.heightForWidth(300)
    bar.resize(one_row + 200, row.heightForWidth(one_row + 200))
    row.setGeometry(bar.rect())
    # With spare room the stretch field grows and Delete sits at the right edge.
    assert field.geometry().width() > field.sizeHint().width()
    assert right.geometry().right() == bar.rect().right()


def test_terminal_zoom_resizes_every_terminal(qapp, monkeypatch):
    from turboadb.gui import settings as settings_mod
    from turboadb.gui.console import AnsiConsole

    monkeypatch.setattr(settings_mod, "set_later", lambda *_a, **_k: None)
    first, second, third = AnsiConsole(), AnsiConsole(), AnsiConsole()
    sizes = []
    third.font_size_changed.connect(sizes.append)
    start = first.font_size()
    for console in (second, third):
        console.set_font_size(start)
    first.bump_font(2)
    assert first.font_size() == second.font_size() == third.font_size() == start + 2
    assert sizes == [start + 2]


class _NoopDispatcher:
    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        return True

    def stop(self):
        pass


class _MockHandler:
    serial = "mock"
    config = None


def _own_stylesheets(root):
    from PyQt5.QtWidgets import QWidget

    return [
        (type(w).__name__, w.objectName(), w.styleSheet())
        for w in [root] + root.findChildren(QWidget)
        if w.styleSheet()
    ]


def test_pages_keep_their_styling_in_the_app_stylesheet(qapp):
    """Qt keeps a separate style engine for every widget with its own
    stylesheet (and its subtree) and rebuilds each on a live theme switch —
    about 320 ms instead of 120 ms with a device tab open. Page styling belongs
    in theme.stylesheet(), keyed by object name or dynamic property."""
    import types

    from turboadb.gui.controls_panel import ControlsPanel
    from turboadb.gui.mirror_panel import MirrorPanel
    from turboadb.gui.welcome import WelcomeScreen

    # (class name, object name) -> why that widget keeps its own stylesheet.
    # None today. (MirrorPanel's video container turns black only while a
    # screen session runs, so it never has one here.)
    kept = {}
    win = types.SimpleNamespace(
        new_session=lambda: None, open_webcam_tab=lambda: None,
        upgrade_tools_gui=lambda: None, _version="test",
    )
    welcome = WelcomeScreen(win)
    controls = ControlsPanel(_MockHandler(), compact=True, dispatcher=_NoopDispatcher())
    mirror = MirrorPanel(_MockHandler(), {"name": "mock"}, dispatcher=_NoopDispatcher())
    try:
        for page in (welcome, controls, mirror):
            own = [item for item in _own_stylesheets(page) if item[:2] not in kept]
            assert own == [], (type(page).__name__, own)
    finally:
        mirror._scrcpy = None
        mirror._recording = False
        mirror.close_panel()
        mirror.close()
        controls.close_panel()
        for page in (welcome, controls, mirror):
            page.deleteLater()


def test_page_rules_follow_every_theme(qapp):
    from turboadb.gui import theme

    for name in ("dark", "light"):
        css = theme.stylesheet(name)
        tokens = theme.palette(name)
        for state, (fg, bg) in theme.status_colors(name).items():
            rule = css.split(f'QLabel#cameraStatus[state="{state}"] {{', 1)[1].split("}", 1)[0]
            assert f"color: {fg};" in rule
            assert ("background" not in rule) if bg == "transparent" else f"background: {bg};" in rule
        assert f"QWidget#screenOptions {{ background: {tokens['raised']}; }}" in css
        assert f'QLineEdit[invalid="true"] {{ color: {tokens["danger_text"]}; }}' in css
        assert f"QPlainTextEdit#logcatView {{ background: {theme.TERM_BG};" in css


def test_error_toast_is_one_red_popup_with_one_beep(qapp, monkeypatch):
    from PyQt5.QtWidgets import QMainWindow
    from turboadb.gui import fileutil

    beeps = []
    monkeypatch.setattr(fileutil, "_play_error_sound", lambda: beeps.append(1))
    monkeypatch.setattr(fileutil, "_LAST_ERROR_SOUND", [float("-inf")])
    host = QMainWindow()
    host.resize(900, 600)
    try:
        first = fileutil.error_toast(host, "adb push failed", action=lambda: None)
        second = fileutil.error_toast(host, "adb pull failed", action=lambda: None)
        assert first is second and first.objectName() == "errorToast"
        assert first.title.text() == "Error (2)" and first.text.text() == "adb pull failed"
        assert beeps == [1]
        activity = fileutil.activity_toast(host, "Saved screenshot", level="ok")
        again = fileutil.activity_toast(host, "Rebooting…")
        assert activity is again and again.text.text() == "Rebooting…"
        assert not again.action.isVisibleTo(again)
    finally:
        for toast in (getattr(host, "_turboadb_error_toast", None),
                      getattr(host, "_turboadb_activity_toast", None)):
            if toast is not None:
                toast.hide()
        host.deleteLater()
