"""WinSCP-style dual-pane file manager for TurboADB:
- Left pane: Local PC with multi-drive selector (C:, D:, E: etc.)
- Right pane: Android device with permissions, owner, type, size, modified date
- Full WinSCP operations: New Folder, New File, Copy, Paste, Rename, Delete, Properties
- Bidirectional drag-and-drop between panes and from external Windows Explorer
- Sequential recursive background transfers with live progress reporting

Every disk or device operation (listing, drive enumeration, copy/delete, editor
reads and saves, adb shell commands) runs on a worker thread; the UI only
renders results.  Stale listing results are dropped when the user navigates
again before they arrive.
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import posixpath
import re
import shlex
import shutil
import stat
import string
import sys
import tempfile
import threading
import time
from typing import List, Optional, Tuple

from PyQt5.QtCore import QThread, pyqtSignal, Qt, QUrl, QMimeData
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
                             QInputDialog, QMessageBox, QLabel, QProgressBar,
                             QSplitter, QFrame, QMenu, QShortcut, QComboBox,
                             QAbstractItemView, QDialog, QPlainTextEdit, QToolButton)
from PyQt5.QtGui import QKeySequence, QFont, QTextCursor, QDrag
from ..results import strip_ansi
from .fileutil import _alive, write_text_file
from .icons import icon
from .qtutil import close_jobs, disconnect_signals, park_thread, run_job, thread_running
from ..remotefs import (  # device file helpers, shared with the engine and CLI
    _ANSI_CSI_RE, _as_text, _chmod_cmd, _chunks, _copy_into_cmd, _cp_cmd, _dir_arg,
    _edit_stat_cmd, _EDIT_STAT_RE, _human_size, _link_dirs_cmd, _list_remote_dir, _Listing,
    _LS_B_ESCAPES, _ls_cmd, _LS_DATE, _ls_escaped_cmd, _LS_FALLBACK_REGEX, _LS_PERMS,
    _LS_REGEX, _LS_TOOLBOX_REGEX, _LS_TOTAL_RE, _mkdir_cmd, _mode_cmd, _mv_cmd,
    _normalize_remote_path, _output_lines, _parse_edit_stat, _parse_ls_entry, _parse_ls_line,
    _parse_ls_listing, _parse_ls_output, _PERMS_RE, _probe_cmd, _probe_remote,
    _rename_check_cmd, _rename_cmd, _result_error, _rm_cmd, _split_link, _touch_cmd,
    _unescape_ls_b,
)


# Characters Qt's plain-text document cannot round-trip (they become block breaks).
_EDIT_UNSAFE_CHARS = (" ", "﷐", "﷑")

_MIME = "application/x-turboadb-file"

# The built-in editor is for config/text files; bigger or binary files are refused.
_EDIT_MAX_BYTES = 2 * 1024 * 1024

_LINE_ENDING_NAMES = {"\r\n": "CRLF", "\r": "CR", "\n": "LF"}

# Editors that outlived their panel (unsaved or mid-save when the tab closed).
_DETACHED_EDITORS = set()


# The name cell shows the plain name; its icon is the item's DecorationRole and
# the icon key is kept here (for tests and tooling), never in the visible text.
ICON_ROLE = Qt.UserRole + 1

_IMAGE_EXT = frozenset(".png .jpg .jpeg .gif .bmp .webp .heic .heif .svg .ico .tif .tiff".split())
_VIDEO_EXT = frozenset(".mp4 .mkv .avi .mov .webm .3gp .m4v .wmv .flv .ts".split())
_AUDIO_EXT = frozenset(".mp3 .wav .ogg .flac .m4a .aac .opus .wma .amr".split())
_APK_EXT = frozenset(".apk .apks .apkm .xapk .aab".split())
_ARCHIVE_EXT = frozenset(".zip .rar .7z .tar .gz .tgz .bz2 .xz .zst .jar .img .iso".split())
_TEXT_EXT = frozenset(
    ".txt .log .md .json .xml .csv .ini .cfg .conf .yaml .yml .prop .properties .sh .py "
    ".bat .ps1 .rc .html .htm .js .kt .java .c .h .cpp .toml".split()
)
_ICONS = {}  # (name, tone) -> QIcon, shared by every row (icons follow the theme)

# Pane operation buttons and their context-menu twins: (icon, tone).
_PANE_OP_ICONS = {
    "New folder": ("folder-plus", "amber"),
    "New file": ("file-plus", "blue"),
    "Edit": ("edit", "purple"),
    "Copy": ("copy", "blue"),
    "Paste": ("paste", "blue"),
    "Rename": ("rename", "teal"),
    "Delete": ("trash", "red"),
    "Push to device": ("arrow-right", "blue"),
    "Pull to this PC": ("arrow-left", "green"),
}


def _cached_icon(name: str, tone: str):
    key = (name, tone)
    cached = _ICONS.get(key)
    if cached is None:
        cached = _ICONS[key] = icon(name, tone)
    return cached


def _entry_icon_key(name: str, is_dir: bool, ftype: str = "") -> Tuple[str, str]:
    """``(icon name, tone)`` for a listing row, chosen by kind and extension."""
    if is_dir and name == "..":
        return ("arrow-up", "accent")
    if ftype.endswith("Link"):  # folder and file links alike
        return ("link", "teal")
    if is_dir:
        return ("folder", "amber")
    if ftype in ("Pipe", "Socket", "Block Device", "Character Device"):
        return ("chip", "dim")
    ext = os.path.splitext(name)[1].lower()
    if ext in _APK_EXT:
        return ("apps", "green")
    if ext in _IMAGE_EXT:
        return ("image", "purple")
    if ext in _VIDEO_EXT:
        return ("video", "red")
    if ext in _AUDIO_EXT:
        return ("music", "pink")
    if ext in _ARCHIVE_EXT:
        return ("file", "orange")
    if ext in _TEXT_EXT:
        return ("file", "blue")
    return ("file", "dim")


def _name_sort_key(item) -> tuple:
    """'..' first, then folders, then files; names compare case-insensitively."""
    data = item.data(Qt.UserRole)
    if isinstance(data, (tuple, list)) and len(data) > 1:
        name = str(data[0])
        return (0 if name == ".." else 1, 0 if data[1] else 1, name.lower())
    return (1, 1, item.text().lower())


class _FileItem(QTableWidgetItem):
    """Custom table item that sorts numerically when size data is present, and
    by the real name (never the displayed text) for name cells."""
    def __lt__(self, other):
        if not isinstance(other, QTableWidgetItem):
            return super().__lt__(other)
        d1 = self.data(Qt.UserRole)
        d2 = other.data(Qt.UserRole)
        if (isinstance(d1, (int, float)) and isinstance(d2, (int, float))
                and not isinstance(d1, bool) and not isinstance(d2, bool)):
            return d1 < d2
        return _name_sort_key(self) < _name_sort_key(other)


# ---- editor file helpers (pure; run on worker threads) ----------------------


class _EditRefused(Exception):
    """The file can't be edited safely in the built-in text editor."""


def _too_large_message(size: int) -> str:
    return (f"The file is too large for the built-in editor ({_human_size(size)}; "
            f"the limit is {_human_size(_EDIT_MAX_BYTES)}).")


def _decode_for_edit(data: bytes) -> Tuple[str, str, bool]:
    """Decode *data* for the editor: ``(text with \\n line breaks, newline, mixed)``.

    Refuses (``_EditRefused``) oversized, binary and non-UTF-8 content, because
    saving a lossy decode would push U+FFFD replacement characters back.
    *newline* is the file's dominant line ending, re-applied on save.
    """
    if len(data) > _EDIT_MAX_BYTES:
        raise _EditRefused(_too_large_message(len(data)))
    if b"\x00" in data:
        raise _EditRefused("The file looks binary (it contains NUL bytes), so it was not opened.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _EditRefused(
            f"The file is not valid UTF-8 text (invalid byte at offset {exc.start}), so it was "
            "not opened: saving it from the editor would corrupt it."
        ) from None
    unsafe = sorted({f"U+{ord(ch):04X}" for ch in _EDIT_UNSAFE_CHARS if ch in text})
    if unsafe:
        raise _EditRefused(
            "The file contains Unicode separator characters the built-in editor cannot keep "
            f"({', '.join(unsafe)}), so it was not opened: saving it would change them."
        )
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf and crlf >= lf and crlf >= cr:
        newline = "\r\n"
    elif cr > lf:
        newline = "\r"
    else:
        newline = "\n"
    mixed = sum(1 for n in (crlf, lf, cr) if n) > 1
    return text.replace("\r\n", "\n").replace("\r", "\n"), newline, mixed


def _read_for_edit(path: str) -> Tuple[str, str, bool]:
    """Read a local file for the editor with the size/binary/UTF-8 guards."""
    size = os.path.getsize(path)
    if size > _EDIT_MAX_BYTES:
        raise _EditRefused(_too_large_message(size))
    with open(path, "rb") as fh:
        data = fh.read(_EDIT_MAX_BYTES + 1)
    return _decode_for_edit(data)


