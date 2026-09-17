"""Multi-display devices: listing displays over adb, the display picker, the
all-displays tab, and where Device Control puts its controls."""
import pytest

from turboadb import ADBConfig, ADBHandler

ANDROID_13_GET_DISPLAYS = (
    "Displays:\n"
    'Display id 0: DisplayInfo{"Built-in Screen", displayId 0, displayGroupId 0, FLAG_SECURE, '
    "real 1920 x 720, largest app 1920 x 1920, mode 1, supportedModes [{id=1, width=1920}], "
    "state ON}, DisplayMetrics{density=1.5}\n"
    'Display id 2: DisplayInfo{"Instrument cluster", displayId 2, displayGroupId 1, '
    "real 1920 x 720, state ON}\n"
    'Display id 3: DisplayInfo{"Passenger", displayId 3, displayGroupId 2, real 1280 x 720, '
    "state OFF}\n"
)

ANDROID_10_DUMPSYS = (
    "  Logical Displays: size=2\n"
    "  Display 0:\n"
    "    mDisplayId=0\n"
    '    mBaseDisplayInfo=DisplayInfo{"Built-in Screen, displayId 0", uniqueId "local:0", '
    "app 1280 x 720, real 1280 x 720, largest app 1280 x 1280}\n"
    '    mOverrideDisplayInfo=DisplayInfo{"Built-in Screen, displayId 0", uniqueId "local:0", '
    "app 1280 x 720, real 1280 x 720}\n"
    "  Display 1:\n"
    "    mDisplayId=1\n"
    '    mBaseDisplayInfo=DisplayInfo{"HDMI Screen, displayId 1", uniqueId "local:1", '
    "real 1920 x 1080}\n"
)

ANDROID_9_DUMPSYS = (
    "  Display 0:\n"
    "    mDisplayId=0\n"
    '    mBaseDisplayInfo=DisplayInfo{"Built-in Screen", uniqueId "local:0", app 1080 x 1920, '
    "real 1080 x 1920}\n"
)


def test_display_info_is_parsed_from_every_android_format():
    assert ADBHandler.parse_display_info(ANDROID_13_GET_DISPLAYS) == [
        {"id": 0, "size": "1920x720", "name": "Built-in Screen"},
        {"id": 2, "size": "1920x720", "name": "Instrument cluster"},
        {"id": 3, "size": "1280x720", "name": "Passenger"},
    ]
    # a uniqueId adds the physical id screencap -d takes; the other keys stay
    assert ADBHandler.parse_display_info(ANDROID_10_DUMPSYS) == [
        {"id": 0, "size": "1280x720", "name": "Built-in Screen",
         "unique_id": "local:0", "physical_id": 0},
        {"id": 1, "size": "1920x1080", "name": "HDMI Screen",
         "unique_id": "local:1", "physical_id": 1},
    ]
    assert ADBHandler.parse_display_info(ANDROID_9_DUMPSYS) == [
        {"id": 0, "size": "1080x1920", "name": "Built-in Screen",
         "unique_id": "local:0", "physical_id": 0}
    ]
    # a phone lists display 0 twice (physical, then the current override size)
    phone = (
        'mBaseDisplayInfo=DisplayInfo{"Built-in Screen", displayId 0, real 1260 x 2800}\n'
        'mOverrideDisplayInfo=DisplayInfo{"Built-in Screen", displayId 0, real 1080 x 2400}\n'
    )
    assert ADBHandler.parse_display_info(phone) == [
        {"id": 0, "size": "1080x2400", "name": "Built-in Screen"}
    ]
    assert ADBHandler.parse_display_info("") == []


def test_listing_displays_asks_android_and_never_starts_scrcpy(fake_adb, monkeypatch):
    import turboadb.scrcpy as scrcpy

    monkeypatch.setattr(
        scrcpy, "list_displays", lambda *a, **k: pytest.fail("scrcpy must not be started")
    )
    fake_adb.add("get-displays", stdout=ANDROID_13_GET_DISPLAYS)
    displays = ADBHandler(ADBConfig(serial="ivi")).list_displays()
    assert [d["id"] for d in displays] == [0, 2, 3]


def test_older_android_is_listed_from_dumpsys_display(fake_adb):
    fake_adb.add("get-displays", stdout="Unknown command: get-displays\n", returncode=255)
    fake_adb.add("dumpsys display", stdout=ANDROID_10_DUMPSYS)
    displays = ADBHandler(ADBConfig(serial="x")).list_displays(method="adb")
    assert [(d["id"], d["name"]) for d in displays] == [(0, "Built-in Screen"), (1, "HDMI Screen")]


def test_scrcpy_is_the_fallback_only_when_android_says_nothing(fake_adb, monkeypatch):
    import turboadb.scrcpy as scrcpy

    monkeypatch.setattr(scrcpy, "list_displays", lambda *a, **k: [{"id": 0, "size": "1080x2400"}])
    dev = ADBHandler(ADBConfig(serial="x"))
    assert dev.list_displays() == [{"id": 0, "size": "1080x2400"}]
    assert dev.list_displays(method="adb") == []  # the quiet scan never falls back
    res = ADBHandler(ADBConfig(serial="x"), safe=True).list_displays(method="bogus")
    assert not res.success


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _NowThread:
    """A FunctionThread stand-in that runs its function on start()."""

    def __init__(self, fn):
        self.fn, self.done, self.fail = fn, _Signal(), _Signal()

    def start(self):
        self.done.emit(self.fn())

    def isRunning(self):
        return False

    def isFinished(self):
        return True


def test_quiet_display_scan_fills_the_picker_with_every_display_once(qapp, monkeypatch):
    """Issue: only "default display" was listed until Manage displays was opened."""
    pytest.importorskip("PyQt5")
    import turboadb.gui.mirror_panel as mp_mod

    calls = []

    class Handler:
        serial = "ivi"
        config = None

        def list_displays(self, method="auto", safe=None):
            calls.append(method)
            return ADBHandler.parse_display_info(ANDROID_13_GET_DISPLAYS)

    monkeypatch.setattr(mp_mod, "FunctionThread", _NowThread)
    panel = mp_mod.MirrorPanel(Handler(), {"name": "ivi"})
    found = []
    panel.displays_changed.connect(found.append)
    try:
        panel.refresh_displays(quiet=True)
        assert calls == ["adb"]
        combo = panel.cmb_display
        assert [combo.itemData(i) for i in range(combo.count())] == [None, 2, 3]
        assert combo.itemText(0) == "Display 0  ·  Built-in Screen  ·  1920x720"
        assert combo.itemText(1) == "Display 2  ·  Instrument cluster  ·  1920x720"
        assert len(found) == 1 and len(found[0]) == 3
        assert not panel.btn_ivi.isHidden()  # several displays: "All displays" is offered
        panel._sync_display_selection(2)
        panel._sync_display_selection(0)  # display 0 selects the default entry
        assert combo.currentData() is None
    finally:
        panel.close_panel()
        panel.close()


