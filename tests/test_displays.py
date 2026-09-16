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
    assert ADBHandler.parse_display_info(ANDROID_10_DUMPSYS) == [
        {"id": 0, "size": "1280x720", "name": "Built-in Screen"},
        {"id": 1, "size": "1920x1080", "name": "HDMI Screen"},
    ]
    assert ADBHandler.parse_display_info(ANDROID_9_DUMPSYS) == [
        {"id": 0, "size": "1080x1920", "name": "Built-in Screen"}
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
        assert cp._ncols == 5 and cp.minimumWidth() == 1
        assert cp.content_height(1800, strip=True) < cp.content_height(340, strip=False)
        cp.set_strip(False)
        assert cp._ncols == 3 and cp.minimumWidth() == 340
    finally:
        cp.close_panel()
        cp.close()
