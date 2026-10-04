"""Keys for display 0 of a device with several displays name their display.

``input keyevent`` without ``-d`` goes to whichever display Android has
focused.  On a head unit with a centre and a passenger screen, Home or Back
pressed for the centre screen (on its tile, or in Device Control while it
shows the default display) went to the passenger screen once someone had
touched that one.  Display 0 is now named (``-d 0``) on a device with several
displays.  A device with one display keeps the plain command, and so does one
whose ``input`` has no ``-d`` (before Android 10) or that has not said which
Android it runs.

Headless; no device or adb is used.
"""

import pytest

pytest.importorskip("PyQt5")

from turboadb import ADBConfig, ADBHandler  # noqa: E402
from turboadb.results import OperationResult  # noqa: E402

from test_adb_processes import _Device, _close, _connect, _probe_output, _pump, _tab  # noqa: E402

CENTRE = {"id": 0, "size": "1920x720", "name": "Centre"}
PASSENGER = {"id": 3, "size": "1280x720", "name": "Passenger"}
TWO_DISPLAYS = (
    'DisplayInfo{"Centre, displayId 0", real 1920 x 720, x}\n'
    'DisplayInfo{"Passenger, displayId 3", real 1280 x 720, x}'
)


def test_display_zero_is_named_only_among_several_displays_on_android_10():
    from turboadb.gui.device_commands import input_display

    assert input_display(0, 2, 34) == 0
    assert input_display(None, 3, 29) == 0  # no display chosen is display 0
    assert input_display(0, 1, 34) is None  # the only display: the plain command
    assert input_display(0, 0, 34) is None  # no display list yet
    assert input_display(0, 2, 28) is None  # Android 9: `input` has no -d
    assert input_display(0, 2, None) is None  # Android version not known (yet)
    # any other display was always named, whatever else is known
    assert input_display(3, 2, 34) == 3
    assert input_display(3, 1, None) == 3
    assert input_display("2", 2, 28) == 2


def test_the_engine_sends_display_zero_when_it_is_named(fake_adb):
    handler = ADBHandler(ADBConfig(serial="ivi"))
    assert handler.keyevent("home", display_id=0) is True
    assert fake_adb.argv_after_adb() == ["-s", "ivi", "shell", "input", "-d", "0", "keyevent", "3"]
    handler.keyevent("home")
    assert fake_adb.argv_after_adb() == ["-s", "ivi", "shell", "input", "keyevent", "3"]


# --------------------------------------------------------------------------- #
# the display tiles
# --------------------------------------------------------------------------- #
class _WallHandler:
    serial = "ivi"
    config = None

    def __init__(self):
        self.keys = []

    def keyevent(self, key, **kwargs):
        self.keys.append((key, kwargs))
        return True


class _RunNow:
    """Runs each submitted command at once and reports its result."""

    submitted = 0

    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        self.submitted += 1
        result = fn()
        if on_done:
            on_done(result)
        return True

    def stop(self):
        pass


def _wall(displays, **kwargs):
    from turboadb.gui.display_wall import DisplayWall

    handler = _WallHandler()
    wall = DisplayWall(handler, {"name": "ivi"}, automotive=True, dispatcher=_RunNow(), **kwargs)
    wall.set_displays([dict(d) for d in displays])
    return wall, handler


def _close_wall(qapp, wall):
    wall.close_panel()
    wall.close()
    wall.deleteLater()
    for _ in range(3):
        qapp.processEvents()


def test_the_centre_tiles_keys_go_to_display_zero_not_the_focused_display(qapp):
    wall, handler = _wall([CENTRE, PASSENGER], sdk=34)
    try:
        centre, passenger = wall._tiles
        passenger.controls.buttons["back"].click()
        centre.controls.buttons["home"].click()
        centre.controls.buttons["back"].click()
        assert handler.keys == [
            ("back", {"safe": True, "display_id": 3}),
            ("home", {"safe": True, "display_id": 0}),
            ("back", {"safe": True, "display_id": 0}),
        ]
    finally:
        _close_wall(qapp, wall)


