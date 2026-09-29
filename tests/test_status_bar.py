"""The status bar in colour.

It was one grey line of text ("● Connected — …", the dot just a character)
and three identical grey pills. Now the state and each message carry the
colour and icon of their kind (green done, amber warning, red error, blue
information), and the ADB, devices and version chips are coloured by their
state, a problem as a solid red pill, and open what belongs to them."""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QThread, pyqtSignal  # noqa: E402

import turboadb.gui.main_window as mw  # noqa: E402
from turboadb.devices import Device  # noqa: E402
from turboadb.gui import theme  # noqa: E402
from turboadb.gui.status_bar import StatusBar, StatusChip, tone_color  # noqa: E402


class _Inert(QThread):
    result = pyqtSignal(list)
    failed = pyqtSignal(str)
    ready = pyqtSignal(bool, str)
    note = pyqtSignal(str)

    def __init__(self, *a, **k):
        super().__init__(k.get("parent"))

    def stop(self):
        pass

    def run(self):
        pass


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(mw, "_DeviceTracker", _Inert)
    monkeypatch.setattr(mw, "_DevicesPoll", _Inert)
    monkeypatch.setattr(mw, "_AdbInitThread", _Inert)
    for name in ("_check_tools", "_ensure_shortcuts", "_quiet_update_check"):
        monkeypatch.setattr(mw.MainWindow, name, lambda self: None)
    win = mw.MainWindow()
    yield win
    win.close()
    win.deleteLater()


# --------------------------------------------------------------------------- #
# the bar on its own
# --------------------------------------------------------------------------- #
def test_a_message_shows_its_kind_and_the_summary_comes_back(qapp):
    bar = StatusBar()
    seen = []
    bar.messageChanged.connect(seen.append)
    bar.set_summary("vivo V2318", "ok", "smartphone", label="Connected")
    assert (bar.message.text(), bar.message.tone(), bar.message.label()) == ("vivo V2318", "ok", "Connected")
    bar.show_message("Could not pull cat.png", "error", timeout=5000)
    assert (bar.message.text(), bar.message.tone(), bar.message.label()) == (
        "Could not pull cat.png", "error", "Error")
    assert bar.currentMessage() == "Could not pull cat.png"
    bar.set_summary("vivo V2318", "ok", "smartphone", label="Connected")  # a message stays in front
    assert bar.message.tone() == "error"
    bar._timer.timeout.emit()  # its time is up
    assert (bar.message.text(), bar.message.label(), bar.currentMessage()) == ("vivo V2318", "Connected", "")
    assert seen == ["Could not pull cat.png", ""]


def test_qt_style_messages_still_work_and_pick_their_colour_from_the_prefix(qapp):
    bar = StatusBar()
    bar.showMessage("Warning: the device refused the change")
    assert (bar.message.text(), bar.message.tone(), bar.message.label()) == (
        "the device refused the change", "warn", "Warning")
    bar.showMessage("Error: adb is gone", 1000)
    assert (bar.message.text(), bar.message.tone()) == ("adb is gone", "error")
    bar.showMessage("Copied 3 files")
    assert (bar.message.tone(), bar.message.label()) == ("info", "")
    bar.clearMessage()
    assert bar.currentMessage() == ""


@pytest.mark.parametrize("name", ["dark", "light", "ocean-light", "contrast-dark"])
def test_chips_paint_in_every_tone_and_theme(qapp, name):
    theme.apply_to_app(qapp, name)
    chip = StatusChip("ADB ready", "ok")
    chip.set_clickable()
    for tone in ("ok", "warn", "error", "info", "idle", "accent"):
        chip.set_state(f"state {tone}", tone)
        chip.resize(chip.sizeHint())
        image = chip.grab().toImage()
        assert image.width() == chip.sizeHint().width() and not image.isNull()
    assert tone_color("error").name() == theme.palette(name)["danger_text"]
    chip.set_state("ADB starting…", "warn", busy=True)
    assert chip.busy() and chip._pulse.state() == chip._pulse.Running
    chip.set_state("ADB ready", "ok")
    assert not chip.busy() and chip._pulse.state() == chip._pulse.Stopped
    theme.apply_to_app(qapp, "dark")


def test_a_click_on_a_clickable_chip_is_reported(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest

    chip = StatusChip("1 device online", "ok")
    clicks = []
    chip.clicked.connect(lambda: clicks.append(1))
    chip.resize(chip.sizeHint())
    QTest.mouseClick(chip, Qt.LeftButton)
    assert clicks == []  # not clickable yet
    chip.set_clickable()
    QTest.mouseClick(chip, Qt.LeftButton)
    assert clicks == [1]


# --------------------------------------------------------------------------- #
# the main window's states
# --------------------------------------------------------------------------- #
def test_log_messages_colour_the_status_bar(window):
    bar = window.statusBar()
    assert isinstance(bar, StatusBar)
    window._log("[ERROR] Could not pull cat.png: remote object does not exist")
    assert (bar.message.tone(), bar.message.label()) == ("error", "Error")
    assert bar.message.text().startswith("Could not pull cat.png")
    window._log("[WARNING] The device refused the change")
    assert bar.message.tone() == "warn"
    window._log("[OK] Saved cat.png to the device")
    assert (bar.message.tone(), bar.message.label()) == ("ok", "")
    window._log("[INFO] Copying cat.png…")
    assert bar.message.tone() == "info"


def test_the_summary_says_what_is_connected(window):
    bar = window.statusBar()
    window._live_devices = []
    window._update_status()
    text, tone, _glyph, label = bar.summary()
    assert (tone, label) == ("idle", "No device connected")
    window._live_devices = [Device(serial="R58M123", state="device", model="Pixel")]
    window._update_status()
    text, tone, _glyph, label = bar.summary()
    assert (tone, label) == ("info", "1 device attached") and "double-click" in text


def test_the_chips_follow_the_adb_server_devices_and_version(window):
    window._set_adb_indicator("starting…")
    assert (window._status_adb.text(), window._status_adb.tone(), window._status_adb.busy()) == (
        "ADB starting…", "warn", True)
    window._set_adb_indicator("restart failed")
    assert (window._status_adb.tone(), window._status_adb.busy()) == ("error", False)
    window._set_adb_indicator("ready")
    assert window._status_adb.tone() == "ok"
    online = Device(serial="R58M123", state="device", model="Pixel")
    pending = Device(serial="10.0.0.5:5555", state="unauthorized")
    window._set_device_indicator([online])
    assert (window._status_devices.text(), window._status_devices.tone()) == ("1 device online", "ok")
    window._set_device_indicator([online, pending])
    assert (window._status_devices.text(), window._status_devices.tone()) == ("1 of 2 online", "warn")
    assert "10.0.0.5:5555: unauthorized" in window._status_devices.toolTip()
    window._set_device_indicator([])
    assert (window._status_devices.text(), window._status_devices.tone()) == ("No devices", "idle")
    assert window._status_version.tone() == "idle"
    window._on_quiet_update("9.9.9")
    assert window._status_version.tone() == "accent" and "9.9.9 available" in window._status_version.text()