def _text_for_save(text: str, newline: str) -> str:
    """Editor text (``\\n`` breaks) -> file text using the file's original line ending."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline and newline != "\n":
        text = text.replace("\n", newline)
    return text


def _edit_load_result(load):
    """Run *load* and turn a refusal into a result instead of an error."""
    try:
        text, newline, mixed = load()
    except _EditRefused as exc:
        return ("refused", str(exc))
    return ("ok", text, newline, mixed)


class _FileEditorDialog(QDialog):
    """Integrated text editor for local and remote files with line status and search."""
    def __init__(self, title: str, path: str, content: str, on_save, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit — {title}")
        self.resize(800, 580)
        self.path = path
        self.on_save = on_save
        self._saving = False
        self._close_after_save = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        v = QVBoxLayout()
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(8)
        root.addLayout(v, 1)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(QLabel(f"<b>File:</b> {html.escape(path)}"))
        top.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find in file… (Enter to next)")
        self.search.addAction(icon("search"), QLineEdit.LeadingPosition)
        self.search.setFixedWidth(220)
        self.search.returnPressed.connect(self._find_next)
        top.addWidget(self.search)
        v.addLayout(top)

        self.edit = QPlainTextEdit()
        self.edit.setFont(QFont("Consolas", 10))
        self.edit.setPlainText(content)
        self.edit.document().setModified(False)
        self.edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(self.edit, 1)

        footer = QWidget()
        footer.setObjectName("dialogFooter")
        footer.setAttribute(Qt.WA_StyledBackground, True)
        bot = QHBoxLayout(footer)
        bot.setContentsMargins(16, 10, 16, 10)
        bot.setSpacing(8)
        self.status = QLabel()
        self._update_status()
        # blockCount()/characterCount() are O(1): no toPlainText() per keystroke.
        self.edit.textChanged.connect(self._update_status)
        self.edit.document().modificationChanged.connect(lambda _m: self._update_status())
        bot.addWidget(self.status)
        bot.addStretch()

        self.b_save = QPushButton("Save")
        self.b_save.setProperty("role", "ok")
        self.b_save.setIcon(icon("save", "on-accent"))
        self.b_save.clicked.connect(self._save)
        self.b_save_close = QPushButton("Save & Close")
        self.b_save_close.setIcon(icon("check", "teal"))
        self.b_save_close.clicked.connect(self._save_and_close)
        self.b_cancel = QPushButton("Close")
        self.b_cancel.setProperty("role", "ghost")
        self.b_cancel.setIcon(icon("x"))
        self.b_cancel.clicked.connect(self.reject)

        bot.addWidget(self.b_save)
        bot.addWidget(self.b_save_close)
        bot.addWidget(self.b_cancel)
        root.addWidget(footer)

        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: self.search.setFocus())

    @property
    def _dirty(self) -> bool:
        return self.edit.document().isModified()

    @_dirty.setter
    def _dirty(self, value: bool):
        self.edit.document().setModified(bool(value))

    def is_dirty(self) -> bool:
        return self._dirty

    def is_saving(self) -> bool:
        return self._saving

    def text(self) -> str:
        """The document text exactly as typed.

        ``toPlainText()`` silently turns NBSP into spaces and U+2028 into line
        breaks.  The raw text keeps them; only Qt's own block separators (U+2029)
        become ``\\n``.  Files containing a real U+2029 are refused on load.
        """
        return self.edit.document().toRawText().replace(" ", "\n")

    def _update_status(self):
        doc = self.edit.document()
        lines = doc.blockCount()
        chars = max(0, doc.characterCount() - 1)  # excludes the final paragraph separator
        dirty_tag = " • Modified" if doc.isModified() else ""
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
        if self._saving:
            return False
        if self.on_save:
            res = self.on_save(self.text())
            if res is True:
                self._dirty = False
                self._update_status()
                return True
            if res is None:
                self._saving = True
                self._set_saving(True)
            return False
        return True

    def _save_and_close(self):
        self._close_after_save = True
        if self._save():
            self.accept()
        elif not self._saving:
            self._close_after_save = False

    def _set_saving(self, saving: bool):
        self.b_save.setDisabled(saving)
        self.b_save_close.setDisabled(saving)
        self.b_cancel.setDisabled(saving)
        self.edit.setReadOnly(saving)
        if saving:
            self.status.setText("Saving…")

    def complete_async_save(self, ok: bool, error: str = ""):
        """Finish a background save requested by ``on_save``."""
        self._saving = False
        self._set_saving(False)
        if not ok:
            self._close_after_save = False
            QMessageBox.critical(self, "Save File", error or "Could not save the file.")
            self._update_status()
            return
        self._dirty = False
        self._update_status()
        if self._close_after_save:
            self.accept()

    def reject(self):
        if self._saving:
            QMessageBox.information(self, "Saving", "The file is still being saved.")
            return
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
    # (paths, paths_are_local, target_dir): paths_are_local is True for local
    # filesystem paths (Explorer or a local pane), False for device paths.
    dropped = pyqtSignal(list, bool, str)

    def __init__(self, is_remote: bool = False, parent=None):
        super().__init__(0, 6, parent)
        self.is_remote = is_remote
        self.base_dir = ""
        self.browser = None  # owning FileBrowser (identifies the device of remote drags)
        # Listed names that could not be identified exactly (never acted on).
        self.unsafe_names = frozenset()
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        header = self.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSortIndicator(0, Qt.AscendingOrder)
        header.sectionClicked.connect(self._on_header_clicked)

    def startDrag(self, supportedActions):
        indexes = self.selectedIndexes()
        if not indexes:
            return
        items = [self.itemFromIndex(idx) for idx in indexes if idx.column() == 0]
        mime = self.mimeData([it for it in items if it is not None])
        if not mime:
            return
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

    def apply_sort(self):
        """Re-apply the header's current sort (called after every listing)."""
        header = self.horizontalHeader()
        self.sortByColumn(header.sortIndicatorSection(), header.sortIndicatorOrder())

    def sortByColumn(self, col: int, order: Qt.SortOrder = Qt.AscendingOrder):
        """Sort rows: '..' pinned first, then folders, then files; numeric columns
        (size, date) sort by their raw ``Qt.UserRole`` value."""
        self.setSortingEnabled(False)
        n_rows, n_cols = self.rowCount(), self.columnCount()
        if n_rows == 0:
            return
        if not 0 <= col < n_cols:
            col = 0
        dot_items = None
        rows_data = []

        for r in range(n_rows):
            it0 = self.item(r, 0)
            data0 = it0.data(Qt.UserRole) if it0 else None
            is_pair = isinstance(data0, (tuple, list)) and len(data0) > 1
            row_items = [self.takeItem(r, c) for c in range(n_cols)]
            if is_pair and data0[0] == ".." and dot_items is None:
                dot_items = row_items
                continue
            is_dir = bool(data0[1]) if is_pair else False
            it_col = row_items[col]
            raw_val = it_col.data(Qt.UserRole) if it_col is not None else None
            if isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool):
                key = (0, raw_val, "")
            elif col == 0 and is_pair:
                key = (1, 0, str(data0[0]).lower())
            elif it_col is not None:
                key = (1, 0, it_col.text().lower())
            else:
                key = (1, 0, it0.text().lower() if it0 else "")
            rows_data.append((is_dir, key, row_items))

        reverse = (order == Qt.DescendingOrder)
        dirs = sorted((x for x in rows_data if x[0]), key=lambda x: x[1], reverse=reverse)
        files = sorted((x for x in rows_data if not x[0]), key=lambda x: x[1], reverse=reverse)
        ordered = ([dot_items] if dot_items else []) + [items for _, _, items in dirs + files]

        self.setRowCount(0)
        self.setRowCount(len(ordered))
        for r, items in enumerate(ordered):
            for c, it in enumerate(items):
                if it is not None:
                    self.setItem(r, c, it)

    def sortItems(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder):
        self.sortByColumn(column, order)

    def mimeTypes(self):
        return ["text/uri-list", _MIME]

    def mimeData(self, items):
        mime = QMimeData()
        rows = sorted({it.row() for it in items if it is not None})
        paths = []
        urls = []
        for r in rows:
            it = self.item(r, 0)
            if it:
                data = it.data(Qt.UserRole)
                if data and isinstance(data, (tuple, list)) and data[0] != "..":
                    name = data[0]
                    full = posixpath.join(self.base_dir, name) if self.is_remote else os.path.join(self.base_dir, name)
                    paths.append(full)
                    if not self.is_remote:
                        # No exists() probe: rows come from a fresh listing and a
                        # stat per item would block the UI on slow/network drives.
                        urls.append(QUrl.fromLocalFile(full))
        mime.setData(_MIME, json.dumps({"is_remote": self.is_remote, "paths": paths}).encode("utf-8"))
        if urls:
            mime.setUrls(urls)
        return mime

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(_MIME):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def _drop_target(self, pos) -> str:
        target_folder = self.base_dir
        item = self.itemAt(pos)
        if item:
            it0 = self.item(item.row(), 0)
            data = it0.data(Qt.UserRole) if it0 else None
            if data and isinstance(data, (tuple, list)) and len(data) > 1 and data[1]:
                if data[0] == "..":
                    if self.is_remote:
                        target_folder = posixpath.dirname(self.base_dir.rstrip("/")) or "/"
                    else:
                        target_folder = os.path.dirname(self.base_dir)
                elif self.is_remote:
                    target_folder = posixpath.join(self.base_dir, data[0])
                else:
                    target_folder = os.path.join(self.base_dir, data[0])
        return target_folder

    def dropEvent(self, event):
        source = event.source()
        if source is self:
            event.ignore()
            return

        mime = event.mimeData()
        target_folder = self._drop_target(event.pos())

        if mime.hasFormat(_MIME):
            try:
                raw = json.loads(bytes(mime.data(_MIME)).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raw = None
            if isinstance(raw, dict):
                paths = [p for p in raw.get("paths", []) if isinstance(p, str) and p]
                payload_remote = bool(raw.get("is_remote"))
                if not paths:
                    event.ignore()
                    return
                if not payload_remote:
                    # Local paths (any tab's local pane): copy locally or push.
                    self.dropped.emit(paths, True, target_folder)
                    event.acceptProposedAction()
                    return
                same_device = (source is not None and self.browser is not None
                               and getattr(source, "browser", None) is self.browser)
                if not self.is_remote and same_device:
                    # This device's pane -> this local pane: pull.
                    self.dropped.emit(paths, False, target_folder)
                    event.acceptProposedAction()
                    return
                # Device paths from another device tab (or onto a device pane)
                # can't be pulled/pushed from here: refuse instead of guessing.
                event.ignore()
                return
        if mime.hasUrls():
            files = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
            if files:
                self.dropped.emit(files, True, target_folder)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _mtime_sort_key(mtime: str, now: Optional[float] = None) -> float:
    """Sortable timestamp for ``2026-09-02 21:40``, ``Jan  5 12:34`` or ``Jan  5  2024``."""
    parts = (mtime or "").split()
    try:
        if len(parts) >= 2 and "-" in parts[0]:
            return time.mktime(time.strptime(f"{parts[0]} {parts[1][:5]}", "%Y-%m-%d %H:%M"))
        if len(parts) == 3 and parts[0][:3].lower() in _MONTHS:
            month, day = _MONTHS[parts[0][:3].lower()], int(parts[1])
            if ":" in parts[2]:
                hour, minute = (int(x) for x in parts[2].split(":", 1))
                now = time.time() if now is None else now
                year = time.localtime(now).tm_year
                stamp = time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))
                if stamp > now + 86400:  # "Mon DD HH:MM" is used for the last ~6 months
                    stamp = time.mktime((year - 1, month, day, hour, minute, 0, 0, 0, -1))
                return stamp
            return time.mktime((int(parts[2]), month, day, 0, 0, 0, 0, 0, -1))
    except (ValueError, OverflowError, OSError):
        pass
    return 0.0


