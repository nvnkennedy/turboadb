"""The Files page's transfer panel: live progress and the full history.

Renders a :class:`~turboadb.gui.transfer_log.TransferLog`.  The table is a
model/view (not one widget per row), so thousands of rows stay cheap, and a
refresh only repaints the rows that changed — a selection survives a live
batch.  Repaints are coalesced: a burst of progress signals costs one render.
"""

from __future__ import annotations

import time

from PyQt5.QtCore import (
    QAbstractTableModel, QModelIndex, QRect, QSortFilterProxyModel, Qt, QTimer, pyqtSignal,
)
from PyQt5.QtGui import QColor, QKeySequence
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QMenu,
    QProgressBar, QPushButton, QShortcut, QSplitter, QStyledItemDelegate, QTableView,
    QToolButton,
    QVBoxLayout, QWidget,
)

from . import theme
from .fileutil import copy_to_clipboard, save_output, write_text_file
from .icons import icon
from .qtutil import cached_icon
from .transfer_log import (
    ACTIVE, CANCELLED, DIRECTION_LABELS, DONE, FAILED, QUEUED, RUNNING,
    TransferStats, human_bytes, human_duration, human_speed,
)

COLUMNS = ("Status", "Name", "Direction", "Size", "Progress", "Speed", "Time", "Details")
(COL_STATUS, COL_NAME, COL_DIRECTION, COL_SIZE, COL_PROGRESS, COL_SPEED, COL_TIME,
 COL_DETAILS) = range(len(COLUMNS))

ITEM_ID_ROLE = Qt.UserRole + 1
STATUS_ROLE = Qt.UserRole + 2
PERCENT_ROLE = Qt.UserRole + 3

_STATUS_TEXT = {
    QUEUED: "Queued", RUNNING: "Copying", DONE: "Done", FAILED: "Failed",
    CANCELLED: "Cancelled",
}
_STATUS_ICON = {
    QUEUED: ("clock", "dim"), RUNNING: ("play", "accent"), DONE: ("check", "green"),
    FAILED: ("alert", "red"), CANCELLED: ("x", "dim"),
}
_RIGHT = Qt.AlignRight | Qt.AlignVCenter
_NUMERIC = (COL_SIZE, COL_PROGRESS, COL_SPEED, COL_TIME)

# (label, statuses shown); None shows everything
FILTERS = (
    ("All", None),
    ("Active", ACTIVE),
    ("Failed", frozenset((FAILED, CANCELLED))),
    ("Done", frozenset((DONE,))),
)


