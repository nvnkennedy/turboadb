"""The device screen: page changes that leave nothing behind, a big view that is
really big and sharp, and scrcpy that installs itself.

- A page change slid the new page in, every page was its own native window,
  and QTabWidget repainted everything inside it once per level of nested tabs:
  four to six whole-window repaints per click, with what was there before
  showing between them.
- Only scrcpy's one host window is native now, and it swaps with the page.
- Maximize view left a phone's picture the same size.
- A display larger than 1080p got the bit rate meant for 1080p; a head unit was
  cut down in size; every display tile got the same share of the wall, however
  large it was, and a maximized one kept it.
- Start with no scrcpy installed ended in an error until the tools were
  downloaded by hand.

Headless (offscreen Qt); no device, adb or scrcpy is used.
"""

import time
import types

import pytest

pytest.importorskip("PyQt5")

from PyQt5 import sip  # noqa: E402
from PyQt5.QtCore import QCoreApplication, QEvent, QObject, QPoint, QRect, Qt  # noqa: E402
from PyQt5.QtGui import QHideEvent  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QWidget,
)

from turboadb import toolsdl  # noqa: E402
from turboadb.exceptions import ADBNotFoundError  # noqa: E402
from turboadb.gui import mirror_panel as mp  # noqa: E402


def _pump(qapp, ms=60):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Launch:
    """_MirrorLaunchThread stand-in: records each launch and runs nothing."""

    made = []

    def __init__(self, handler, opts, compat, log_path):
        self.opts, self.compat = opts, compat
        self.done, self.fail = _Signal(), _Signal()
        _Launch.made.append(self)

    def start(self):
        pass

    def cancel(self):
        pass

    def isRunning(self):
        return False

    def isFinished(self):
        return True


@pytest.fixture
def launches(monkeypatch):
    _Launch.made = []
    monkeypatch.setattr(mp, "_MirrorLaunchThread", _Launch)
    monkeypatch.setattr(mp, "park_thread", lambda *_a: None)
    return _Launch.made


@pytest.fixture
def panel(qapp, monkeypatch):
    monkeypatch.setattr(mp, "_IS_WIN", True)
    made = mp.MirrorPanel(types.SimpleNamespace(serial="s", config=None), {"name": "s"})
    yield made
    if sip.isdeleted(made):  # went with the window it was put in
        return
    made._closing = True
    made._host_sync_timer.stop()
    made._place_timer.stop()
    made._install_spec = None
    made.setParent(None)
    made.deleteLater()
    _pump(qapp, 20)


# ---- a page change ----------------------------------------------------------
def test_a_page_change_does_not_move_the_page(qapp):
    from turboadb.gui.qtutil import AnimatedTabWidget

    tabs = AnimatedTabWidget(transition_ms=50)
    first, second = QWidget(), QWidget()
    tabs.addTab(first, "First")
    tabs.addTab(second, "Second")
    tabs.resize(400, 300)
    tabs.show()
    _pump(qapp)
    tabs.setCurrentIndex(1)
    where = second.pos()
    for _ in range(4):
        _pump(qapp, 15)
        assert second.pos() == where
    tabs.close()


class _Paints(QObject):
    def __init__(self):
        super().__init__()
        self.count = 0

    def eventFilter(self, _obj, event):
        if event.type() == QEvent.Paint:
            self.count += 1
        return False


def test_one_page_change_is_one_repaint_however_deep_the_tabs(qapp):
    """Each nested tab widget repainted everything inside it, one level at a
    time, for a layout pass in which nothing had moved."""
    from turboadb.gui.qtutil import AnimatedTabWidget

    outer, inner = AnimatedTabWidget(transition_ms=0), AnimatedTabWidget(transition_ms=0)
    middle = AnimatedTabWidget(transition_ms=0)
    first, second = QWidget(), QWidget()
    inner.addTab(first, "First")
    inner.addTab(second, "Second")
    middle.addTab(inner, "Device")
    outer.addTab(middle, "Window")
    outer.resize(800, 600)
    outer.show()
    _pump(qapp, 150)
    paints = _Paints()
    second.installEventFilter(paints)
    inner.setCurrentIndex(1)
    _pump(qapp, 200)
    assert 1 <= paints.count <= 2
    outer.close()


