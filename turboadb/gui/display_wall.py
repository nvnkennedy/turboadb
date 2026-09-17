"""Every display of one device, live and controllable, in one tab.

Multi-display devices (an Android Automotive head unit's centre stack,
instrument cluster and passenger screen) get one screen tile per display. Each
tile is a full MirrorPanel locked to its display: embedded screen, typing,
screenshot and recording, plus a row of basic controls (Back, Home, Recents,
volume, power, screenshot) aimed at that display. The tiles start one after
another the first time the tab is shown, so the device never has to start
several scrcpy servers at once, and they share one video budget: the wall as a
whole asks the device for about what one full Device Control screen does.
"""

from __future__ import annotations

import math

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QShortcut,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .display_controls import DisplayControls
from .flowlayout import ToolbarFlowLayout
from .mirror_panel import MirrorPanel, _bitrate_to_bps, display_label


def best_columns(count, width, height, aspect, header=0.0) -> int:
    """How many columns show *count* screens of shape *aspect* (width / height)
    largest in a *width* x *height* area, each tile losing *header* px of
    height to its title and toolbar."""
    count = max(1, int(count))
    aspect = aspect if aspect and aspect > 0 else 16.0 / 9.0
    best, best_width = 1, -1.0
    for cols in range(1, count + 1):
        rows = math.ceil(count / cols)
        cell_w = width / cols
        cell_h = max(1.0, height / rows - header)
        shown = min(cell_w, cell_h * aspect)
        if shown > best_width + 0.5:
            best, best_width = cols, shown
    return best


class _DisplayTile(QFrame):
    """One display: a title row with its controls, and a MirrorPanel locked to
    that display."""

    def __init__(self, handler, session, display, automotive, dispatcher, on_focus, parent=None):
        super().__init__(parent)
        self.setObjectName("iviPreviewTile")
        self.display = dict(display)
        self.display_id = int(display.get("id", 0) or 0)

        v = QVBoxLayout(self)
        v.setContentsMargins(1, 1, 1, 1)
        v.setSpacing(0)
        head_w = QWidget()
        head_w.setMinimumWidth(1)  # a narrow tile wraps its header, never widens the tab
        head = ToolbarFlowLayout(head_w, hspacing=8, vspacing=4)
        head.setContentsMargins(12, 8, 8, 0)
        self.title = QLabel(display_label(display))
        self.title.setObjectName("iviPreviewTitle")
        head.addWidget(self.title)
        head.addStretch()
        # Maximize: this display alone fills the wall (DisplayWall._set_focus);
        # Restore, or Esc, returns to the grid.
        self.btn_focus = QPushButton()
        self.btn_focus.setProperty("role", "ghost")
        self.btn_focus.setCheckable(True)
        self.btn_focus.setIcon(icons.icon("maximize", "accent"))
        self.btn_focus.toggled.connect(lambda on: on_focus(self if on else None))
        self.sync_max_button()

        self.panel = MirrorPanel(
            handler, session, automotive=automotive, prefer_embed=True, dispatcher=dispatcher
        )
        self.panel.set_fixed_display(display)
        # The panel's own dispatcher when the wall has none: presses are still
        # queued in order, off the UI thread.
        self.controls = DisplayControls(
            handler,
            self.display_id,
            self.panel._dispatcher,
            on_screenshot=lambda: self.panel._take_screenshot(),
        )
        self.controls.log.connect(self.panel.log)
        head.addWidget(self.controls)
        head.addWidget(self.btn_focus)
        v.addWidget(head_w)
        v.addWidget(self.panel, 1)

    @property
    def btn_max(self):
        """The tile's Maximize / Restore button (the same widget as btn_focus)."""
        return self.btn_focus

    def sync_max_button(self, on=None) -> None:
        """Label the button for what a click does next, checked while maximized."""
        button = self.btn_focus
        if on is None:
            on = button.isChecked()
        on = bool(on)
        if button.isChecked() != on:
            button.blockSignals(True)
            button.setChecked(on)
            button.blockSignals(False)
        button.setText("Restore" if on else "Maximize")
        button.setToolTip(
            "Restore: show every display again (Esc)" if on
            else "Maximize: show only this display, filling the tab"
        )

    def aspect(self) -> float:
        """The display's width / height: the real video once shown, else its size."""
        return self.panel.display_aspect()

    def header_height(self) -> int:
        """Height above this tile's video: its title row plus the panel's own
        toolbar. 0 until the tile has been laid out at least once."""
        chrome = self.panel.chrome_height()
        if chrome <= 0 or self.panel.height() <= 0:
            return 0
        return max(0, self.height() - self.panel.height()) + chrome

    def update_display(self, display) -> None:
        """Take a fresh entry for this display from a rescan."""
        display = dict(display)
        if display == self.display:
            return
        self.display = display
        self.title.setText(display_label(display))
        # A display that changed resolution must reshape its panel too, or the
        # tile keeps the old aspect and idle frame.
        self.panel.set_fixed_display(display)