def test_a_display_tile_is_locked_to_its_display_without_audio(qapp):
    pytest.importorskip("PyQt5")
    from turboadb.config import ScrcpyOptions
    from turboadb.gui.mirror_panel import MirrorPanel

    class Handler:
        serial = "ivi"
        config = None

    panel = MirrorPanel(Handler(), {"name": "ivi"}, automotive=True)
    try:
        panel.set_fixed_display({"id": 3, "size": "1280x720", "name": "Passenger"})
        assert panel.cmb_display.count() == 1 and panel.cmb_display.currentData() == 3
        for widget in (panel.cmb_display, panel.btn_audio, panel.btn_opts, panel.btn_max, panel.btn_ivi):
            assert widget.isHidden()
        panel._got_displays([{"id": 0}, {"id": 3}])  # a device-wide scan never changes a tile
        assert panel.cmb_display.count() == 1
        opts = panel._apply_audio_options(ScrcpyOptions(), allow_audio=not panel._fixed_display)
        assert opts.no_audio is True
        assert abs(panel.display_aspect() - 1280 / 720) < 0.01
    finally:
        panel.close_panel()
        panel.close()


class _WallHandler:
    serial = "ivi"
    config = None


class _NullDispatcher:
    """Accepts device commands and runs none (these tests touch no device)."""

    submitted = 0

    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        return True

    def stop(self):
        pass


def _wall(displays, automotive=True):
    """A DisplayWall showing *displays*, never shown and never started."""
    from turboadb.gui.display_wall import DisplayWall

    wall = DisplayWall(
        _WallHandler(), {"name": "ivi"}, automotive=automotive, dispatcher=_NullDispatcher()
    )
    wall.set_displays(displays)
    return wall


def _close_wall(wall, qapp):
    """Close the wall and let Qt finish its deferred deletes inside the test
    (a retired tile is destroyed from the event loop, not on the spot)."""
    wall.close_panel()
    wall.close()
    wall.deleteLater()
    for _ in range(3):
        qapp.processEvents()


def test_an_empty_display_scan_never_removes_the_shown_screens(qapp):
    """Issue: list_displays legitimately returns [], so pressing Rescan on a
    head unit tore down every live tile."""
    pytest.importorskip("PyQt5")
    wall = _wall([{"id": 0, "size": "1920x720"}, {"id": 2, "size": "1920x720"}])
    try:
        tiles = list(wall._tiles)
        assert len(tiles) == 2
        wall.set_displays([])
        wall.set_displays(None)
        assert wall._tiles == tiles
        assert "2 displays" in wall.summary.text()
        # a scan that does find something is still applied
        wall.set_displays([{"id": 0, "size": "1920x720"}])
        assert [tile.display_id for tile in wall._tiles] == [0]
    finally:
        _close_wall(wall, qapp)


def test_the_wall_waits_as_long_as_a_screen_may_take_to_start():
    """Issue: a 15 s wall timeout started the next display while the previous
    one was still pushing its scrcpy server (a panel waits 25 s for that)."""
    pytest.importorskip("PyQt5")
    from turboadb.gui.display_wall import DisplayWall
    from turboadb.gui.mirror_panel import MirrorPanel

    assert DisplayWall.START_TIMEOUT_MS >= MirrorPanel._STARTUP_TIMEOUT * 1000


def test_start_all_and_stop_all_follow_what_the_tiles_are_doing(qapp):
    """Issue: "Start all" stayed enabled with everything running and "Stop all"
    with nothing running."""
    pytest.importorskip("PyQt5")
    wall = _wall([{"id": 0, "size": "1920x720"}, {"id": 2, "size": "1920x720"}])
    try:
        assert wall.btn_start_all.isEnabled() and not wall.btn_stop_all.isEnabled()
        wall._tiles[0].panel.is_active = lambda: True
        wall._sync_buttons()
        assert wall.btn_start_all.isEnabled() and wall.btn_stop_all.isEnabled()
        wall._tiles[1].panel.is_active = lambda: True
        wall._sync_buttons()
        assert not wall.btn_start_all.isEnabled() and wall.btn_stop_all.isEnabled()
    finally:
        _close_wall(wall, qapp)


def test_a_display_that_changed_size_reshapes_its_tile(qapp):
    """Issue: update_display refreshed the dict and the title but left the
    panel locked to the old display, so the tile kept a stale aspect."""
    pytest.importorskip("PyQt5")
    wall = _wall([{"id": 2, "size": "1920x720", "name": "Cluster"}])
    try:
        tile = wall._tiles[0]
        assert abs(tile.aspect() - 1920 / 720) < 0.01
        tile.panel._video_aspect = 1920 / 720  # the shape scrcpy reported
        wall.set_displays([{"id": 2, "size": "1280x720", "name": "Cluster"}])
        assert wall._tiles == [tile]  # the same tile keeps running
        assert "1280x720" in tile.title.text()
        assert tile.panel._displays == [{"id": 2, "size": "1280x720", "name": "Cluster"}]
        assert tile.panel._video_aspect is None  # the old video shape is dropped
        assert abs(tile.aspect() - 1280 / 720) < 0.01
    finally:
        _close_wall(wall, qapp)


def test_the_tile_header_is_measured_rather_than_assumed(qapp):
    """Issue: TILE_HEADER hard-coded what MirrorPanel.chrome_height measures."""
    pytest.importorskip("PyQt5")
    wall = _wall([{"id": 0, "size": "1920x720"}])
    try:
        tile = wall._tiles[0]
        tile.header_height = lambda: 0  # nothing laid out yet
        assert wall._tile_header() == wall.TILE_HEADER
        tile.header_height = lambda: 132
        assert wall._tile_header() == 132
    finally:
        _close_wall(wall, qapp)