def test_a_tab_strip_that_really_changed_is_laid_out_again(qapp):
    from turboadb.gui.qtutil import AnimatedTabWidget

    tabs = AnimatedTabWidget(transition_ms=0)
    tabs.addTab(QWidget(), "One")
    tabs.addTab(QWidget(), "Two")
    tabs.resize(700, 400)
    tabs.show()
    _pump(qapp, 100)
    bar = tabs.tabBar()
    before = bar.width()
    tabs.setTabText(1, "A much longer title for the second tab")
    _pump(qapp, 100)
    assert bar.width() > before
    tabs.resize(900, 500)
    _pump(qapp, 100)
    assert tabs.currentWidget().width() > 800
    tabs.close()


def test_only_the_screens_host_is_native(qapp):
    """The app sets this before anything is built: otherwise every widget
    beside a native one becomes a window of its own."""
    from turboadb.gui import app as gui_app

    was = QCoreApplication.testAttribute(Qt.AA_DontCreateNativeWidgetSiblings)
    QCoreApplication.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings, False)
    try:
        gui_app.keep_widgets_in_one_window()
        assert QCoreApplication.testAttribute(Qt.AA_DontCreateNativeWidgetSiblings)
    finally:
        QCoreApplication.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings, was)


# ---- scrcpy's one native window -------------------------------------------
class _FromTheSystem(QHideEvent):
    """What Qt delivers when the whole window goes to the taskbar."""

    def spontaneous(self):
        return True


@pytest.fixture
def hosted(panel, qapp, monkeypatch):
    """A panel on one page of a window, with scrcpy's window (a stand-in) in it."""
    monkeypatch.setattr(mp, "_answering", lambda *_a, **_k: True)
    win = QMainWindow()
    central = QWidget()
    spacer = QWidget(central)
    spacer.setGeometry(0, 0, 120, 30)
    stack = QStackedWidget(central)
    stack.setGeometry(0, 40, 800, 560)
    other = QWidget()
    stack.addWidget(panel)
    stack.addWidget(other)
    win.setCentralWidget(central)
    win.resize(800, 600)
    win.show()
    _pump(qapp, 80)
    panel._child_hwnd = 0x4242
    host = panel._ensure_video_host()
    panel._place_video_host()
    panel._sync_video_host()
    _pump(qapp, 30)
    yield panel, win, stack, other, host
    panel._child_hwnd = None
    stack.removeWidget(panel)
    panel.setParent(None)
    win.close()


def test_nothing_but_the_host_is_a_native_window(hosted):
    panel, win, _stack, _other, host = hosted
    assert host.parentWidget() is win
    assert panel.container.windowHandle() is None
    assert panel.windowHandle() is None
    assert host.windowHandle() is not None


def test_the_host_lies_over_the_screen_area(hosted):
    panel, win, _stack, _other, host = hosted
    assert host.geometry() == QRect(panel.container.mapTo(win, QPoint(0, 0)), panel.container.size())
    assert not host.isHidden()


def test_the_host_follows_when_a_parent_moves_the_screen(hosted, qapp):
    """The screen's own place in its parent does not change here."""
    panel, win, stack, _other, host = hosted
    stack.move(35, 90)
    _pump(qapp, 40)
    assert host.geometry().topLeft() == panel.container.mapTo(win, QPoint(0, 0))


def test_the_host_goes_and_comes_with_its_page(hosted, qapp):
    panel, _win, stack, other, host = hosted
    stack.setCurrentWidget(other)
    _pump(qapp, 40)
    assert host.isHidden()
    stack.setCurrentWidget(panel)
    _pump(qapp, 40)
    assert not host.isHidden()


