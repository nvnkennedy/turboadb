"""Every display of one device, live and controllable, in one tab.

Multi-display devices (an Android Automotive head unit's centre stack,
instrument cluster and passenger screen) get one screen tile per display. Each
tile is a full MirrorPanel locked to its display: embedded screen, typing,
screenshot and recording. The tiles start one after another the first time the
tab is shown, so the device never has to start several scrcpy servers at once.
"""

from __future__ import annotations

import math

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .flowlayout import ToolbarFlowLayout
from .mirror_panel import MirrorPanel, _parse_display_size, display_label


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
    """One display: a title row and a MirrorPanel locked to that display."""

    def __init__(self, handler, session, display, automotive, dispatcher, on_focus, parent=None):
        super().__init__(parent)
        self.setObjectName("iviPreviewTile")
        self.display = dict(display)
        self.display_id = int(display.get("id", 0) or 0)

        v = QVBoxLayout(self)
        v.setContentsMargins(1, 1, 1, 1)
        v.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(12, 8, 8, 0)
        head.setSpacing(8)
        self.title = QLabel(display_label(display))
        self.title.setObjectName("iviPreviewTitle")
        head.addWidget(self.title, 1)
        self.btn_focus = QPushButton("Focus")
        self.btn_focus.setProperty("role", "ghost")
        self.btn_focus.setCheckable(True)
        self.btn_focus.setIcon(icons.icon("maximize", "accent"))
        self.btn_focus.setToolTip("Show only this display here (click again to show all)")
        self.btn_focus.toggled.connect(lambda on: on_focus(self if on else None))
        head.addWidget(self.btn_focus)
        v.addLayout(head)

        self.panel = MirrorPanel(
            handler, session, automotive=automotive, prefer_embed=True, dispatcher=dispatcher
        )
        self.panel.set_fixed_display(display)
        v.addWidget(self.panel, 1)

    def aspect(self) -> float:
        """The display's width / height: the real video once shown, else its size."""
        if self.panel._video_aspect:
            return float(self.panel._video_aspect)
        size = _parse_display_size(self.display.get("size"))
        return size[0] / size[1] if size else 16.0 / 9.0

    def update_display(self, display) -> None:
        self.display = dict(display)
        self.title.setText(display_label(display))


class DisplayWall(QWidget):
    """The (IVI) Displays tab: every display of the device at once."""

    log = pyqtSignal(str)
    rescan_requested = pyqtSignal()

    START_GAP_MS = 500  # after one screen is up, before starting the next
    START_TIMEOUT_MS = 15000  # move on if a screen never comes up
    TILE_HEADER = 96  # a tile's title row + screen toolbar

    def __init__(self, handler, session, automotive=False, dispatcher=None, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.session = session
        self.automotive = bool(automotive)
        self._dispatcher = dispatcher
        self._tiles = []
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
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._relayout)

    # ---- displays ----
    def set_displays(self, displays) -> None:
        """Show these displays; tiles of displays that are still there keep running."""
        if self._closing:
            return
        wanted = [dict(d) for d in (displays or []) if isinstance(d, dict) and "id" in d]
        wanted.sort(key=lambda d: int(d.get("id", 0) or 0))
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
            else:
                tile.update_display(display)
            tiles.append(tile)
        self._tiles = tiles
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
        if tile is self._focused:
            self._focused = None
        if tile in self._queue:
            self._queue.remove(tile)
        if tile is self._waiting:
            self._waiting = None
        self.grid.removeWidget(tile)
        try:
            tile.panel.close_panel()
        except Exception:
            pass
        tile.hide()
        tile.deleteLater()

    def set_automotive(self, automotive) -> None:
        self.automotive = bool(automotive)
        for tile in self._tiles:
            tile.panel.set_automotive_default(self.automotive)

    def _sync_buttons(self) -> None:
        has_tiles = bool(self._tiles)
        self.btn_start_all.setEnabled(has_tiles)
        self.btn_stop_all.setEnabled(has_tiles)

    # ---- starting and stopping ----
    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_layout()
        self._auto_start()

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
            return

    def _on_tile_ready(self, tile) -> None:
        if tile is self._waiting:
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

    # ---- layout ----
    def _set_focus(self, tile) -> None:
        self._focused = tile if tile in self._tiles else None
        for other in self._tiles:
            if other is not tile:
                other.btn_focus.blockSignals(True)
                other.btn_focus.setChecked(False)
                other.btn_focus.blockSignals(False)
        self._columns = 0
        self._relayout()

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
            len(shown), self.body.width(), self.body.height(), aspect, self.TILE_HEADER
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
        self._next_timer.stop()
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
        for tile in self._tiles:
            try:
                threads.extend(tile.panel.shutdown_threads())
            except RuntimeError:
                pass
        return threads