def summary_text(stats: TransferStats) -> str:
    """The one line under the overall bar, e.g.
    ``Pushing 24 of 57 · 1.2 GB of 2.9 GB · 18.4 MB/s · about 1:32 left``."""
    if not stats.total:
        return ""
    parts = []
    if stats.active:
        current = stats.processed + (1 if stats.running else 0)
        parts.append(f"{stats.verb} {max(1, current)} of {stats.total}")
    else:
        noun = "item" if stats.total == 1 else "items"
        parts.append(f"Last batch: {stats.done} of {stats.total} {noun} {stats.past.lower()}")
    if stats.bytes_total is not None:
        total = ("~" if stats.bytes_estimated else "") + human_bytes(stats.bytes_total)
        parts.append(f"{human_bytes(stats.bytes_done)} of {total}")
    elif stats.active and stats.measuring:
        parts.append(f"measuring {stats.measuring} item{'s' if stats.measuring != 1 else ''}…")
    if stats.speed > 0:
        parts.append(human_speed(stats.speed))
    if stats.active and stats.eta is not None:
        parts.append(f"about {human_duration(stats.eta)} left")
    elif not stats.active and stats.elapsed:
        parts.append(f"in {human_duration(stats.elapsed)}")
    if stats.failed:
        parts.append(f"{stats.failed} failed")
    if stats.cancelled:
        parts.append(f"{stats.cancelled} cancelled")
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
class TransferTableModel(QAbstractTableModel):
    """Rows are the log's items in history order (oldest first)."""

    def __init__(self, log, parent=None):
        super().__init__(parent)
        self._log = log
        self._ids = []  # row -> item id
        self._rev = {}  # item id -> revision last announced to the view
        self._structure = None
        self._now = log.now()

    # ---- Qt model API ------------------------------------------------------
    def rowCount(self, parent=QModelIndex()):  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self._ids)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and 0 <= section < len(COLUMNS):
            return COLUMNS[section]
        if orientation == Qt.Horizontal and role == Qt.TextAlignmentRole and section in _NUMERIC:
            return _RIGHT
        return None

    def item_at(self, row: int):
        if 0 <= row < len(self._ids):
            return self._log.get(self._ids[row])
        return None

    def row_of(self, item_id: int) -> int:
        try:
            return self._ids.index(item_id)
        except ValueError:
            return -1

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.item_at(index.row())
        if item is None:
            return None
        column = index.column()
        if role == Qt.DisplayRole:
            return self._display(item, column)
        if role == ITEM_ID_ROLE:
            return item.id
        if role == STATUS_ROLE:
            return item.status
        if role == PERCENT_ROLE:
            return item.percent
        if role == Qt.DecorationRole and column == COL_STATUS:
            return cached_icon(*_STATUS_ICON.get(item.status, ("info", "dim")))
        if role == Qt.TextAlignmentRole and column in _NUMERIC:
            return _RIGHT
        if role == Qt.ForegroundRole:
            colours = theme.palette()
            if item.status == FAILED:
                return QColor(colours["danger_text"])
            if item.status in (QUEUED, CANCELLED):
                return QColor(colours["dim"])
            return None
        if role == Qt.ToolTipRole:
            return self._tooltip(item)
        return None

    def _display(self, item, column):
        now = self._now
        if column == COL_STATUS:
            return _STATUS_TEXT.get(item.status, item.status)
        if column == COL_NAME:
            return item.name
        if column == COL_DIRECTION:
            return DIRECTION_LABELS.get(item.direction, item.direction)
        if column == COL_SIZE:
            if item.size is None:
                return "measuring…" if item.active else "—"
            text = ("~" if not item.size_exact else "") + human_bytes(item.size)
            if item.files and item.files > 1:
                text += f" · {item.files} files"
            return text
        if column == COL_PROGRESS:
            if item.status == QUEUED:
                return ""
            if item.status == CANCELLED and item.started_at is None:
                return ""
            return f"{item.percent}%"
        if column == COL_SPEED:
            return human_speed(item.speed(now)) if item.status in (RUNNING, DONE) else ""
        if column == COL_TIME:
            elapsed = item.elapsed(now)
            if elapsed is None:
                return ""
            # "0:00" said nothing about a photo copied in a fraction of a second
            return "< 1 s" if elapsed < 1 else human_duration(elapsed)
        if column == COL_DETAILS:
            return item.error if item.status == FAILED and item.error else item.dst
        return None

    def _tooltip(self, item):
        lines = [item.name, f"{item.src}  →  {item.dst}",
                 f"{DIRECTION_LABELS.get(item.direction, item.direction)} · "
                 f"{_STATUS_TEXT.get(item.status, item.status)}"]
        if item.size is not None:
            lines.append(("About " if not item.size_exact else "") + human_bytes(item.size)
                         + (f" in {item.files} files" if item.files and item.files > 1 else ""))
        if item.error:
            lines.append(f"Error: {item.error}")
        return "\n".join(lines)

    # ---- keeping up with the log -------------------------------------------
    def sync(self) -> bool:
        """Bring the rows up to date with the log; True when anything changed.

        Items appended at the end become inserted rows (a selection survives);
        anything else that restructures the list (clear, remove, retry) resets
        the view.  Otherwise only rows whose item changed — and running rows,
        whose time and speed move on their own — are repainted."""
        self._now = self._log.now()
        changed = False
        if self._log.structure != self._structure:
            ids = [item.id for item in self._log]
            old = self._ids
            if len(ids) >= len(old) and ids[:len(old)] == old:
                if len(ids) > len(old):
                    self.beginInsertRows(QModelIndex(), len(old), len(ids) - 1)
                    self._ids = ids
                    self.endInsertRows()
            else:
                self.beginResetModel()
                self._ids = ids
                self._rev = {}
                self.endResetModel()
            self._structure = self._log.structure
            changed = True
        first = last = -1
        for row, item_id in enumerate(self._ids):
            item = self._log.get(item_id)
            if item is None:
                continue
            if item.rev != self._rev.get(item_id) or item.status == RUNNING:
                self._rev[item_id] = item.rev
                if first < 0:
                    first = row
                last = row
        if first >= 0:
            self.dataChanged.emit(self.index(first, 0), self.index(last, len(COLUMNS) - 1))
            changed = True
        return changed