def test_a_removed_tile_is_destroyed_only_once_its_screen_has_stopped(qapp):
    """Issue: the tile (and the embed container HWND scrcpy is a child of) was
    deleted while the stop was still running on a parked thread."""
    pytest.importorskip("PyQt5")
    wall = _wall([{"id": 0, "size": "1920x720"}, {"id": 2, "size": "1920x720"}])
    try:
        tile = wall._tiles[1]
        tile.panel._stopping_mirror = True  # a stop is in flight
        wall.set_displays([{"id": 0, "size": "1920x720"}])
        assert wall._retiring == [tile] and tile.isHidden()
        tile.panel._stopping_mirror = False
        tile.panel.screen_stopped.emit()
        assert wall._retiring == []
    finally:
        _close_wall(wall, qapp)


def test_a_hidden_wall_slows_its_tiles_polling_without_stopping_them(qapp):
    """Issue: every tile kept a 500 ms re-fit and process poll while the tab
    was off screen."""
    pytest.importorskip("PyQt5")
    from PyQt5.QtCore import QTimer
    from turboadb.gui.mirror_panel import MirrorPanel

    wall = _wall([{"id": 0, "size": "1920x720"}, {"id": 2, "size": "1920x720"}])
    try:
        panel = wall._tiles[0].panel
        panel._mon = QTimer(panel)
        panel._mon.start(MirrorPanel.POLL_MS)
        wall._set_tiles_paused(True)
        assert [t.panel._poll_interval for t in wall._tiles] == [MirrorPanel.IDLE_POLL_MS] * 2
        assert panel._mon.interval() == MirrorPanel.IDLE_POLL_MS
        assert not any(t.panel.is_active() for t in wall._tiles)  # nothing was stopped
        wall._set_tiles_paused(False)
        assert [t.panel._poll_interval for t in wall._tiles] == [MirrorPanel.POLL_MS] * 2
        assert panel._mon.interval() == MirrorPanel.POLL_MS
        panel._mon.stop()
    finally:
        _close_wall(wall, qapp)


def test_a_tile_retries_inside_the_wall_and_reports_without_a_dialog(qapp, monkeypatch):
    """Issue: the last retry rung dropped embedding, so a failing tile opened a
    free-floating scrcpy window, and each failure stacked a modal log dialog."""
    pytest.importorskip("PyQt5")
    wall = _wall([{"id": 2, "size": "1920x720", "name": "Cluster"}])
    try:
        panel = wall._tiles[0].panel
        retries, starts, opened = [], [], []
        monkeypatch.setattr(panel, "_schedule_retry", lambda ms, cb: retries.append(cb))
        monkeypatch.setattr(panel, "start", lambda **kwargs: starts.append(kwargs))
        monkeypatch.setattr(panel, "_open_scrcpy_log", lambda err: opened.append(err))
        panel._launch_spec = {"display_id": 2, "compat": False, "embed": True}

        panel._retry_count = 1  # the compatibility rung
        panel._retry_or_show_scrcpy_failure("no encoder")
        retries.pop()()
        assert starts == [
            {"display_id": 2, "compat": True, "embed": True, "_retry": True}
        ]

        panel._retry_count = 2  # out of retries: report, never open a dialog
        panel._retry_or_show_scrcpy_failure("no encoder")
        qapp.processEvents()
        assert opened == [] and not retries
        assert "Show log" in panel.status.text()
        panel._on_status_link("#scrcpy-log")  # …until the tile's link is used
        qapp.processEvents()
        assert opened == ["no encoder"]
    finally:
        _close_wall(wall, qapp)


def test_best_columns_shows_displays_as_large_as_possible():
    pytest.importorskip("PyQt5")
    from turboadb.gui.display_wall import best_columns

    assert best_columns(1, 1600, 900, 16 / 9) == 1
    # two wide head-unit displays in a wide window: stacked beats side by side
    assert best_columns(2, 1800, 1000, 1920 / 720, header=96) == 1
    # four square displays: a 2 x 2 grid
    assert best_columns(4, 1800, 1400, 1.0, header=96) == 2
    # two portrait phones sit side by side
    assert best_columns(2, 1800, 1000, 9 / 19.5, header=96) == 2


def test_controls_go_where_the_device_screen_is_largest():
    """Issue: on a big monitor a wide IVI screen was squeezed by the controls."""
    pytest.importorskip("PyQt5")
    from turboadb.gui.device_tab import choose_control_layout

    # a portrait phone keeps its controls at the side
    assert choose_control_layout(1800, 900, 9 / 19.5, 360, 380) == "side"
    # a 1920x720 head unit in a normal window: a narrow side column wins
    assert choose_control_layout(1850, 700, 1920 / 720, 360, 360) == "side"
    # the same head unit in a large window: the controls move below it
    assert choose_control_layout(2500, 1300, 1920 / 720, 360, 360) == "below"
    # close to the boundary the current layout is kept (no flip-flopping)
    assert choose_control_layout(2000, 1000, 1920 / 720, 360, 360, current="side") == "side"
    assert choose_control_layout(2000, 1000, 1920 / 720, 360, 360, current="below") == "below"


def test_controls_strip_uses_more_columns_and_is_shorter(qapp):
    pytest.importorskip("PyQt5")
    from turboadb.gui.controls_panel import ControlsPanel

    cp = ControlsPanel(None, compact=True)
    try:
        cp.resize(1800, 400)
        cp.set_strip(True)
        qapp.processEvents()  # the flip relayouts from the event loop, once
        assert cp._ncols == 5 and cp.minimumWidth() == 1
        assert cp.content_height(1800, strip=True) < cp.content_height(340, strip=False)
        cp.set_strip(False)
        qapp.processEvents()
        assert cp._ncols == 3 and cp.minimumWidth() == 340
    finally:
        cp.close_panel()
        cp.close()


def test_a_strip_flip_rebuilds_the_controls_once_at_the_new_width(qapp):
    """Issue: set_strip rebuilt every section at the OLD width and the
    splitter's following resize rebuilt them all over again."""
    pytest.importorskip("PyQt5")
    from turboadb.gui.controls_panel import ControlsPanel

    cp = ControlsPanel(None, compact=True)
    try:
        cp.resize(360, 900)
        qapp.processEvents()
        rebuilds = []
        relayout = cp._relayout
        cp._relayout = lambda ncols: (rebuilds.append(ncols), relayout(ncols))[-1]
        cp.set_strip(True)
        assert rebuilds == []  # nothing is rebuilt at the width we are leaving
        cp.resize(1800, 320)  # the splitter hands over the strip's real width
        qapp.processEvents()
        assert rebuilds == [5]  # exactly one rebuild, at the new width
    finally:
        cp.close_panel()
        cp.close()