def test_a_page_change_asks_scrcpy_nothing(hosted, qapp, monkeypatch):
    """scrcpy may be busy or stuck: the host is TurboADB's own window."""
    panel, _win, stack, other, _host = hosted
    asked = []
    monkeypatch.setattr(mp, "_answering", lambda *a, **k: asked.append(a) or True)

    def not_scrcpy(*_a, **_k):
        asked.append("win32")
        raise AssertionError("no window call on a page change")

    monkeypatch.setattr(mp, "_win_api", not_scrcpy)
    panel._native_keyboard = False
    stack.setCurrentWidget(other)
    panel._sync_video_host()
    stack.setCurrentWidget(panel)
    panel._sync_video_host()
    assert asked == []


def test_the_app_going_to_the_taskbar_changes_nothing(hosted):
    panel, _win, _stack, _other, _host = hosted
    panel._host_sync_timer.stop()
    panel.hideEvent(_FromTheSystem())
    assert not panel._host_sync_timer.isActive()


def test_a_stopped_screen_takes_its_window_away(hosted):
    panel, _win, _stack, _other, host = hosted
    panel._release_viewer()
    assert host.isHidden()
    assert panel._video_host is host  # kept: scrcpy's window is still in it


def test_a_resized_screen_area_refits_at_once(hosted, qapp):
    panel, _win, _stack, _other, host = hosted
    panel._place_timer.stop()
    panel.container.resize(500, 300)
    assert panel._place_timer.isActive()
    _pump(qapp, 40)
    assert host.size() == panel.container.size()


def test_the_window_paints_under_the_host(panel, monkeypatch):
    style = {"value": mp._WS_CLIPCHILDREN | 0x10}

    class User32:
        @staticmethod
        def GetWindowLongW(_hwnd, _index):
            return style["value"]

        @staticmethod
        def SetWindowLongW(_hwnd, _index, value):
            style["value"] = value

    monkeypatch.setattr(mp, "_win_api", lambda: (None, User32))
    panel._paint_under_host(panel)
    assert style["value"] == 0x10


# ---- Maximize view -----------------------------------------------------------
def _window_around(panel):
    win = QMainWindow()
    win.setMenuWidget(QWidget())
    toolbar = QToolBar(win)
    win.addToolBar(toolbar)
    win.setStatusBar(QStatusBar(win))
    tabs = QTabWidget()
    corner = QWidget()
    tabs.setCornerWidget(corner, Qt.TopRightCorner)
    tabs.addTab(panel, "Device Control")
    win.setCentralWidget(tabs)
    return win, toolbar, tabs, corner


def test_maximize_view_leaves_only_the_screen(panel, qapp):
    win, toolbar, tabs, corner = _window_around(panel)
    win.resize(900, 700)
    win.show()
    _pump(qapp)
    menu, status = win.menuWidget(), win.statusBar()
    before = panel.height()
    panel.act_max.setChecked(True)
    _pump(qapp)
    for widget in (menu, toolbar, status, tabs.tabBar(), corner):
        assert not widget.isVisible()
    assert panel.height() > before
    panel.act_max.setChecked(False)
    _pump(qapp)
    for widget in (menu, toolbar, status, tabs.tabBar(), corner):
        assert widget.isVisible()
    win.close()


def test_maximize_view_brings_back_only_what_it_hid(panel, qapp):
    win, toolbar, tabs, _corner = _window_around(panel)
    toolbar.hide()  # the user had this off
    win.resize(900, 700)
    win.show()
    _pump(qapp)
    panel.act_max.setChecked(True)
    _pump(qapp)
    panel.act_max.setChecked(False)
    _pump(qapp)
    assert not toolbar.isVisible()
    assert tabs.tabBar().isVisible()
    win.close()


# ---- the video -------------------------------------------------------------
@pytest.mark.parametrize(
    "rate, size, max_edge, expected",
    [
        ("16M", (1920, 1080), None, "16M"),  # the size the default is meant for
        ("16M", (1280, 720), None, "16M"),  # never lowered
        ("16M", None, None, "16M"),  # unknown
        ("16M", (2560, 1440), None, "28M"),
        ("16M", (3840, 2160), None, "50M"),  # the most Settings offers
        ("16M", (2560, 1440), 1920, "16M"),  # the video is what counts
    ],
)
def test_bit_rate_follows_the_pixels(rate, size, max_edge, expected):
    assert mp._bit_rate_for_display(rate, size, max_edge) == expected


