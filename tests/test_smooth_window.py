"""The window keeps answering however much is going on (6.2.0).

Every test here is about work that used to hold the window's thread: a logcat
flood painted line by line, terminals flooding together, Windows' file icons
looked up while a folder was drawn, Device Control's sections moved to new
parents on every resize, the screen's log opened twice a second.
"""

import os
import sys
import threading
import time
import weakref

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, QObject  # noqa: E402
from PyQt5.QtGui import QColor, QImage  # noqa: E402


def _wait(qapp, done, timeout=5.0):
    """Let the event loop run until *done()* (the icons' worker answers)."""
    end = time.monotonic() + timeout
    while not done():
        if time.monotonic() > end:
            return False
        qapp.processEvents()
        time.sleep(0.005)
    return True


def _busy(seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


# --------------------------------------------------------------------------- #
# logcat: a repaint puts on screen what fits in its budget
# --------------------------------------------------------------------------- #
def test_a_logcat_flood_shows_the_newest_lines_that_fit_and_counts_the_rest(qapp):
    """Issue: a logcat flood was painted line by line, every line of every
    repaint: the window's thread was gone for 300 ms at a time with one busy
    device, and clicks and keys waited."""
    from turboadb.gui.logcat_view import LogcatPanel

    panel = LogcatPanel(None)
    try:
        panel._line_cost = panel._PAINT_BUDGET_S / 300  # 300 lines fit in a repaint
        lines = [f"11-13 10:00:00.000  1  2 I Tag: line {i}" for i in range(2000)]
        panel._on_batch(lines)
        panel._render_pending()
        shown = panel.view.toPlainText().splitlines()
        assert shown[0] == "… (1700 lines skipped on screen — saved log has all)"
        assert shown[1:] == lines[-300:]  # the newest, in order
        assert panel._skipped == 0 and panel._pending == []
    finally:
        panel.close_panel()


def test_the_logcat_repaint_learns_what_a_line_costs(qapp):
    from turboadb.gui.logcat_view import LogcatPanel

    panel = LogcatPanel(None)
    try:
        panel._line_cost = 1.0  # absurdly slow lines: still a screenful goes on
        panel._on_batch([f"line {i}" for i in range(1000)])
        panel._render_pending()
        shown = panel.view.toPlainText().splitlines()
        assert len(shown) == panel._PAINT_MIN_LINES + 1  # and the skipped note
        assert shown[-1] == "line 999"
        assert 2e-6 <= panel._line_cost < 1.0  # measured on what was drawn
    finally:
        panel.close_panel()


def test_a_log_view_keeps_no_undo_history(qapp):
    """Every line went into the view's undo history too (a log is never
    edited), which cost time and memory for nothing."""
    from turboadb.gui.logcat_view import LogcatPanel

    panel = LogcatPanel(None)
    try:
        assert not panel.view.isUndoRedoEnabled()
        panel._on_batch([f"line {i}" for i in range(50)])
        panel._render_pending()
        assert panel.view.document().availableUndoSteps() == 0
    finally:
        panel.close_panel()


# --------------------------------------------------------------------------- #
# terminals flooding together share one budget
# --------------------------------------------------------------------------- #
def test_terminals_flooding_together_share_the_windows_time(qapp, monkeypatch):
    """Issue: each terminal drew for up to 30 ms a tick; with several
    devices' terminals flooding at once the window's thread had nothing left
    for a click or a key."""
    from turboadb.gui.console import AnsiConsole

    monkeypatch.setattr(AnsiConsole, "_drawing", weakref.WeakSet())
    drawn = []

    def slow_draw(self, _text):  # one slice of output costs a millisecond
        drawn.append(self)
        _busy(0.001)

    monkeypatch.setattr(AnsiConsole, "_process", slow_draw)
    consoles = [AnsiConsole() for _ in range(3)]
    chunk = b"".join(b"output line %d\r\n" % i for i in range(100))
    try:
        for con in consoles:
            for _ in range(60):
                con.feed(chunk)
        consoles[0]._drain_tick()  # the only one drawing
        alone = len(drawn)
        for con in consoles[1:]:
            con._drain_tick()  # now all three are
        drawn.clear()
        consoles[0]._drain_tick()
        shared = len(drawn)
        assert len(AnsiConsole._drawing) == 3
        assert alone >= 7  # most of the shared budget: about 9 ms
        assert shared <= 5  # a third of it
    finally:
        for con in consoles:
            con._inq.clear()
            con._drain_tick()  # drawn out: no longer counted
            con.deleteLater()
    assert len(AnsiConsole._drawing) == 0


def test_a_terminal_tick_is_one_edit_of_its_document(qapp):
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole()
    try:
        con.feed(b"".join(b"line %d\r\n" % i for i in range(500)))
        edits = []
        con.document().contentsChange.connect(lambda *a: edits.append(a))
        con._drain_tick()
        assert con.toPlainText().count("\n") >= 400
        assert len(edits) == 1  # laid out once for the whole tick
    finally:
        con.deleteLater()


# --------------------------------------------------------------------------- #
# Windows' file icons: looked up on a thread of their own
# --------------------------------------------------------------------------- #
def _image(colour):
    image = QImage(16, 16, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(colour))
    return image


class _Lookups:
    """Stands in for SHGetFileInfo: an image per path, the calls in order,
    and a gate a test may close to hold the worker."""

    def __init__(self, colours=None):
        self.calls = []
        self.colours = colours or {}
        self.gate = threading.Event()
        self.gate.set()
        self.hold = set()  # paths that wait for the gate
        self.lock = threading.Lock()

    def __call__(self, path, attributes, flags):
        with self.lock:
            self.calls.append(path)
        if path in self.hold:
            self.gate.wait(10)
        ext = os.path.splitext(path)[1].lower()
        return [_image(self.colours.get(path, self.colours.get(ext, "#3366ff")))]


def _colour(icon):
    return icon.pixmap(16, 16).toImage().pixelColor(8, 8).name()


def test_a_shell_icon_is_looked_up_once_off_the_windows_thread(qapp):
    from turboadb.gui.shell_icons import ShellIcons

    lookups = _Lookups()
    shell = ShellIcons(lookup=lookups)
    try:
        caller = threading.current_thread()
        seen = []
        real = lookups.__call__

        def on_worker(path, attributes, flags):
            seen.append(threading.current_thread() is caller)
            return real(path, attributes, flags)

        shell._lookup = on_worker
        changed = []
        shell.changed.connect(lambda: changed.append(1))
        assert [shell.type_icon(".png") for _ in range(5)] == [None] * 5  # asked once
        assert _wait(qapp, lambda: changed)
        icon = shell.type_icon(".png")
        assert icon is not None and _colour(icon) == "#3366ff"
        assert seen == [False]  # one lookup, never on the window's thread
        assert lookups.calls == ["x.png"]
    finally:
        shell.stop()


def test_only_looking_does_not_ask_the_shell(qapp):
    from turboadb.gui.shell_icons import ShellIcons

    lookups = _Lookups()
    shell = ShellIcons(lookup=lookups)
    try:
        assert shell.type_icon(".png", ask=False) is None
        assert shell.path_icon(r"C:\tools\a.exe", False, ask=False) is None
        qapp.processEvents()
        time.sleep(0.05)
        assert lookups.calls == []
    finally:
        shell.stop()


def test_the_folder_just_opened_is_looked_up_first(qapp):
    """A long folder left behind must not hold up the icons of the one the
    user went to."""
    from turboadb.gui.shell_icons import ShellIcons

    lookups = _Lookups()
    lookups.hold = {"x.first"}
    lookups.gate.clear()
    shell = ShellIcons(lookup=lookups)
    try:
        shell.type_icon(".first")
        assert _wait(qapp, lambda: lookups.calls == ["x.first"])  # the worker is held there
        shell.new_batch()
        for ext in (".a1", ".a2", ".a3"):
            shell.type_icon(ext)  # the folder being left
        shell.new_batch()
        for ext in (".b1", ".b2"):
            shell.type_icon(ext)  # the folder just opened
        lookups.gate.set()
        assert _wait(qapp, lambda: len(lookups.calls) == 6)
        assert lookups.calls == ["x.first", "x.b1", "x.b2", "x.a1", "x.a2", "x.a3"]
    finally:
        lookups.gate.set()
        shell.stop()


def test_a_lookup_that_never_ends_does_not_hold_up_the_others(qapp, monkeypatch):
    """A folder on a network drive that went away: its lookup may never
    return; a new worker answers the rest."""
    from turboadb.gui import shell_icons

    monkeypatch.setattr(shell_icons, "STUCK_S", 0.2)
    lookups = _Lookups()
    lookups.hold = {"x.stuck"}
    lookups.gate.clear()
    shell = shell_icons.ShellIcons(lookup=lookups)
    try:
        shell.type_icon(".stuck")
        assert _wait(qapp, lambda: lookups.calls == ["x.stuck"])
        time.sleep(0.3)
        shell.type_icon(".next")
        assert _wait(qapp, lambda: shell.type_icon(".next", ask=False) is not None)
        assert shell.type_icon(".stuck", ask=False) is None  # still out there
    finally:
        lookups.gate.set()
        shell.stop()


def test_programs_and_folders_are_looked_up_by_path_other_files_by_type(qapp):
    from turboadb.gui.shell_icons import ShellIcons

    lookups = _Lookups()
    shell = ShellIcons(lookup=lookups)
    try:
        shell.path_icon(r"C:\Users\me\Downloads", True)
        shell.path_icon(r"C:\Users\me\Downloads\setup.exe", False)
        shell.path_icon(r"C:\Users\me\Downloads\photo.jpg", False)
        shell.path_icon(r"C:\Users\me\Downloads\other.jpg", False)
        assert _wait(qapp, lambda: len(lookups.calls) == 3)
        time.sleep(0.05)
        assert lookups.calls == [r"C:\Users\me\Downloads", r"C:\Users\me\Downloads\setup.exe",
                                 "x.jpg"]
    finally:
        shell.stop()


def test_rows_wait_for_their_icon_with_their_glyph(qapp):
    from turboadb.gui import file_icons as fi
    from turboadb.gui.qtutil import cached_icon
    from turboadb.gui.shell_icons import ShellIcons

    lookups = _Lookups({".png": "#3366ff", ".apk": "#808080", ".turboadb-unknown-type": "#808080"})
    shell = ShellIcons(lookup=lookups)
    icons = fi.FileIcons(native=True, shell=shell)
    try:
        assert icons.row_icon_ready("a.png", False, "File", "image", "purple", ask=False) == \
            (cached_icon("image", "purple"), False)
        assert lookups.calls == []
        icon, ready = icons.row_icon_ready("a.png", False, "File", "image", "purple")
        assert not ready and icon is cached_icon("image", "purple")
        apk, ready = icons.row_icon_ready("app.apk", False, "File", "apps", "green")
        assert not ready  # its badge needs the blank file's icon too
        assert _wait(qapp, lambda: icons.row_icon_ready(
            "app.apk", False, "File", "apps", "green")[1])
        png, ready = icons.row_icon_ready("b.png", False, "File", "image", "purple")
        assert ready and _colour(png) == "#3366ff"
        apk, ready = icons.row_icon_ready("app.apk", False, "File", "apps", "green")
        assert ready
        image = apk.pixmap(16, 16).toImage()
        assert any(image.pixelColor(x, y).name() == fi._ANDROID_PLATE
                   for x in range(8, 16) for y in range(8, 16))
    finally:
        shell.stop()


def test_a_long_pc_folder_looks_up_only_the_rows_on_screen(qapp, monkeypatch, tmp_path):
    """Issue: opening a PC folder froze the window while Windows was asked
    for every row's icon (Downloads: a second).  Now only the rows drawn ask,
    on the icons' own thread, and take their icon when it is in."""
    from turboadb.gui import file_browser
    from turboadb.gui import file_icons as fi
    from turboadb.gui.shell_icons import ShellIcons

    lookups = _Lookups({".exe": "#aa00aa"})
    shell = ShellIcons(lookup=lookups)
    monkeypatch.setattr(fi, "_shared", fi.FileIcons(native=True, shell=shell))
    browser = file_browser.FileBrowser(None)
    try:
        browser.resize(1200, 700)
        browser.show()
        assert _wait(qapp, lambda: not browser._jobs)  # its own first listing is in
        table = browser.local_table
        table.base_dir = str(tmp_path)
        rows = [(f"tool{i:03d}.exe", 1, "1 B", "File", "", "", "", False) for i in range(300)]
        browser._populate(table, rows, parent_row=True)
        assert table.icons_waiting
        later = file_browser._ICON_LATER_ROLE

        def first_rows_in():
            table.viewport().repaint()
            return all(table.item(r, 0).data(later) is None for r in range(1, 6))

        assert _wait(qapp, first_rows_in)
        assert _colour(table.item(1, 0).icon()) == "#aa00aa"
        assert table.item(299, 0).data(later) == (str(tmp_path),)  # never drawn, never asked
        assert 0 < len(lookups.calls) < 100
        assert all(os.path.dirname(p) == str(tmp_path) for p in lookups.calls)
    finally:
        browser.close_panel()
        shell.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' shell")
def test_windows_answers_on_the_icons_thread(qapp):
    """The real shell, from the worker thread: the window's thread only ever
    asks (at once) and takes the answer."""
    from turboadb.gui.shell_icons import ShellIcons

    shell = ShellIcons()
    try:
        started = time.perf_counter()
        assert shell.folder() is None and shell.type_icon(".txt") is None
        assert time.perf_counter() - started < 0.05
        assert _wait(qapp, lambda: shell.folder(ask=False) is not None
                     and shell.type_icon(".txt", ask=False) is not None, timeout=20)
        for icon in (shell.folder(), shell.type_icon(".txt")):
            if not icon.isNull():  # a runner without a desktop may have none
                image = icon.pixmap(32, 32).toImage()
                assert any(image.pixelColor(x, y).alpha() for x in range(32) for y in range(32))
    finally:
        shell.stop()


# --------------------------------------------------------------------------- #
# Device Control's sections stay where they are when the columns change
# --------------------------------------------------------------------------- #
class _ParentChanges(QObject):
    def __init__(self, widgets):
        super().__init__()
        self.count = 0
        for widget in widgets:
            widget.installEventFilter(self)

    def eventFilter(self, _watched, event):
        if event.type() == QEvent.ParentChange:
            self.count += 1
        return False


def test_device_control_reflows_its_sections_without_moving_them(qapp):
    """Issue: every change in the number of columns gave each section a new
    parent, which applied the stylesheet again to all of its buttons: a
    frozen window for a fifth of a second, again and again while resizing."""
    from turboadb.gui.controls_panel import ControlsPanel

    cp = ControlsPanel(None, compact=False)
    try:
        cp.resize(600, 900)
        cp.show()
        qapp.processEvents()
        changes = _ParentChanges(cp._groups)
        for width in (900, 1300, 1700, 600, 1300):
            cp.resize(width, 900)
            qapp.processEvents()
            assert len(cp._columns) == cp._ncols == cp._columns_for(width)
            placed = [cp._columns[c].indexOf(g) >= 0 for g in cp._groups
                      for c in range(cp._ncols)]
            assert sum(placed) == len(cp._groups)  # each section in one column
        assert changes.count == 0
        assert all(g.parentWidget() is cp._grid_host and g.isVisible() for g in cp._groups)
    finally:
        cp.close_panel()
        cp.close()


# --------------------------------------------------------------------------- #
# the screen's log is not opened again while it has not changed
# --------------------------------------------------------------------------- #
def test_an_unchanged_screen_log_is_not_opened_again(tmp_path, monkeypatch):
    """The screen's alive check read scrcpy's log twice a second for the
    whole session; opening the file (a virus scanner looks at every open)
    froze the window for a seventh of a second at times."""
    import types

    from turboadb.gui import mirror_panel

    log = tmp_path / "scrcpy.log"
    log.write_bytes(b"INFO: Renderer: direct3d\nINFO: Texture: 1080x2400\n")
    opened = []
    real_open = open

    def counting_open(path, *a, **k):
        opened.append(path)
        return real_open(path, *a, **k)

    monkeypatch.setattr(mirror_panel, "open", counting_open, raising=False)
    session = types.SimpleNamespace(log_path=str(log))
    first = mirror_panel._read_log_tail(session, 4096)
    assert "Texture: 1080x2400" in first
    assert mirror_panel._read_log_tail(session, 4096) == first
    assert mirror_panel._read_log_tail(session, 16) == first[-16:]
    assert len(opened) == 1
    with real_open(log, "ab") as fh:
        fh.write(b"INFO: Texture: 2400x1080\n")
    assert mirror_panel._read_log_tail(session, 4096).endswith("Texture: 2400x1080\n")
    assert len(opened) == 2