def test_maximizing_a_tile_shows_that_display_alone_and_restore_returns_the_grid(qapp):
    """Issue: Maximize in the displays tab did nothing visible. A tile's
    Maximize shows only its display, filling the wall; Restore (or Esc) brings
    the grid back."""
    pytest.importorskip("PyQt5")
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest

    wall = _wall([
        {"id": 0, "size": "1920x720"},
        {"id": 2, "size": "1920x720"},
        {"id": 3, "size": "1280x720"},
    ])
    wall.start_all = lambda: None  # showing the wall never starts a screen here
    wall.setAttribute(Qt.WA_DontShowOnScreen, True)
    wall.resize(1600, 900)
    wall.show()
    for _ in range(10):
        qapp.processEvents()
    try:
        first, second, third = wall._tiles
        wall._relayout()
        grid_size = second.size()
        assert second.btn_max is second.btn_focus
        assert second.btn_max.text() == "Maximize" and not second.btn_max.isChecked()
        assert not wall._esc_restore.isEnabled()

        second.btn_max.click()
        for _ in range(10):
            qapp.processEvents()
        assert wall.maximized_tile() is second
        assert first.isHidden() and third.isHidden() and not second.isHidden()
        assert second.btn_max.isChecked() and second.btn_max.text() == "Restore"
        assert "Esc" in second.btn_max.toolTip()
        # the display fills the wall's whole body now
        assert second.width() == wall.body.width() and second.height() == wall.body.height()
        assert second.width() > grid_size.width() or second.height() > grid_size.height()

        # maximizing another display switches to it
        third.btn_max.click()
        assert wall.maximized_tile() is third and second.isHidden()
        assert not second.btn_max.isChecked() and second.btn_max.text() == "Maximize"

        third.btn_max.click()  # Restore
        for _ in range(10):
            qapp.processEvents()
        assert wall.maximized_tile() is None
        assert not any(tile.isHidden() for tile in wall._tiles)
        assert all(tile.btn_max.text() == "Maximize" for tile in wall._tiles)

        # Esc restores the grid too
        first.btn_max.click()
        assert wall._esc_restore.isEnabled()
        qapp.setActiveWindow(wall)
        QTest.keyClick(wall.btn_rescan, Qt.Key_Escape)
        for _ in range(5):
            qapp.processEvents()
        assert wall.maximized_tile() is None and not second.isHidden()
        assert not first.btn_max.isChecked() and not wall._esc_restore.isEnabled()
    finally:
        wall.hide()
        _close_wall(wall, qapp)


def test_esc_outside_the_wall_is_left_to_whatever_has_the_keyboard(qapp):
    """Esc restores a maximized display only while the keyboard focus is in
    the displays tab; a field elsewhere in the window keeps its own Esc."""
    pytest.importorskip("PyQt5")
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QHBoxLayout, QLineEdit, QWidget

    class Field(QLineEdit):
        def __init__(self):
            super().__init__()
            self.keys = []

        def keyPressEvent(self, event):
            self.keys.append(event.key())
            super().keyPressEvent(event)

    host = QWidget()
    host.setAttribute(Qt.WA_DontShowOnScreen, True)
    lay = QHBoxLayout(host)
    field = Field()
    wall = _wall([{"id": 0, "size": "1920x720"}, {"id": 2, "size": "1920x720"}])
    wall.start_all = lambda: None
    lay.addWidget(field)
    lay.addWidget(wall, 1)
    host.resize(1600, 800)
    host.show()
    qapp.setActiveWindow(host)
    for _ in range(10):
        qapp.processEvents()
    try:
        first, second = wall._tiles
        field.setFocus()
        wall.maximize_tile(second)  # not a click: the focus was outside the wall
        assert wall.isAncestorOf(qapp.focusWidget())  # moved in, so Esc works now
        field.setFocus()
        QTest.keyClick(field, Qt.Key_Escape)
        assert Qt.Key_Escape in field.keys
        assert wall.maximized_tile() is second  # the field's Esc, not the wall's
        second.btn_max.setFocus()
        QTest.keyClick(second.btn_max, Qt.Key_Escape)
        assert wall.maximized_tile() is None and not first.isHidden()
    finally:
        host.hide()
        _close_wall(wall, qapp)
        host.deleteLater()
        qapp.processEvents()


# --------------------------------------------------------------------------- #
# Physical display ids (screencap -d) and the screencap renderer
# --------------------------------------------------------------------------- #
# Trimmed from a real Android 16 phone (one 1080x2400 display, override size;
# the panel is 1260x2800). uniqueId sits after nested {…} lists.
PHONE_GET_DISPLAYS = (
    "Displays:\n"
    'Display id 0: DisplayInfo{"Built-in Screen", displayId 0, displayGroupId 0, FLAG_SECURE, '
    "FLAG_SUPPORTS_PROTECTED_BUFFERS, FLAG_TRUSTED, real 1080 x 2400, largest app 2400 x 2400, "
    "smallest app 1080 x 1080, appVsyncOff 1000000, presDeadline 16666666, mode 3, "
    "renderFrameRate 60.000004, hasArrSupport false, frameRateCategoryRate "
    "FrameRateCategoryRate {normal=60.0, high=90.0}, supportedRefreshRates [120.00001, 90.0, "
    "60.000004], defaultMode 1, userPreferredModeId -1, supportedModes [{id=1, width=1260, "
    "height=2800, fps=120.00001, vsync=120.00001, synthetic=false, alternativeRefreshRates="
    "[60.000004, 90.0], supportedHdrTypes=[2, 3, 4]}], hdrCapabilities HdrCapabilities{"
    "mSupportedHdrTypes=[2, 3, 4], mMaxLuminance=800.0}, isForceSdr false, rotation 0, state ON, "
    'committedState ON, type INTERNAL, uniqueId "local:4630946650788219010", app 1080 x 2400, '
    "density 420 (450.76 x 452.993) dpi, layerStack 0, colorMode 0}, DisplayMetrics{density=2.625, "
    "width=1080, height=2400}, isValid=true\n"
)

