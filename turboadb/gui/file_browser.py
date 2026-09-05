"""WinSCP-style dual-pane file manager for TurboADB:
- Left pane: Local PC with multi-drive selector (C:, D:, E: etc.)
- Right pane: Android device with permissions, owner, type, size, modified date
- Full WinSCP operations: New Folder, New File, Copy, Paste, Rename, Delete, Properties
- Bidirectional drag-and-drop between panes and from external Windows Explorer
- Sequential recursive background transfers with live progress reporting
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import shutil
import string
import time
import tempfile
from typing import List, Tuple

from PyQt5.QtCore import QThread, pyqtSignal, Qt, QUrl, QMimeData
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
                             QInputDialog, QMessageBox, QLabel, QProgressBar,
                             QSplitter, QFrame, QMenu, QShortcut, QComboBox,
                             QAbstractItemView, QDialog, QPlainTextEdit)
from PyQt5.QtGui import QKeySequence, QFont, QTextCursor, QDrag
from ..results import strip_ansi


_LS_REGEX = re.compile(
    r'^([bcdlpsw-][rwxstST-]{9})\s+'
    r'(\d+)\s+'
    r'(\S+)\s+'
    r'(\S+)\s+'
    r'(\d+)\s+'
    r'('
    r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}'
    r'|'
    r'[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{2}:\d{2}|\d{4})'
    r')\s+'
    r'(.+)$'
)


class _FileItem(QTableWidgetItem):
    """Custom table item that sorts numerically when size data is present."""
    def __lt__(self, other):
        if not isinstance(other, QTableWidgetItem):
            return super().__lt__(other)
        d1 = self.data(Qt.UserRole)
        d2 = other.data(Qt.UserRole)
        if isinstance(d1, (int, float)) and isinstance(d2, (int, float)):
            return d1 < d2
        return self.text().lower() < other.text().lower()


class _FileEditorDialog(QDialog):
    """Integrated text editor for local and remote files with line status and search."""
    def __init__(self, title: str, path: str, content: str, on_save, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit — {title}")
        self.resize(800, 580)
        self.path = path
        self.on_save = on_save
        self._dirty = False

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(QLabel(f"<b>File:</b> {path}"))
        top.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find in file… (Enter to next)")
        self.search.setFixedWidth(220)
        self.search.returnPressed.connect(self._find_next)
        top.addWidget(self.search)
        v.addLayout(top)

        self.edit = QPlainTextEdit()
        self.edit.setFont(QFont("Consolas", 10))
        self.edit.setPlainText(content)
        self.edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(self.edit, 1)

        bot = QHBoxLayout()
        self.status = QLabel()
        self._update_status()
        self.edit.textChanged.connect(self._on_text_changed)
        bot.addWidget(self.status)
        bot.addStretch()

        b_save = QPushButton("💾 Save")
        b_save.setProperty("role", "ok")
        b_save.clicked.connect(self._save)
        b_save_close = QPushButton("Save & Close")
        b_save_close.clicked.connect(self._save_and_close)
        b_cancel = QPushButton("Close")
        b_cancel.setProperty("role", "ghost")
        b_cancel.clicked.connect(self.reject)

        bot.addWidget(b_save)
        bot.addWidget(b_save_close)
        bot.addWidget(b_cancel)
        v.addLayout(bot)

        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: self.search.setFocus())

    def _on_text_changed(self):
        self._dirty = True
        self._update_status()

    def _update_status(self):
        lines = self.edit.blockCount()
        chars = len(self.edit.toPlainText())
        dirty_tag = " • Modified" if self._dirty else ""
        self.status.setText(f"{lines} lines  ·  {chars} characters{dirty_tag}")

    def _find_next(self):
        query = self.search.text()
        if not query:
            return
        if not self.edit.find(query):
            cursor = self.edit.textCursor()
            cursor.movePosition(QTextCursor.Start)
            self.edit.setTextCursor(cursor)
            self.edit.find(query)

    def _save(self) -> bool:
        if self.on_save:
            res = self.on_save(self.edit.toPlainText())
            if res is True:
                self._dirty = False
                self._update_status()
                return True
            return False
        return True

    def _save_and_close(self):
        if self._save():
            self.accept()

    def reject(self):
        if self._dirty:
            res = QMessageBox.question(
                self,
                "Unsaved changes",
                "You have unsaved changes. Discard them?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if res != QMessageBox.Yes:
                return
        super().reject()


class _FileTableWidget(QTableWidget):
    """Table widget with native bidirectional drag-and-drop and fixed '..' row 0."""
    dropped = pyqtSignal(list, bool, str)  # (paths: list[str], is_external: bool, target_dir: str)

    def __init__(self, is_remote: bool = False, parent=None):
        super().__init__(0, 6, parent)
        self.is_remote = is_remote
        self.base_dir = ""
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        self.horizontalHeader().setSortIndicatorShown(True)
        self.horizontalHeader().sectionClicked.connect(self._on_header_clicked)

    def startDrag(self, supportedActions):
        indexes = self.selectedIndexes()
        if not indexes:
            return
        items = [self.itemFromIndex(idx) for idx in indexes if idx.column() == 0]
        mime = self.mimeData(items)
        if not mime:
            return
        from PyQt5.QtGui import QDrag
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec_(Qt.CopyAction)

    def _on_header_clicked(self, col: int):
        header = self.horizontalHeader()
        cur_col = header.sortIndicatorSection()
        cur_order = header.sortIndicatorOrder()
        order = Qt.DescendingOrder if cur_col == col and cur_order == Qt.AscendingOrder else Qt.AscendingOrder
        header.setSortIndicator(col, order)
        self.sortByColumn(col, order)

    def sortByColumn(self, col: int, order: Qt.SortOrder = Qt.AscendingOrder):
        self.setSortingEnabled(False)
        dot_items = None
        rows_data = []

        for r in range(self.rowCount()):
            it0 = self.item(r, 0)
            text0 = it0.text() if it0 else ""
            data0 = it0.data(Qt.UserRole) if it0 else None

            is_dot = bool(data0 and isinstance(data0, (tuple, list)) and len(data0) > 0 and data0[0] == "..")
            if is_dot and dot_items is None:
                dot_items = [self.takeItem(r, c) for c in range(self.columnCount())]
                continue

            is_dir = data0[1] if (data0 and isinstance(data0, (tuple, list)) and len(data0) > 1) else False
            it_col = self.item(r, col)
            raw_val = it_col.data(Qt.UserRole) if it_col else None
            if raw_val is not None and isinstance(raw_val, (int, float)):
                key = raw_val
            elif it_col:
                key = it_col.text().lower()
            else:
                key = text0.lower()

            row_items = [self.takeItem(r, c) for c in range(self.columnCount())]
            rows_data.append((is_dir, key, row_items))

        self.setRowCount(0)
        reverse = (order == Qt.DescendingOrder)
        dirs = [x for x in rows_data if x[0]]
        files = [x for x in rows_data if not x[0]]
        dirs.sort(key=lambda x: x[1], reverse=reverse)
        files.sort(key=lambda x: x[1], reverse=reverse)

        curr_r = 0
        if dot_items:
            self.insertRow(curr_r)
            for c, it in enumerate(dot_items):
                if it:
                    self.setItem(curr_r, c, it)
            curr_r += 1

        for _, _, items in (dirs + files):
            self.insertRow(curr_r)
            for c, it in enumerate(items):
                if it:
                    self.setItem(curr_r, c, it)
            curr_r += 1

    def sortItems(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder):
        self.sortByColumn(column, order)

    def mimeTypes(self):
        return ["text/uri-list", "application/x-turboadb-file"]

    def mimeData(self, items):
        mime = QMimeData()
        rows = sorted({it.row() for it in items})
        paths = []
        urls = []
        for r in rows:
            it = self.item(r, 0)
            if it:
                data = it.data(Qt.UserRole)
                if data and isinstance(data, (tuple, list)) and data[0] != "..":
                    name, _ = data
                    full = posixpath.join(self.base_dir, name) if self.is_remote else os.path.join(self.base_dir, name)
                    paths.append(full)
                    if not self.is_remote and os.path.exists(full):
                        urls.append(QUrl.fromLocalFile(full))
        mime.setData("application/x-turboadb-file", json.dumps({"is_remote": self.is_remote, "paths": paths}).encode("utf-8"))
        if urls:
            mime.setUrls(urls)
        return mime

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat("application/x-turboadb-file"):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat("application/x-turboadb-file"):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.source() is self:
            event.ignore()
            return

        target_folder = self.base_dir
        item = self.itemAt(event.pos())
        if item:
            r = item.row()
            it0 = self.item(r, 0)
            if it0:
                data = it0.data(Qt.UserRole)
                if data and isinstance(data, (tuple, list)) and len(data) > 1 and data[1]:
                    if data[0] == "..":
                        target_folder = posixpath.dirname(self.base_dir.rstrip("/")) or "/" if self.is_remote else os.path.dirname(self.base_dir)
                    else:
                        target_folder = posixpath.join(self.base_dir, data[0]) if self.is_remote else os.path.join(self.base_dir, data[0])

        if event.mimeData().hasFormat("application/x-turboadb-file"):
            try:
                raw = json.loads(event.mimeData().data("application/x-turboadb-file").data().decode("utf-8"))
                paths = raw.get("paths", [])
                if paths:
                    self.dropped.emit(paths, False, target_folder)
                    event.acceptProposedAction()
                    return
            except Exception:
                pass
        if event.mimeData().hasUrls():
            files = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            if files:
                self.dropped.emit(files, True, target_folder)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


def _human_size(num_bytes: int) -> str:
    """Format bytes into a clean, human-readable string."""
    if num_bytes <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024.0:
            return f"{num_bytes:3.1f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def _q(path: str) -> str:
    """Single-quote a remote path for the device shell."""
    return "'" + path.replace("'", "'\\''") + "'"


def get_windows_drives() -> list[str]:
    """Enumerate mounted drive root paths on Windows (e.g. ['C:\\', 'D:\\'])."""
    if os.name != "nt":
        return ["/"]
    drives = []
    for letter in string.ascii_uppercase:
        drive_path = f"{letter}:\\"
        if os.path.exists(drive_path):
            drives.append(drive_path)
    return drives or ["C:\\"]


class _TransferThread(QThread):
    progress = pyqtSignal(int)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, handler, direction, a, b):
        super().__init__()
        self.handler, self.direction, self.a, self.b = handler, direction, a, b

    def run(self):
        try:
            if self.direction == "push":
                res = self.handler.push(self.a, self.b,
                                        on_progress=self.progress.emit, safe=False)
            else:
                res = self.handler.pull(self.a, self.b,
                                        on_progress=self.progress.emit, safe=False)
            self.done.emit(str(res))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _ShellThread(QThread):
    ok = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, handler, cmd):
        super().__init__()
        self.handler, self.cmd = handler, cmd

    def run(self):
        try:
            self.ok.emit(self.handler.shell(self.cmd, safe=False))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class FileBrowser(QWidget):
    log = pyqtSignal(str)

    COLUMNS = ["Name", "Size", "Type", "Date Modified", "Permissions", "Owner"]

    def __init__(self, handler, start="/sdcard", parent=None):
        super().__init__(parent)
        self.handler = handler
        self.local_cwd = os.path.expanduser("~")
        self.remote_cwd = start
        self._threads = []
        self._queue = []
        self._clipboard = []  # items in copy buffer
        self._clipboard_src = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        # Quick Jump Toolbar across the top
        qbar = QHBoxLayout()
        qbar.setSpacing(4)
        qbar.addWidget(QLabel("<b>Local:</b>"))
        for lbl, p in (("🏠 Home", os.path.expanduser("~")),
                       ("🖥 Desktop", os.path.join(os.path.expanduser("~"), "Desktop")),
                       ("📥 Downloads", os.path.join(os.path.expanduser("~"), "Downloads"))):
            b = QPushButton(lbl); b.setProperty("role", "ghost")
            b.clicked.connect(lambda _=False, path=p: self._jump_local(path))
            qbar.addWidget(b)
        qbar.addStretch(1)
        qbar.addWidget(QLabel("<b>Device:</b>"))
        for lbl, p in (("📱 /sdcard", "/sdcard"),
                       ("⚡ /data/local/tmp", "/data/local/tmp"),
                       ("📁 /", "/")):
            b = QPushButton(lbl); b.setProperty("role", "ghost")
            b.clicked.connect(lambda _=False, path=p: self._jump_remote(path))
            qbar.addWidget(b)
        lay.addLayout(qbar)

        # Main Splitter: [Local PC Table] | [Center Transfer Controls] | [Android Device Table]
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(6)

        # ----------------- Left Pane: Local PC -----------------
        left_w = QFrame(); left_w.setFrameShape(QFrame.StyledPanel)
        left_lay = QVBoxLayout(left_w)
        left_lay.setContentsMargins(4, 4, 4, 4); left_lay.setSpacing(4)

        ltop = QHBoxLayout()
        self.cmb_drives = QComboBox()
        self.cmb_drives.addItems(get_windows_drives())
        self.cmb_drives.setCurrentText(os.path.splitdrive(self.local_cwd)[0] + "\\")
        self.cmb_drives.currentTextChanged.connect(self._on_drive_changed)
        self.local_path = QLineEdit(self.local_cwd)
        self.local_path.returnPressed.connect(self._go_local)
        lup = QPushButton("⬆ Up"); lup.setProperty("role", "ghost")
        lup.clicked.connect(self._up_local)
        lref = QPushButton(" 🔄 Refresh")
        lref.setProperty("role", "ghost")
        lref.setToolTip("Refresh local listing (F5)")
        lref.clicked.connect(self.refresh_local)

        ltop.addWidget(self.cmb_drives)
        ltop.addWidget(self.local_path, 1)
        ltop.addWidget(lup)
        ltop.addWidget(lref)
        left_lay.addLayout(ltop)

        self.local_table = self._create_table(is_remote=False)
        self.local_table.cellDoubleClicked.connect(self._on_local_double_click)
        self.local_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.local_table.customContextMenuRequested.connect(self._context_local)
        self.local_table.dropped.connect(self._on_local_dropped)
        left_lay.addWidget(self.local_table, 1)

        lops = QHBoxLayout()
        for text, fn in (("📁 New Folder", self._local_mkdir),
                         ("📄 New File", self._local_newfile),
                         ("📝 Edit", self._local_edit),
                         ("📋 Copy", self._local_copy),
                         ("📥 Paste", self._local_paste),
                         ("✏ Rename", self._local_rename),
                         ("🗑 Delete", self._local_delete)):
            b = QPushButton(text); b.setProperty("role", "ghost" if "Delete" not in text else "danger")
            b.clicked.connect(fn)
            lops.addWidget(b)
        lops.addStretch(1)
        left_lay.addLayout(lops)

        # ----------------- Center Action Column -----------------
        center_w = QWidget()
        center_lay = QVBoxLayout(center_w)
        center_lay.setContentsMargins(4, 10, 4, 10)
        center_lay.setSpacing(10)
        center_lay.addStretch(1)

        self.btn_push = QPushButton("Push ➡")
        self.btn_push.setProperty("role", "ok")
        self.btn_push.setToolTip("Push selected local files to Android device folder (F5)")
        self.btn_push.setFixedWidth(84)
        self.btn_push.setFixedHeight(34)
        self.btn_push.clicked.connect(self.push_selected)
        center_lay.addWidget(self.btn_push)

        self.btn_pull = QPushButton("⬅ Pull")
        self.btn_pull.setProperty("role", "ok")
        self.btn_pull.setToolTip("Pull selected Android files to local PC folder (F5)")
        self.btn_pull.setFixedWidth(84)
        self.btn_pull.setFixedHeight(34)
        self.btn_pull.clicked.connect(self.pull_selected)
        center_lay.addWidget(self.btn_pull)

        center_lay.addStretch(1)

        # ----------------- Right Pane: Android Device -----------------
        right_w = QFrame(); right_w.setFrameShape(QFrame.StyledPanel)
        right_lay = QVBoxLayout(right_w)
        right_lay.setContentsMargins(4, 4, 4, 4); right_lay.setSpacing(4)

        rtop = QHBoxLayout()
        self.remote_path = QLineEdit(self.remote_cwd)
        self.remote_path.returnPressed.connect(self._go_remote)
        rup = QPushButton("⬆ Up"); rup.setProperty("role", "ghost")
        rup.clicked.connect(self._up_remote)
        rref = QPushButton(" 🔄 Refresh")
        rref.setProperty("role", "ghost")
        rref.setToolTip("Refresh device listing (F5)")
        rref.clicked.connect(self.refresh_remote)

        rtop.addWidget(QLabel("📱 <b>Android:</b>"))
        rtop.addWidget(self.remote_path, 1)
        rtop.addWidget(rup); rtop.addWidget(rref)
        right_lay.addLayout(rtop)

        self.remote_table = self._create_table(is_remote=True)
        self.remote_table.cellDoubleClicked.connect(self._on_remote_double_click)
        self.remote_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.remote_table.customContextMenuRequested.connect(self._context_remote)
        self.remote_table.dropped.connect(self._on_remote_dropped)
        right_lay.addWidget(self.remote_table, 1)

        rops = QHBoxLayout()
        for text, fn in (("📁 New Folder", self._remote_mkdir),
                         ("📄 New File", self._remote_newfile),
                         ("📝 Edit", self._remote_edit),
                         ("📋 Copy", self._remote_copy),
                         ("📥 Paste", self._remote_paste),
                         ("✏ Rename", self._remote_rename),
                         ("🗑 Delete", self._remote_delete)):
            b = QPushButton(text); b.setProperty("role", "ghost" if "Delete" not in text else "danger")
            b.clicked.connect(fn)
            rops.addWidget(b)
        rops.addStretch(1)
        right_lay.addLayout(rops)

        split.addWidget(left_w)
        split.addWidget(center_w)
        split.addWidget(right_w)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 0)
        split.setStretchFactor(2, 5)
        split.setSizes([460, 92, 460])
        lay.addWidget(split, 1)

        # Bottom Progress Bar
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        lay.addWidget(self.bar)

        self.hint = QLabel("<b>WinSCP Dual-Pane:</b> Multi-drive support • Detailed size, permissions & owner • "
                           "Drag & drop between panes or from Explorer • F4 Edit • F5 Push/Pull • F2 Rename • Del Delete")
        self.hint.setStyleSheet("color:#94a3b8; font-size:8.5pt;")
        lay.addWidget(self.hint)

        # Shortcuts
        QShortcut(QKeySequence("F5"), self, activated=self._on_f5)
        QShortcut(QKeySequence("F4"), self.local_table, activated=self._local_edit)
        QShortcut(QKeySequence("F4"), self.remote_table, activated=self._remote_edit)
        QShortcut(QKeySequence.Delete, self.local_table, activated=self._local_delete)
        QShortcut(QKeySequence.Delete, self.remote_table, activated=self._remote_delete)
        QShortcut(QKeySequence("F2"), self.local_table, activated=self._local_rename)
        QShortcut(QKeySequence("F2"), self.remote_table, activated=self._remote_rename)
        QShortcut(QKeySequence.Copy, self.local_table, activated=self._local_copy)
        QShortcut(QKeySequence.Paste, self.local_table, activated=self._local_paste)
        QShortcut(QKeySequence.Copy, self.remote_table, activated=self._remote_copy)
        QShortcut(QKeySequence.Paste, self.remote_table, activated=self._remote_paste)

        self._loaded_remote = False
        self.refresh_local()
        self.refresh_remote()

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_loaded_remote", False):
            self._loaded_remote = True
            self.refresh_remote()

    def _create_table(self, is_remote: bool = False) -> _FileTableWidget:
        table = _FileTableWidget(is_remote=is_remote, parent=self)
        table.setHorizontalHeaderLabels(self.COLUMNS)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(22)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.setColumnWidth(1, 80)
        table.setColumnWidth(2, 85)
        table.setColumnWidth(3, 120)
        table.setColumnWidth(4, 90)
        table.setColumnWidth(5, 90)
        table.setSortingEnabled(False)
        table.setDragEnabled(True)
        return table

    @property
    def cwd(self) -> str:
        return self.remote_cwd

    @cwd.setter
    def cwd(self, val: str):
        self.remote_cwd = val

    @property
    def local_list(self):
        return self.local_table

    @property
    def remote_list(self):
        return self.remote_table

    def _track_thread(self, t):
        self._threads.append(t)
        t.finished.connect(t.deleteLater)
        t.finished.connect(self._on_thread_finished)

    def _on_thread_finished(self):
        t = self.sender()
        if t in self._threads:
            self._threads.remove(t)

    # ---- Navigation: Local PC ----
    def _on_drive_changed(self, drive: str):
        if os.path.exists(drive):
            self.local_cwd = os.path.abspath(drive)
            self.refresh_local()

    def _jump_local(self, path: str):
        if os.path.exists(path):
            self.local_cwd = os.path.abspath(path)
            self._sync_drive_combo()
            self.refresh_local()

    def _sync_drive_combo(self):
        drive = os.path.splitdrive(self.local_cwd)[0] + "\\"
        if drive and self.cmb_drives.currentText() != drive:
            idx = self.cmb_drives.findText(drive)
            if idx >= 0:
                self.cmb_drives.blockSignals(True)
                self.cmb_drives.setCurrentIndex(idx)
                self.cmb_drives.blockSignals(False)

    def _go_local(self):
        p = self.local_path.text().strip()
        if os.path.exists(p) and os.path.isdir(p):
            self.local_cwd = os.path.abspath(p)
            self._sync_drive_combo()
        self.refresh_local()

    def _up_local(self):
        parent = os.path.dirname(self.local_cwd)
        if parent and parent != self.local_cwd and os.path.exists(parent):
            self.local_cwd = parent
            self._sync_drive_combo()
            self.refresh_local()

    def refresh_local(self):
        self.local_path.setText(self.local_cwd)
        self.local_table.base_dir = self.local_cwd
        self.local_table.setRowCount(0)

        # Parent directory row
        parent = os.path.dirname(self.local_cwd)
        if parent and parent != self.local_cwd:
            self._add_row(self.local_table, "..", 0, "<DIR>", "Folder", "", "", "", is_dir=True)

        try:
            dirs = []
            files = []
            with os.scandir(self.local_cwd) as iters:
                for entry in iters:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        is_dir = entry.is_dir(follow_symlinks=False)
                        size = st.st_size if not is_dir else 0
                        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                        ext = os.path.splitext(entry.name)[1].lower()
                        ftype = "Folder" if is_dir else (mimetypes.types_map.get(ext, ext.upper() or "File"))
                        perms = oct(st.st_mode)[-3:]
                        owner = ""
                        if is_dir:
                            dirs.append((entry.name, 0, "<DIR>", ftype, mtime, perms, owner, True, st.st_mtime))
                        else:
                            files.append((entry.name, size, _human_size(size), ftype, mtime, perms, owner, False, st.st_mtime))
                    except (PermissionError, FileNotFoundError, OSError):
                        continue

            for name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir, raw_mtime in dirs + files:
                self._add_row(self.local_table, name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir, raw_mtime)
        except Exception as exc:
            self.log.emit(f"[ERROR] local list {self.local_cwd}: {exc}")

    def _on_local_double_click(self, row: int, _col: int):
        it = self.local_table.item(row, 0)
        if not it:
            return
        data = it.data(Qt.UserRole)
        if not data:
            return
        name, is_dir = data
        if not is_dir:
            self._local_edit()
            return
        if name == "..":
            self._up_local()
        else:
            target = os.path.join(self.local_cwd, name)
            if os.path.exists(target) and os.path.isdir(target):
                self.local_cwd = target
                self._sync_drive_combo()
                self.refresh_local()

    # ---- Navigation: Android Device ----
    def _jump_remote(self, path: str):
        self.remote_cwd = path
        self.refresh_remote()

    def _go_remote(self):
        p = self.remote_path.text().strip()
        if p:
            self.remote_cwd = p
        self.refresh_remote()

    def _up_remote(self):
        if self.remote_cwd in ("/", ""):
            return
        self.remote_cwd = posixpath.dirname(self.remote_cwd.rstrip("/")) or "/"
        self.refresh_remote()

    def refresh_remote(self):
        self.remote_path.setText(self.remote_cwd)
        self.remote_table.base_dir = self.remote_cwd
        self.remote_table.setRowCount(0)
        self._add_row(self.remote_table, "..", 0, "<DIR>", "Folder", "", "", "", is_dir=True)

        t = _ShellThread(self.handler, f"ls -la {_q(self.remote_cwd)}")
        self._ls = t
        t.ok.connect(lambda res, t=t: self._on_remote_ls(t, res))
        t.fail.connect(lambda m, t=t: self._on_remote_ls_fail(t, m))
        self._track_thread(t); t.start()

    def _on_remote_ls(self, t, res):
        if t is not getattr(self, "_ls", None):
            return
        self.remote_table.setRowCount(0)
        self._add_row(self.remote_table, "..", 0, "<DIR>", "Folder", "", "", "", is_dir=True)

        if not res.ok:
            self.log.emit(f"[ERROR] ls {self.remote_cwd}: {res.stderr.strip() or res.text}")
            return

        cleaned = strip_ansi(res.stdout or "")
        lines = cleaned.splitlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("total "):
                continue
            parsed = self._parse_ls_line(line)
            if not parsed:
                continue
            name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir = parsed
            if name in (".", ".."):
                continue
            self._add_row(self.remote_table, name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir)

    def _on_remote_ls_fail(self, t, msg):
        if t is not getattr(self, "_ls", None):
            return
        self.remote_table.setRowCount(0)
        self._add_row(self.remote_table, "..", 0, "<DIR>", "Folder", "", "", "", is_dir=True)
        self.log.emit(f"[ERROR] ls {self.remote_cwd}: {msg}")

    def _on_remote_double_click(self, row: int, _col: int):
        it = self.remote_table.item(row, 0)
        if not it:
            return
        data = it.data(Qt.UserRole)
        if not data:
            return
        name, is_dir = data
        if not is_dir:
            self._remote_edit()
            return
        if name == "..":
            self._up_remote()
        else:
            self.remote_cwd = posixpath.join(self.remote_cwd, name)
            self.refresh_remote()

    def refresh(self):
        self.refresh_local()
        self.refresh_remote()

    @staticmethod
    def _parse_ls_line(line: str):
        """Parse one `ls -la` output line into structured attributes (supporting ISO & traditional dates)."""
        clean_line = strip_ansi(line).strip()
        m = _LS_REGEX.match(clean_line)
        if m:
            perms, _links, user, group, size_str, mtime, name = m.groups()
            is_symlink = perms.startswith("l")
            if is_symlink and " -> " in name:
                link_name, _, link_dest = name.partition(" -> ")
                is_dir = link_dest.endswith("/")
                name = link_name
                ftype = "Folder Link" if is_dir else "File Link"
            else:
                is_dir = perms.startswith("d")
                ftype = "Folder" if is_dir else "File"
            owner = f"{user}:{group}"
            try:
                raw_size = int(size_str)
            except ValueError:
                raw_size = 0
            sz_str = "<DIR>" if is_dir else _human_size(raw_size)
            if " -> " in name:
                name = name.split(" -> ")[0]
            ext = os.path.splitext(name)[1].lower()
            if not is_dir and ftype != "File Link":
                ftype = mimetypes.types_map.get(ext, ext.upper() or "File")
            return name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir

        parts = clean_line.split(maxsplit=7)
        if len(parts) < 8:
            return None
        perms = parts[0]
        is_dir = perms.startswith("d")
        owner = f"{parts[2]}:{parts[3]}"
        try:
            raw_size = int(parts[4])
        except ValueError:
            raw_size = 0
        sz_str = "<DIR>" if is_dir else _human_size(raw_size)
        mtime = f"{parts[5]} {parts[6]}"
        name = parts[7]
        if " -> " in name:
            link_name, _, link_dest = name.partition(" -> ")
            is_dir = link_dest.endswith("/")
            name = link_name
            ftype = "Folder Link" if is_dir else "File Link"
        else:
            ext = os.path.splitext(name)[1].lower()
            ftype = "Folder" if is_dir else (mimetypes.types_map.get(ext, ext.upper() or "File"))
        return name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir

    def _add_row(self, table: QTableWidget, name: str, raw_size: int, sz_str: str,
                 ftype: str, mtime: str, perms: str, owner: str, is_dir: bool, raw_mtime: float = None):
        row = table.rowCount()
        table.insertRow(row)

        icon = "📁 " if is_dir else "📄 "
        it_name = _FileItem(icon + name)
        it_name.setData(Qt.UserRole, (name, is_dir))
        it_size = _FileItem(sz_str)
        it_size.setData(Qt.UserRole, raw_size)
        it_type = _FileItem(ftype)
        it_date = _FileItem(mtime)
        if raw_mtime is not None:
            it_date.setData(Qt.UserRole, raw_mtime)
        else:
            try:
                t_val = time.mktime(time.strptime(mtime[:16], "%Y-%m-%d %H:%M"))
                it_date.setData(Qt.UserRole, t_val)
            except Exception:
                it_date.setData(Qt.UserRole, 0.0)
        it_perms = _FileItem(perms)
        it_owner = _FileItem(owner)

        for col, it in enumerate((it_name, it_size, it_type, it_date, it_perms, it_owner)):
            flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
            if not (is_dir and name == ".."):
                flags |= Qt.ItemIsDragEnabled
            flags |= Qt.ItemIsDropEnabled
            it.setFlags(flags)
            table.setItem(row, col, it)

    # ---- Selection helpers ----
    def _selected_local(self) -> List[Tuple[str, bool]]:
        selected = []
        rows = sorted({idx.row() for idx in self.local_table.selectedIndexes()})
        for r in rows:
            it = self.local_table.item(r, 0)
            if it:
                data = it.data(Qt.UserRole)
                if data and data[0] != "..":
                    selected.append((os.path.join(self.local_cwd, data[0]), data[1]))
        return selected

    def _selected_remote(self) -> List[Tuple[str, bool]]:
        selected = []
        rows = sorted({idx.row() for idx in self.remote_table.selectedIndexes()})
        for r in rows:
            it = self.remote_table.item(r, 0)
            if it:
                data = it.data(Qt.UserRole)
                if data and data[0] != "..":
                    selected.append((data[0], data[1]))
        return selected

    # ---- Push / Pull Operations ----
    def push_selected(self):
        items = self._selected_local()
        if not items:
            QMessageBox.information(self, "Push", "Select one or more items on Local PC to push.")
            return
        self._queue = [(path, posixpath.join(self.remote_cwd, os.path.basename(path)), "push")
                       for path, _ in items]
        self.log.emit(f"Pushing {len(self._queue)} item(s) to {self.remote_cwd}…")
        self._process_queue()

    def pull_selected(self):
        items = self._selected_remote()
        if not items:
            QMessageBox.information(self, "Pull", "Select one or more items on Android Device to pull.")
            return
        self._queue = [(posixpath.join(self.remote_cwd, name), os.path.join(self.local_cwd, name), "pull")
                       for name, _ in items]
        self.log.emit(f"Pulling {len(self._queue)} item(s) to {self.local_cwd}…")
        self._process_queue()

    def _on_f5(self):
        if self.remote_table.hasFocus():
            self.refresh_remote()
        else:
            self.refresh_local()

    def _process_queue(self):
        if not self._queue:
            self.bar.setVisible(False)
            self.refresh_local()
            self.refresh_remote()
            return

        src, dst, direction = self._queue.pop(0)
        self.bar.setValue(0)
        self.bar.setVisible(True)

        t = _TransferThread(self.handler, direction, src, dst)
        t.progress.connect(self.bar.setValue)
        t.done.connect(lambda res: (self.log.emit(f"[OK] {direction}: {os.path.basename(src)}"),
                                    self._process_queue()))
        t.failed.connect(lambda err: (self.log.emit(f"[ERROR] {direction} {src}: {err}"),
                                      self._process_queue()))
        self._track_thread(t); t.start()

    # ---- File Operations: Local ----
    def _local_mkdir(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if ok and name.strip():
            try:
                os.makedirs(os.path.join(self.local_cwd, name.strip()), exist_ok=True)
                self.refresh_local()
            except Exception as exc:
                QMessageBox.warning(self, "Error", str(exc))

    def _local_newfile(self):
        name, ok = QInputDialog.getText(self, "New File", "File name:")
        if ok and name.strip():
            try:
                target = os.path.join(self.local_cwd, name.strip())
                with open(target, "a"):
                    pass
                self.refresh_local()
            except Exception as exc:
                QMessageBox.warning(self, "Error", str(exc))

    def _local_copy(self):
        items = self._selected_local()
        if items:
            self._clipboard = [p for p, _ in items]
            self._clipboard_src = "local"
            self.log.emit(f"Copied {len(self._clipboard)} local item(s) to clipboard.")

    def _local_paste(self):
        if not self._clipboard:
            return
        if self._clipboard_src == "local":
            for src in self._clipboard:
                try:
                    dst = os.path.join(self.local_cwd, os.path.basename(src))
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                except Exception as exc:
                    self.log.emit(f"[ERROR] paste {src}: {exc}")
            self.refresh_local()
        else:
            # Clipboard is from device -> pull to current local folder
            self._queue = [(src, os.path.join(self.local_cwd, posixpath.basename(src)), "pull")
                           for src in self._clipboard]
            self._process_queue()

    def _local_rename(self):
        items = self._selected_local()
        if len(items) != 1:
            return
        old_path = items[0][0]
        old_name = os.path.basename(old_path)
        new_name, ok = QInputDialog.getText(self, "Rename Local Item", "New name:", text=old_name)
        if ok and new_name.strip() and new_name != old_name:
            try:
                os.rename(old_path, os.path.join(os.path.dirname(old_path), new_name.strip()))
                self.refresh_local()
            except Exception as exc:
                QMessageBox.warning(self, "Error", str(exc))

    def _local_delete(self):
        items = self._selected_local()
        if not items:
            return
        if QMessageBox.question(self, "Delete Local",
                                f"Permanently delete {len(items)} item(s) from Local PC?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        for p, _ in items:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
            except Exception as exc:
                self.log.emit(f"[ERROR] Delete local {p}: {exc}")
        self.refresh_local()

    # ---- File Operations: Remote ----
    def _remote_mkdir(self):
        name, ok = QInputDialog.getText(self, "New Device Folder", "Folder name:")
        if ok and name.strip():
            self._run_shell(f"mkdir {name.strip()}",
                            f"mkdir -p {_q(posixpath.join(self.remote_cwd, name.strip()))}")

    def _remote_newfile(self):
        name, ok = QInputDialog.getText(self, "New Device File", "File name:")
        if ok and name.strip():
            target = _q(posixpath.join(self.remote_cwd, name.strip()))
            self._run_shell(f"create {name.strip()}", f"touch {target} 2>/dev/null || : > {target}")

    def _remote_copy(self):
        items = self._selected_remote()
        if items:
            self._clipboard = [posixpath.join(self.remote_cwd, name) for name, _ in items]
            self._clipboard_src = "remote"
            self.log.emit(f"Copied {len(self._clipboard)} device item(s) to clipboard.")

    def _remote_paste(self):
        if not self._clipboard:
            return
        if self._clipboard_src == "remote":
            for src in self._clipboard:
                dst = posixpath.join(self.remote_cwd, posixpath.basename(src))
                self._run_shell(f"copy {posixpath.basename(src)}", f"cp -r {_q(src)} {_q(dst)}")
        else:
            # Clipboard is from Local -> push to current device folder
            self._queue = [(src, posixpath.join(self.remote_cwd, os.path.basename(src)), "push")
                           for src in self._clipboard]
            self._process_queue()

    def _remote_rename(self):
        items = self._selected_remote()
        if len(items) != 1:
            return
        old_name = items[0][0]
        new_name, ok = QInputDialog.getText(self, "Rename Device Item", "New name:", text=old_name)
        if ok and new_name.strip() and new_name != old_name:
            src = posixpath.join(self.remote_cwd, old_name)
            dst = posixpath.join(self.remote_cwd, new_name.strip())
            self._run_shell(f"rename {old_name}", f"mv {_q(src)} {_q(dst)}")

    def _remote_delete(self):
        items = self._selected_remote()
        if not items:
            return
        if QMessageBox.question(self, "Delete Device Items",
                                f"Permanently delete {len(items)} item(s) from Device?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        targets = " ".join(_q(posixpath.join(self.remote_cwd, n)) for n, _ in items)
        self._run_shell(f"delete {len(items)} item(s)", f"rm -rf {targets}")

    def _run_shell(self, label, cmd):
        t = _ShellThread(self.handler, cmd)
        t.ok.connect(lambda res: (self.log.emit(
            f"[OK] {label}" if res.ok else f"[ERROR] {label}: " + (res.stderr.strip() or res.text)),
            self.refresh_remote()))
        t.fail.connect(lambda m: (self.log.emit(f"[ERROR] {label}: {m}"), self.refresh_remote()))
        self._track_thread(t); t.start()

    # ---- Context Menus ----
    def _context_local(self, pos):
        menu = QMenu(self)
        menu.addAction("➡ Push to Device (F5)", self.push_selected)
        menu.addAction("📝 Edit (F4)", self._local_edit)
        menu.addSeparator()
        menu.addAction("📁 New Folder", self._local_mkdir)
        menu.addAction("📄 New File", self._local_newfile)
        menu.addAction("📋 Copy (Ctrl+C)", self._local_copy)
        menu.addAction("📥 Paste (Ctrl+V)", self._local_paste)
        menu.addAction("✏ Rename (F2)", self._local_rename)
        menu.addAction("🗑 Delete (Del)", self._local_delete)
        menu.exec_(self.local_table.mapToGlobal(pos))

    def _context_remote(self, pos):
        menu = QMenu(self)
        menu.addAction("⬅ Pull to Local PC (F5)", self.pull_selected)
        menu.addAction("📝 Edit (F4)", self._remote_edit)
        menu.addSeparator()
        menu.addAction("📁 New Folder", self._remote_mkdir)
        menu.addAction("📄 New File", self._remote_newfile)
        menu.addAction("📋 Copy (Ctrl+C)", self._remote_copy)
        menu.addAction("📥 Paste (Ctrl+V)", self._remote_paste)
        menu.addAction("✏ Rename (F2)", self._remote_rename)
        menu.addAction("🗑 Delete (Del)", self._remote_delete)
        menu.exec_(self.remote_table.mapToGlobal(pos))

    # ---- Edit File ----
    def _local_edit(self):
        items = self._selected_local()
        if not items:
            return
        path, is_dir = items[0]
        if is_dir:
            return
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception as exc:
            QMessageBox.critical(self, "Edit File", f"Could not read {name}:\n{exc}")
            return

        def save_fn(new_text):
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
                self.log.emit(f"[OK] Saved local file: {name}")
                self.refresh_local()
                return True
            except Exception as exc:
                QMessageBox.critical(self, "Save File", f"Could not save {name}:\n{exc}")
                return False

        dlg = _FileEditorDialog(name, path, content, save_fn, self)
        dlg.exec_()

    def _remote_edit(self):
        items = self._selected_remote()
        if not items:
            return
        name, is_dir = items[0]
        if is_dir:
            return
        full_path = posixpath.join(self.remote_cwd, name)
        self.log.emit(f"Fetching {name} for editing…")
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, prefix="turboadb-edit-")
        tmp_path = tmp.name
        tmp.close()

        try:
            self.handler.pull(full_path, tmp_path, safe=False)
            with open(tmp_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception as exc:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            QMessageBox.critical(self, "Edit Remote File", f"Could not pull {name} from device:\n{exc}")
            return

        def save_fn(new_text):
            try:
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
                self.handler.push(tmp_path, full_path, safe=False)
                self.log.emit(f"[OK] Saved remote file: {full_path}")
                self.refresh_remote()
                return True
            except Exception as exc:
                QMessageBox.critical(self, "Save Remote File", f"Could not push updated {name}:\n{exc}")
                return False

        dlg = _FileEditorDialog(name, full_path, content, save_fn, self)
        try:
            dlg.exec_()
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # ---- Drag and drop ----
    def _on_local_dropped(self, paths: list, is_external: bool, target_dir: str = ""):
        if not paths:
            return
        dst_base = target_dir or self.local_cwd
        if is_external:
            for src in paths:
                if os.path.exists(src):
                    try:
                        dst = os.path.join(dst_base, os.path.basename(src.rstrip("/\\")))
                        if dst != src:
                            if os.path.isdir(src):
                                shutil.copytree(src, dst, dirs_exist_ok=True)
                            else:
                                shutil.copy2(src, dst)
                    except Exception as exc:
                        self.log.emit(f"[ERROR] local copy {src}: {exc}")
            self.refresh_local()
        else:
            # Dropped remote items into local table -> PULL!
            self._queue = [(src, os.path.join(dst_base, posixpath.basename(src.rstrip("/\\"))), "pull")
                           for src in paths]
            self.log.emit(f"Pulling {len(self._queue)} item(s) to {dst_base}…")
            self._process_queue()

    def _on_remote_dropped(self, paths: list, is_external: bool, target_dir: str = ""):
        if not paths:
            return
        locals_ = [p for p in paths if p and os.path.exists(p)]
        if not locals_:
            return
        dst_base = target_dir or self.remote_cwd
        self._queue = [(src, posixpath.join(dst_base, os.path.basename(src.rstrip("/\\"))), "push")
                       for src in locals_]
        self.log.emit(f"Pushing {len(self._queue)} item(s) to {dst_base}…")
        self._process_queue()

    def close_panel(self):
        self._queue.clear()
        from .qtutil import park_thread
        for t in list(self._threads):
            for sig in ("progress", "done", "failed", "ok", "fail"):
                try:
                    getattr(t, sig).disconnect()
                except Exception:
                    pass
            t.wait(300)
            park_thread(t)
        self._threads.clear()