def test_a_tile_of_the_only_display_keeps_the_plain_key(qapp):
    wall, handler = _wall([CENTRE], sdk=34)
    try:
        wall._tiles[0].controls.buttons["home"].click()
        assert handler.keys == [("home", {"safe": True})]
        # a rescan finds a second display: display 0's keys name it from now on
        wall.set_displays([dict(CENTRE), dict(PASSENGER)])
        wall._tiles[0].controls.buttons["home"].click()
        assert handler.keys[-1] == ("home", {"safe": True, "display_id": 0})
        # ...and stop once it is gone again
        wall.set_displays([dict(CENTRE)])
        wall._tiles[0].controls.buttons["home"].click()
        assert handler.keys[-1] == ("home", {"safe": True})
    finally:
        _close_wall(qapp, wall)


@pytest.mark.parametrize("sdk", [None, 28], ids=["android-unknown", "android-9"])
def test_tiles_keep_the_plain_key_where_input_takes_no_display(qapp, sdk):
    wall, handler = _wall([CENTRE, PASSENGER], sdk=sdk)
    try:
        wall._tiles[0].controls.buttons["home"].click()
        assert handler.keys == [("home", {"safe": True})]
        wall.set_sdk(33)  # the device profile arrived
        wall._tiles[0].controls.buttons["home"].click()
        assert handler.keys[-1] == ("home", {"safe": True, "display_id": 0})
    finally:
        _close_wall(qapp, wall)