PHONE_DUMPSYS_DISPLAY = (
    "  mViewports=[DisplayViewport{type=INTERNAL, valid=true, isActive=true, displayId=0, "
    "uniqueId='local:4630946650788219010', physicalPort=130, orientation=0}]\n"
    "Display Devices: size=1\n"
    '  DisplayDeviceInfo{"Built-in Screen": uniqueId="local:4630946650788219010", 1260 x 2800, '
    "modeId 3, supportedModes [{id=1, width=1260, height=2800}], state ON}\n"
    "  Logical Displays: size=1\n"
    "  Display 0:\n"
    "    mDisplayId=0\n"
    '    mBaseDisplayInfo=DisplayInfo{"Built-in Screen", displayId 0, displayGroupId 0, '
    "FLAG_SECURE, real 1260 x 2800, largest app 1260 x 2800, supportedModes [{id=1, width=1260, "
    'height=2800}], rotation 0, state ON, type INTERNAL, uniqueId "local:4630946650788219010", '
    "app 1260 x 2800, density 420}\n"
    '    mOverrideDisplayInfo=DisplayInfo{"Built-in Screen", displayId 0, displayGroupId 0, '
    "FLAG_SECURE, real 1080 x 2400, largest app 2400 x 2400, supportedModes [{id=1, width=1260, "
    'height=2800}], rotation 0, state ON, type INTERNAL, uniqueId "local:4630946650788219010", '
    "app 1080 x 2400, density 420}\n"
)

# a head unit: two physical displays and a virtual cluster display
IVI_GET_DISPLAYS = (
    "Displays:\n"
    'Display id 0: DisplayInfo{"Built-in Screen", displayId 0, real 1920 x 720, supportedModes '
    '[{id=1, width=1920, height=720}], type INTERNAL, uniqueId "local:4619827259835644672"}\n'
    'Display id 2: DisplayInfo{"Passenger", displayId 2, real 1280 x 720, supportedModes '
    '[{id=1}], type EXTERNAL, uniqueId "local:4619827551948147201"}\n'
    'Display id 5: DisplayInfo{"ClusterDisplay", displayId 5, real 1920 x 720, type VIRTUAL, '
    'uniqueId "virtual:com.android.car.cluster.home,1000,ClusterDisplay,0"}\n'
)


def test_the_physical_display_id_is_parsed_from_real_phone_output():
    for text in (PHONE_GET_DISPLAYS, PHONE_DUMPSYS_DISPLAY):
        assert ADBHandler.parse_display_info(text) == [
            {
                "id": 0,
                "size": "1080x2400",  # the override size, not the 1260x2800 panel
                "name": "Built-in Screen",
                "unique_id": "local:4630946650788219010",
                "physical_id": 4630946650788219010,
            }
        ]
    ivi = ADBHandler.parse_display_info(IVI_GET_DISPLAYS)
    assert [(d["id"], d.get("physical_id")) for d in ivi] == [
        (0, 4619827259835644672), (2, 4619827551948147201), (5, None)
    ]
    assert ivi[2]["unique_id"].startswith("virtual:")


def test_a_display_screenshot_captures_by_physical_id(fake_adb, tmp_path):
    """Issue: screencap -d took the logical id, which Android 10+ rejects for
    any display whose physical id differs (every IVI secondary display)."""
    fake_adb.add("get-displays", stdout=IVI_GET_DISPLAYS)
    fake_adb.add(
        lambda argv: "screencap" in argv and "4619827551948147201" in argv and "-p" in argv,
        stdout=b"\x89PNG\r\n\x1a\nimage",
    )
    handler = ADBHandler(ADBConfig(serial="ivi"))
    assert handler.capture_png(display_id=2)[:4] == b"\x89PNG"
    screencaps = [argv for argv in fake_adb.calls if "screencap" in argv]
    assert screencaps[0][screencaps[0].index("-d") + 1] == "4619827551948147201"
    # the id is looked up once per handler
    handler.capture_png(display_id=2)
    assert sum("get-displays" in " ".join(argv) for argv in fake_adb.calls) == 1
    # display 0 is the default display: no -d, no lookup
    fake_adb.add(lambda argv: "screencap" in argv and "-d" not in argv, stdout=b"\x89PNG\r\n\x1a\n0")
    assert handler.capture_png(display_id=0)[:4] == b"\x89PNG"


def test_tile_controls_send_keys_to_their_display_in_order(qapp):
    pytest.importorskip("PyQt5")
    from turboadb.gui.display_controls import DISPLAY_KEYS

    class Queue:
        """A dispatcher that only queues, so the order can be checked."""

        submitted = 0

        def __init__(self):
            self.items = []

        def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
            self.items.append((fn, on_done, on_fail))
            self.submitted += 1
            return True

        def stop(self):
            pass

    sent = []

    class Handler(_WallHandler):
        def keyevent(self, key, **kwargs):
            sent.append((key, kwargs))
            return key != "vol_mute"

    from turboadb.gui.display_wall import DisplayWall

    queue = Queue()
    wall = DisplayWall(Handler(), {"name": "ivi"}, automotive=True, dispatcher=queue)
    logs = []
    wall.log.connect(logs.append)
    try:
        wall.set_displays([{"id": 0, "size": "1920x720"}, {"id": 2, "size": "1280x720"}])
        centre, passenger = wall._tiles
        assert set(passenger.controls.buttons) == {k for _l, _i, k, _t in DISPLAY_KEYS} | {
            "screenshot"
        }
        passenger.controls.buttons["back"].click()
        centre.controls.buttons["home"].click()
        passenger.controls.buttons["vol_mute"].click()
        assert sent == [] and len(queue.items) == 3  # queued, nothing ran on the UI thread
        assert passenger.controls.pending == 2
        for fn, on_done, _on_fail in queue.items:
            on_done(fn())
        assert sent == [
            ("back", {"safe": True, "display_id": 2}),
            ("home", {"safe": True}),  # display 0: the default display, no -d
            ("vol_mute", {"safe": True, "display_id": 2}),
        ]
        assert passenger.controls.pending == 0
        assert any("display 2 Mute" in line and "rejected" in line for line in logs)
    finally:
        _close_wall(wall, qapp)


def test_tile_controls_drop_presses_a_slow_device_cannot_keep_up_with(qapp):
    pytest.importorskip("PyQt5")
    from turboadb.gui.display_controls import DisplayControls

    class Stuck(_NullDispatcher):
        pass

    controls = DisplayControls(_WallHandler(), 2, Stuck())
    notes = []
    controls.log.connect(notes.append)
    try:
        results = [controls.send_key("home") for _ in range(DisplayControls.MAX_PENDING + 3)]
        assert results.count(True) == DisplayControls.MAX_PENDING
        assert len(notes) == 1 and "not keeping up" in notes[0]
    finally:
        controls.close()