class _StatusFilter(QSortFilterProxyModel):
    """Show all rows, or only the rows in some statuses."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._statuses = None
        self.setDynamicSortFilter(True)

    def set_statuses(self, statuses) -> None:
        self._statuses = statuses
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):  # noqa: N802 - Qt API
        if not self._statuses:
            return True
        source = self.sourceModel()
        return source.data(source.index(row, 0, parent), STATUS_ROLE) in self._statuses


class _ProgressDelegate(QStyledItemDelegate):
    """The percentage, with a thin bar along the bottom of the cell."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        status = index.data(STATUS_ROLE)
        percent = index.data(PERCENT_ROLE) or 0
        if status not in (RUNNING, DONE, FAILED) or (status != DONE and not percent):
            return
        colours = theme.palette()
        rect = option.rect.adjusted(6, option.rect.height() - 5, -6, -2)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        fill = {DONE: "ok_text", FAILED: "danger_text"}.get(status, "accent")
        width = int(rect.width() * max(0, min(100, percent if status != DONE else 100)) / 100)
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colours["border"]))
        painter.drawRect(rect)
        if width > 0:
            painter.setBrush(QColor(colours[fill]))
            painter.drawRect(QRect(rect.x(), rect.y(), width, rect.height()))
        painter.restore()


# --------------------------------------------------------------------------- #
# the panel
# --------------------------------------------------------------------------- #
class TransferPanel(QFrame):
    """Overall progress, the running transfer and the history of a Files page."""

    cancel_requested = pyqtSignal()
    retry_requested = pyqtSignal(object)  # list of item ids
    open_requested = pyqtSignal(object)  # a TransferItem
    details_toggled = pyqtSignal(bool)

    RENDER_MS = 150  # coalesce a burst of progress signals into one repaint
    TICK_MS = 1000  # while active: time, speed and "left" keep moving

    def __init__(self, log, parent=None):
        super().__init__(parent)
        self.setObjectName("transferPanel")
        self._log = log
        self._closed = False
        self._user_chose_details = False
        self._auto_batch = 0  # the batch that last auto-expanded the details
        self._running_id = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 6, 0, 0)
        outer.setSpacing(4)

        # header: overall progress and actions
        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("Transfers")
        title.setObjectName("paneTitle")
        head.addWidget(title)
        self.overall = QProgressBar()
        self.overall.setObjectName("transferOverall")
        self.overall.setRange(0, 1000)
        self.overall.setTextVisible(True)
        self.overall.setFormat("%p%")
        self.overall.setMaximumHeight(18)
        head.addWidget(self.overall, 1)
        self.btn_retry = QPushButton()
        self.btn_retry.setProperty("role", "ghost")
        self.btn_retry.setIcon(icon("refresh", "amber"))
        self.btn_retry.setToolTip("Queue every failed or cancelled transfer again")
        self.btn_retry.setFocusPolicy(Qt.TabFocus)
        self.btn_retry.clicked.connect(self._retry_failed)
        self.btn_retry.hide()
        head.addWidget(self.btn_retry)
        self.btn_details = QToolButton()
        self.btn_details.setText("Details")
        self.btn_details.setCheckable(True)
        self.btn_details.setIcon(icon("list", "blue"))
        self.btn_details.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_details.setToolTip("Show every transfer: size, progress, speed and errors")
        self.btn_details.setFocusPolicy(Qt.TabFocus)
        self.btn_details.toggled.connect(self._on_details_clicked)
        head.addWidget(self.btn_details)
        self.btn_menu = QToolButton()
        self.btn_menu.setIcon(icon("more", "dim"))
        self.btn_menu.setToolTip("Transfer actions")
        self.btn_menu.setPopupMode(QToolButton.InstantPopup)
        self.btn_menu.setFocusPolicy(Qt.TabFocus)
        self._menu = QMenu(self.btn_menu)
        self._menu.aboutToShow.connect(self._fill_menu)
        self.btn_menu.setMenu(self._menu)
        head.addWidget(self.btn_menu)
        outer.addLayout(head)

        self.summary = QLabel("")
        self.summary.setObjectName("mutedHint")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        outer.addWidget(self.summary)

        # the running transfer's own bar and Cancel (the Files page hands them in)
        self._current = QVBoxLayout()
        self._current.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self._current)

        # details: filter and table
        self.details = QWidget()
        details = QVBoxLayout(self.details)
        details.setContentsMargins(0, 2, 0, 0)
        details.setSpacing(4)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        show = QLabel("Show")
        show.setObjectName("mutedHint")
        filter_row.addWidget(show)
        self.filter = QComboBox()
        for label, _statuses in FILTERS:
            self.filter.addItem(label)
        self.filter.setToolTip("Show every transfer, or only active, failed or finished ones")
        self.filter.currentIndexChanged.connect(self._on_filter)
        filter_row.addWidget(self.filter)
        self.counts = QLabel("")
        self.counts.setObjectName("mutedHint")
        filter_row.addWidget(self.counts, 1)
        details.addLayout(filter_row)

        self.model = TransferTableModel(log, self)
        self._proxy = _StatusFilter(self)
        self._proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setObjectName("transferTable")
        self.table.setModel(self._proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setItemDelegateForColumn(COL_PROGRESS, _ProgressDelegate(self.table))
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setHighlightSections(False)
        for column, width in ((COL_STATUS, 96), (COL_NAME, 220), (COL_DIRECTION, 104),
                              (COL_SIZE, 110), (COL_PROGRESS, 76), (COL_SPEED, 92),
                              (COL_TIME, 64)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            header.resizeSection(column, width)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(self._on_double_click)
        QShortcut(QKeySequence.Delete, self.table, self._remove_selected,
                  context=Qt.WidgetShortcut)
        details.addWidget(self.table, 1)
        self.details.hide()
        outer.addWidget(self.details, 1)

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(self.RENDER_MS)
        self._render_timer.timeout.connect(self.render)
        self._tick = QTimer(self)
        self._tick.setInterval(self.TICK_MS)
        self._tick.timeout.connect(self.render)

        self.setVisible(False)
        self._fit_height()

    # ---- wiring ------------------------------------------------------------
    def set_current_widget(self, widget: QWidget) -> None:
        """Place the running transfer's own row (its bar and Cancel) here."""
        self._current.addWidget(widget)

    def close_panel(self) -> None:
        self._closed = True
        self._render_timer.stop()
        self._tick.stop()

    @property
    def details_shown(self) -> bool:
        return self.btn_details.isChecked()

    def set_details_shown(self, shown: bool, *, by_user: bool = False) -> None:
        if by_user:
            self._user_chose_details = True
        if self.btn_details.isChecked() != bool(shown):
            self.btn_details.blockSignals(True)
            self.btn_details.setChecked(bool(shown))
            self.btn_details.blockSignals(False)
        self.details.setVisible(bool(shown))
        self._fit_height()
        self.details_toggled.emit(bool(shown))
        if shown:
            self.render()

    def _on_details_clicked(self, checked: bool) -> None:
        self.set_details_shown(checked, by_user=True)

    def _fit_height(self) -> None:
        """Collapsed, the panel is only as tall as its header rows, and the
        splitter slot it sits in shrinks to match (Qt leaves a capped child's
        spare room empty rather than handing it back to the panes). The height
        changes while collapsed too: the running transfer's bar comes and goes,
        and the summary wraps on a narrow window."""
        if self.details.isVisible() or self.btn_details.isChecked():
            self.setMaximumHeight(16777215)
            return
        layout = self.layout()
        width = self.width()
        if width > 0 and layout.hasHeightForWidth():
            cap = layout.totalHeightForWidth(width)
        else:
            cap = self.sizeHint().height()
        cap = max(1, cap)
        self.setMaximumHeight(cap)
        splitter = self.parentWidget()
        if self.isHidden() or not isinstance(splitter, QSplitter):
            return
        index = splitter.indexOf(self)
        sizes = splitter.sizes()
        if index < 0 or not sum(sizes) or sizes[index] == cap:
            return
        spare = sizes[index] - cap
        sizes[index] = cap
        other = 0 if index != 0 else 1  # the file panes take (or give) the rest
        if 0 <= other < len(sizes):
            sizes[other] = max(1, sizes[other] + spare)
        splitter.setSizes(sizes)

    # ---- refreshing --------------------------------------------------------
    def refresh(self) -> None:
        """Something in the log changed: show the panel now, render soon."""
        if self._closed:
            return
        visible = len(self._log) > 0
        if self.isHidden() == visible:
            self.setVisible(visible)
        if self._log.active and not self._tick.isActive():
            self._tick.start()
        if not self._render_timer.isActive():
            self._render_timer.start()

    def render(self) -> None:
        if self._closed:
            return
        self.model.sync()
        stats = self._log.stats()
        self.setVisible(len(self._log) > 0)
        if not stats.active:
            self._tick.stop()
        self.overall.setValue(int(round(stats.fraction * 1000)))
        self.overall.setEnabled(stats.total > 0)
        self.summary.setText(summary_text(stats))
        retry = len(self._log.retryable())
        self.btn_retry.setText(f"Retry {retry} failed" if retry != 1 else "Retry 1 failed")
        self.btn_retry.setVisible(retry > 0 and not stats.active)
        self._update_counts()
        self._auto_expand(stats)
        self._follow_running()
        self._fit_height()

    def _update_counts(self) -> None:
        items = self._log.items
        done = sum(1 for item in items if item.status == DONE)
        failed = sum(1 for item in items if item.status in (FAILED, CANCELLED))
        active = sum(1 for item in items if item.active)
        bits = [f"{len(items)} in history"]
        if active:
            bits.append(f"{active} active")
        bits.append(f"{done} done")
        if failed:
            bits.append(f"{failed} failed or cancelled")
        self.counts.setText(" · ".join(bits))

    def _auto_expand(self, stats) -> None:
        """Open the details for a new batch of several items, and for a batch
        that ended with failures (a quick batch of small files can finish
        before the first render) — unless the user already chose, in which
        case their choice stands."""
        batch = self._log.batch
        if (self._user_chose_details or batch == self._auto_batch or stats.total < 2
                or not (stats.active or stats.failed)):
            return
        self._auto_batch = batch
        if not self.details_shown:
            self.set_details_shown(True)

    def _follow_running(self) -> None:
        """Keep the running row in view when it changes, unless the user is
        looking at a selection of their own."""
        running = next((item.id for item in self._log.batch_items()
                        if item.status == RUNNING), None)
        if running == self._running_id:
            return
        self._running_id = running
        if running is None or not self.table.isVisible():
            return
        if self.table.selectionModel().hasSelection():
            return
        source_row = self.model.row_of(running)
        if source_row < 0:
            return
        index = self._proxy.mapFromSource(self.model.index(source_row, 0))
        if index.isValid():
            self.table.scrollTo(index, QAbstractItemView.EnsureVisible)

    # ---- actions -----------------------------------------------------------
    def _on_filter(self, index: int) -> None:
        self._proxy.set_statuses(FILTERS[index][1] if 0 <= index < len(FILTERS) else None)

    def selected_items(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        items = []
        for row in rows:
            source = self._proxy.mapToSource(self._proxy.index(row, 0))
            item = self.model.item_at(source.row())
            if item is not None:
                items.append(item)
        return items

    def _retry_failed(self) -> None:
        ids = self._log.retryable()
        if ids:
            self.retry_requested.emit(ids)

    def _clear_finished(self) -> None:
        if self._log.clear_finished():
            self.refresh()
            self.render()

    def _remove_selected(self) -> None:
        ids = [item.id for item in self.selected_items() if item.finished]
        if ids and self._log.remove(ids):
            self.render()

    def _copy_report(self) -> None:
        copy_to_clipboard(self, self._log.report(), "the transfer report")

    def _save_report(self) -> None:
        text = self._log.report()  # snapshot now: the write runs on a worker
        save_output(
            self, "Save transfer report",
            f"turboadb-transfers-{time.strftime('%Y%m%d-%H%M%S')}.txt",
            lambda path: write_text_file(path, text),
            what="transfer report", file_filter="Text files (*.txt);;All files (*)",
            suffix=".txt",
        )

    def _fill_menu(self) -> None:
        menu = self._menu
        menu.clear()
        active = self._log.active
        retry = len(self._log.retryable())
        if active:
            menu.addAction(icon("stop", "red"), "Cancel all transfers", self.cancel_requested.emit)
        act = menu.addAction(icon("refresh", "amber"),
                             f"Retry failed ({retry})" if retry else "Retry failed",
                             self._retry_failed)
        act.setEnabled(retry > 0)
        finished = sum(1 for item in self._log.items if item.finished)
        act = menu.addAction(icon("clear", "dim"), "Clear finished", self._clear_finished)
        act.setEnabled(finished > 0)
        menu.addSeparator()
        has_items = len(self._log) > 0
        act = menu.addAction(icon("copy", "blue"), "Copy report", self._copy_report)
        act.setEnabled(has_items)
        act = menu.addAction(icon("save", "blue"), "Save report…", self._save_report)
        act.setEnabled(has_items)

    def _on_double_click(self, index) -> None:
        source = self._proxy.mapToSource(index)
        item = self.model.item_at(source.row())
        if item is not None:
            self.open_requested.emit(item)

    def _context_menu(self, pos) -> None:
        items = self.selected_items()
        if not items:
            return
        menu = QMenu(self.table)
        retry = [item.id for item in items if item.status in (FAILED, CANCELLED)]
        if retry:
            menu.addAction(icon("refresh", "amber"),
                           "Retry" if len(retry) == 1 else f"Retry {len(retry)} transfers",
                           lambda: self.retry_requested.emit(retry))
        if len(items) == 1:
            item = items[0]
            where = "on the device" if item.direction == "push" else "on this PC"
            menu.addAction(icon("folder", "blue"), f"Open location {where}",
                           lambda: self.open_requested.emit(item))
            menu.addAction(icon("copy", "dim"), "Copy source path",
                           lambda: copy_to_clipboard(self, item.src, "the source path"))
            menu.addAction(icon("copy", "dim"), "Copy destination path",
                           lambda: copy_to_clipboard(self, item.dst, "the destination path"))
            if item.error:
                menu.addAction(icon("copy", "dim"), "Copy error",
                               lambda: copy_to_clipboard(self, item.error, "the error"))
        finished = [item.id for item in items if item.finished]
        if finished:
            menu.addSeparator()
            menu.addAction(icon("trash", "dim"),
                           "Remove from list" if len(finished) == 1
                           else f"Remove {len(finished)} from list",
                           self._remove_selected)
        menu.exec_(self.table.viewport().mapToGlobal(pos))
        menu.deleteLater()