# --------------------------------------------------------------------------- #
# Device Control, in a device tab
# --------------------------------------------------------------------------- #
class _KeyDevice(_Device):
    """A fake device that also takes keys and typed text."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.keys = []

    def keyevent(self, key, **kwargs):
        self.keys.append(("key", key, kwargs))
        return OperationResult(True, "keyevent", value=True)

    def input_text(self, text, **kwargs):
        self.keys.append(("text", text, kwargs))
        return OperationResult(True, "input_text", value=True)


def _control_tab(qapp, **probe):
    """A connected tab whose probe answered with *probe* (see _probe_output)."""
    from turboadb.gui.device_tab import _DeviceProbe

    device = _KeyDevice(probe_output=_probe_output(**probe))
    tab = _tab(qapp)
    _connect(tab, device)
    tab._on_probe_details(device, _DeviceProbe(device).run())
    return tab, device


def _press(tab, name):
    from PyQt5.QtWidgets import QToolButton

    button = next(b for b in tab.controls.findChildren(QToolButton) if b.accessibleName() == name)
    button.click()


def test_device_control_names_the_default_display_of_a_multi_display_device(qapp):
    tab, device = _control_tab(qapp, characteristics="automotive", displays=TWO_DISPLAYS)
    try:
        assert [d["id"] for d in tab.mirror_tab._displays] == [0, 3]
        _press(tab, "Home")
        tab.controls.text.setText("hi")
        tab.controls._send_text()
        assert _pump(qapp, lambda: len(device.keys) == 2)
        assert device.keys == [
            ("key", "home", {"safe": True, "display_id": 0}),
            ("text", "hi", {"safe": True, "display_id": 0}),
        ]
        # the passenger display chosen in the picker: its id, as before
        tab.mirror_tab._sync_display_selection(3)
        _press(tab, "Back")
        assert _pump(qapp, lambda: len(device.keys) == 3)
        assert device.keys[-1] == ("key", "back", {"safe": True, "display_id": 3})
    finally:
        _close(qapp, tab)


def test_device_control_on_a_phone_keeps_the_plain_key(qapp):
    tab, device = _control_tab(qapp)  # one display
    try:
        assert [d["id"] for d in tab.mirror_tab._displays] == [0]
        _press(tab, "Home")
        assert _pump(qapp, lambda: len(device.keys) == 1)
        assert device.keys == [("key", "home", {"safe": True})]
    finally:
        _close(qapp, tab)


# --------------------------------------------------------------------------- #
# keys typed on the screen itself (the keyboard's ADB route)
# --------------------------------------------------------------------------- #
class _TypingHandler(_WallHandler):
    """Also takes typed text and several keys in one call."""

    def input_text(self, text, **kwargs):
        self.keys.append((text, kwargs))
        return True

    def keyevents(self, keys, **kwargs):
        self.keys.append((list(keys), kwargs))
        return True


def _typing_wall(displays, **kwargs):
    from turboadb.gui.display_wall import DisplayWall

    handler = _TypingHandler()
    wall = DisplayWall(handler, {"name": "ivi"}, automotive=True, dispatcher=_RunNow(), **kwargs)
    wall.set_displays([dict(d) for d in displays])
    return wall, handler


def _type(panel, text):
    """Type *text* on the screen of *panel*, and let its batch go out."""
    panel._send_key("text", text)
    panel._key_batcher.flush()


def test_keys_typed_on_the_centre_tile_go_to_display_zero(qapp):
    wall, handler = _typing_wall([CENTRE, PASSENGER], sdk=34)
    try:
        centre, passenger = wall._tiles
        passenger.panel._send_key("key", 4)  # Back, typed on the passenger screen
        centre.panel._send_key("key", 4)  # ...then on the centre screen
        _type(centre.panel, "hi")
        assert handler.keys == [
            (4, {"safe": True, "display_id": 3}),
            (4, {"safe": True, "display_id": 0}),
            ("hi", {"safe": True, "display_id": 0}),
        ]
    finally:
        _close_wall(qapp, wall)


def test_keys_typed_on_a_tile_follow_the_number_of_displays(qapp):
    wall, handler = _typing_wall([CENTRE], sdk=34)
    try:
        panel = wall._tiles[0].panel
        _type(panel, "a")
        assert handler.keys == [("a", {"safe": True})]  # the only display
        wall.set_displays([dict(CENTRE), dict(PASSENGER)])
        _type(panel, "b")
        assert handler.keys[-1] == ("b", {"safe": True, "display_id": 0})
        wall.set_displays([dict(CENTRE)])
        _type(panel, "c")
        assert handler.keys[-1] == ("c", {"safe": True})
    finally:
        _close_wall(qapp, wall)


@pytest.mark.parametrize("sdk", [None, 28], ids=["android-unknown", "android-9"])
def test_keys_typed_on_a_tile_stay_plain_where_input_takes_no_display(qapp, sdk):
    wall, handler = _typing_wall([CENTRE, PASSENGER], sdk=sdk)
    try:
        panel = wall._tiles[0].panel
        panel._send_key("key", 66)
        assert handler.keys == [(66, {"safe": True})]
        wall.set_sdk(33)  # the device profile arrived
        panel._send_key("key", 67)
        assert handler.keys[-1] == (67, {"safe": True, "display_id": 0})
    finally:
        _close_wall(qapp, wall)


def test_keys_typed_on_device_controls_screen_name_the_default_display(qapp):
    tab, device = _control_tab(qapp, characteristics="automotive", displays=TWO_DISPLAYS)
    try:
        mirror = tab.mirror_tab
        mirror._send_key("key", 66)  # Enter, typed on the screen
        assert _pump(qapp, lambda: len(device.keys) == 1)
        _type(mirror, "hi")
        assert _pump(qapp, lambda: len(device.keys) == 2)
        assert device.keys == [
            ("key", 66, {"safe": True, "display_id": 0}),
            ("text", "hi", {"safe": True, "display_id": 0}),
        ]
        mirror._sync_display_selection(3)  # the passenger display: its id, as before
        mirror._send_key("key", 4)
        assert _pump(qapp, lambda: len(device.keys) == 3)
        assert device.keys[-1] == ("key", 4, {"safe": True, "display_id": 3})
    finally:
        _close(qapp, tab)


def test_keys_typed_on_a_phones_screen_stay_plain(qapp):
    tab, device = _control_tab(qapp)  # one display
    try:
        tab.mirror_tab._send_key("key", 66)
        assert _pump(qapp, lambda: len(device.keys) == 1)
        assert device.keys == [("key", 66, {"safe": True})]
    finally:
        _close(qapp, tab)