def test_keys_typed_on_a_tile_go_to_its_display(qapp):
    """Issue: the ADB keyboard route sent every key without -d, so typing on a
    cluster tile went to whichever display Android considered focused."""
    pytest.importorskip("PyQt5")
    from turboadb.gui.mirror_panel import MirrorPanel

    class Now(_NullDispatcher):
        def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
            self.submitted += 1
            result = fn()
            if on_done:
                on_done(result)
            return True

    sent = []

    class Handler(_WallHandler):
        def keyevent(self, code, **kwargs):
            sent.append(("key", code, kwargs))
            return True

        def input_text(self, text, **kwargs):
            sent.append(("text", text, kwargs))
            return True

    panel = MirrorPanel(Handler(), {"name": "ivi"}, automotive=True, dispatcher=Now())
    try:
        panel.set_fixed_display({"id": 5, "size": "1920x720"})
        panel._send_key("key", 66)
        panel._send_key("text", "hi")
        panel._key_batcher.flush()
        assert sent == [
            ("key", 66, {"safe": True, "display_id": 5}),
            ("text", "hi", {"safe": True, "display_id": 5}),
        ]
    finally:
        panel.close_panel()
        panel.close()


def test_the_wall_shares_one_video_budget_between_its_tiles(qapp, monkeypatch):
    pytest.importorskip("PyQt5")
    import turboadb.gui.mirror_panel as mp

    launched = []

    class Launch:
        def __init__(self, handler, opts, compat, log_path):
            launched.append(opts)
            self.done = self.fail = _Signal()

        def start(self):
            pass

        def isFinished(self):
            return True

        def isRunning(self):
            return False

    monkeypatch.setattr(mp, "_MirrorLaunchThread", Launch)
    monkeypatch.setattr(mp, "park_thread", lambda thread: None)
    wall = _wall([{"id": 0, "size": "1920x720"}, {"id": 2}, {"id": 3}])
    try:
        assert wall.tile_bit_rate() == "5M" and wall.tile_bit_rate(1) == "16M"
        assert wall.tile_bit_rate(8) == wall.TILE_MIN_BIT_RATE
        wall._tiles[1].panel.start()
        assert launched[-1].bit_rate == "5M" and launched[-1].display_id == 2
        # a lower bit rate chosen in Settings is never raised
        monkeypatch.setattr(
            mp.settings_mod, "load", lambda: {"scrcpy_bit_rate": "2M", "scrcpy_stay_awake": True}
        )
        wall._tiles[2].panel.start()
        assert launched[-1].bit_rate == "2M"
        # Device Control (no budget) keeps the full rate
        panel = mp.MirrorPanel(_WallHandler(), {"name": "ivi"}, dispatcher=_NullDispatcher())
        panel.start()
        assert launched[-1].bit_rate == "2M"
        panel._launch_pending = False
        panel.close_panel()
        panel.close()
        for tile in wall._tiles:
            tile.panel._launch_pending = False
    finally:
        _close_wall(wall, qapp)


def test_a_tile_never_builds_the_options_popover(qapp):
    """The Options popover is built on first use; a tile hides that button."""
    pytest.importorskip("PyQt5")
    wall = _wall([{"id": 0, "size": "1920x720"}, {"id": 2, "size": "1920x720"}])
    try:
        panels = [tile.panel for tile in wall._tiles]
        assert not any(p._options_built for p in panels)
        assert panels[0].btn_opts.menu().isEmpty()
        # asking for a popover control builds it, with the panel's current options
        panels[1]._set_option("compat", False)
        assert panels[1].act_compat.isChecked() is False and panels[1]._options_built
        panels[1].act_soft.setChecked(True)
        assert panels[1]._opt["soft"] is True
        assert panels[1].btn_ivi.isHidden()  # a locked tile offers no "All displays"
    finally:
        _close_wall(wall, qapp)


def test_the_screencap_setting_shows_the_screen_with_screencap_frames(qapp, monkeypatch):
    pytest.importorskip("PyQt5")
    from turboadb.gui import settings as settings_mod
    from turboadb.gui import screencap_view
    from turboadb.gui.mirror_panel import MirrorPanel

    import turboadb.gui.mirror_panel as mp

    started, launched = [], []
    monkeypatch.setattr(screencap_view.ScreencapView, "start", lambda self: started.append(self))

    class Launch:
        def __init__(self, handler, opts, compat, log_path):
            launched.append(opts)
            self.done = self.fail = _Signal()

        def start(self):
            pass

        def isFinished(self):
            return True

        def isRunning(self):
            return False

    monkeypatch.setattr(mp, "_MirrorLaunchThread", Launch)
    monkeypatch.setattr(mp, "park_thread", lambda thread: None)
    settings_mod.update({"screen_backend": "screencap"})
    panel = MirrorPanel(_WallHandler(), {"name": "ivi"}, automotive=True, dispatcher=_NullDispatcher())
    try:
        panel._got_displays(ADBHandler.parse_display_info(IVI_GET_DISPLAYS))
        panel._sync_display_selection(2)
        assert panel.screen_backend() == "screencap"
        # Record works without scrcpy: it records on the device
        assert panel.btn_record.isEnabled() and "screenrecord" in panel.btn_record.toolTip()
        panel.start()
        view = panel.screencap_view()
        assert isinstance(view, screencap_view.ScreencapView) and started == [view]
        assert view.parent() is panel.container and view.display["physical_id"] == 4619827551948147201
        assert view.input_display_id == 2 and launched == [] and not panel._launch_pending
        assert panel.is_active() and not panel.btn_stop.isHidden() and panel.btn_mirror.isHidden()
        assert panel.input_display_id() == 2
        # keys typed on the view go through the panel's batcher, to display 2
        assert view._key_sink == panel._send_key
        # the panel's own Options can switch this panel back to scrcpy
        panel.set_screen_backend("scrcpy")
        assert panel.screen_backend() == "scrcpy" and settings_mod.get("screen_backend") == "screencap"
        assert panel.screencap_view() is None and launched[-1].display_id == 2
        panel._launch_pending = False
        panel.stop()
        panel.set_screen_backend("screencap")
        panel.start()
        view = panel.screencap_view()
        panel.stop()
        assert panel.screencap_view() is None and not panel.is_active()
    finally:
        settings_mod.update({"screen_backend": "scrcpy"})
        panel.close_panel()
        panel.close()