def test_a_rate_chosen_in_settings_is_kept(panel, monkeypatch):
    monkeypatch.setattr(panel, "_selected_display_entry", lambda _d: {"size": "2560x1440"})
    opts = mp.ScrcpyOptions(bit_rate="8M")
    panel._raise_bit_rate_for_big_display(opts, 0)
    assert opts.bit_rate == "8M"


def test_the_default_rate_rises_for_a_big_display(panel, monkeypatch):
    monkeypatch.setattr(panel, "_selected_display_entry", lambda _d: {"size": "2560x1440"})
    opts = mp.ScrcpyOptions(bit_rate="16M")
    panel._raise_bit_rate_for_big_display(opts, 0)
    assert opts.bit_rate == "28M"


def test_compat_keeps_a_large_display_whole(monkeypatch):
    """scrcpy lowers the size itself, only if the device's encoder refuses."""
    from turboadb import scrcpy as scrcpy_mod
    from turboadb.config import ADBConfig
    from turboadb.core import ADBHandler

    seen = {}

    def fake_launch(serial, opts, **_kw):
        seen["opts"] = opts
        return object()

    monkeypatch.setattr(scrcpy_mod, "launch_scrcpy", fake_launch)
    handler = ADBHandler(ADBConfig(adb_path="adb"), quiet=True)
    monkeypatch.setattr(ADBHandler, "adb_path", "adb", raising=False)
    handler.mirror(compat=True)
    assert seen["opts"].max_size is None
    assert seen["opts"].video_codec == "h264"
    assert seen["opts"].max_fps == 30
    handler.mirror(mp.ScrcpyOptions(max_size=1600), compat=True)
    assert seen["opts"].max_size == 1600  # a limit set on purpose stays


def test_remote_desktop_no_longer_halves_a_large_display(panel):
    panel._opt["soft"] = True
    assert panel._tune(mp.ScrcpyOptions(no_audio=True)).max_size == 1920
    assert panel._tune(mp.ScrcpyOptions(no_audio=True, max_size=1000)).max_size == 1000


def test_the_log_says_what_the_device_really_encodes(panel, monkeypatch):
    logged = []
    panel.log.connect(logged.append)
    panel._launch_opts = mp.ScrcpyOptions(bit_rate="32M")
    panel._video_reported = False
    monkeypatch.setattr(mp, "_read_log_tail", lambda *_a, **_k: "INFO: Renderer: opengl")
    panel._report_video_size()
    assert logged == [] and not panel._video_reported  # not known yet
    monkeypatch.setattr(mp, "_read_log_tail", lambda *_a, **_k: "INFO: Texture: 2560x1600\n")
    panel._report_video_size()
    assert logged == ["[INFO] screen video: 2560x1600 at 32M bit/s"] and panel._video_reported


class _Session:
    running = True

    def stop(self, *_a, **_k):
        pass


def test_a_maximized_tile_restarts_at_full_quality(panel, monkeypatch):
    panel._scrcpy = _Session()
    panel._launch_opts = mp.ScrcpyOptions(bit_rate="5M")
    panel._launch_spec = {"display_id": 0, "compat": False, "embed": None, "record": None,
                          "record_format": None, "camera": None}
    panel.set_video_budget(bit_rate="5M")
    starts = []
    monkeypatch.setattr(panel, "start", lambda **spec: starts.append(spec))
    try:
        assert panel.use_full_video_quality() is True
    finally:
        panel._scrcpy = None
    assert panel._video_budget == {}
    assert starts and starts[0]["display_id"] == 0


def test_a_tile_already_at_full_quality_is_not_restarted(panel, monkeypatch):
    panel._scrcpy = _Session()
    panel._launch_opts = mp.ScrcpyOptions(bit_rate="50M")
    starts = []
    monkeypatch.setattr(panel, "start", lambda **spec: starts.append(spec))
    try:
        assert panel.use_full_video_quality() is False
    finally:
        panel._scrcpy = None
    assert starts == []