# ---- local filesystem workers ------------------------------------------------


def get_windows_drives() -> List[str]:
    """Enumerate mounted drive root paths on Windows (e.g. ['C:\\', 'D:\\'])."""
    if os.name != "nt":
        return ["/"]
    mask = 0
    try:
        import ctypes

        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except (AttributeError, OSError, ValueError):
        mask = 0
    if mask:
        # One bitmask query: no per-drive probe, so empty card readers or dead
        # network mappings can't stall enumeration.
        drives = [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase) if mask & (1 << i)]
    else:
        drives = [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    return drives or ["C:\\"]


def _drive_of(path: str) -> str:
    if os.name != "nt":
        return "/"
    return os.path.splitdrive(path)[0] + "\\"


def _list_local_dir(path: str):
    """Worker: ``(absolute_path, rows)`` for a local folder; raises if not a folder."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise NotADirectoryError(f"Not a folder: {path}")
    dirs = []
    files = []
    with os.scandir(path) as iters:
        for entry in iters:
            try:
                st = entry.stat(follow_symlinks=False)
                is_dir = entry.is_dir()  # folder symlinks/junctions open as folders
            except OSError:
                continue
            size = st.st_size if not is_dir else 0
            try:
                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            except (OSError, OverflowError, ValueError):
                mtime = ""  # e.g. a pre-1970 timestamp on Windows (Errno 22)
            ext = os.path.splitext(entry.name)[1].lower()
            ftype = "Folder" if is_dir else (mimetypes.types_map.get(ext, ext.upper() or "File"))
            perms = oct(st.st_mode)[-3:]
            if is_dir:
                dirs.append((entry.name, 0, "<DIR>", ftype, mtime, perms, "", True, st.st_mtime))
            else:
                files.append((entry.name, size, _human_size(size), ftype, mtime, perms, "", False, st.st_mtime))
    return path, dirs + files


def _local_dest(src: str, dst_dir: str) -> str:
    return os.path.join(dst_dir, os.path.basename(src.rstrip("/\\")))


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _find_local_collisions(sources, dst_dir: str) -> List[str]:
    collisions = []
    for src in sources:
        dst = _local_dest(src, dst_dir)
        if not _same_path(src, dst) and os.path.exists(dst):
            collisions.append(os.path.basename(dst))
    return collisions


def _copy_local_items(sources, dst_dir: str) -> List[str]:
    """Worker: copy files/folders into *dst_dir*; returns error messages."""
    errors = []
    for src in sources:
        dst = _local_dest(src, dst_dir)
        try:
            if _same_path(src, dst):
                continue
            if os.path.isdir(src):
                src_abs = os.path.normcase(os.path.abspath(src)).rstrip("\\/") + os.sep
                if os.path.normcase(os.path.abspath(dst)).startswith(src_abs):
                    raise OSError("cannot copy a folder into itself")
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        except OSError as exc:
            errors.append(f"{src}: {exc}")
    return errors


def _is_junction(path: str) -> bool:
    """True for a Windows directory junction (a mount-point reparse point)."""
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    mount_point = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
    return bool(getattr(st, "st_file_attributes", 0) & reparse) and \
        getattr(st, "st_reparse_tag", 0) == mount_point


def _retry_writable(func, path, exc):
    """rmtree error hook: clear the read-only attribute and retry the removal once."""
    if func in (os.unlink, os.remove, os.rmdir):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
            return
        except OSError:
            pass
    raise exc[1] if isinstance(exc, tuple) else exc


def _rmtree(path: str) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry_writable)
    else:
        shutil.rmtree(path, onerror=_retry_writable)


def _delete_local_items(paths) -> List[str]:
    """Worker: delete files/folders.  A folder symlink or junction loses only the
    link (never its target); read-only files are made writable first."""
    errors = []
    for p in paths:
        try:
            if os.path.islink(p) or _is_junction(p):
                try:
                    os.unlink(p)
                except (IsADirectoryError, PermissionError):
                    os.rmdir(p)  # Windows directory symlink / junction
            elif os.path.isdir(p):
                _rmtree(p)
            else:
                try:
                    os.remove(p)
                except PermissionError:  # [WinError 5] on a read-only file
                    os.chmod(p, stat.S_IWRITE)
                    os.remove(p)
        except OSError as exc:
            errors.append(f"{p}: {exc}")
    return errors


def _create_empty_file(path: str) -> None:
    with open(path, "a"):
        pass


_WIN_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = ({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                 | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})


def _safe_local_name(name: str, windows: Optional[bool] = None) -> str:
    """*name* made valid as a local file name.  Windows forbids ``:`` ``?`` etc.,
    trailing dots/spaces and device names such as ``nul`` or ``con.txt``."""
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return name
    safe = _WIN_BAD_CHARS_RE.sub("_", name)
    trimmed = safe.rstrip(" .")
    if trimmed != safe:
        safe = trimmed + "_" * (len(safe) - len(trimmed))
    if safe.split(".", 1)[0].rstrip(" ").upper() in _WIN_RESERVED:
        safe = "_" + safe
    return safe or "_"


def _transfer_name(path) -> str:
    """Display name of a transfer source (``folder/.`` merge sources included)."""
    text = str(path)
    if text.endswith(("/.", "\\.")):
        text = text[:-2]
    return re.split(r"[/\\]", text.rstrip("/\\"))[-1] or text


def _plan_push(handler, sources, dst_dir: str):
    """Worker: plan local -> device copies into *dst_dir*.

    Returns ``(jobs, collisions, errors)``.  Every job targets the exact device
    path; a folder that already exists there is merged (``src/.``) instead of
    being nested as ``dst/name/name``.
    """
    entries, errors = [], []
    for src in sources:
        name = os.path.basename(os.path.normpath(src))
        if not name or name in (".", ".."):
            errors.append(f"{src}: this item can't be pushed")
            continue
        if not os.path.lexists(src):
            errors.append(f"{src}: not found")
            continue
        entries.append((src, name, os.path.isdir(src), posixpath.join(dst_dir, name)))
    info = _probe_remote(handler, [e[3] for e in entries]) if entries else {}
    jobs, collisions, targets = [], [], set()
    for src, name, is_dir, dst in entries:
        if dst in targets:
            errors.append(f"{name}: another selected item has the same name")
            continue
        targets.add(dst)
        kind = info[dst][0]
        if kind == "b":
            errors.append(f"{name}: the device already has a broken symbolic link with that name")
            continue
        if kind != "n":
            if (kind == "d") != is_dir:
                what = "folder" if kind == "d" else "file"
                errors.append(f"{name}: a {what} with that name already exists on the device")
                continue
            collisions.append(name)
        jobs.append((os.path.join(src, ".") if is_dir and kind == "d" else src, dst, "push"))
    return jobs, collisions, errors


def _plan_pull(handler, sources, dst_dir: str, windows: Optional[bool] = None):
    """Worker: plan device -> local copies into *dst_dir*.

    Returns ``(jobs, collisions, errors, notes)``.  Symlinks are resolved with
    ``readlink -f`` (adbd can't stat a link), names this PC can't store are
    renamed, and an existing local folder is merged (``src/.``), not nested.
    """
    info = _probe_remote(handler, sources) if sources else {}
    jobs, collisions, errors, notes, targets = [], [], [], [], set()
    for src in sources:
        name = posixpath.basename(src.rstrip("/")) or src
        kind, is_link, resolved = info[src]
        if kind == "n":
            errors.append(f"{name}: no longer exists on the device")
            continue
        if kind == "b":
            errors.append(f"{name}: broken symbolic link (its target is missing)")
            continue
        real = resolved if is_link and resolved else src
        if real != src:
            notes.append(f"{name} is a symbolic link; pulling its target {real}")
        local_name = _safe_local_name(name, windows)
        if local_name != name:
            notes.append(f"{name!r} is saved as {local_name!r} (the name isn't valid on this PC)")
        dst = os.path.join(dst_dir, local_name)
        key = os.path.normcase(dst)
        if key in targets:
            errors.append(f"{name}: another selected item would be saved under the same name")
            continue
        targets.add(key)
        is_dir = kind == "d"
        if os.path.lexists(dst):
            if os.path.isdir(dst) != is_dir:
                what = "folder" if os.path.isdir(dst) else "file"
                errors.append(f"{local_name}: a {what} with that name already exists on the PC")
                continue
            collisions.append(local_name)
            if is_dir:
                real = real.rstrip("/") + "/."
        jobs.append((real, dst, "pull"))
    return jobs, collisions, errors, notes


def _plan_remote_copy(handler, sources, dst_dir: str):
    """Worker: plan device -> device copies into *dst_dir*.

    Returns ``(commands, collisions, errors)``.  A folder is never copied into
    itself or its own subfolder, also not through a symlinked path (``cp -r``
    would recurse until "Too many open files").
    """
    dst_dir = _normalize_remote_path(dst_dir)
    dests = {src: posixpath.join(dst_dir, posixpath.basename(src.rstrip("/"))) for src in sources}
    info = _probe_remote(handler, list(sources) + list(dests.values()) + [dst_dir])
    commands, collisions, errors, targets = [], [], [], set()
    if info[dst_dir][0] != "d":
        return commands, collisions, [f"{dst_dir}: the destination folder no longer exists"]
    dst_real = posixpath.normpath(info[dst_dir][2] or dst_dir)
    for src in sources:
        dst = dests[src]
        name = posixpath.basename(dst)
        kind, is_link, resolved = info[src]
        if kind == "n":
            errors.append(f"{name}: no longer exists on the device")
            continue
        n_src = posixpath.normpath(src)
        if n_src == posixpath.normpath(dst):
            errors.append(f"{name}: is already in this folder")
            continue
        if kind == "d" and not is_link:  # cp -r copies a symlink as a link: no recursion
            real_src = posixpath.normpath(resolved or src)
            if posixpath.join(dst_real, name) == real_src:
                errors.append(f"{name}: is already in this folder")
                continue
            if any(b == a or b.startswith(a.rstrip("/") + "/")
                   for a, b in ((n_src, dst_dir), (real_src, dst_real))):
                errors.append(f"{name}: a folder can't be copied into itself or its own subfolder")
                continue
        if dst in targets:
            errors.append(f"{name}: another selected item has the same name")
            continue
        targets.add(dst)
        dkind = info[dst][0]
        merge = False
        if dkind != "n":
            if is_link or dkind == "b" or (dkind == "d") != (kind == "d"):
                errors.append(f"{name}: a different item with that name already exists here")
                continue
            collisions.append(name)
            merge = kind == "d"
        commands.append((f"copy {name}", _copy_into_cmd(src, dst, merge)))
    return commands, collisions, errors


class _TransferThread(QThread):
    progress = pyqtSignal(int)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, handler, direction, a, b):
        super().__init__()
        self.handler, self.direction, self.a, self.b = handler, direction, a, b
        self.cancel_event = threading.Event()

    def stop(self):
        self.cancel_event.set()

    def run(self):
        try:
            if self.direction == "push":
                res = self.handler.push(self.a, self.b,
                                        on_progress=self.progress.emit,
                                        cancel_event=self.cancel_event, safe=False)
            else:
                res = self.handler.pull(self.a, self.b,
                                        on_progress=self.progress.emit,
                                        cancel_event=self.cancel_event, safe=False)
            self.done.emit(str(res))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class FileBrowser(QWidget):
    log = pyqtSignal(str)

    COLUMNS = ["Name", "Size", "Type", "Date Modified", "Permissions", "Owner"]

    _parse_ls_line = staticmethod(_parse_ls_line)

    def __init__(self, handler, start="/sdcard", parent=None):
        super().__init__(parent)
        self.handler = handler
        self.local_cwd = os.path.expanduser("~")
        self.remote_cwd = start
        self._jobs = []           # listing / file-op workers (detached on close)
        self._save_jobs = []      # editor saves: never detached, the editor needs the result
        self._editors = []
        self._queue = []          # pending transfers: (src, dst, direction)
        self._transfer = None     # the single active _TransferThread
        self._cancel = threading.Event()
        self._clipboard = []  # items in copy buffer
        self._clipboard_src = ""
        self._ls = None
        self._remote_gen = 0
        self._local_gen = 0
        self._remote_refresh_pending = False
        self._closing = False
        self._local_loading_path = None   # local folder whose listing is on its way
        self._remote_loading_path = None  # device folder whose listing is on its way
        self._remote_shown = None         # device folder the table last listed successfully
        self._remote_failed = False       # the current device folder could not be listed
        self._cancelled_transfer = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Page toolbar: quick jumps for this PC (left) and the device (right).
        toolbar = QWidget()
        toolbar.setObjectName("pageToolbar")
        toolbar.setAttribute(Qt.WA_StyledBackground, True)
        from .flowlayout import ToolbarFlowLayout

        # Rows that wrap instead of widening the window when the tab is narrow.
        qbar = ToolbarFlowLayout(toolbar, hspacing=8, vspacing=6)
        qbar.setContentsMargins(12, 8, 12, 8)
        # Colour language: this PC is blue, the device is green (Push/Pull match).
        jump_pc = QLabel("PC folders")
        jump_pc.setObjectName("mutedHint")
        qbar.addWidget(jump_pc)
        home = os.path.expanduser("~")
        for lbl, glyph, p in (("Home", "house", home),
                              ("Desktop", "monitor", os.path.join(home, "Desktop")),
                              ("Downloads", "download", os.path.join(home, "Downloads"))):
            b = QPushButton(lbl); b.setProperty("role", "ghost")
            b.setIcon(icon(glyph, "blue"))
            b.setToolTip(f"Open {p}")
            b.clicked.connect(lambda _=False, path=p: self._jump_local(path))
            qbar.addWidget(b)
        qbar.addStretch(1)
        jump_dev = QLabel("Device folders")
        jump_dev.setObjectName("mutedHint")
        qbar.addWidget(jump_dev)
        for lbl, glyph, p in (("/sdcard", "smartphone", "/sdcard"),
                              ("/data/local/tmp", "folder", "/data/local/tmp"),
                              ("/", "folder", "/")):
            b = QPushButton(lbl); b.setProperty("role", "ghost")
            b.setIcon(icon(glyph, "green"))
            b.setToolTip(f"Open {p} on the device")
            b.clicked.connect(lambda _=False, path=p: self._jump_remote(path))
            qbar.addWidget(b)
        lay.addWidget(toolbar)

        body = QVBoxLayout()
        body.setContentsMargins(12, 12, 12, 8)
        body.setSpacing(8)
        lay.addLayout(body, 1)

        # Main Splitter: [Local PC Table] | [Center Transfer Controls] | [Android Device Table]
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(8)

        # ----------------- Left Pane: Local PC -----------------
        left_w = QWidget()
        left_lay = QVBoxLayout(left_w)
        left_lay.setContentsMargins(0, 0, 0, 0); left_lay.setSpacing(8)

        ltop = ToolbarFlowLayout(hspacing=8, vspacing=6)
        ltitle = self._pane_title("This PC", "monitor", "blue")
        self.cmb_drives = QComboBox()
        # Filled by a worker (_on_drives); starts with the current drive only.
        self.cmb_drives.addItem(_drive_of(self.local_cwd))
        self.cmb_drives.setToolTip("Drive")
        self.cmb_drives.currentTextChanged.connect(self._on_drive_changed)
        self.local_path = QLineEdit(self.local_cwd)
        self.local_path.returnPressed.connect(self._go_local)
        lup = QPushButton("Up"); lup.setProperty("role", "ghost")
        lup.setIcon(icon("arrow-up", "accent"))
        lup.setToolTip("Parent folder")
        lup.clicked.connect(self._up_local)
        lref = QPushButton("Refresh")
        lref.setProperty("role", "ghost")
        lref.setIcon(icon("refresh", "green"))
        lref.setToolTip("Refresh local listing (F5)")
        lref.clicked.connect(self.refresh_local)

        ltop.addWidget(ltitle)
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

        left_lay.addLayout(self._pane_ops((
            ("New folder", self._local_mkdir),
            ("New file", self._local_newfile),
            ("Edit", self._local_edit),
            ("Copy", self._local_copy),
            ("Paste", self._local_paste),
            ("Rename", self._local_rename),
        ), self._local_delete))

        # ----------------- Center Action Column -----------------
        center_w = QWidget()
        center_lay = QVBoxLayout(center_w)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(8)
        center_lay.addStretch(1)

        self.btn_push = QPushButton("Push")
        self.btn_push.setProperty("tone", "blue")
        self.btn_push.setIcon(icon("arrow-right", "blue"))
        # icon after the text, so the button reads "Push →"
        self.btn_push.setLayoutDirection(Qt.RightToLeft)
        self.btn_push.setToolTip("Push selected local files to the Android device folder")
        self.btn_push.setFixedWidth(84)
        self.btn_push.setFixedHeight(32)
        self.btn_push.clicked.connect(self.push_selected)
        center_lay.addWidget(self.btn_push)

        self.btn_pull = QPushButton("Pull")
        self.btn_pull.setProperty("tone", "green")
        self.btn_pull.setIcon(icon("arrow-left", "green"))
        self.btn_pull.setToolTip("Pull selected Android files to the local PC folder")
        self.btn_pull.setFixedWidth(84)
        self.btn_pull.setFixedHeight(32)
        self.btn_pull.clicked.connect(self.pull_selected)
        center_lay.addWidget(self.btn_pull)

        center_lay.addStretch(1)

        # ----------------- Right Pane: Android Device -----------------
        right_w = QWidget()
        right_lay = QVBoxLayout(right_w)
        right_lay.setContentsMargins(0, 0, 0, 0); right_lay.setSpacing(8)

        rtop = ToolbarFlowLayout(hspacing=8, vspacing=6)
        rtitle = self._pane_title("Device", "smartphone", "green")
        self.remote_path = QLineEdit(self.remote_cwd)
        self.remote_path.returnPressed.connect(self._go_remote)
        rup = QPushButton("Up"); rup.setProperty("role", "ghost")
        rup.setIcon(icon("arrow-up", "accent"))
        rup.setToolTip("Parent folder")
        rup.clicked.connect(self._up_remote)
        rref = QPushButton("Refresh")
        rref.setProperty("role", "ghost")
        rref.setIcon(icon("refresh", "green"))
        rref.setToolTip("Refresh device listing (F5)")
        rref.clicked.connect(self.refresh_remote)

        rtop.addWidget(rtitle)
        rtop.addWidget(self.remote_path, 1)
        rtop.addWidget(rup); rtop.addWidget(rref)
        right_lay.addLayout(rtop)

        self.remote_table = self._create_table(is_remote=True)
        self.remote_table.cellDoubleClicked.connect(self._on_remote_double_click)
        self.remote_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.remote_table.customContextMenuRequested.connect(self._context_remote)
        self.remote_table.dropped.connect(self._on_remote_dropped)
        right_lay.addWidget(self.remote_table, 1)

        right_lay.addLayout(self._pane_ops((
            ("New folder", self._remote_mkdir),
            ("New file", self._remote_newfile),
            ("Edit", self._remote_edit),
            ("Copy", self._remote_copy),
            ("Paste", self._remote_paste),
            ("Rename", self._remote_rename),
        ), self._remote_delete))

        split.addWidget(left_w)
        split.addWidget(center_w)
        split.addWidget(right_w)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 0)
        split.setStretchFactor(2, 5)
        split.setSizes([460, 92, 460])
        body.addWidget(split, 1)

        # Bottom Progress Bar, with Cancel for the running transfer and the queue
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.btn_cancel_transfer = QPushButton("Cancel")
        self.btn_cancel_transfer.setProperty("role", "danger")
        self.btn_cancel_transfer.setIcon(icon("x", "on-danger"))
        self.btn_cancel_transfer.setToolTip("Stop the running transfer and clear the queue")
        self.btn_cancel_transfer.setVisible(False)
        self.btn_cancel_transfer.clicked.connect(self.cancel_transfers)
        bar_row = QHBoxLayout()
        bar_row.setSpacing(8)
        bar_row.addWidget(self.bar, 1)
        bar_row.addWidget(self.btn_cancel_transfer)
        body.addLayout(bar_row)

        self.hint = QLabel("Drag and drop between panes or from Explorer   ·   F4 Edit   ·   "
                           "F5 Refresh   ·   F2 Rename   ·   Del Delete")
        self.hint.setObjectName("mutedHint")
        body.addWidget(self.hint)

        # Shortcuts.  The table shortcuts exist once per table, so each is scoped
        # to its table: two window-wide shortcuts on the same key are ambiguous
        # to Qt, and then neither fires.
        QShortcut(QKeySequence("F5"), self, activated=self._on_f5)
        for table, edit, delete, rename, copy, paste in (
                (self.local_table, self._local_edit, self._local_delete, self._local_rename,
                 self._local_copy, self._local_paste),
                (self.remote_table, self._remote_edit, self._remote_delete, self._remote_rename,
                 self._remote_copy, self._remote_paste)):
            for key, slot in ((QKeySequence("F4"), edit),
                              (QKeySequence(QKeySequence.Delete), delete),
                              (QKeySequence("F2"), rename),
                              (QKeySequence(QKeySequence.Copy), copy),
                              (QKeySequence(QKeySequence.Paste), paste)):
                QShortcut(key, table, activated=slot, context=Qt.WidgetWithChildrenShortcut)

        # The device listing starts lazily on first show (one listing, not two).
        self._loaded_remote = False
        self._job(get_windows_drives, self._on_drives,
                  lambda msg: self.log.emit(f"[ERROR] drive list: {msg}"))
        self.refresh_local()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._loaded_remote:
            self.refresh_remote()

    @staticmethod
    def _pane_title(text: str, glyph: str, tone: str) -> QToolButton:
        """A pane heading with its icon (a flat, non-interactive tool button, so
        the icon follows live theme switches like every other icon)."""
        title = QToolButton()
        title.setText(text)
        title.setIcon(icon(glyph, tone))
        title.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        title.setProperty("role", "ghost")
        title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        title.setFocusPolicy(Qt.NoFocus)
        title.setObjectName("paneTitle")  # bold, flush left (theme.py)
        return title

    @staticmethod
    def _pane_ops(actions, delete_slot):
        """A pane's file-operation row: plain actions left, Delete on the right."""
        from .flowlayout import ToolbarFlowLayout

        row = ToolbarFlowLayout(hspacing=6, vspacing=6)
        for text, fn in actions:
            b = QPushButton(text)
            b.setProperty("role", "ghost")
            glyph, tone = _PANE_OP_ICONS.get(text, ("file", "dim"))
            b.setIcon(icon(glyph, tone))
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        delete = QPushButton("Delete")
        delete.setProperty("role", "danger")
        delete.setIcon(icon("trash", "on-danger"))
        delete.clicked.connect(delete_slot)
        row.addWidget(delete)
        return row

    def _create_table(self, is_remote: bool = False) -> _FileTableWidget:
        table = _FileTableWidget(is_remote=is_remote, parent=self)
        table.browser = self
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

    # ---- Worker plumbing ----
    def _guarded(self, fn):
        """Wrap a job callback so it is skipped once the panel is closing/deleted."""
        if fn is None:
            return None

        def call(*args):
            if self._closing or not _alive(self):
                return
            fn(*args)
        return call

    def _job(self, fn, on_done=None, on_fail=None):
        return run_job(self._jobs, fn, self._guarded(on_done), self._guarded(on_fail))

    def _local_job(self, label: str, fn, error_title: str = ""):
        """Run a local filesystem operation off the UI thread, then refresh."""
        def done(errors):
            for err in errors or ():
                self.log.emit(f"[ERROR] {label}: {err}")
            if not errors:
                self.log.emit(f"[OK] {label}")
            self.refresh_local()

        def fail(msg):
            self.log.emit(f"[ERROR] {label}: {msg}")
            if error_title:
                QMessageBox.warning(self, error_title, msg)
            self.refresh_local()

        self._job(fn, done, fail)

    @staticmethod
    def _set_loading(table: QTableWidget):
        table.setRowCount(0)
        table.insertRow(0)
        it = QTableWidgetItem(_cached_icon("refresh", "dim"), "Loading…")
        it.setFlags(Qt.ItemIsEnabled)
        table.setItem(0, 0, it)

    def _populate(self, table: QTableWidget, rows, parent_row: bool):
        if isinstance(table, _FileTableWidget):
            unsafe, seen = set(getattr(rows, "uncertain", ())), set()
            for row in rows:
                if row[0] in seen:
                    unsafe.add(row[0])  # two rows share a name: can't tell them apart
                seen.add(row[0])
            table.unsafe_names = frozenset(unsafe)
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(0)
            if parent_row:
                self._add_row(table, "..", 0, "<DIR>", "Folder", "", "", "", is_dir=True)
            for row in rows:
                self._add_row(table, *row)
            if isinstance(table, _FileTableWidget):
                table.apply_sort()
        finally:
            table.setUpdatesEnabled(True)

    # ---- Navigation: Local PC ----
    def _on_drives(self, drives):
        current = _drive_of(self.local_cwd)
        self.cmb_drives.blockSignals(True)
        try:
            self.cmb_drives.clear()
            self.cmb_drives.addItems(list(drives))
            # -1 (blank) for a UNC path, so picking any drive navigates again.
            self.cmb_drives.setCurrentIndex(self.cmb_drives.findText(current))
        finally:
            self.cmb_drives.blockSignals(False)

    def _on_drive_changed(self, drive: str):
        if drive:
            self._navigate_local(drive)

    def _jump_local(self, path: str):
        self._navigate_local(path)

    def _sync_drive_combo(self):
        drive = _drive_of(self.local_cwd)
        if drive and self.cmb_drives.currentText() != drive:
            # A UNC share isn't in the list: show no drive instead of a stale one.
            idx = self.cmb_drives.findText(drive)
            self.cmb_drives.blockSignals(True)
            self.cmb_drives.setCurrentIndex(idx)
            self.cmb_drives.blockSignals(False)

    def _go_local(self):
        self._navigate_local(self.local_path.text().strip() or self.local_cwd)

    def _up_local(self):
        parent = os.path.dirname(self.local_cwd)
        if parent and parent != self.local_cwd:
            self._navigate_local(parent)

    def refresh_local(self):
        self._navigate_local(self.local_cwd)

    def _navigate_local(self, path: str):
        """List *path* on a worker; ``local_cwd`` changes only if it is a folder."""
        if self._closing:
            return
        self._local_gen += 1
        gen = self._local_gen
        self._local_loading_path = path
        self.local_path.setText(path)
        self._set_loading(self.local_table)
        self._job(lambda: _list_local_dir(path),
                  lambda result: self._on_local_listed(gen, result),
                  lambda msg: self._on_local_list_failed(gen, path, msg))

    def _on_local_listed(self, gen: int, result):
        if gen != self._local_gen:
            return  # the user navigated again; a newer listing is on its way
        self._local_loading_path = None
        path, rows = result
        self.local_cwd = path
        self.local_path.setText(path)
        self.local_table.base_dir = path
        self._sync_drive_combo()
        parent = os.path.dirname(path)
        self._populate(self.local_table, rows, parent_row=bool(parent) and parent != path)

    def _on_local_list_failed(self, gen: int, path: str, msg: str):
        if gen != self._local_gen:
            return
        self._local_loading_path = None
        self.log.emit(f"[ERROR] local list {path}: {msg}")
        if not _same_path(path, self.local_cwd):
            self._navigate_local(self.local_cwd)  # stay where we were
            return
        self.local_path.setText(self.local_cwd)
        self.local_table.base_dir = self.local_cwd
        parent = os.path.dirname(self.local_cwd)
        self._populate(self.local_table, [], parent_row=bool(parent) and parent != self.local_cwd)

    def _on_local_double_click(self, row: int, _col: int):
        it = self.local_table.item(row, 0)
        if not it:
            return
        data = it.data(Qt.UserRole)
        if not data:
            return
        name, is_dir = data
        if not is_dir:
            self._local_edit_row(row)  # the clicked row, not the first selected one
            return
        if name == "..":
            self._up_local()
        else:
            self._navigate_local(os.path.join(self.local_cwd, name))

    # ---- Navigation: Android Device ----
    def _jump_remote(self, path: str):
        self.remote_cwd = _normalize_remote_path(path)
        self.refresh_remote()

    def _go_remote(self):
        p = self.remote_path.text().strip()
        if p:
            self.remote_cwd = _normalize_remote_path(p)
        self.refresh_remote()

    def _up_remote(self):
        if self.remote_cwd in ("/", ""):
            return
        self.remote_cwd = posixpath.dirname(self.remote_cwd.rstrip("/")) or "/"
        self.refresh_remote()

    def refresh_remote(self):
        if self._closing:
            return
        self._loaded_remote = True
        self._remote_loading_path = self.remote_cwd
        self.remote_path.setText(self.remote_cwd)
        self.remote_table.base_dir = self.remote_cwd
        self._set_loading(self.remote_table)
        if thread_running(self._ls):
            # Coalesce rapid navigation/F5 requests into one refresh for the
            # newest directory instead of piling up stale ADB ls calls.
            self._remote_refresh_pending = True
            return
        self._remote_refresh_pending = False
        self._remote_gen += 1
        gen = self._remote_gen
        path = self.remote_cwd
        handler = self.handler
        self._ls = self._job(lambda: _list_remote_dir(handler, path),
                             lambda result: self._on_remote_listed(gen, path, result),
                             lambda msg: self._on_remote_list_failed(gen, path, msg))

    def _restart_pending_remote(self, gen: int) -> bool:
        """True when this result must be dropped (stale, or superseded by a pending refresh)."""
        if gen != self._remote_gen:
            return True
        self._ls = None
        if self._remote_refresh_pending:
            self._remote_refresh_pending = False
            self.refresh_remote()
            return True
        return False

    def _on_remote_listed(self, gen: int, path: str, result):
        if self._restart_pending_remote(gen):
            return
        rows, error = result
        if error and not rows:
            self._remote_listing_failed(path, error)
            return
        if error:
            self.log.emit(f"[ERROR] ls {path}: {error}")
        self._remote_loading_path = None
        self._remote_shown = path
        self._remote_failed = False
        self.remote_table.base_dir = path
        self._populate(self.remote_table, rows, parent_row=True)
        unsafe = sorted(self.remote_table.unsafe_names)
        if unsafe:
            preview = ", ".join(repr(n) for n in unsafe[:4]) + ("…" if len(unsafe) > 4 else "")
            self.log.emit(f"[WARN] ls {path}: {len(unsafe)} name(s) could not be read exactly and "
                          f"can't be transferred, renamed or deleted from here: {preview}")

    def _on_remote_list_failed(self, gen: int, path: str, msg: str):
        if self._restart_pending_remote(gen):
            return
        self._remote_listing_failed(path, msg)

    def _remote_listing_failed(self, path: str, msg: str):
        """Stay in the last folder that listed: a mistyped path never becomes the cwd."""
        self.log.emit(f"[ERROR] ls {path}: {msg}")
        self._remote_loading_path = None
        shown = self._remote_shown
        if shown is not None and shown != path:
            self.remote_cwd = shown
            self.refresh_remote()
            return
        self._remote_failed = True
        self.remote_table.base_dir = self.remote_cwd
        self._populate(self.remote_table, [], parent_row=True)

    def _on_remote_double_click(self, row: int, _col: int):
        it = self.remote_table.item(row, 0)
        if not it:
            return
        data = it.data(Qt.UserRole)
        if not data:
            return
        name, is_dir = data
        if not is_dir:
            self._remote_edit_row(row)  # the clicked row, not the first selected one
            return
        if name == "..":
            self._up_remote()
        else:
            self.remote_cwd = posixpath.join(self.remote_cwd, name)
            self.refresh_remote()

    def refresh(self):
        self.refresh_local()
        self.refresh_remote()

    def _add_row(self, table: QTableWidget, name: str, raw_size: int, sz_str: str,
                 ftype: str, mtime: str, perms: str, owner: str, is_dir: bool, raw_mtime: float = None):
        row = table.rowCount()
        table.insertRow(row)

        # The visible text is the exact name; the kind shows as the item's icon
        # (DecorationRole), so nothing that reads or sorts names sees a prefix.
        it_name = _FileItem(name)
        it_name.setData(Qt.UserRole, (name, is_dir))
        glyph, tone = _entry_icon_key(name, is_dir, ftype)
        it_name.setIcon(_cached_icon(glyph, tone))
        it_name.setData(ICON_ROLE, glyph)
        it_size = _FileItem(sz_str)
        it_size.setData(Qt.UserRole, raw_size)
        it_type = _FileItem(ftype)
        it_date = _FileItem(mtime)
        if raw_mtime is not None:
            it_date.setData(Qt.UserRole, raw_mtime)
        else:
            it_date.setData(Qt.UserRole, _mtime_sort_key(mtime))
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
    @staticmethod
    def _row_entry(table, row: int):
        """``(name, is_dir)`` of a real file row (not '..' or 'Loading…'), else None."""
        it = table.item(row, 0)
        data = it.data(Qt.UserRole) if it else None
        if not isinstance(data, (tuple, list)) or len(data) < 2 or data[0] == "..":
            return None
        return data[0], data[1]

    def _selected_rows(self, table) -> List[int]:
        rows = sorted({idx.row() for idx in table.selectedIndexes()})
        return [r for r in rows if self._row_entry(table, r) is not None]

    def _selected_local(self) -> List[Tuple[str, bool]]:
        entries = (self._row_entry(self.local_table, r) for r in self._selected_rows(self.local_table))
        return [(os.path.join(self.local_cwd, name), is_dir) for name, is_dir in entries]

    def _selected_remote(self) -> List[Tuple[str, bool]]:
        return [self._row_entry(self.remote_table, r) for r in self._selected_rows(self.remote_table)]

    def _refuse_unsafe(self, table, names, title: str) -> bool:
        """Refuse (True) when a name could not be identified exactly in the listing."""
        unsafe = getattr(table, "unsafe_names", ())
        bad = [n for n in names if n in unsafe]
        if not bad:
            return False
        preview = ", ".join(repr(n) for n in bad[:4]) + ("…" if len(bad) > 4 else "")
        self.log.emit(f"[ERROR] {title}: refused for inexactly listed name(s): {preview}")
        QMessageBox.warning(
            self, title,
            f"{len(bad)} selected item(s) could not be identified exactly (unusual characters in "
            f"the name, or two entries that look the same): {preview}\n\n"
            "Nothing was done, so the wrong item can't be affected. Use a device shell for these.")
        return True

    def _local_dir_for_action(self, title: str) -> Optional[str]:
        """The local folder the user is looking at, or None while another one is loading."""
        loading = self._local_loading_path
        if loading is not None and not _same_path(loading, self.local_cwd):
            QMessageBox.information(self, title, "The local folder is still loading. "
                                                 "Try again when its listing appears.")
            return None
        return self.local_cwd

    def _remote_dir_for_action(self, title: str) -> Optional[str]:
        """The device folder the user is looking at, or None while it is loading or
        could not be listed (so nothing is created under a mistyped path)."""
        loading = self._remote_loading_path
        if loading is not None and loading != self._remote_shown:
            QMessageBox.information(self, title, "The device folder is still loading. "
                                                 "Try again when its listing appears.")
            return None
        if self._remote_failed:
            QMessageBox.information(self, title, f"The device folder {self.remote_cwd} could not "
                                                 "be listed, so nothing was done there.")
            return None
        return self.remote_cwd

    # ---- Push / Pull Operations ----
    def push_selected(self):
        items = self._selected_local()
        if not items:
            QMessageBox.information(self, "Push", "Select one or more items on Local PC to push.")
            return
        dst_dir = self._remote_dir_for_action("Push")
        if dst_dir is not None:
            self._start_push([path for path, _ in items], dst_dir)

    def pull_selected(self):
        items = self._selected_remote()
        if not items:
            QMessageBox.information(self, "Pull", "Select one or more items on Android Device to pull.")
            return
        if self._refuse_unsafe(self.remote_table, [name for name, _ in items], "Pull"):
            return
        dst_dir = self._local_dir_for_action("Pull")
        if dst_dir is not None:
            self._start_pull([posixpath.join(self.remote_cwd, name) for name, _ in items], dst_dir)

    def _start_push(self, sources, dst_dir: str):
        """Check the device side on a worker, ask before overwriting, then queue."""
        sources = [s for s in sources if s]
        if self._closing or not sources:
            return
        handler = self.handler
        self._job(lambda: _plan_push(handler, sources, dst_dir),
                  lambda plan: self._apply_transfer_plan(
                      "Push", "Pushing", dst_dir, plan[0], plan[1], plan[2], (), "on the device"),
                  lambda msg: self._report_errors("Push", [msg]))

    def _start_pull(self, sources, dst_dir: str):
        """Resolve links and check the local side on a worker, ask, then queue."""
        sources = [s for s in sources if s]
        if self._closing or not sources:
            return
        handler = self.handler
        self._job(lambda: _plan_pull(handler, sources, dst_dir),
                  lambda plan: self._apply_transfer_plan(
                      "Pull", "Pulling", dst_dir, plan[0], plan[1], plan[2], plan[3], "on the PC"),
                  lambda msg: self._report_errors("Pull", [msg]))

    def _apply_transfer_plan(self, title, verb, dst_dir, jobs, collisions, errors, notes, where):
        for note in notes:
            self.log.emit(note)
        if errors:
            self._report_errors(title, errors)
        if not jobs:
            return
        if collisions and not self._ask_overwrite(collisions, where):
            self.log.emit(f"{title} cancelled: nothing was overwritten.")
            return
        self._enqueue_transfers(jobs, f"{verb} {len(jobs)} item(s) to {dst_dir}…")

    def _report_errors(self, title: str, errors):
        for err in errors:
            self.log.emit(f"[ERROR] {title}: {err}")
        text = "\n".join(errors[:8])
        if len(errors) > 8:
            text += f"\n… and {len(errors) - 8} more (see the log)"
        QMessageBox.warning(self, title, text)

    def _on_f5(self):
        if self.remote_table.hasFocus():
            self.refresh_remote()
        else:
            self.refresh_local()

    def _enqueue_transfers(self, jobs, message: str):
        """Append transfers to the queue; only one transfer thread runs at a time."""
        if self._closing or not jobs:
            return
        busy = self._transfer is not None
        self._queue.extend(jobs)
        if busy:
            message += f" (queued; {len(self._queue)} waiting)"
        self.log.emit(message)
        if not busy:
            self._process_queue()

    def _process_queue(self):
        if self._closing or self._transfer is not None:
            return
        if not self._queue:
            self.bar.setVisible(False)
            self.btn_cancel_transfer.setVisible(False)
            self.refresh_local()
            self.refresh_remote()
            return

        src, dst, direction = self._queue.pop(0)
        name = _transfer_name(src)
        waiting = f"  ({len(self._queue)} queued)" if self._queue else ""
        self.bar.setFormat(f"{direction} {name}: %p%{waiting}")
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.btn_cancel_transfer.setEnabled(True)
        self.btn_cancel_transfer.setVisible(True)

        t = _TransferThread(self.handler, direction, src, dst)
        self._transfer = t
        t.progress.connect(self.bar.setValue)
        t.done.connect(lambda _res, t=t: self._on_transfer_finished(t, True, ""))
        t.failed.connect(lambda err, t=t: self._on_transfer_finished(t, False, err))
        # Safety net: a thread that ends without done/failed must not wedge the queue.
        t.finished.connect(lambda t=t: self._on_transfer_finished(t, False, "transfer stopped"))
        park_thread(t)
        t.start()

    def _on_transfer_finished(self, t, ok: bool, message: str):
        if t is not self._transfer:
            return
        self._transfer = None
        if self._closing or not _alive(self):
            return
        if self._cancelled_transfer is t:
            self._cancelled_transfer = None
            self.log.emit(f"[CANCELLED] {t.direction} {_transfer_name(t.a)}"
                          + ("" if ok else " (a partial copy may remain)"))
        elif ok:
            self.log.emit(f"[OK] {t.direction}: {_transfer_name(t.a)}")
        else:
            self.log.emit(f"[ERROR] {t.direction} {t.a}: {message}")
        self._process_queue()

    def cancel_transfers(self):
        """Stop the running transfer and drop everything still queued."""
        dropped = len(self._queue)
        self._queue.clear()
        t = self._transfer
        if t is None:
            if dropped:
                self.log.emit(f"Cleared {dropped} queued transfer(s).")
            return
        self._cancelled_transfer = t
        try:
            t.stop()
        except RuntimeError:
            pass
        self.bar.setFormat("Cancelling…")
        self.btn_cancel_transfer.setEnabled(False)
        extra = f" and {dropped} queued transfer(s)" if dropped else ""
        self.log.emit(f"Cancelling {t.direction} {_transfer_name(t.a)}{extra}…")

    # ---- File Operations: Local ----
    def _local_mkdir(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if ok and name.strip():
            base = self._local_dir_for_action("New Folder")
            if base is None:
                return
            target = os.path.join(base, name.strip())
            self._local_job(f"mkdir {name.strip()}",
                            lambda: os.makedirs(target, exist_ok=True), "Error")

    def _local_newfile(self):
        name, ok = QInputDialog.getText(self, "New File", "File name:")
        if ok and name.strip():
            base = self._local_dir_for_action("New File")
            if base is None:
                return
            target = os.path.join(base, name.strip())
            self._local_job(f"create {name.strip()}", lambda: _create_empty_file(target), "Error")

    def _local_copy(self):
        items = self._selected_local()
        if items:
            self._clipboard = [p for p, _ in items]
            self._clipboard_src = "local"
            self.log.emit(f"Copied {len(self._clipboard)} local item(s) to clipboard.")

    def _local_paste(self):
        if not self._clipboard:
            return
        base = self._local_dir_for_action("Paste")
        if base is None:
            return
        if self._clipboard_src == "local":
            self._copy_local_into(list(self._clipboard), base, "paste")
        else:
            # Clipboard is from device -> pull to current local folder
            self._start_pull(list(self._clipboard), base)

    def _copy_local_into(self, sources, dst_dir: str, label: str):
        """Collision check and copy both run on workers; only the prompt is on the UI."""
        sources = [s for s in sources if s]
        if not sources:
            return

        def checked(collisions):
            if collisions and not self._ask_overwrite(collisions):
                return
            self._local_job(f"{label} {len(sources)} item(s)",
                            lambda: _copy_local_items(sources, dst_dir))

        self._job(lambda: _find_local_collisions(sources, dst_dir), checked,
                  lambda msg: self.log.emit(f"[ERROR] {label}: {msg}"))

    def _local_rename(self):
        items = self._selected_local()
        if len(items) != 1:
            return
        old_path = items[0][0]
        old_name = os.path.basename(old_path)
        new_name, ok = QInputDialog.getText(self, "Rename Local Item", "New name:", text=old_name)
        new_name = new_name.strip() if ok else ""
        if new_name and new_name != old_name:
            new_path = os.path.join(os.path.dirname(old_path), new_name)
            self._local_job(f"rename {old_name}", lambda: os.rename(old_path, new_path), "Error")

    def _local_delete(self):
        items = self._selected_local()
        if not items:
            return
        if QMessageBox.question(self, "Delete Local",
                                f"Permanently delete {len(items)} item(s) from Local PC?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        paths = [p for p, _ in items]
        self._local_job(f"delete {len(paths)} local item(s)", lambda: _delete_local_items(paths))

    # ---- File Operations: Remote ----
    def _remote_mkdir(self):
        name, ok = QInputDialog.getText(self, "New Device Folder", "Folder name:")
        if ok and name.strip():
            base = self._remote_dir_for_action("New Device Folder")
            if base is None:
                return
            self._run_shell(f"mkdir {name.strip()}",
                            _mkdir_cmd(posixpath.join(base, name.strip())))

    def _remote_newfile(self):
        name, ok = QInputDialog.getText(self, "New Device File", "File name:")
        if ok and name.strip():
            base = self._remote_dir_for_action("New Device File")
            if base is None:
                return
            self._run_shell(f"create {name.strip()}",
                            _touch_cmd(posixpath.join(base, name.strip())))

    def _remote_copy(self):
        items = self._selected_remote()
        if items:
            if self._refuse_unsafe(self.remote_table, [name for name, _ in items], "Copy"):
                return
            self._clipboard = [posixpath.join(self.remote_cwd, name) for name, _ in items]
            self._clipboard_src = "remote"
            self.log.emit(f"Copied {len(self._clipboard)} device item(s) to clipboard.")

    def _remote_paste(self):
        if not self._clipboard:
            return
        base = self._remote_dir_for_action("Paste")
        if base is None:
            return
        if self._clipboard_src == "remote":
            self._start_remote_copy(list(self._clipboard), base)
        else:
            # Clipboard is from Local -> push to current device folder
            self._start_push(list(self._clipboard), base)

    def _start_remote_copy(self, sources, dst_dir: str):
        """Device-side paste: refuse self-recursion and ask before merging/overwriting."""
        if self._closing or not sources:
            return
        handler = self.handler

        def planned(plan):
            commands, collisions, errors = plan
            if errors:
                self._report_errors("Paste", errors)
            if not commands:
                return
            if collisions and not self._ask_overwrite(collisions, "in this device folder"):
                self.log.emit("Paste cancelled: nothing was overwritten.")
                return
            self._run_shell_batch(commands, timeout=600)

        self._job(lambda: _plan_remote_copy(handler, sources, dst_dir), planned,
                  lambda msg: self._report_errors("Paste", [msg]))

    def _remote_rename(self):
        rows = self._selected_rows(self.remote_table)
        if len(rows) != 1:
            return
        old_name = self._row_entry(self.remote_table, rows[0])[0]
        if self._refuse_unsafe(self.remote_table, [old_name], "Rename"):
            return
        new_name, ok = QInputDialog.getText(self, "Rename Device Item", "New name:", text=old_name)
        new_name = new_name.strip() if ok else ""
        if not new_name or new_name == old_name:
            return
        if "/" in new_name or new_name in (".", ".."):
            QMessageBox.warning(self, "Rename", f"{new_name!r} is not a valid name.")
            return
        src = posixpath.join(self.remote_cwd, old_name)
        dst = posixpath.join(self.remote_cwd, new_name)
        handler = self.handler
        self._job(lambda: handler.shell(_rename_check_cmd(src, dst), timeout=30, safe=False),
                  lambda res: self._on_rename_checked(old_name, new_name, src, dst, res),
                  lambda msg: self._report_errors("Rename", [msg]))

    def _on_rename_checked(self, old_name: str, new_name: str, src: str, dst: str, res):
        lines = _output_lines(getattr(res, "stdout", ""))
        state = lines[-1].strip() if lines else ""
        if state == "dir":
            QMessageBox.warning(self, "Rename", f"A folder named {new_name!r} already exists here.")
            return
        if state == "file":
            if QMessageBox.question(
                    self, "Rename",
                    f"{new_name!r} already exists here. Replace it with {old_name!r}?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            cmd = _rename_cmd(src, dst, overwrite=True)
        elif state in ("free", "same"):
            cmd = _rename_cmd(src, dst, verify=state == "free")
        else:
            self._report_errors("Rename", [f"could not check {new_name!r}: {_result_error(res)}"])
            return
        self._run_shell(f"rename {old_name}", cmd)

    def _remote_delete(self):
        items = self._selected_remote()
        if not items:
            return
        if self._refuse_unsafe(self.remote_table, [name for name, _ in items], "Delete"):
            return
        if QMessageBox.question(self, "Delete Device Items",
                                f"Permanently delete {len(items)} item(s) from Device?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        targets = [posixpath.join(self.remote_cwd, n) for n, _ in items]
        self._run_shell_batch([(f"delete {len(items)} item(s)", _rm_cmd(targets))], timeout=600)

    def _run_shell(self, label, cmd):
        self._run_shell_batch([(label, cmd)])

    def _run_shell_batch(self, commands, timeout: float = 30):
        """Run device commands in order on one worker, log each, refresh once."""
        handler = self.handler

        def work():
            out = []
            for label, cmd in commands:
                try:
                    res = handler.shell(cmd, timeout=timeout, safe=False)
                except Exception as exc:  # reported per command in done()
                    out.append((label, False, f"{type(exc).__name__}: {exc}"))
                    continue
                out.append((label, bool(res.ok), "" if res.ok else _result_error(res)))
            return out

        def done(results):
            for label, ok, err in results:
                self.log.emit(f"[OK] {label}" if ok else f"[ERROR] {label}: {err}")
            self.refresh_remote()

        def fail(msg):
            self.log.emit(f"[ERROR] {commands[0][0] if commands else 'device command'}: {msg}")
            self.refresh_remote()

        self._job(work, done, fail)

    # ---- Context Menus ----
    def _context_local(self, pos):
        menu = QMenu(self)
        self._menu_action(menu, "Push to device", self.push_selected)
        self._menu_action(menu, "Edit\tF4", self._local_edit)
        menu.addSeparator()
        self._menu_action(menu, "New folder", self._local_mkdir)
        self._menu_action(menu, "New file", self._local_newfile)
        self._menu_action(menu, "Copy\tCtrl+C", self._local_copy)
        self._menu_action(menu, "Paste\tCtrl+V", self._local_paste)
        self._menu_action(menu, "Rename\tF2", self._local_rename)
        self._menu_action(menu, "Delete\tDel", self._local_delete)
        menu.exec_(self.local_table.mapToGlobal(pos))

    def _context_remote(self, pos):
        menu = QMenu(self)
        self._menu_action(menu, "Pull to this PC", self.pull_selected)
        self._menu_action(menu, "Edit\tF4", self._remote_edit)
        menu.addSeparator()
        self._menu_action(menu, "New folder", self._remote_mkdir)
        self._menu_action(menu, "New file", self._remote_newfile)
        self._menu_action(menu, "Copy\tCtrl+C", self._remote_copy)
        self._menu_action(menu, "Paste\tCtrl+V", self._remote_paste)
        self._menu_action(menu, "Rename\tF2", self._remote_rename)
        self._menu_action(menu, "Delete\tDel", self._remote_delete)
        menu.exec_(self.remote_table.mapToGlobal(pos))

    @staticmethod
    def _menu_action(menu, text: str, slot):
        """A context-menu action with the same icon as its pane button."""
        glyph, tone = _PANE_OP_ICONS.get(text.split("\t", 1)[0], (None, None))
        if glyph is None:
            return menu.addAction(text, slot)
        return menu.addAction(icon(glyph, tone), text, slot)

    # ---- Edit File ----
    def _open_editor(self, name: str, path: str, text: str, newline: str, mixed: bool, write, refresh):
        """Show the (window-modal, non-blocking) editor; *write(file_text)* runs on a worker."""
        if mixed:
            ending = _LINE_ENDING_NAMES.get(newline, "LF")
            self.log.emit(f"{name} has mixed line endings; saving will use {ending} throughout.")
        dlg = _FileEditorDialog(name, path, text, None, self)

        def save_fn(new_text):
            payload = _text_for_save(new_text, newline)

            def done(_result):
                if _alive(self) and not self._closing:
                    self.log.emit(f"[OK] Saved {path}")
                    refresh()
                if _alive(dlg):
                    dlg.complete_async_save(True)

            def fail(msg):
                if _alive(dlg):
                    dlg.complete_async_save(False, f"Could not save {name}:\n{msg}")

            # Not in self._jobs: close_panel must not cut an open editor's save path.
            run_job(self._save_jobs, lambda: write(payload), done, fail)
            return None

        dlg.on_save = save_fn
        self._editors.append(dlg)
        dlg.finished.connect(lambda _r, d=dlg: self._forget_editor(d))
        dlg.open()
        return dlg

    def _forget_editor(self, dlg):
        try:
            self._editors.remove(dlg)
        except ValueError:
            pass
        _DETACHED_EDITORS.discard(dlg)
        if _alive(dlg):
            dlg.deleteLater()

    def _release_editors(self):
        """On close: unchanged editors close; unsaved or saving ones become
        top-level windows so they survive the panel being deleted."""
        for dlg in list(self._editors):
            if not _alive(dlg):
                continue
            if dlg.is_dirty() or dlg.is_saving():
                dlg.setParent(None, dlg.windowFlags())
                dlg.setWindowModality(Qt.NonModal)
                _DETACHED_EDITORS.add(dlg)
                dlg.show()
            else:
                dlg.done(QDialog.Rejected)
        self._editors.clear()

    def _handle_edit_load(self, name: str, path: str, result, write, refresh):
        if result is None:
            return  # cancelled
        if result[0] == "refused":
            QMessageBox.information(self, "Edit File", f"{name} was not opened.\n\n{result[1]}")
            return
        _status, text, newline, mixed = result
        self._open_editor(name, path, text, newline, mixed, write, refresh)

    def _local_edit(self):
        rows = self._selected_rows(self.local_table)
        if rows:
            self._local_edit_row(rows[0])

    def _local_edit_row(self, row: int):
        entry = self._row_entry(self.local_table, row)
        if not entry or entry[1]:
            return
        name = entry[0]
        path = os.path.join(self.local_cwd, name)
        self._job(lambda: _edit_load_result(lambda: _read_for_edit(path)),
                  lambda result: self._handle_edit_load(
                      name, path, result, lambda payload: write_text_file(path, payload),
                      self.refresh_local),
                  lambda msg: QMessageBox.critical(self, "Edit File", f"Could not read {name}:\n{msg}"))

    def _remote_edit(self):
        rows = self._selected_rows(self.remote_table)
        if rows:
            self._remote_edit_row(rows[0])

    def _remote_edit_row(self, row: int):
        table = self.remote_table
        entry = self._row_entry(table, row)
        if not entry or entry[1]:
            return
        name = entry[0]
        if self._refuse_unsafe(table, [name], "Edit File"):
            return
        perms_item = table.item(row, 4)
        kind = perms_item.text()[:1] if perms_item is not None else ""
        size_item = table.item(row, 1)
        size = size_item.data(Qt.UserRole) if size_item is not None else None
        # A link's listed size is its target's name length; the real size is checked below.
        if kind != "l" and isinstance(size, int) and size > _EDIT_MAX_BYTES:
            QMessageBox.information(
                self, "Edit File",
                f"{name} was not opened.\n\n{_too_large_message(size)} Pull it to the PC instead.")
            return
        full_path = posixpath.join(self.remote_cwd, name)
        handler = self.handler
        cancel = self._cancel
        # Filled by load(): the file a symlink points to and its permission bits.
        target = {"path": full_path, "mode": ""}
        self.log.emit(f"Fetching {name} for editing…")

        def load():
            try:
                res = handler.shell(_edit_stat_cmd(full_path), timeout=30, safe=False)
                st = _parse_edit_stat(res.stdout, full_path) if res.ok else None
            except Exception:
                st = None  # no usable stat: fall back to pulling the listed path
            if st is not None:
                real_size, mode, ftype, real = st
                if not ftype.startswith("regular"):
                    return ("refused", f"It is not a regular file ({ftype}), so it can't be "
                                       "edited as text.")
                if real_size > _EDIT_MAX_BYTES:
                    return ("refused", f"{_too_large_message(real_size)} Pull it to the PC instead.")
                target["path"], target["mode"] = real, mode
            elif kind in ("b", "c", "p", "s"):
                return ("refused", "It is a device node, pipe or socket, so it can't be edited as text.")
            fd, tmp_path = tempfile.mkstemp(prefix="turboadb-edit-")
            os.close(fd)
            try:
                handler.pull(target["path"], tmp_path, cancel_event=cancel, safe=False)
                if cancel.is_set():
                    return None
                return _edit_load_result(lambda: _read_for_edit(tmp_path))
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        def write(payload):
            real = target["path"]
            mode = target["mode"]
            try:  # the mode right before saving, in case it changed while editing
                res = handler.shell(_mode_cmd(real), timeout=30, safe=False)
                lines = _output_lines(res.stdout) if res.ok else []
                if lines and re.fullmatch(r"[0-7]{3,4}", lines[0].strip()):
                    mode = lines[0].strip()
            except Exception:
                pass
            fd, tmp_path = tempfile.mkstemp(prefix="turboadb-edit-")
            os.close(fd)
            try:
                write_text_file(tmp_path, payload)  # newline="" keeps line endings exactly
                handler.push(tmp_path, real, safe=False)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            if mode:
                # adb push resets the file mode (e.g. 755 -> 666): restore it.
                res = handler.shell(_chmod_cmd(mode, real), timeout=30, safe=False)
                if not res.ok:
                    raise RuntimeError(f"The file was saved, but its permissions ({mode}) could "
                                       f"not be restored: {_result_error(res)}")

        self._job(load,
                  lambda result: self._handle_edit_load(name, target["path"], result, write,
                                                        self.refresh_remote),
                  lambda msg: QMessageBox.critical(
                      self, "Edit Remote File", f"Could not pull {name} from device:\n{msg}"))

    # ---- Drag and drop ----
    def _on_local_dropped(self, paths: list, is_external: bool, target_dir: str = ""):
        if not paths:
            return
        if self._local_dir_for_action("Drop") is None:
            return
        dst_base = target_dir or self.local_cwd
        if is_external:
            # Local filesystem paths (Explorer or a local pane): copy on a worker.
            self._copy_local_into(list(paths), dst_base, "local copy")
        else:
            # This device's items dropped into the local table -> PULL
            base = self.remote_table.base_dir
            names = [posixpath.basename(p) for p in paths if posixpath.dirname(p) == base]
            if self._refuse_unsafe(self.remote_table, names, "Pull"):
                return
            self._start_pull(list(paths), dst_base)

    def _ask_overwrite(self, collisions, where: str = "on the PC") -> bool:
        """Ask before a transfer, drop or paste overwrites or merges into existing items."""
        preview = ", ".join(collisions[:4]) + ("…" if len(collisions) > 4 else "")
        return QMessageBox.question(
            self,
            "Replace existing items?",
            f"{len(collisions)} existing item(s) {where} will be overwritten or merged: "
            f"{preview}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) == QMessageBox.Yes

    def _on_remote_dropped(self, paths: list, is_external: bool, target_dir: str = ""):
        if not paths or not is_external:
            return  # only local filesystem paths can be pushed
        locals_ = [p for p in paths if p]
        if not locals_:
            return
        if self._remote_dir_for_action("Drop") is None:
            return
        self._start_push(locals_, target_dir or self.remote_cwd)

    def close_panel(self):
        """Detach every worker without waiting on the UI thread."""
        if self._closing:
            return
        self._closing = True
        self._cancel.set()
        self._queue.clear()
        self._remote_refresh_pending = False
        transfer, self._transfer = self._transfer, None
        if transfer is not None:
            try:
                transfer.stop()
            except RuntimeError:
                pass
            disconnect_signals(transfer, ("progress", "done", "failed"))
            park_thread(transfer)
        close_jobs(self._jobs)
        self._ls = None
        self._release_editors()