# --------------------------------------------------------------------------- #
# review regressions: renderer switching, the wall's start chain, recording
# --------------------------------------------------------------------------- #
class _FakeLaunch:
    """_MirrorLaunchThread stand-in: records the options, never runs scrcpy."""

    launched = []

    def __init__(self, handler, opts, compat, log_path):
        _FakeLaunch.launched.append(opts)
        self.done = _Signal()
        self.fail = _Signal()

    def start(self):
        pass

    def isFinished(self):
        return True

    def isRunning(self):
        return False

    def cancel(self):
        pass


class _FakeStop:
    """_MirrorStopThread stand-in that finishes at once."""

    def __init__(self, session, window_title=None, window_handle=None):
        self.done = _Signal()

    def start(self):
        self.done.emit(True)

    def isRunning(self):
        return False

    def isFinished(self):
        return True


class _Session:
    running = True

    def read_log(self):
        return ""


@pytest.fixture
def no_scrcpy(monkeypatch):
    """Panels launch and stop no real scrcpy; screencap views start no worker."""
    pytest.importorskip("PyQt5")
    import turboadb.gui.mirror_panel as mp
    from turboadb.gui import screencap_view
    from turboadb.gui import settings as settings_mod

    _FakeLaunch.launched = []
    started = []
    monkeypatch.setattr(mp, "_MirrorLaunchThread", _FakeLaunch)
    monkeypatch.setattr(mp, "_MirrorStopThread", _FakeStop)
    monkeypatch.setattr(mp, "park_thread", lambda thread: None)
    monkeypatch.setattr(screencap_view.ScreencapView, "start", lambda self: started.append(self))
    settings_mod.update({"screen_backend": "scrcpy"})
    yield started
    settings_mod.update({"screen_backend": "scrcpy"})


def test_switching_to_screencap_with_embedding_off_never_relaunches_scrcpy(qapp, no_scrcpy):
    """Issue: the resolved "show inside this tab" option (off) was queued as an
    explicit separate-window request, so the restart launched scrcpy again."""
    from turboadb.gui.mirror_panel import MirrorPanel

    panel = MirrorPanel(_WallHandler(), {"name": "phone"}, dispatcher=_NullDispatcher())
    try:
        panel._set_option("embed", False)
        panel.start()
        assert len(_FakeLaunch.launched) == 1 and panel._launch_spec["embed"] is None
        panel._on_mirror_launched(_Session())  # a separate scrcpy window is up
        panel.cmb_renderer.setCurrentIndex(panel.cmb_renderer.findData("screencap"))
        for _ in range(3):
            qapp.processEvents()  # the queued start runs once scrcpy has stopped
        assert len(_FakeLaunch.launched) == 1  # scrcpy was not launched again
        assert panel.screencap_view() is not None and no_scrcpy == [panel.screencap_view()]
        assert panel._opt["embed"] is False  # the user's option is untouched
        # an explicit separate window stays scrcpy whatever the renderer
        panel.stop()
        assert not panel._uses_screencap(False, None, None)
        assert panel._uses_screencap(None, None, None)
    finally:
        panel.close_panel()
        panel.close()


def test_a_failed_screencap_tile_does_not_hold_up_the_next_display(qapp, monkeypatch):
    """Issue: the wall advanced only on screen_ready, so a tile whose screencap
    failed at once kept the next display waiting START_TIMEOUT_MS (~27 s)."""
    pytest.importorskip("PyQt5")
    import gzip
    import io
    import threading
    import time

    from turboadb.gui import settings as settings_mod
    from turboadb.gui.display_wall import DisplayWall

    marker = ADBHandler.SCREENCAP_ERROR_MARKER
    failing = gzip.compress(b"") * 3 + marker + b" Display Id '5' is not valid.\n"
    opened = []

    class Proc:
        """adb exec-out stand-in: *data* then end of stream, or nothing until killed."""

        def __init__(self, data):
            self.chunks = [data] if data else []
            self.ends = bool(data)
            self.gone = threading.Event()
            self.stderr = io.BytesIO(b"")
            self.stdout = self
            self.returncode = None

        def read(self, _size):
            if self.chunks:
                return self.chunks.pop(0)
            if not self.ends:
                self.gone.wait(5)
            return b""

        def close(self):
            pass

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9
            self.gone.set()

        def wait(self, timeout=None):
            return self.returncode

    class Handler(_WallHandler):
        def open_screencap_stream(self, **kwargs):
            opened.append((time.monotonic(), kwargs["capture_id"]))
            return Proc(failing if kwargs["capture_id"] == 5 else b"")

    monkeypatch.setattr(DisplayWall, "START_GAP_MS", 50)
    settings_mod.update({"screen_backend": "screencap"})
    wall = DisplayWall(Handler(), {"name": "ivi"}, automotive=True, dispatcher=_NullDispatcher())
    logs = []
    wall.log.connect(logs.append)
    try:
        wall._auto_started = True
        wall.resize(900, 700)
        wall.show()
        wall.set_displays(ADBHandler.parse_display_info(IVI_GET_DISPLAYS)[2:] + [
            {"id": 6, "size": "1280x720", "unique_id": "local:66", "physical_id": 66}
        ])
        started = time.monotonic()
        wall.start_all()
        deadline = started + 5
        while len(opened) < 2 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert [capture for _t, capture in opened] == [5, 66]
        assert opened[1][0] - started < 3  # not START_TIMEOUT_MS later
        assert any("virtual display" in line for line in logs)
    finally:
        settings_mod.update({"screen_backend": "scrcpy"})
        _close_wall(wall, qapp)


def test_a_retrying_scrcpy_tile_still_holds_the_start_chain(qapp):
    """A scrcpy tile is briefly idle between a failed attempt and its retry;
    that pause must not start the next display."""
    pytest.importorskip("PyQt5")
    wall = _wall([{"id": 0, "size": "1920x720"}, {"id": 2, "size": "1920x720"}])
    try:
        tile = wall._tiles[0]
        wall._waiting = tile
        tile.panel._retry_pending = True
        wall._advance_past_idle_tile()
        assert wall._waiting is tile and not wall._next_timer.isActive()
        tile.panel._retry_pending = False
        wall._advance_past_idle_tile()
        assert wall._waiting is None and wall._next_timer.interval() == wall.START_GAP_MS
        wall._next_timer.stop()
    finally:
        _close_wall(wall, qapp)