def test_a_recording_is_never_restarted_for_quality(panel, monkeypatch):
    panel._scrcpy = _Session()
    panel._launch_opts = mp.ScrcpyOptions(bit_rate="5M")
    panel._recording = True
    starts = []
    monkeypatch.setattr(panel, "start", lambda **spec: starts.append(spec))
    try:
        assert panel.use_full_video_quality() is False
    finally:
        panel._scrcpy = None
        panel._recording = False
    assert starts == []


def test_the_wall_shares_its_bit_rate_by_pixels(qapp):
    from turboadb.gui.display_wall import DisplayWall

    wall = DisplayWall(types.SimpleNamespace(serial="s", config=None), {"name": "s"})
    wall.set_displays([{"id": 0, "size": "2560x1600"}, {"id": 2, "size": "1920x720"},
                       {"id": 3, "size": "320x240"}])
    big, cluster, tiny = wall._tiles
    rates = [int(wall.tile_bit_rate(tile)[:-1]) for tile in (big, cluster, tiny)]
    assert rates[0] > rates[1] > rates[2]
    assert wall.tile_bit_rate(tiny) == wall.TILE_MIN_BIT_RATE
    wall.close_panel()
    wall.setParent(None)
    wall.deleteLater()
    _pump(qapp, 20)


def test_icons_are_drawn_once_per_size_and_colour(qapp, monkeypatch):
    from PyQt5.QtGui import QPainter, QPixmap
    from turboadb.gui import icons

    monkeypatch.setattr(icons, "_DRAWINGS", {})
    drawn = []
    original = icons._renderer

    def counting(name, colour, width):
        drawn.append(name)
        return original(name, colour, width)

    monkeypatch.setattr(icons, "_renderer", counting)
    icon = icons.icon("play", "accent")
    for _ in range(3):
        target = QPixmap(24, 24)
        target.fill(Qt.transparent)
        painter = QPainter(target)
        icon.paint(painter, 0, 0, 24, 24)
        painter.end()
    assert drawn == ["play"]
    image = target.toImage()
    assert any(image.pixelColor(x, y).alpha() > 0 for x in range(24) for y in range(24))


def test_the_wall_gives_a_maximized_tile_full_quality_and_takes_it_back(qapp, monkeypatch):
    from turboadb.gui.display_wall import DisplayWall

    wall = DisplayWall(types.SimpleNamespace(serial="s", config=None), {"name": "s"})
    wall.set_displays([{"id": 0, "size": "1920x1080"}, {"id": 2, "size": "1280x720"},
                       {"id": 3, "size": "1280x720"}])
    tile = wall._tiles[0]
    full = []
    monkeypatch.setattr(
        tile.panel, "use_full_video_quality",
        lambda: full.append(1) or tile.panel.set_video_budget(),
    )
    wall.maximize_tile(tile)
    assert full == [1]
    assert tile.panel._video_budget == {}
    wall.restore_grid()
    assert tile.panel._video_budget == {"bit_rate": wall.tile_bit_rate(tile)}
    wall.close_panel()
    wall.setParent(None)
    wall.deleteLater()
    _pump(qapp, 20)


# ---- scrcpy installs itself ---------------------------------------------------
class _NoScrcpy:
    """find_scrcpy with no copy anywhere, and a download that can be finished."""

    @staticmethod
    def missing(*_a, **_k):
        raise ADBNotFoundError("no scrcpy")


class _InstallThread:
    made = []

    def __init__(self, install):
        self.install = install
        self.progress, self.done, self.fail = _Signal(), _Signal(), _Signal()
        _InstallThread.made.append(self)

    def start(self):
        pass

    def isRunning(self):
        return not self.finished

    finished = False


@pytest.fixture
def no_scrcpy(monkeypatch, launches):
    from turboadb import tools

    _InstallThread.made = []
    monkeypatch.setattr(tools, "find_scrcpy", _NoScrcpy.missing)
    monkeypatch.setattr(toolsdl, "auto_fetch_enabled", lambda: True)
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: True)
    monkeypatch.setattr(mp, "_ScrcpyInstallThread", _InstallThread)
    return launches