class DisplayWall(QWidget):
    """The (IVI) Displays tab: every display of the device at once."""

    log = pyqtSignal(str)
    rescan_requested = pyqtSignal()

    START_GAP_MS = 500  # after one screen is up, before starting the next
    # Move on if a screen never comes up. A panel gives its own scrcpy launch
    # _STARTUP_TIMEOUT before it kills and retries it, so derive the wait from
    # that (plus a beat to report): a shorter wall timeout would start the next
    # display while the previous one is still pushing its server to the device,
    # which is exactly the contention this sequencing exists to avoid.
    START_TIMEOUT_MS = int(MirrorPanel._STARTUP_TIMEOUT * 1000) + 2000
    TILE_HEADER = 96  # fallback for a tile's title row + screen toolbar
    # The tiles share one screen's worth of video bit rate (never less than
    # TILE_MIN_BIT_RATE each): several full-rate sessions at once load the
    # device's encoder and the adb link that also carries every touch.
    WALL_BIT_RATE = "16M"
    TILE_MIN_BIT_RATE = "4M"

    def __init__(self, handler, session, automotive=False, dispatcher=None, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.session = session
        self.automotive = bool(automotive)
        self._dispatcher = dispatcher
        self._tiles = []
        self._retiring = []  # removed tiles, kept alive until their screen stops
        self._queue = []
        self._waiting = None
        self._auto_started = False
        self._stopped_by_user = False
        self._focused = None
        self._closing = False
        self._columns = 0
        self._placed = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        bar_w = QWidget()
        bar_w.setObjectName("pageToolbar")
        bar_w.setAttribute(Qt.WA_StyledBackground, True)
        bar = ToolbarFlowLayout(bar_w, hspacing=8, vspacing=6)
        bar.setContentsMargins(12, 8, 12, 8)
        self.summary = QLabel("Looking for displays…")
        self.summary.setObjectName("iviPreviewTitle")
        bar.addWidget(self.summary)
        bar.addStretch()
        self.btn_start_all = QPushButton("Start all")
        self.btn_start_all.setProperty("role", "ok")
        self.btn_start_all.setIcon(icons.icon("play", "on-accent"))
        self.btn_start_all.setToolTip("Show every display, one after another")
        self.btn_start_all.clicked.connect(lambda _checked=False: self.start_all())
        self.btn_stop_all = QPushButton("Stop all")
        self.btn_stop_all.setProperty("role", "danger")
        self.btn_stop_all.setIcon(icons.icon("stop", "on-danger"))
        self.btn_stop_all.setToolTip("Stop every display's screen")
        self.btn_stop_all.clicked.connect(lambda _checked=False: self.stop_all())
        self.btn_rescan = QPushButton("Rescan")
        self.btn_rescan.setProperty("role", "ghost")
        self.btn_rescan.setIcon(icons.icon("refresh", "accent"))
        self.btn_rescan.setToolTip("Look for displays again (after a display was added or removed)")
        self.btn_rescan.clicked.connect(lambda _checked=False: self.rescan_requested.emit())
        for button in (self.btn_start_all, self.btn_stop_all, self.btn_rescan):
            bar.addWidget(button)
        lay.addWidget(bar_w)

        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        lay.addWidget(self.body, 1)

        self.empty = QLabel("Looking for this device's displays…")
        self.empty.setObjectName("emptyState")
        self.empty.setAlignment(Qt.AlignCenter)
        self.grid.addWidget(self.empty, 0, 0)
        self._sync_buttons()

        self._next_timer = QTimer(self)
        self._next_timer.setSingleShot(True)
        self._next_timer.timeout.connect(self._start_next)
        # Checked from the event loop, once the panel has settled: a scrcpy
        # panel is briefly idle between a failed attempt and its retry.
        self._idle_check = QTimer(self)
        self._idle_check.setSingleShot(True)
        self._idle_check.timeout.connect(self._advance_past_idle_tile)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._relayout)
        # Esc restores the grid while one display is maximized and the keyboard
        # focus is inside this tab (a screen that has the keyboard keeps Esc as
        # a device key: it takes ShortcutOverride). Maximizing moves the focus
        # here when it was elsewhere, so Esc works straight away.
        self._esc_restore = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._esc_restore.setContext(Qt.WidgetWithChildrenShortcut)
        self._esc_restore.setEnabled(False)
        self._esc_restore.activated.connect(self.restore_grid)

    # ---- displays ----
    def set_displays(self, displays) -> None:
        """Show these displays; tiles of displays that are still there keep running."""
        if self._closing:
            return
        wanted = [dict(d) for d in (displays or []) if isinstance(d, dict) and "id" in d]
        wanted.sort(key=lambda d: int(d.get("id", 0) or 0))
        if not wanted and self._tiles:
            # ``list_displays`` legitimately answers with nothing (a head unit
            # that is slow to reply, or an adb hiccup during a Rescan). Dropping
            # every tile for that would kill three live screens, so keep them.
            count = len(self._tiles)
            self.summary.setText(
                f"{count} display{'s' if count != 1 else ''} — the last scan found none"
            )
            self.log.emit(
                "[WARNING] displays: that scan found none; keeping the displays already shown"
            )
            return
        new_ids = [int(d["id"]) for d in wanted]
        for tile in list(self._tiles):
            if tile.display_id not in new_ids:
                self._remove_tile(tile)
        existing = {tile.display_id: tile for tile in self._tiles}
        tiles = []
        for display in wanted:
            tile = existing.get(int(display["id"]))
            if tile is None:
                tile = _DisplayTile(
                    self.handler,
                    self.session,
                    display,
                    self.automotive,
                    self._dispatcher,
                    self._set_focus,
                    self.body,
                )
                tile.panel.log.connect(self.log)
                tile.panel.screen_ready.connect(lambda t=tile: self._on_tile_ready(t))
                tile.panel.display_shape_changed.connect(self._schedule_layout)
                # a session that ends on its own also changes Start all / Stop all,
                # and a screen that ended or failed must not hold up the next one
                tile.panel.video_active_changed.connect(
                    lambda on, t=tile: self._on_tile_active_changed(t, on)
                )
                tile.panel.screen_failed.connect(lambda _message: self._idle_check.start(0))
            else:
                tile.update_display(display)
            tiles.append(tile)
        self._tiles = tiles
        self._apply_video_budget()
        self._set_tiles_paused(not self.isVisible())
        count = len(tiles)
        if count:
            self.summary.setText(f"{count} display{'s' if count != 1 else ''}")
        else:
            self.summary.setText("No displays found")
            self.empty.setText("No displays found — press Rescan to look again.")
        self.empty.setVisible(not count)
        self._sync_buttons()
        self._columns = 0
        self._relayout()
        if self._auto_started:
            if not self._stopped_by_user:
                self.start_all()  # displays found later join the running wall
        elif self.isVisible():
            self._auto_start()

    def _remove_tile(self, tile) -> None:
        """Take a tile off the wall and close its screen.

        The tile itself is only hidden: the scrcpy window is closed on a parked
        worker thread while it is still a Win32 child of the panel's embed
        container, so destroying the widget here would pull that HWND out from
        under a live scrcpy. The tile is deleted once the stop reports back.
        """
        if tile is self._focused:
            self._focused = None  # the others show again (set_displays relayouts)
            self._esc_restore.setEnabled(False)
        if tile in self._queue:
            self._queue.remove(tile)
        if tile is self._waiting:
            self._waiting = None
        self.grid.removeWidget(tile)
        tile.hide()
        self._retiring.append(tile)
        try:
            tile.panel.screen_stopped.connect(lambda t=tile: self._tile_retired(t))
            tile.panel.close_panel()
        except Exception:
            pass
        if not tile.panel.is_active():
            self._tile_retired(tile)  # nothing was running: nothing to wait for

    def _tile_retired(self, tile) -> None:
        """Its screen has stopped — the hidden tile can finally be destroyed."""
        if tile not in self._retiring:
            return
        self._retiring.remove(tile)
        tile.setParent(None)
        tile.deleteLater()

    def tile_bit_rate(self, count=None) -> str:
        """Each tile's scrcpy bit-rate cap: the wall's budget split between
        its tiles, at least TILE_MIN_BIT_RATE."""
        count = max(1, len(self._tiles) if count is None else int(count))
        total = int(_bitrate_to_bps(self.WALL_BIT_RATE))
        floor = int(_bitrate_to_bps(self.TILE_MIN_BIT_RATE))
        share = max(floor, total // count)
        return f"{max(1, share // 1_000_000)}M"

    def _apply_video_budget(self) -> None:
        rate = self.tile_bit_rate()
        for tile in self._tiles:
            try:
                tile.panel.set_video_budget(bit_rate=rate)
            except RuntimeError:
                pass

    def set_automotive(self, automotive) -> None:
        self.automotive = bool(automotive)
        for tile in self._tiles:
            tile.panel.set_automotive_default(self.automotive)

    def _sync_buttons(self) -> None:
        """Start all / Stop all follow what the tiles are actually doing, so
        neither stays enabled when it has nothing left to do."""
        if self._closing:
            return
        tiles = self._tiles
        active = sum(1 for tile in tiles if tile.panel.is_active())
        self.btn_start_all.setEnabled(active < len(tiles))
        self.btn_stop_all.setEnabled(active > 0)

    # ---- starting and stopping ----
    def showEvent(self, event):
        super().showEvent(event)
        self._set_tiles_paused(False)
        self._schedule_layout()
        self._auto_start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._set_tiles_paused(True)

    def _set_tiles_paused(self, paused) -> None:
        """Another tab is showing (or this one is again): slow down — or restore
        — each tile's re-fit and process polling. The screens keep running, so
        coming back to the tab is immediate."""
        for tile in self._tiles:
            try:
                tile.panel.set_polling_paused(paused)
            except RuntimeError:  # the tile is being destroyed
                pass

    def _auto_start(self) -> None:
        if self._auto_started or not self._tiles or self._closing:
            return
        self._auto_started = True
        # let the tab paint and size its tiles before the first screen embeds
        QTimer.singleShot(150, self.start_all)

    def start_all(self) -> None:
        """Start every display that isn't showing yet, one after another."""
        if self._closing:
            return
        self._auto_started = True
        self._stopped_by_user = False
        self._queue = [tile for tile in self._tiles if not tile.panel.is_active()]
        if self._waiting is None:
            self._start_next()
        self._sync_buttons()

    def _start_next(self) -> None:
        self._next_timer.stop()
        self._waiting = None
        while self._queue and not self._closing:
            tile = self._queue.pop(0)
            if tile not in self._tiles or tile.panel.is_active():
                continue
            self._waiting = tile
            tile.panel.start()
            self._next_timer.start(self.START_TIMEOUT_MS)
            self._idle_check.start(0)  # a start that was refused at once
            self._sync_buttons()
            return
        self._sync_buttons()

    def _on_tile_ready(self, tile) -> None:
        self._sync_buttons()
        if tile is self._waiting:
            self._waiting = None
            self._next_timer.start(self.START_GAP_MS)

    def _on_tile_active_changed(self, tile, active) -> None:
        self._sync_buttons()
        if not active and tile is self._waiting and not self._closing:
            self._idle_check.start(0)

    def _advance_past_idle_tile(self) -> None:
        """The screen the start chain waits for ended or failed (a screencap
        error is final within a second): start the next display after the
        usual gap instead of waiting START_TIMEOUT_MS for it."""
        tile = self._waiting
        if tile is None or self._closing:
            return
        try:
            idle = not tile.panel.is_active()
        except RuntimeError:
            idle = True
        if idle:
            self._waiting = None
            self._next_timer.start(self.START_GAP_MS)

    def stop_all(self) -> None:
        self._stopped_by_user = True
        self._queue = []
        self._waiting = None
        self._next_timer.stop()
        for tile in self._tiles:
            if tile.panel.is_active():
                tile.panel.stop()
        self._sync_buttons()

    # ---- layout ----
    def maximized_tile(self):
        """The tile shown alone (Maximize), or None while the grid shows."""
        return self._focused

    def maximize_tile(self, tile) -> None:
        """Show *tile* alone, filling the wall; None restores the grid."""
        self._set_focus(tile)

    def restore_grid(self) -> None:
        self._set_focus(None)

    def _set_focus(self, tile) -> None:
        self._focused = tile if tile in self._tiles else None
        for other in self._tiles:
            other.sync_max_button(other is self._focused)
        self._esc_restore.setEnabled(self._focused is not None)
        self._columns = 0
        self._relayout()
        if self._focused is not None:
            self._focus_for_esc(self._focused)
        # re-fit embedded screens to their new tile size at once, rather than
        # on their next poll
        for shown in [self._focused] if self._focused is not None else list(self._tiles):
            QTimer.singleShot(0, shown.panel._fit)

    def _focus_for_esc(self, tile) -> None:
        """Keyboard focus outside this tab moves to the tile's Restore button
        (focus already inside, e.g. on a screen typing to the device, stays)."""
        from PyQt5.QtWidgets import QApplication

        if self._closing or not self.isVisible():
            return
        focus = QApplication.focusWidget()
        if focus is not None and (focus is self or self.isAncestorOf(focus)):
            return
        tile.btn_focus.setFocus(Qt.OtherFocusReason)

    def _tile_header(self) -> int:
        """Height a tile loses to its title row and screen toolbar. Measured
        from a laid-out tile (the toolbar wraps, so it is not a fixed number);
        TILE_HEADER stands in until the first layout has happened."""
        for tile in self._tiles:
            header = tile.header_height()
            if header > 0:
                return header
        return self.TILE_HEADER

    def _schedule_layout(self, *_args) -> None:
        if not self._closing:
            self._layout_timer.start(50)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_layout()

    def _relayout(self) -> None:
        """Place the shown tiles in the column count that shows them largest."""
        if self._closing:
            return
        shown = [self._focused] if self._focused is not None else list(self._tiles)
        for tile in self._tiles:
            tile.setVisible(tile in shown)
        if not shown:
            return
        aspect = sum(tile.aspect() for tile in shown) / len(shown)
        cols = best_columns(
            len(shown), self.body.width(), self.body.height(), aspect, self._tile_header()
        )
        order = [id(tile) for tile in shown]
        if cols == self._columns and order == self._placed:
            return
        self._columns, self._placed = cols, order
        for tile in self._tiles:
            self.grid.removeWidget(tile)
        for column in range(self.grid.columnCount()):
            self.grid.setColumnStretch(column, 0)
        for row in range(self.grid.rowCount()):
            self.grid.setRowStretch(row, 0)
        for index, tile in enumerate(shown):
            self.grid.addWidget(tile, index // cols, index % cols)
        for column in range(cols):
            self.grid.setColumnStretch(column, 1)
        for row in range(math.ceil(len(shown) / cols)):
            self.grid.setRowStretch(row, 1)

    # ---- teardown ----
    def close_panel(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._esc_restore.setEnabled(False)
        self._next_timer.stop()
        self._idle_check.stop()
        self._layout_timer.stop()
        self._queue = []
        self._waiting = None
        for tile in self._tiles:
            try:
                tile.panel.close_panel()
            except Exception:
                pass

    def shutdown_threads(self):
        """Workers that must finish before the ADB handler is disconnected."""
        threads = []
        # A removed tile is still closing its own screen: its stop worker counts.
        for tile in self._tiles + self._retiring:
            try:
                threads.extend(tile.panel.shutdown_threads())
            except RuntimeError:
                pass
        return threads
