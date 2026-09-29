"""Where a display tile shows its display.

Only Windows can put scrcpy's window inside a tile. Elsewhere every display
that was started opened a scrcpy window of its own outside TurboADB (Start all:
one after another), while the tiles kept their idle frames. Off Windows a tile
now shows its display with ADB screencap frames, inside the tile. Headless; no
device, adb or scrcpy is used.
"""

import pytest

pytest.importorskip("PyQt5")

DISPLAYS = [
    {"id": 0, "size": "1920x720", "name": "Centre"},
    {"id": 2, "size": "1920x720", "name": "Cluster"},
]


class _Handler:
    serial = "ivi"
    config = None


class _NullDispatcher:
    submitted = 0

    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        return True

    def stop(self):
        pass


@pytest.fixture
def screens(monkeypatch):
    """Screencap views start no worker, and no scrcpy may be launched."""
    import turboadb.gui.mirror_panel as mp
    from turboadb.gui import screencap_view

    started = []

    def no_scrcpy(*_args, **_kwargs):
        raise AssertionError("a tile started scrcpy")

    monkeypatch.setattr(screencap_view.ScreencapView, "start", lambda self: started.append(self))
    monkeypatch.setattr(mp, "_MirrorLaunchThread", no_scrcpy)
    return started


def _wall(qapp, displays=DISPLAYS):
    from PyQt5.QtCore import Qt
    from turboadb.gui.display_wall import DisplayWall

    wall = DisplayWall(_Handler(), {"name": "ivi"}, automotive=True, dispatcher=_NullDispatcher())
    wall.setAttribute(Qt.WA_DontShowOnScreen, True)
    wall.resize(1200, 700)
    wall.set_displays([dict(d) for d in displays])
    wall.show()
    qapp.processEvents()
    return wall


def _close(qapp, wall):
    wall.close_panel()
    wall.close()
    wall.deleteLater()
    for _ in range(3):
        qapp.processEvents()


def test_off_windows_a_tile_shows_its_display_inside_the_tile(qapp, monkeypatch, screens):
    import turboadb.gui.mirror_panel as mp
    from turboadb.gui import settings as settings_mod

    monkeypatch.setattr(mp, "_IS_WIN", False)
    wall = _wall(qapp)
    try:
        cluster = wall._tiles[1].panel
        assert all(tile.panel.screen_backend() == "screencap" for tile in wall._tiles)
        assert cluster.btn_mirror.text() == "Start in this tab"
        cluster.start()  # the tile's own Start
        view = cluster.screencap_view()
        assert view is not None and screens == [view]
        assert view.parent() is cluster.container and view.input_display_id == 2
        wall.start_all()  # ...and Start all: every display inside its tile
        assert len(screens) == 2 and wall._tiles[0].panel.screencap_view() is screens[1]

        # a renderer chosen in Settings never turns the tiles into scrcpy windows
        settings_mod.update({"screen_backend": "scrcpy"})
        mp.MirrorPanel.apply_default_backend(wall, "screencap")
        assert cluster.screen_backend() == "screencap"
        assert cluster.screencap_view() is view
    finally:
        settings_mod.update({"screen_backend": "scrcpy"})
        _close(qapp, wall)


def test_off_windows_recording_a_tile_opens_no_scrcpy_window(qapp, monkeypatch, screens):
    import turboadb.gui.mirror_panel as mp

    monkeypatch.setattr(mp, "_IS_WIN", False)
    monkeypatch.setattr(
        mp.QFileDialog, "getSaveFileName", lambda *_a, **_k: ("cluster.mp4", "MP4 video (*.mp4)")
    )
    wall = _wall(qapp)
    try:
        cluster = wall._tiles[1].panel
        recorded = []
        cluster._record_device = lambda path, display_id=None: recorded.append((path, display_id))
        assert "screenrecord" in cluster.btn_record.toolTip()
        cluster.start()
        cluster._toggle_record()
        assert recorded == [("cluster.mp4", 2)]  # recorded on the device
    finally:
        _close(qapp, wall)


def test_on_windows_a_tile_embeds_scrcpy(qapp, monkeypatch):
    import turboadb.gui.mirror_panel as mp

    monkeypatch.setattr(mp, "_IS_WIN", True)
    wall = _wall(qapp)
    try:
        assert all(tile.panel.screen_backend() == "scrcpy" for tile in wall._tiles)
        assert all(tile.panel._backend_override is None for tile in wall._tiles)
    finally:
        _close(qapp, wall)