def test_start_downloads_scrcpy_then_shows_the_screen(panel, no_scrcpy, monkeypatch):
    from turboadb import tools

    panel.start(embed=False)
    assert len(_InstallThread.made) == 1
    assert no_scrcpy == []  # nothing is launched before it is there
    assert panel.is_active()
    assert "Installing scrcpy" in panel.status.text()
    panel._on_scrcpy_install_progress(40)
    assert "40%" in panel.status.text()
    monkeypatch.setattr(tools, "find_scrcpy", lambda *_a, **_k: "scrcpy.exe")
    _InstallThread.made[0].finished = True
    panel._on_scrcpy_installed("scrcpy.exe")
    assert len(no_scrcpy) == 1


def test_stop_cancels_the_start_a_download_waits_for(panel, no_scrcpy, monkeypatch):
    from turboadb import tools

    panel.start(embed=False)
    panel.stop()
    assert not panel.is_active()
    monkeypatch.setattr(tools, "find_scrcpy", lambda *_a, **_k: "scrcpy.exe")
    panel._on_scrcpy_installed("scrcpy.exe")
    assert no_scrcpy == []


def test_a_failed_download_says_what_to_do(panel, no_scrcpy):
    failed = []
    panel.screen_failed.connect(failed.append)
    panel.start(embed=False)
    panel._on_scrcpy_install_failed("HTTP 403")
    assert not panel.is_active()
    assert "could not be installed" in panel.status.text()
    assert failed == ["HTTP 403"]


def test_scrcpy_that_is_there_is_not_downloaded(panel, launches, monkeypatch):
    from turboadb import tools

    monkeypatch.setattr(tools, "find_scrcpy", lambda *_a, **_k: "scrcpy.exe")
    monkeypatch.setattr(toolsdl, "auto_fetch_enabled", lambda: True)
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: True)
    assert not panel._scrcpy_needs_installing()


def test_a_path_set_on_purpose_is_not_replaced_by_a_download(panel, monkeypatch):
    from turboadb import tools

    def configured(*_a, **_k):
        raise ADBNotFoundError("TURBOADB_SCRCPY names nothing", configured="C:/nope")

    monkeypatch.setattr(tools, "find_scrcpy", configured)
    monkeypatch.setattr(toolsdl, "auto_fetch_enabled", lambda: True)
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: True)
    assert not panel._scrcpy_needs_installing()


def test_no_download_when_downloads_are_switched_off(panel, monkeypatch):
    from turboadb import tools

    monkeypatch.setattr(tools, "find_scrcpy", _NoScrcpy.missing)
    monkeypatch.setattr(toolsdl, "auto_fetch_enabled", lambda: False)
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: True)
    assert not panel._scrcpy_needs_installing()


def test_install_uses_what_is_found(monkeypatch):
    monkeypatch.setattr(toolsdl, "find_scrcpy", lambda: "found.exe")
    monkeypatch.setattr(toolsdl, "download_scrcpy", lambda **_k: pytest.fail("downloaded"))
    assert toolsdl.install_missing_scrcpy() == "found.exe"


def test_install_downloads_what_is_missing(monkeypatch):
    monkeypatch.setattr(toolsdl, "find_scrcpy", _NoScrcpy.missing)
    monkeypatch.setattr(toolsdl, "download_scrcpy", lambda **_k: "new.exe")
    assert toolsdl.install_missing_scrcpy() == "new.exe"


def test_install_leaves_a_path_set_on_purpose_to_be_corrected(monkeypatch):
    def configured():
        raise ADBNotFoundError("bad path", configured="C:/nope")

    monkeypatch.setattr(toolsdl, "find_scrcpy", configured)
    monkeypatch.setattr(toolsdl, "download_scrcpy", lambda **_k: pytest.fail("downloaded"))
    with pytest.raises(ADBNotFoundError):
        toolsdl.install_missing_scrcpy()