def test_record_follows_the_renderer_and_is_enabled_again_for_scrcpy(qapp, no_scrcpy):
    """Issue: Record was disabled in screencap mode and never re-enabled after
    switching back to scrcpy."""
    from turboadb.gui import settings as settings_mod
    from turboadb.gui.mirror_panel import MirrorPanel

    panel = MirrorPanel(_WallHandler(), {"name": "phone"}, dispatcher=_NullDispatcher())
    try:
        panel.set_screen_backend("screencap")
        assert panel.btn_record.isEnabled() and "screenrecord" in panel.btn_record.toolTip()
        panel.set_screen_backend("scrcpy")
        assert panel.btn_record.isEnabled() and "screenrecord" not in panel.btn_record.toolTip()
        panel._record_finalizing = True
        panel._refresh_buttons()
        assert not panel.btn_record.isEnabled()
        panel._record_finalizing = False
        # Settings changed while the panel was hidden: showing it re-syncs
        panel.set_screen_backend(None)
        settings_mod.update({"screen_backend": "screencap"})
        panel.btn_record.setToolTip("stale")
        panel.show()
        assert "screenrecord" in panel.btn_record.toolTip() and panel.btn_record.isEnabled()
        panel.btn_record.setToolTip("stale")
        panel.refresh_theme()
        assert "screenrecord" in panel.btn_record.toolTip()
    finally:
        panel.close_panel()
        panel.close()


def test_a_settings_renderer_change_moves_running_screens_that_follow_it(qapp, no_scrcpy):
    """Issue: Settings → Screen renderer did not reach open screens."""
    from turboadb.gui import settings as settings_mod
    from turboadb.gui.mirror_panel import MirrorPanel
    from PyQt5.QtWidgets import QWidget

    root = QWidget()
    follows = MirrorPanel(_WallHandler(), {"name": "a"}, dispatcher=_NullDispatcher(), parent=root)
    own = MirrorPanel(_WallHandler(), {"name": "b"}, dispatcher=_NullDispatcher(), parent=root)
    idle = MirrorPanel(_WallHandler(), {"name": "c"}, dispatcher=_NullDispatcher(), parent=root)
    try:
        own.set_screen_backend("scrcpy")
        for panel in (follows, own):
            panel.start()
            panel._on_mirror_launched(_Session())
        assert len(_FakeLaunch.launched) == 2
        settings_mod.update({"screen_backend": "screencap"})
        MirrorPanel.apply_default_backend(root, "scrcpy")
        for _ in range(3):
            qapp.processEvents()
        assert follows.screencap_view() is not None  # moved to screencap frames
        assert own.screencap_view() is None and own._scrcpy is not None  # its own choice
        assert idle.screencap_view() is None and not idle.is_active()  # nothing to restart
        assert "screenrecord" in idle.btn_record.toolTip()  # but its tooltips follow
        # and back: the screencap screen returns to scrcpy
        settings_mod.update({"screen_backend": "scrcpy"})
        follows.default_backend_changed("screencap")
        assert follows.screencap_view() is None and len(_FakeLaunch.launched) == 3
        for panel in (follows, own):
            panel._launch_pending = False
            panel._scrcpy = None
    finally:
        for panel in (follows, own, idle):
            panel.close_panel()
        root.close()


def test_screencap_mode_records_on_the_device_by_logical_display(qapp, no_scrcpy, monkeypatch):
    """Record in screencap mode uses device-side screenrecord (no scrcpy)."""
    import turboadb.gui.mirror_panel as mp

    records = []

    class Recorder:
        def __init__(self, handler, path, stop_event, bit_rate=None, size=None, display_id=None):
            records.append((path, display_id, stop_event))
            self.done, self.fail, self.part = _Signal(), _Signal(), _Signal()

        def start(self):
            pass

        def isFinished(self):
            return False

        def isRunning(self):
            return True

    monkeypatch.setattr(mp, "_RecordThread", Recorder)
    monkeypatch.setattr(mp.QFileDialog, "getSaveFileName", lambda *a, **k: ("clip.mp4", ""))
    warnings = []
    monkeypatch.setattr(mp.QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    panel = mp.MirrorPanel(_WallHandler(), {"name": "ivi"}, automotive=True, dispatcher=_NullDispatcher())
    try:
        panel._got_displays(ADBHandler.parse_display_info(IVI_GET_DISPLAYS))
        panel.set_screen_backend("screencap")
        panel._sync_display_selection(2)
        panel.start()
        panel._toggle_record()
        (path, display, stop_event), = records
        assert path == "clip.mp4" and display == 2 and panel._rec_mode == "device"
        assert _FakeLaunch.launched == []  # no scrcpy anywhere
        panel._toggle_record()  # Stop: the worker pulls and saves the file
        assert stop_event.is_set() and panel._record_finalizing
        panel._record_finished([])  # (the fake saved nothing)
        # a virtual display cannot be recorded by screenrecord: said clearly
        panel._sync_display_selection(5)
        panel._toggle_record()
        assert len(records) == 1 and not panel._recording
        assert warnings and "virtual display" in warnings[-1]
    finally:
        panel.close_panel()
        panel.close()


def test_screen_record_records_a_display_by_its_physical_id(monkeypatch, tmp_path):
    """Android 10+ screenrecord --display-id takes the physical id; older
    builds that reject it get the logical id."""
    import turboadb.core as core
    from turboadb.results import CommandResult

    handler = ADBHandler(ADBConfig(serial="ivi"))
    handler._adb = "adb"

    def run(args, **kwargs):
        text = IVI_GET_DISPLAYS if "get-displays" in args else ""
        return CommandResult(" ".join(args), 0, text, "", 0.0)

    recorded = []

    class Proc:
        stdout = None

        def __init__(self, cmd):
            recorded.append(cmd[-1])
            self.code = 1 if "--display-id 4619827551948147201" in cmd[-1] else 0

        def poll(self):
            return self.code

        def wait(self, timeout=None):
            return self.code

        def terminate(self):
            pass

        def kill(self):
            pass

    handler._run = run
    out = tmp_path / "clip.mp4"
    handler._transfer = lambda *a: out.write_bytes(b"video")
    monkeypatch.setattr(core.subprocess, "Popen", lambda cmd, **kw: Proc(cmd))
    assert handler.screen_record(str(out), time_limit=5, display_id=2) == str(out)
    assert "--display-id 4619827551948147201" in recorded[0]  # the physical id first
    assert "--display-id 2 " in recorded[1]  # then the logical id this build takes
    recorded.clear()
    handler.screen_record(str(out), time_limit=5)  # the default display: no --display-id
    assert "--display-id" not in recorded[0]
    with pytest.raises(ValueError):
        handler.screen_record(str(out), display_id="1; reboot")
