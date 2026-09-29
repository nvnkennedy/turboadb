"""Every display in the (IVI) Displays tab starts stopped.

A head unit's displays are started only when they are wanted: showing or
hiding the tab, Device Control's Options → All displays, a rescan (the tab's
own, Device Control's or the connect probe's) and a newly found display never
start a screen. Only a display's own Start or Start all does, and a display
the user stopped stays stopped. Headless (offscreen Qt); no device, adb or
scrcpy is used.
"""

import time

import pytest

pytest.importorskip("PyQt5")

D3 = [
    {"id": 0, "size": "1920x720", "name": "Centre"},
    {"id": 2, "size": "1920x720", "name": "Cluster"},
    {"id": 3, "size": "1280x720", "name": "Passenger"},
]
REAR = {"id": 4, "size": "800x480", "name": "Rear"}


def _pump(qapp, ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


def _pump_until(qapp, until, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return until()


class _Handler:
    serial = "ivi"
    config = None


class _NullDispatcher:
    """Accepts device commands and runs none."""

    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        return True

    def stop(self):
        pass


class _Starts(list):
    """The display id of every MirrorPanel.start, in order (None: a panel
    that is not a display tile, i.e. Device Control's own screen)."""


@pytest.fixture
def starts(monkeypatch):
    """Record every screen start instead of running one.

    A start shows "video" at once (like the real one, which turns the panel
    active straight away); the test says when a screen is up by emitting its
    panel's screen_ready. No scrcpy launch or screencap stream may happen.
    """
    import turboadb.gui.mirror_panel as mp
    from turboadb.gui import screencap_view
    from turboadb.gui.display_wall import DisplayWall

    started = _Starts()

    def start(self, *_args, **_kwargs):
        started.append(int(self._displays[0]["id"]) if self._fixed_display else None)
        self._fake_on = True
        self.video_active_changed.emit(True)

    def stop(self):
        if getattr(self, "_fake_on", False):
            self._fake_on = False
            self.video_active_changed.emit(False)

    def no_real_screen(*_args, **_kwargs):
        raise AssertionError("no scrcpy launch or screencap stream may start here")

    monkeypatch.setattr(mp.MirrorPanel, "start", start)
    monkeypatch.setattr(mp.MirrorPanel, "stop", stop)
    monkeypatch.setattr(
        mp.MirrorPanel, "is_active", lambda self: bool(getattr(self, "_fake_on", False))
    )
    monkeypatch.setattr(mp, "_MirrorLaunchThread", no_real_screen)
    monkeypatch.setattr(screencap_view.ScreencapView, "start", no_real_screen)
    monkeypatch.setattr(DisplayWall, "START_GAP_MS", 20)
    return started


def _shown_wall(qapp, displays=D3):
    from PyQt5.QtCore import Qt
    from turboadb.gui.display_wall import DisplayWall

    wall = DisplayWall(_Handler(), {"name": "ivi"}, automotive=True, dispatcher=_NullDispatcher())
    wall.setAttribute(Qt.WA_DontShowOnScreen, True)
    wall.resize(1400, 800)
    if displays:
        wall.set_displays([dict(d) for d in displays])
    wall.show()
    _pump(qapp, 300)  # the old auto-start began 150 ms after the first show
    return wall


def _close(qapp, wall):
    wall.close_panel()
    wall.hide()
    wall.close()
    wall.deleteLater()
    for _ in range(3):
        qapp.processEvents()


def test_showing_the_displays_tab_starts_no_display(qapp, starts):
    wall = _shown_wall(qapp)
    try:
        assert starts == []
        assert wall.summary.text() == "3 displays — all stopped"
        assert wall.btn_start_all.isEnabled() and not wall.btn_stop_all.isEnabled()
        assert "one after another" in wall.btn_start_all.toolTip()
        for tile in wall._tiles:
            # each display offers its own Start: on its idle frame and toolbar
            assert tile.panel.empty_state.isVisible()
            assert tile.panel.empty_state.btn_start.isVisible()
            assert tile.panel.btn_mirror.isVisible()
        wall.hide()  # another tab…
        _pump(qapp, 50)
        wall.show()  # …and back
        _pump(qapp, 300)
        assert starts == []
    finally:
        _close(qapp, wall)


def test_displays_found_while_the_tab_is_open_arrive_stopped(qapp, starts):
    """Issue: a scan that answered while the tab was on screen (the tab opened
    before the first scan, or Rescan on an empty tab) started every display."""
    wall = _shown_wall(qapp, displays=None)
    try:
        assert wall.summary.text() == "Looking for displays…"
        wall.set_displays([dict(d) for d in D3])
        _pump(qapp, 300)
        assert starts == []
        assert [tile.display_id for tile in wall._tiles] == [0, 2, 3]
        assert wall.summary.text() == "3 displays — all stopped"
    finally:
        _close(qapp, wall)


def test_a_display_the_user_stopped_stays_stopped_across_rescans(qapp, starts):
    """Issue: once anything had started, every scan (Rescan, Device Control's
    Refresh, the connect probe) restarted displays stopped with their own Stop
    and started newly found ones, even while the tab was hidden."""
    wall = _shown_wall(qapp)
    try:
        cluster = wall._tiles[1]
        cluster.panel.btn_mirror.click()  # the tile's own Start
        assert starts == [2]
        assert wall.summary.text() == "3 displays — 1 running"
        cluster.panel.stop()  # …and its own Stop
        wall.set_displays([dict(d) for d in D3] + [dict(REAR)])  # a rescan finds one more
        _pump(qapp, 300)
        assert starts == [2]  # nothing restarted, and the new display is stopped
        assert [tile.display_id for tile in wall._tiles] == [0, 2, 3, 4]
        assert wall.summary.text() == "4 displays — all stopped"
        wall.hide()  # the user is on another tab when Device Control rescans
        wall.set_displays([dict(d) for d in D3])
        _pump(qapp, 100)
        wall.show()
        _pump(qapp, 300)
        assert starts == [2]
        assert wall.summary.text() == "3 displays — all stopped"
    finally:
        _close(qapp, wall)


def test_start_all_starts_one_after_another_and_later_displays_arrive_stopped(qapp, starts):
    wall = _shown_wall(qapp)
    try:
        wall.btn_start_all.click()
        assert starts == [0]  # one at a time
        assert wall.summary.text() == "3 displays — 1 running, 2 more to start"
        wall._tiles[0].panel.screen_ready.emit()  # display 0 is up: the next follows
        assert _pump_until(qapp, lambda: starts == [0, 2])
        wall._tiles[1].panel.screen_ready.emit()
        assert _pump_until(qapp, lambda: starts == [0, 2, 3])
        wall._tiles[2].panel.screen_ready.emit()
        _pump(qapp, 100)
        assert wall.summary.text() == "3 displays — all running"
        assert not wall.btn_start_all.isEnabled() and wall.btn_stop_all.isEnabled()

        wall.set_displays([dict(d) for d in D3] + [dict(REAR)])  # found afterwards
        _pump(qapp, 300)
        assert starts == [0, 2, 3]  # the new display is not started
        assert wall._queue == [] and wall._waiting is None
        assert wall.summary.text() == "4 displays — 3 running"

        wall.btn_stop_all.click()
        assert wall.summary.text() == "4 displays — all stopped"
        wall.hide()
        wall.show()
        _pump(qapp, 300)
        assert starts == [0, 2, 3]
    finally:
        _close(qapp, wall)


def test_start_all_leaves_alone_a_display_started_and_stopped_by_hand_meanwhile(qapp, starts):
    """Issue: Start all's queue was built once, so a display the user started
    and stopped by hand while the chain waited on another was started again."""
    wall = _shown_wall(qapp)
    try:
        wall.start_all()
        assert starts == [0]  # the chain waits for display 0 to come up
        passenger = wall._tiles[2]
        passenger.panel.btn_mirror.click()  # the user starts display 3 meanwhile…
        passenger.panel.stop()  # …and stops it again
        assert starts == [0, 3] and passenger not in wall._queue
        wall._tiles[0].panel.stop()  # display 0 ends without coming up: move on
        assert _pump_until(qapp, lambda: starts == [0, 3, 2])
        wall._tiles[1].panel.screen_ready.emit()  # display 2 is up
        _pump(qapp, 300)
        assert starts == [0, 3, 2]  # display 3 is not started again
        assert wall._queue == [] and not passenger.panel.is_active()
    finally:
        _close(qapp, wall)


def test_start_all_carries_on_while_the_tab_is_hidden_until_stop_all(qapp, starts):
    """Start all is the user's own request, so switching to another tab does
    not cancel it (and coming back starts nothing new); Stop all ends it."""
    wall = _shown_wall(qapp)
    try:
        wall.start_all()
        assert starts == [0]
        wall.hide()  # the user switches to another tab
        wall._tiles[0].panel.screen_ready.emit()
        assert _pump_until(qapp, lambda: starts == [0, 2])
        wall.stop_all()  # only Stop all ends it early
        wall._tiles[1].panel.screen_ready.emit()
        _pump(qapp, 200)
        assert starts == [0, 2]
        wall.show()
        _pump(qapp, 300)
        assert starts == [0, 2]
        assert wall.summary.text() == "3 displays — all stopped"
    finally:
        _close(qapp, wall)


def test_a_renderer_change_never_starts_a_stopped_display(qapp, starts):
    from turboadb.gui import settings as settings_mod
    from turboadb.gui.mirror_panel import MirrorPanel

    wall = _shown_wall(qapp)
    try:
        settings_mod.update({"screen_backend": "screencap"})
        MirrorPanel.apply_default_backend(wall, "scrcpy")  # Settings was accepted
        _pump(qapp, 200)
        assert starts == []
    finally:
        settings_mod.update({"screen_backend": "scrcpy"})
        _close(qapp, wall)


# --------------------------------------------------------------------------- #
# The device tab: the connect probe, Options → All displays, Rescan
# --------------------------------------------------------------------------- #
class _Shell:
    """An idle interactive ``adb shell``."""

    def __init__(self):
        self.closed = False

    @property
    def alive(self):
        return not self.closed

    running = alive

    def read(self, _size=65536):
        time.sleep(0.005)
        return b""

    def send(self, _data):
        return True

    def close(self):
        self.closed = True


class _Car:
    """A fake head unit for a device tab: it answers what the tab asks and
    never runs scrcpy."""

    serial = "FAKEIVI"
    config = None

    def __init__(self):
        self.scans = []

    def open_shell(self, tty=True, safe=None):
        from turboadb.results import OperationResult

        return OperationResult(True, "open_shell", value=_Shell())

    def list_displays(self, method="auto", safe=None):
        from turboadb.results import OperationResult

        self.scans.append(method)
        return OperationResult(True, "list_displays", value=[dict(d) for d in D3])

    def disconnect(self, safe=None):
        return True


def test_all_displays_opens_the_tab_and_every_scan_leaves_the_displays_stopped(
    qapp, starts, monkeypatch
):
    """Options → All displays only navigates; the connect probe feeds the new
    tab once (it used to set the same displays twice); Rescan and switching
    tabs start nothing."""
    from turboadb.gui.device_tab import DeviceTab
    from turboadb.gui.display_wall import DisplayWall

    fed = []
    set_displays = DisplayWall.set_displays

    def counting(self, displays):
        fed.append([int(d["id"]) for d in displays or []])
        set_displays(self, displays)

    monkeypatch.setattr(DisplayWall, "set_displays", counting)
    car = _Car()
    tab = DeviceTab({"name": "ivi", "serial": car.serial})
    tab._started_connect = True  # never run a real connect for the made-up serial
    tab.resize(1400, 900)
    try:
        tab._on_connected(car, {"_probe_pending": True})
        tab._on_probe_details(
            car,
            {
                "prompt": None,
                "displays": [dict(d) for d in D3],
                "info": {"manufacturer": "Acme", "model": "Car", "automotive": True},
            },
        )
        wall = tab.display_wall
        assert tab.inner.tabText(tab.inner.indexOf(wall)) == "IVI Displays"
        assert [tile.display_id for tile in wall._tiles] == [0, 2, 3]
        assert fed == [[0, 2, 3]]  # once per scan

        tab.show_subtab("controls")
        tab.show()
        _pump(qapp, 200)
        tab.mirror_tab.open_ivi_view()  # Device Control → Options → All displays
        _pump(qapp, 400)
        assert tab.inner.currentWidget() is wall and wall.isVisible()
        assert starts == []
        assert wall.summary.text() == "3 displays — all stopped"
        assert wall.btn_start_all.isVisible() and wall.btn_start_all.isEnabled()
        assert all(tile.panel.btn_mirror.isVisible() for tile in wall._tiles)

        wall.btn_rescan.click()  # the tab's own Rescan
        assert _pump_until(qapp, lambda: len(fed) == 2)
        _pump(qapp, 200)
        assert car.scans and starts == []

        tab.show_subtab("controls")
        _pump(qapp, 100)
        tab.show_subtab("ivi")
        _pump(qapp, 300)
        assert starts == []
    finally:
        tab.hide()
        tab.close_session()
        tab.close()
        tab.deleteLater()
        qapp.processEvents()
