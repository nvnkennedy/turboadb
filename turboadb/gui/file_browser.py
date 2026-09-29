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

import errno
import html
import json
import mimetypes
import os
import posixpath
import re
import shutil
import stat
import string
import sys
import tempfile
import threading
import time
from collections import deque
from itertools import accumulate
from typing import List, Optional, Tuple

from PyQt5.QtCore import (QItemSelection, QItemSelectionModel, QMimeData, QPoint, QRect,
                          QSize, QThread, QTimer, QUrl, Qt, pyqtSignal)
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
                             QInputDialog, QMessageBox, QLabel, QProgressBar,
                             QSplitter, QMenu, QShortcut, QComboBox,
                             QAbstractItemView, QApplication, QDialog, QPlainTextEdit,
                             QRubberBand, QStyledItemDelegate, QToolButton)
from PyQt5.QtGui import QColor, QCursor, QKeySequence, QFont, QPainter, QTextCursor, QDrag
from . import file_open, theme
from .file_icons import file_icons
from .fileutil import _alive, desktop_dir, download_dir, write_text_file
from .icons import icon
from .qtutil import (cached_icon, close_jobs, disconnect_signals, page_toolbar, park_thread,
                     run_job, thread_running)
from .transfer_log import (CANCELLED, DONE, FAILED, TransferLog, _strip_merge, measure_local,
                           measure_remote, transfer_name)
from .transfer_panel import TransferPanel
from ..remotefs import (  # device file helpers, shared with the engine and CLI
    # _ls_cmd/_rm_cmd/_parse_ls_listing are re-exported: the tests reach them
    # as file_browser.<name>, so they are not dead.
    _chunks, _copy_decision, _copy_into_cmd, _human_size, _list_remote_dir,  # noqa: F401
    _ls_cmd, _mkdir_cmd, _normalize_remote_path, _output_lines,  # noqa: F401
    _parse_ls_line, _parse_ls_listing, _parse_stamp, _probe_remote,  # noqa: F401
    _regular_file_cmd, _rename_check_cmd, _rename_cmd, _result_error, _rm_cmd,  # noqa: F401
    _stamp_cmd, _touch_cmd,  # noqa: F401
)


# Characters Qt's plain-text document cannot round-trip (they become block breaks).
_EDIT_UNSAFE_CHARS = (" ", "﷐", "﷑")

_MIME = "application/x-turboadb-file"

# The built-in editor is for config/text files; bigger or binary files are refused.
_EDIT_MAX_BYTES = 2 * 1024 * 1024

_LINE_ENDING_NAMES = {"\r\n": "CRLF", "\r": "CR", "\n": "LF"}

# Editors that outlived their panel (unsaved or mid-save when the tab closed).
_DETACHED_EDITORS = set()
# Copies of opened device files are tidied once per run (file_open.forget_old_copies).
_OLD_COPIES = {"tidied": False}


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
# Row icons come from qtutil.cached_icon: one QIcon per (name, tone), shared by
# every row (icons read the palette at paint time, so they follow the theme).

# Pane operation buttons and their context-menu twins: (icon, tone).
_PANE_OP_ICONS = {
    "New folder": ("folder-plus", "amber"),
    "New file": ("file-plus", "blue"),
    "Open": ("external", "blue"),
    "Open with…": ("external", "teal"),
    "Edit": ("edit", "purple"),
    "Copy": ("copy", "blue"),
    "Paste": ("paste", "blue"),
    "Rename": ("rename", "teal"),
    "Delete": ("trash", "red"),
    "Delete permanently": ("trash", "red"),
    "Select all": ("check", "accent"),
    "Push to device": ("arrow-right", "blue"),
    "Pull to this PC": ("arrow-left", "green"),
}


_PANE_OP_TIPS = {
    "Open": ("Open in the app this PC has for it (Enter). A device file opens as a copy on "
             "this PC, and saving it there sends it back to the device."),
    "Edit": "Edit as text in TurboADB's editor (F4)",
}


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


class _FileItem(QTableWidgetItem):
    """A listing cell: the plain name/size text plus its raw value in
    ``Qt.UserRole``.

    Ordering lives in :meth:`_FileTableWidget.sortByColumn` — the table keeps
    Qt's own sorting disabled and reorders the rows itself, so an item never
    needs to compare against another one.
    """


def _sort_key(value, text: str):
    """How a cell sorts: a number (a size, a date) by its value, anything else
    by its text, ignoring case."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, value, "")
    return (1, 0, text.lower())


# Where each column's text is in a listing row (see _list_local_dir).
_ROW_TEXT = (0, 2, 3, 4, 5, 6)


class _RowSummary:
    """What a pane's status line needs, kept so it never walks the rows.

    Per row, in display order: *kinds* is 0 for the '..' row or a status row,
    1 for a folder and 2 for a file; *sizes* is a file's size (0 otherwise).
    *entries* and *bytes* are running totals, so the files and folders in any
    span of rows, and their size, are two subtractions each."""

    __slots__ = ("kinds", "sizes", "entries", "bytes", "folders", "files")

    def __init__(self, kinds, sizes):
        self.kinds, self.sizes = list(kinds), list(sizes)
        self.folders = self.kinds.count(1)
        self.files = self.kinds.count(2)
        self.entries = list(accumulate((1 if kind else 0 for kind in self.kinds), initial=0))
        self.bytes = list(accumulate(self.sizes, initial=0))


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


# Windows marks a new file would not keep (and a read-only file must fail as before).
_KEEP_IN_PLACE_ATTRS = 0x1 | 0x2 | 0x4  # READONLY, HIDDEN, SYSTEM


def _replace_keeps_the_file(path: str) -> bool:
    """True when saving *path* by replacing it with a new file changes nothing
    but its text: a plain file with no other hard link (they would keep the
    old text), that the user owns and may write (a read-only file is never
    replaced behind its back), and on Windows without the hidden, system or
    read-only mark."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
        return False
    if os.name == "nt":
        return not getattr(st, "st_file_attributes", 0) & _KEEP_IN_PLACE_ATTRS
    return st.st_uid == os.geteuid() and os.access(path, os.W_OK)


def _save_local_text(path: str, text: str) -> None:
    """Worker: save the editor's text over the PC file *path*, never leaving it
    cut short.

    The text goes to a temporary file beside the real one (beside a symbolic
    link's target, which stays a link), which then replaces it: a full disk,
    a network drive that drops, or a character that can't be written fails
    with the old file untouched.  A file that replacing would change in other
    ways (see :func:`_replace_keeps_the_file`), or that another program holds
    open, is written in place as before - but only when the disk has room for
    the new text."""
    data = text.encode("utf-8")  # before anything is touched
    real = os.path.realpath(path)
    if _replace_keeps_the_file(real):
        try:
            fd, tmp = tempfile.mkstemp(prefix=".turboadb-save-", suffix=".tmp",
                                       dir=os.path.dirname(real))
        except OSError:
            tmp = None  # the folder takes no new files: in place below
        if tmp is not None:
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                shutil.copymode(real, tmp)
                os.replace(tmp, real)
                return
            except OSError:
                pass  # e.g. open elsewhere without delete sharing: in place below
            finally:
                if os.path.lexists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
    try:
        room = shutil.disk_usage(os.path.dirname(real) or ".").free + os.path.getsize(real)
    except OSError:
        room = None
    if room is not None and room < len(data):
        raise OSError(errno.ENOSPC, "Not enough free space on the drive to save the file")
    write_text_file(path, text)


def _edit_load_result(load):
    """Run *load* and turn a refusal into a result instead of an error."""
    try:
        text, newline, mixed = load()
    except _EditRefused as exc:
        return ("refused", str(exc))
    return ("ok", text, newline, mixed)


def _device_stamp(handler, path: str):
    """Worker: ``(size, modified)`` of the device file *path*, None when the
    device can't say (no ``stat``, no answer)."""
    try:
        res = handler.shell(_stamp_cmd(path), timeout=30, safe=False)
    except Exception:
        return None
    return _parse_stamp(getattr(res, "stdout", "")) if getattr(res, "ok", True) else None


def _links_to_a_file(handler, path: str) -> bool:
    """Worker: True when the device *path* is, or links to, a regular file."""
    try:
        res = handler.shell(_regular_file_cmd(path), timeout=30, safe=False)
    except Exception:
        return False
    lines = _output_lines(getattr(res, "stdout", ""))
    return bool(lines) and lines[-1].strip() == "f"


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

    def complete_async_save(self, ok: bool, error: str = "", *, report: bool = True):
        """Finish a background save requested by ``on_save``. *report=False*:
        the failure is shown elsewhere (the device tab offers write access)."""
        self._saving = False
        self._set_saving(False)
        if not ok:
            self._close_after_save = False
            if report:
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


class _NameDelegate(QStyledItemDelegate):
    """Name cells shorten in the middle, so the extension stays visible."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.textElideMode = Qt.ElideMiddle


class _PathEdit(QLineEdit):
    """The folder path field: it takes the spare width of its header row but
    asks for little, so Up and Refresh stay beside it on a laptop screen."""

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(min(hint.width(), 140), hint.height())


class _FileTableWidget(QTableWidget):
    """Table widget with native bidirectional drag-and-drop and fixed '..' row 0.

    Like Explorer, a drag that starts on a name moves files, and a drag that
    starts anywhere else (another column, or below the rows) selects with a
    rectangle, scrolling while the pointer is above or below the rows.
    """
    # (paths, paths_are_local, target_dir): paths_are_local is True for local
    # filesystem paths (Explorer or a local pane), False for device paths.
    dropped = pyqtSignal(list, bool, str)

    BAND_SCROLL_MS = 40
    # (column, table width below which it hides): on a narrow pane Type (the
    # icon already says it) and then Owner give their room to the names.
    NARROW_HIDDEN_COLUMNS = ((2, 760), (5, 640))

    def __init__(self, is_remote: bool = False, parent=None):
        super().__init__(0, 6, parent)
        self.is_remote = is_remote
        self.base_dir = ""
        self.browser = None  # owning FileBrowser (identifies the device of remote drags)
        # Listed names that could not be identified exactly (never acted on).
        self.unsafe_names = frozenset()
        # Shown in the middle of a folder with nothing to list ("" while loading).
        self.empty_text = ""
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        header = self.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSortIndicator(0, Qt.AscendingOrder)
        # a select-all must not paint every header section as pressed
        header.setHighlightSections(False)
        header.sectionClicked.connect(self._on_header_clicked)
        self._band = None          # the QRubberBand, created on the first drag
        self._band_origin = None   # press point in content coordinates
        self._band_base = QItemSelection()  # kept under the band (Ctrl / Shift)
        self._band_active = False
        self._band_pos = QPoint()
        self._band_timer = QTimer(self)
        self._band_timer.setInterval(self.BAND_SCROLL_MS)
        self._band_timer.timeout.connect(self._band_autoscroll)
        # The rows' kinds and sizes (see _RowSummary), set with each listing and
        # forgotten whenever rows come or go without one.
        self._summary = None
        model = self.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset,
                       model.layoutChanged):
            signal.connect(self._forget_rows)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = self.width()
        for column, below in self.NARROW_HIDDEN_COLUMNS:
            if column < self.columnCount() and self.isColumnHidden(column) != (width < below):
                self.setColumnHidden(column, width < below)

    # ---- selection ----
    def is_parent_row(self, row: int) -> bool:
        it = self.item(row, 0)
        data = it.data(Qt.UserRole) if it is not None else None
        return isinstance(data, (tuple, list)) and len(data) > 1 and data[0] == ".."

    def _entry_rows(self):
        """``(first, last)`` rows holding files or folders ('..' excluded)."""
        first = 1 if self.rowCount() and self.is_parent_row(0) else 0
        return first, self.rowCount() - 1

    def select_all_entries(self) -> None:
        """Ctrl+A: every file and folder, never the '..' row."""
        first, last = self._entry_rows()
        model = self.selectionModel()
        head = self.item(first, 0) if first <= last else None
        if head is None or not head.flags() & Qt.ItemIsSelectable:
            model.clearSelection()  # nothing listed, or only "Loading…"
            return
        whole = QItemSelection(self.model().index(first, 0),
                               self.model().index(last, self.columnCount() - 1))
        model.select(whole, QItemSelectionModel.ClearAndSelect)

    # ---- what is listed and selected, without walking every row ----
    def _forget_rows(self, *_args) -> None:
        self._summary = None

    def set_row_summary(self, kinds, sizes) -> None:
        """Record the rows' kinds and sizes (see :class:`_RowSummary`)."""
        self._summary = _RowSummary(kinds, sizes)

    def row_summary(self) -> _RowSummary:
        """The rows' kinds and sizes: kept from the listing, or read from the
        rows once when they changed some other way."""
        if self._summary is None or len(self._summary.kinds) != self.rowCount():
            kinds, sizes = [], []
            for row in range(self.rowCount()):
                it = self.item(row, 0)
                data = it.data(Qt.UserRole) if it is not None else None
                if not isinstance(data, (tuple, list)) or len(data) < 2 or data[0] == "..":
                    kinds.append(0)
                    sizes.append(0)
                elif data[1]:
                    kinds.append(1)
                    sizes.append(0)
                else:
                    size_item = self.item(row, 1) if self.columnCount() > 1 else None
                    raw = size_item.data(Qt.UserRole) if size_item is not None else None
                    kinds.append(2)
                    sizes.append(raw if isinstance(raw, int) else 0)
            self._summary = _RowSummary(kinds, sizes)
        return self._summary

    def selected_spans(self) -> List[Tuple[int, int]]:
        """The selected rows as sorted, separate ``(first, last)`` spans.

        Read from the selection's ranges: ``selectedIndexes()`` made an index
        for every selected cell - 120,000 of them for 20,000 selected rows, on
        each mouse move of a selection rectangle.  Ranges may overlap, so they
        are merged first."""
        model = self.selectionModel()
        if model is None:
            return []
        spans = []
        for top, bottom in sorted((rng.top(), rng.bottom()) for rng in model.selection()):
            if spans and top <= spans[-1][1] + 1:
                if bottom > spans[-1][1]:
                    spans[-1] = (spans[-1][0], bottom)
            else:
                spans.append((top, bottom))
        return spans

    def selection_summary(self) -> Tuple[int, int]:
        """``(selected files and folders, bytes of the selected files)``."""
        summary = self.row_summary()
        last = len(summary.kinds) - 1
        count = size = 0
        for top, bottom in self.selected_spans():
            top, bottom = max(0, top), min(bottom, last)
            if top <= bottom:
                count += summary.entries[bottom + 1] - summary.entries[top]
                size += summary.bytes[bottom + 1] - summary.bytes[top]
        return count, size

    def selected_entry_rows(self) -> List[int]:
        """The rows of the selected files and folders, top to bottom."""
        kinds = self.row_summary().kinds
        last = len(kinds) - 1
        return [row for top, bottom in self.selected_spans()
                for row in range(max(0, top), min(bottom, last) + 1) if kinds[row]]

    def sorted_listing(self, listing):
        """*listing* - ``(row, date key)`` pairs, the rows shaped as
        :func:`_list_local_dir` makes them - in the order the header asks for,
        folders first: exactly how :meth:`sortByColumn` would order their rows.
        A new listing is put in order before its rows are built, instead of
        being built and then taken apart and built again."""
        header = self.horizontalHeader()
        col = header.sortIndicatorSection()
        if not 0 <= col < min(self.columnCount(), len(_ROW_TEXT)):
            col = 0
        reverse = header.sortIndicatorOrder() == Qt.DescendingOrder
        text_at = _ROW_TEXT[col]

        def key(entry):
            row, date_key = entry
            value = row[1] if col == 1 else date_key if col == 3 else None
            return _sort_key(value, str(row[text_at]))

        folders = sorted((entry for entry in listing if entry[0][7]), key=key, reverse=reverse)
        files = sorted((entry for entry in listing if not entry[0][7]), key=key, reverse=reverse)
        return folders + files

    def _offset(self) -> QPoint:
        return QPoint(self.horizontalOffset(), self.verticalOffset())

    def mousePressEvent(self, event):
        self._end_band()
        if event.button() == Qt.LeftButton:
            item = self.itemAt(event.pos())
            if item is None or item.column() != 0:
                keep = event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)
                self._band_base = (QItemSelection(self.selectionModel().selection())
                                   if keep else QItemSelection())
                self._band_origin = event.pos() + self._offset()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._band_origin is None or not event.buttons() & Qt.LeftButton:
            super().mouseMoveEvent(event)
            return
        # Never a file drag from here: the press was not on a name.
        if not self._band_active:
            start = self._band_origin - self._offset()
            if (event.pos() - start).manhattanLength() < QApplication.startDragDistance():
                return
            self._band_active = True
            if self._band is None:
                self._band = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._band_pos = event.pos()
        self._update_band()
        inside = 0 <= event.pos().y() < self.viewport().height()
        if inside:
            self._band_timer.stop()
        elif not self._band_timer.isActive():
            self._band_timer.start()

    def mouseReleaseEvent(self, event):
        used = self._band_active and event.button() == Qt.LeftButton
        self._end_band()
        if used:
            event.accept()  # keep the rectangle's selection (a click would replace it)
            return
        super().mouseReleaseEvent(event)

    def focusOutEvent(self, event):
        if not (event.reason() == Qt.PopupFocusReason):
            self._end_band()
        super().focusOutEvent(event)

    def _end_band(self) -> None:
        self._band_timer.stop()
        self._band_origin = None
        self._band_active = False
        if self._band is not None:
            self._band.hide()

    def _band_autoscroll(self) -> None:
        if not self._band_active:
            self._band_timer.stop()
            return
        pos = self.viewport().mapFromGlobal(QCursor.pos())
        bar = self.verticalScrollBar()
        if pos.y() < 0:
            bar.setValue(bar.value() - 1)
        elif pos.y() >= self.viewport().height():
            bar.setValue(bar.value() + 1)
        else:
            self._band_timer.stop()
        self._band_pos = pos
        self._update_band()

    def band_rows(self, top: int, bottom: int):
        """``(first, last)`` rows touched by content y range *top*..*bottom*."""
        first_entry, last_row = self._entry_rows()
        if last_row < 0:
            return 0, -1
        header = self.verticalHeader()

        def row_at(y):
            lo, hi = 0, last_row
            while lo < hi:  # rows are sorted by position; find the last one starting <= y
                mid = (lo + hi + 1) // 2
                if header.sectionPosition(mid) <= y:
                    lo = mid
                else:
                    hi = mid - 1
            return lo

        end = header.sectionPosition(last_row) + header.sectionSize(last_row)
        if top >= end:
            return 0, -1  # the rectangle is below the last row
        return max(first_entry, row_at(max(0, top))), row_at(max(0, bottom))

    def _update_band(self) -> None:
        if self._band_origin is None or self._band is None:
            return
        offset = self._offset()
        start = self._band_origin - offset
        rect = QRect(start, self._band_pos).normalized()
        self._band.setGeometry(rect.intersected(self.viewport().rect()))
        self._band.show()
        top = min(self._band_origin.y(), self._band_pos.y() + offset.y())
        bottom = max(self._band_origin.y(), self._band_pos.y() + offset.y())
        first, last = self.band_rows(top, bottom)
        selection = QItemSelection(self._band_base)
        if first <= last:
            selection.merge(
                QItemSelection(self.model().index(first, 0),
                               self.model().index(last, self.columnCount() - 1)),
                QItemSelectionModel.Select)
        self.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect)

    def paintEvent(self, event):
        super().paintEvent(event)
        rows = self.rowCount()
        if not self.empty_text or rows > 1 or (rows == 1 and not self.is_parent_row(0)):
            return
        painter = QPainter(self.viewport())
        painter.setPen(QColor(theme.palette()["dim"]))
        area = self.viewport().rect()
        if rows == 1:
            area.setTop(self.rowViewportPosition(0) + self.rowHeight(0))
        painter.drawText(area, Qt.AlignCenter, self.empty_text)
        painter.end()

    def startDrag(self, supportedActions):
        rows = [row for top, bottom in self.selected_spans() for row in range(top, bottom + 1)]
        if not rows:
            return
        items = [self.item(row, 0) for row in rows]
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

    def sortByColumn(self, col: int, order: Qt.SortOrder = Qt.AscendingOrder):
        """Sort rows: '..' pinned first, then folders, then files; numeric columns
        (size, date) sort by their raw ``Qt.UserRole`` value."""
        self.setSortingEnabled(False)
        n_rows, n_cols = self.rowCount(), self.columnCount()
        if n_rows == 0:
            return
        if not 0 <= col < n_cols:
            col = 0
        # the summary moves with the rows (setRowCount below forgets it)
        summary = self._summary if self._summary is not None and \
            len(self._summary.kinds) == n_rows else None
        dot_items = dot_row = None
        rows_data = []

        for r in range(n_rows):
            it0 = self.item(r, 0)
            data0 = it0.data(Qt.UserRole) if it0 else None
            is_pair = isinstance(data0, (tuple, list)) and len(data0) > 1
            row_items = [self.takeItem(r, c) for c in range(n_cols)]
            if is_pair and data0[0] == ".." and dot_items is None:
                dot_items, dot_row = row_items, r
                continue
            is_dir = bool(data0[1]) if is_pair else False
            it_col = row_items[col]
            if col == 0 and is_pair:
                key = _sort_key(data0, str(data0[0]))
            elif it_col is not None:
                key = _sort_key(it_col.data(Qt.UserRole), it_col.text())
            else:
                key = _sort_key(None, it0.text() if it0 else "")
            rows_data.append((is_dir, key, r, row_items))

        reverse = (order == Qt.DescendingOrder)
        dirs = sorted((x for x in rows_data if x[0]), key=lambda x: x[1], reverse=reverse)
        files = sorted((x for x in rows_data if not x[0]), key=lambda x: x[1], reverse=reverse)
        rows_before = ([dot_row] if dot_items else []) + [r for _, _, r, _ in dirs + files]
        ordered = ([dot_items] if dot_items else []) + [items for _, _, _, items in dirs + files]

        self.setRowCount(0)
        self.setRowCount(len(ordered))
        for r, items in enumerate(ordered):
            for c, it in enumerate(items):
                if it is not None:
                    self.setItem(r, c, it)
        if summary is not None:
            self.set_row_summary((summary.kinds[r] for r in rows_before),
                                 (summary.sizes[r] for r in rows_before))

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
            _accept_as_copy(event)
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(_MIME):
            if self._drop_target(event.pos()) is None:
                event.ignore()  # the cursor says so before the drop
            else:
                _accept_as_copy(event)
        else:
            super().dragMoveEvent(event)

    def _drop_target(self, pos) -> Optional[str]:
        """The folder a drop at *pos* goes to: the folder row under it, else
        the folder shown.  None for a folder whose name the listing could not
        read exactly: its real path is unknown, and a push to the name shown
        would create a new folder beside it."""
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
                elif data[0] in self.unsafe_names:
                    return None
                elif self.is_remote:
                    target_folder = posixpath.join(self.base_dir, data[0])
                else:
                    target_folder = os.path.join(self.base_dir, data[0])
        return target_folder

    def dropEvent(self, event):
        source = event.source()
        if source is self or not _copy_possible(event):
            event.ignore()
            return

        mime = event.mimeData()
        target_folder = self._drop_target(event.pos())
        if target_folder is None:
            event.ignore()
            item = self.itemAt(event.pos())
            it0 = self.item(item.row(), 0) if item is not None else None
            log = getattr(self.browser, "log", None)
            if it0 is not None and log is not None:
                log.emit(f"[ERROR] Drop: refused, the folder {it0.text()!r} could not be "
                         "identified exactly (unusual characters in its name, or two "
                         "entries that look the same)")
            return

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
                    _accept_as_copy(event)
                    return
                same_device = (source is not None and self.browser is not None
                               and getattr(source, "browser", None) is self.browser)
                if not self.is_remote and same_device:
                    # This device's pane -> this local pane: pull.
                    self.dropped.emit(paths, False, target_folder)
                    _accept_as_copy(event)
                    return
                # Device paths from another device tab (or onto a device pane)
                # can't be pulled/pushed from here: refuse instead of guessing.
                event.ignore()
                return
        if mime.hasUrls():
            files = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
            if files:
                self.dropped.emit(files, True, target_folder)
                _accept_as_copy(event)
                return
        super().dropEvent(event)


def _copy_possible(event) -> bool:
    return bool(event.possibleActions() & Qt.CopyAction)


def _accept_as_copy(event) -> bool:
    """Accept a drag or drop as a COPY, whatever the source proposed.

    Holding Shift makes Explorer (and Linux file managers) propose a Move, and
    accepting that tells the source to delete its originals once the drop
    returns.  TurboADB never moves: it plans the copy or push on a worker and
    runs it later, so the files would be gone before they were read.  A source
    that offers no Copy at all (the Recycle Bin) is refused."""
    if not _copy_possible(event):
        event.ignore()
        return False
    event.setDropAction(Qt.CopyAction)
    event.accept()
    return True


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}

# "2026-09-02 21:40", toybox's full-iso "2026-09-02 21:40:05.123456789 +0200" and
# the same with a "T"; the minute is all a sort needs.
_ISO_MTIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T|\s+)(\d{2}):(\d{2})")


def _mtime_sort_key(mtime: str, now: Optional[float] = None) -> float:
    """Sortable timestamp for ``2026-09-02 21:40`` (also with a ``T``, seconds or
    a zone), ``Jan  5 12:34``, ``Jan  5  2024``, or with the day first
    (``5 Jan 2024``).  Month names other than English ones sort as 0.

    A listing asks this for every row, so the ISO form does without
    ``strptime``, which took a third of the time a big folder took to show."""
    text = mtime or ""
    try:
        iso = _ISO_MTIME_RE.match(text)
        if iso:
            year, month, day, hour, minute = (int(x) for x in iso.groups())
            if 1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60:
                return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))
            return 0.0
        parts = text.split()
        if len(parts) != 3:
            return 0.0
        if parts[0][:3].lower() in _MONTHS:  # "Jan  5 12:34" / "Jan  5  2024"
            month_word, day_word, last = parts
        elif parts[1][:3].lower() in _MONTHS:  # "5 Jan 12:34" / "5 Jan 2024"
            day_word, month_word, last = parts
        else:
            return 0.0
        month, day = _MONTHS[month_word[:3].lower()], int(day_word)
        if ":" in last:
            hour, minute = (int(x) for x in last.split(":", 1))
            now = time.time() if now is None else now
            year = time.localtime(now).tm_year
            stamp = time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))
            if stamp > now + 86400:  # "Mon DD HH:MM" is used for the last ~6 months
                stamp = time.mktime((year - 1, month, day, hour, minute, 0, 0, 0, -1))
            return stamp
        return time.mktime((int(last), month, day, 0, 0, 0, 0, 0, -1))
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
    """Names in *dst_dir* a copy of *sources* would overwrite (a file) or merge
    into (a folder).  A file and a folder of the same name are not asked
    about: the copy refuses that pair and says so."""
    collisions = []
    for src in sources:
        dst = _local_dest(src, dst_dir)
        if not _same_path(src, dst) and os.path.exists(dst) and \
                os.path.isdir(src) == os.path.isdir(dst):
            collisions.append(os.path.basename(dst))
    return collisions


def _loop_guard(root: str, skipped: List[str]):
    """A ``copytree`` *ignore* hook for copying *root*: it leaves out every link
    or junction that leads back to a folder the copy is inside (or above it),
    and lists them in *skipped*.  Following one copied the tree into itself,
    level after level, until the path grew too long."""
    root = os.path.abspath(root)
    above = set()
    path = os.path.realpath(root)
    while True:  # the folder copied and every folder above it
        above.add(os.path.normcase(path))
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent

    def ignore(folder, names):
        inside = set(above)
        current = root
        relative = os.path.relpath(os.path.abspath(folder), root)
        if relative != os.curdir:
            for part in relative.split(os.sep):  # how the copy got here: links too
                current = os.path.join(current, part)
                inside.add(os.path.normcase(os.path.realpath(current)))
        left_out = []
        for name in names:
            path = os.path.join(folder, name)
            try:
                if (os.path.islink(path) or _is_junction(path)) and os.path.isdir(path) and \
                        os.path.normcase(os.path.realpath(path)) in inside:
                    left_out.append(name)
                    skipped.append(path)
            except (OSError, ValueError):
                continue
        return left_out

    return ignore


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
                if os.path.lexists(dst) and not os.path.isdir(dst):
                    raise OSError("a file with that name is already there")
                skipped = []
                shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_loop_guard(src, skipped))
                errors.extend(f"{path}: not copied - it links back into the folder being "
                              "copied" for path in skipped)
            elif os.path.isdir(dst):
                # copy2 would put the file INSIDE the folder of its name
                raise OSError("a folder with that name is already there")
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


def recycle_bin_available() -> bool:
    """Local Delete moves items to the Recycle Bin on Windows (Shift+Delete, and
    other systems, delete permanently)."""
    return os.name == "nt"


def _recycle_list(paths) -> str:
    """The ``pFrom`` value SHFileOperationW wants: every absolute path
    NUL-terminated, with one more NUL at the end ("" for no paths)."""
    paths = [os.path.abspath(p) for p in paths if p]
    return "\0".join(paths) + "\0\0" if paths else ""


def _recycle_local_items(paths) -> List[str]:
    """Move *paths* to the Windows Recycle Bin in one shell call; returns errors.

    Items on a drive without a Recycle Bin get Windows' own "delete
    permanently?" warning instead of silently disappearing."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        # shellapi.h packs this structure to 1 byte only on 32-bit Windows
        _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    text = _recycle_list(paths)
    if not text:
        return []
    fo_delete = 3
    flags = 0x0004 | 0x0010 | 0x0040 | 0x0400 | 0x4000  # SILENT NOCONFIRMATION ALLOWUNDO NOERRORUI WANTNUKEWARNING
    names = (ctypes.c_wchar * len(text))(*text)
    op = SHFILEOPSTRUCTW()
    op.wFunc = fo_delete
    op.pFrom = ctypes.cast(names, ctypes.c_wchar_p)
    op.fFlags = flags
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if op.fAnyOperationsAborted:
        return ["cancelled: nothing more was moved to the Recycle Bin"]
    if code:
        return [f"the Recycle Bin refused the items (error 0x{code:x}); "
                "Shift+Delete deletes them permanently"]
    return []


def _create_empty_file(path: str) -> None:
    with open(path, "a"):
        pass


def _is_real_dir(path: str) -> bool:
    return os.path.isdir(path) and not os.path.islink(path) and not _is_junction(path)


def _local_rename_state(old: str, new: str) -> str:
    """Worker: what renaming the PC item *old* to *new* would meet: ``free``,
    ``same`` (*new* is *old* itself: a case-only rename on Windows or macOS),
    ``dir`` (a folder is there), ``file`` (a file *old* would replace) or
    ``taken`` (a file is there and *old* is a folder, which can't replace it)."""
    if not os.path.lexists(new):
        return "free"
    try:
        a, b = os.lstat(old), os.lstat(new)
        same = bool(a.st_ino) and (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)
    except OSError:
        same = False
    if (same and os.path.basename(old).lower() == os.path.basename(new).lower()) or \
            os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(new)):
        return "same"
    if _is_real_dir(new):
        return "dir"
    return "taken" if _is_real_dir(old) else "file"


def _rename_local_item(old: str, new: str, replace: bool = False) -> List[str]:
    """Worker: rename *old* to *new*.  *replace* (asked first) overwrites a file
    at *new*; without it nothing that appeared there since the check is ever
    replaced - ``os.rename`` replaces a file without a word on Linux and macOS."""
    if replace:
        os.replace(old, new)
    else:
        if _local_rename_state(old, new) not in ("free", "same"):
            raise FileExistsError(f"{os.path.basename(new)!r} already exists")
        os.rename(old, new)
    return []


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


# How the engine ends a transfer it stopped on request, with a note of what the
# copy left behind (``ADBHandler._leftover_note``) when it left something.
_CANCELLED_RE = re.compile(r"\badb (?:push|pull) cancelled(?:; (.+))?$", re.S)


def _cancel_leftover(message: str) -> str:
    """What a cancelled transfer left behind, as the engine's error says it
    ("the unfinished file was removed", "a partial copy may remain at …"), or
    "" when it left nothing.  A transfer that ended some other way while the
    cancel was on its way may have left part of a copy, and that is said."""
    m = _CANCELLED_RE.search(str(message or "").strip())
    if m is None:
        return "a partial copy may remain"
    return (m.group(1) or "").strip()


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
            notes.append(f"[INFO] {name} is a symbolic link; pulling its target {real}")
        local_name = _safe_local_name(name, windows)
        if local_name != name:
            notes.append(f"[WARNING] {name!r} is saved as {local_name!r} (the name isn't valid on this PC)")
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


_PASTE_PROBLEMS = {
    "missing": "no longer exists on the device",
    "same": "is already in this folder",
    "inside": "a folder can't be copied into itself or its own subfolder",
    "taken": "another selected item has the same name",
    "different": "a different item with that name already exists here",
}


def _plan_remote_copy(handler, sources, dst_dir: str):
    """Worker: plan device -> device copies into *dst_dir*.

    Returns ``(commands, collisions, errors)``.  What may be copied is decided
    by :func:`remotefs._copy_decision`, as for ``turboadb cp``: a folder is never
    copied into itself or its own subfolder, also not through a symlinked path
    (``cp -r`` would recurse until "Too many open files").
    """
    dst_dir = _normalize_remote_path(dst_dir)
    dests = {src: posixpath.join(dst_dir, posixpath.basename(src.rstrip("/"))) for src in sources}
    info = _probe_remote(handler, list(sources) + list(dests.values()) + [dst_dir])
    commands, collisions, errors, targets = [], [], [], set()
    if info[dst_dir][0] != "d":
        return commands, collisions, [f"{dst_dir}: the destination folder no longer exists"]
    for src in sources:
        dst = dests[src]
        name = posixpath.basename(dst)
        problem, exists, merge = _copy_decision(src, dst, info, dst_dir, targets)
        if problem:
            errors.append(f"{name}: {_PASTE_PROBLEMS[problem]}")
            continue
        if exists:
            collisions.append(name)
        commands.append((f"copy {name}", _copy_into_cmd(src, dst, merge)))
    return commands, collisions, errors


_QUOTED_RE = re.compile(r"'([^']+)'")


def _is_within(path: str, root: str) -> bool:
    try:
        path, root = (os.path.normcase(os.path.abspath(p)) for p in (path, root))
    except (TypeError, ValueError):
        return False
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _unreadable(path: str) -> bool:
    """True when *path* is on this PC and can't be read."""
    try:
        if os.path.isdir(path):
            with os.scandir(path):
                pass
        elif os.path.lexists(path):
            with open(path, "rb"):
                pass
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def _unwritable(path: str) -> bool:
    """True when *path* can't be written on this PC: an existing file that
    won't open for writing (read-only, or locked by another program), or a
    folder - the nearest one that exists - that takes no new file.  A real
    file is created to find out: on Windows ``os.access`` ignores folder
    permissions, so it passes ``C:\\Program Files``."""
    if os.path.isfile(path):
        try:
            with open(path, "ab"):
                pass
        except OSError:
            return True
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    while folder and not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent
    if not folder:
        return False
    try:
        with tempfile.TemporaryFile(dir=folder):
            pass
    except OSError:
        return True
    return False


def _local_refusal(direction: str, a: str, b: str, error: str) -> str:
    """Worker: the path on THIS PC that made a push or pull fail with *error*
    (a permission error), or ``""`` when this PC's side is fine and the device
    refused.

    adb words a failure on the PC the way it words one on the device
    ("cannot create 'C:\\Program Files\\x': Permission denied" for a pull,
    a locked file for a push), and offering adb root for it restarted the
    device's adbd for nothing.  So the PC's side is checked itself: a push's
    source must be readable, a pull's destination writable - and so must any
    path under them that adb names."""
    local = _strip_merge(a if direction == "push" else b)
    named = [path for path in _QUOTED_RE.findall(error or "")
             if os.path.isabs(path) and _is_within(path, local)]
    check = _unreadable if direction == "push" else _unwritable
    for path in named + [local]:
        try:
            if check(path):
                return path
        except (OSError, ValueError):
            continue
    return ""


class _TransferThread(QThread):
    progress = pyqtSignal(int)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)
    # The TransferResult itself, just before done: done carries only its
    # text, which threw away the real size and time the history needs.
    result = pyqtSignal(object)

    def __init__(self, handler, direction, a, b):
        super().__init__()
        self.handler, self.direction, self.a, self.b = handler, direction, a, b
        self.cancel_event = threading.Event()
        # After a permission error: the path on this PC that caused it ("" when
        # the device refused).  Checked here, off the UI thread, before failed.
        self.local_refusal = ""

    def stop(self):
        self.cancel_event.set()

    def _merge_source(self):
        """The source to hand adb, re-checked now that the job is about to run.

        The plan copies a folder as ``src`` when its target does not exist yet.
        A retry after a partial copy, or the same folder queued twice, finds the
        target there by now, and adb then copies the folder INTO it
        (``dst/name/name``): merge with ``src/.`` instead, as the plan does for
        a target that already existed.  Best effort — on any error the job
        runs as planned."""
        a, b = self.a, self.b
        try:
            if os.path.basename(str(a).rstrip("/\\")) == ".":
                return a  # already the merge form
            if self.direction == "push":
                if os.path.isdir(a) and _probe_remote(self.handler, [b])[b][0] == "d":
                    return os.path.join(a, ".")
            elif os.path.isdir(b) and _probe_remote(self.handler, [a])[a][0] == "d":
                return a.rstrip("/") + "/."
        except Exception:
            pass
        return a

    def run(self):
        try:
            # Read by the GUI thread only after result/done, which come later.
            self.a = self._merge_source()
            if self.direction == "push":
                res = self.handler.push(self.a, self.b,
                                        on_progress=self.progress.emit,
                                        cancel_event=self.cancel_event, safe=False)
            else:
                res = self.handler.pull(self.a, self.b,
                                        on_progress=self.progress.emit,
                                        cancel_event=self.cancel_event, safe=False)
            self.result.emit(res)
            self.done.emit(str(res))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            from .device_access import is_permission_problem

            if is_permission_problem(error):
                self.local_refusal = _local_refusal(self.direction, self.a, self.b, error)
            self.failed.emit(error)


class FileBrowser(QWidget):
    log = pyqtSignal(str)
    # Per-file progress during a BATCH: the log panel keeps every line, but
    # the window shows one activity toast at a time, so 200 of these in a
    # row just thrash it and leave the last file name on screen instead of
    # a result. The batch reports itself once when the queue drains.
    trace = pyqtSignal(str)
    # {"path": device folder, "error": text, "action": "deleting 2 items",
    # "retry": callable}: the device refused a change; the device tab offers
    # adb root / disable-verity / remount and then calls retry.
    write_access_needed = pyqtSignal(object)
    # (device folder, PC folder): open another Files tab there (Ctrl+Shift+T)
    new_tab_requested = pyqtSignal(str, str)

    COLUMNS = ["Name", "Size", "Type", "Date Modified", "Permissions", "Owner"]

    _parse_ls_line = staticmethod(_parse_ls_line)

    def __init__(self, handler, start="/sdcard", parent=None, *, adb_gate=None,
                 local_start=None):
        """*adb_gate* (optional, from the device tab) has ``wrap(fn)``: device
        jobs then wait for one of the tab's adb slots before starting adb.
        *local_start* opens the PC pane in that folder instead of the home
        folder (a second Files tab opened from this one starts where it is)."""
        super().__init__(parent)
        self.handler = handler
        self._adb_gate = adb_gate
        self.local_cwd = (local_start if local_start and os.path.isdir(local_start)
                          else os.path.expanduser("~"))
        self.remote_cwd = start
        self._jobs = []           # listing / file-op workers (detached on close)
        self._save_jobs = []      # editor saves: never detached, the editor needs the result
        self._editors = []
        # Device files open in their apps on this PC (see file_open): each save
        # there goes back to the device.  And what to do once the pull that
        # copies one to this PC ends: (src, dst, "pull") -> (if done, if not).
        self._copies = file_open.CopyWatcher(self)
        self._copies.saved.connect(self._send_copy_back)
        self._after_pull = {}
        self._queue = []          # pending transfers: (src, dst, direction)
        # Every queued transfer's history (what the Transfers panel shows), and
        # the history ids still waiting in the queue: per job, in queue order.
        self.transfers = TransferLog()
        self._pending_ids = {}
        self._summarised_batch = 0  # the batch whose summary toast was shown
        self._transfer = None     # the single active _TransferThread
        self._cancel = threading.Event()
        self._cancels = 0         # Cancel presses: a plan made before one is dropped
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
        self._refused_transfers = []  # (src, dst, direction, error) the device refused
        # Selection changes arrive in bursts (a rectangle drag, an autoscroll
        # tick): the pane status lines are redone once the burst is handled.
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(0)
        self._status_timer.timeout.connect(self._update_pane_statuses)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        from .flowlayout import ToolbarFlowLayout

        # Page toolbar: quick jumps for this PC (left) and the device (right).
        # Its rows wrap instead of widening the window when the tab is narrow.
        toolbar, qbar = page_toolbar()
        # Colour language: this PC is blue, the device is green (Push/Pull match).
        jump_pc = QLabel("PC folders")
        jump_pc.setObjectName("mutedHint")
        qbar.addWidget(jump_pc)
        home = os.path.expanduser("~")
        # where the system keeps them: a Desktop that OneDrive backs up is not ~/Desktop
        for lbl, glyph, p in (("Home", "house", home),
                              ("Desktop", "monitor", desktop_dir()),
                              ("Downloads", "download", download_dir())):
            b = self._flat_button(lbl)
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
            b = self._flat_button(lbl)
            b.setIcon(icon(glyph, "green"))
            b.setToolTip(f"Open {p} on the device")
            b.clicked.connect(lambda _=False, path=p: self._jump_remote(path))
            qbar.addWidget(b)
        self.btn_new_tab = self._flat_button("New tab")
        self.btn_new_tab.setIcon(icon("plus", "blue"))
        self.btn_new_tab.setToolTip(
            "Open another Files tab on this device, starting in these folders "
            "(Ctrl+Shift+T)")
        self.btn_new_tab.clicked.connect(self._request_new_tab)
        self.btn_new_tab.hide()  # shown once a device tab listens (showEvent)
        qbar.addWidget(self.btn_new_tab)
        # Not Ctrl+T/Ctrl+W: the main window owns those (new session / close
        # tab), and a second binding would make both ambiguous.
        QShortcut(QKeySequence("Ctrl+Shift+T"), self, self._request_new_tab,
                  context=Qt.WidgetWithChildrenShortcut)
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
        self.local_path = _PathEdit(self.local_cwd)
        self.local_path.returnPressed.connect(self._go_local)
        lup = self._flat_button("Up")
        lup.setIcon(icon("arrow-up", "accent"))
        lup.setToolTip("Parent folder (Backspace)")
        lup.clicked.connect(self._up_local)
        lref = self._flat_button("Refresh")
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
        self.local_status = self._pane_status()
        left_lay.addWidget(self.local_status)

        left_lay.addLayout(self._pane_ops((
            ("New folder", self._local_mkdir),
            ("New file", self._local_newfile),
            ("Open", self._local_open),
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
        self.btn_push.setFocusPolicy(Qt.TabFocus)  # a click keeps the pane's focus
        self.btn_push.clicked.connect(self.push_selected)
        center_lay.addWidget(self.btn_push)

        self.btn_pull = QPushButton("Pull")
        self.btn_pull.setProperty("tone", "green")
        self.btn_pull.setIcon(icon("arrow-left", "green"))
        self.btn_pull.setToolTip("Pull selected Android files to the local PC folder")
        self.btn_pull.setFixedWidth(84)
        self.btn_pull.setFixedHeight(32)
        self.btn_pull.setFocusPolicy(Qt.TabFocus)
        self.btn_pull.clicked.connect(self.pull_selected)
        center_lay.addWidget(self.btn_pull)

        center_lay.addStretch(1)

        # ----------------- Right Pane: Android Device -----------------
        right_w = QWidget()
        right_lay = QVBoxLayout(right_w)
        right_lay.setContentsMargins(0, 0, 0, 0); right_lay.setSpacing(8)

        rtop = ToolbarFlowLayout(hspacing=8, vspacing=6)
        rtitle = self._pane_title("Device", "smartphone", "green")
        self.remote_path = _PathEdit(self.remote_cwd)
        self.remote_path.returnPressed.connect(self._go_remote)
        rup = self._flat_button("Up")
        rup.setIcon(icon("arrow-up", "accent"))
        rup.setToolTip("Parent folder (Backspace)")
        rup.clicked.connect(self._up_remote)
        rref = self._flat_button("Refresh")
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
        self.remote_status = self._pane_status()
        right_lay.addWidget(self.remote_status)

        right_lay.addLayout(self._pane_ops((
            ("New folder", self._remote_mkdir),
            ("New file", self._remote_newfile),
            ("Open", self._remote_open),
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
        # The panes above, the transfer history below: the handle between them
        # gives the history as much room as the user wants.
        self._vsplit = QSplitter(Qt.Vertical)
        self._vsplit.setHandleWidth(8)
        self._vsplit.setChildrenCollapsible(False)
        self._vsplit.addWidget(split)
        self.transfer_panel = TransferPanel(self.transfers)
        self._vsplit.addWidget(self.transfer_panel)
        self._vsplit.setStretchFactor(0, 1)
        self._vsplit.setStretchFactor(1, 0)
        body.addWidget(self._vsplit, 1)

        # The running transfer's bar, with Cancel for it and the queue; it sits
        # in the transfer panel, under the overall progress.
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.btn_cancel_transfer = QPushButton("Cancel")
        self.btn_cancel_transfer.setProperty("role", "danger")
        self.btn_cancel_transfer.setIcon(icon("x", "on-danger"))
        self.btn_cancel_transfer.setToolTip("Stop the running transfer and clear the queue")
        self.btn_cancel_transfer.setVisible(False)
        self.btn_cancel_transfer.setFocusPolicy(Qt.TabFocus)
        self.btn_cancel_transfer.clicked.connect(self.cancel_transfers)
        current = QWidget()
        bar_row = QHBoxLayout(current)
        bar_row.setContentsMargins(0, 0, 0, 0)
        bar_row.setSpacing(8)
        bar_row.addWidget(self.bar, 1)
        bar_row.addWidget(self.btn_cancel_transfer)
        self.transfer_panel.set_current_widget(current)
        self.transfer_panel.cancel_requested.connect(self.cancel_transfers)
        self.transfer_panel.retry_requested.connect(self._retry_transfers)
        self.transfer_panel.open_requested.connect(self._open_transfer_location)
        self.transfer_panel.details_toggled.connect(self._on_transfer_details_toggled)

        delete_hint = "Del Recycle Bin   ·   Shift+Del Delete permanently" if recycle_bin_available() else \
            "Del Delete"
        self.hint = QLabel("Drag files between panes or from Explorer   ·   drag beside the names "
                           "to select   ·   Ctrl+A Select all   ·   Enter Open   ·   F2 Rename   ·   "
                           f"F4 Edit   ·   F5 Refresh   ·   {delete_hint}")
        self.hint.setObjectName("mutedHint")
        self.hint.setWordWrap(True)
        body.addWidget(self.hint)

        # Shortcuts.  Each pane has its own set, scoped to the pane, so they
        # work after clicking Up, Refresh or a pane button too (and two
        # window-wide shortcuts on one key would be ambiguous: neither fires).
        # A focused path field keeps its own Ctrl+A / Delete / Backspace.
        # F5 is scoped to this page for the same reason: two Files tabs side
        # by side in a split view are two pages in one window.
        QShortcut(QKeySequence("F5"), self, activated=self._on_f5,
                  context=Qt.WidgetWithChildrenShortcut)
        for pane, table, actions in (
                (left_w, self.local_table, (
                    (QKeySequence("F4"), self._local_edit),
                    (QKeySequence(QKeySequence.Delete), self._local_delete),
                    (QKeySequence("Shift+Del"), self._local_delete_permanently),
                    (QKeySequence("F2"), self._local_rename),
                    (QKeySequence(QKeySequence.Copy), self._local_copy),
                    (QKeySequence(QKeySequence.Paste), self._local_paste),
                    (QKeySequence("Ctrl+Shift+N"), self._local_mkdir),
                    (QKeySequence("Backspace"), self._up_local),
                    (QKeySequence("Alt+Up"), self._up_local),
                )),
                (right_w, self.remote_table, (
                    (QKeySequence("F4"), self._remote_edit),
                    (QKeySequence(QKeySequence.Delete), self._remote_delete),
                    (QKeySequence("Shift+Del"), self._remote_delete),
                    (QKeySequence("F2"), self._remote_rename),
                    (QKeySequence(QKeySequence.Copy), self._remote_copy),
                    (QKeySequence(QKeySequence.Paste), self._remote_paste),
                    (QKeySequence("Ctrl+Shift+N"), self._remote_mkdir),
                    (QKeySequence("Backspace"), self._up_remote),
                    (QKeySequence("Alt+Up"), self._up_remote),
                ))):
            for key, slot in actions:
                QShortcut(key, pane, activated=slot, context=Qt.WidgetWithChildrenShortcut)
            QShortcut(QKeySequence(QKeySequence.SelectAll), pane,
                      activated=lambda t=table: self._select_all(t),
                      context=Qt.WidgetWithChildrenShortcut)
            # Enter and Esc only on the table: the path field needs its own Enter.
            for key in ("Return", "Enter"):
                QShortcut(QKeySequence(key), table, activated=lambda t=table: self._open_current(t),
                          context=Qt.WidgetShortcut)
            QShortcut(QKeySequence("Esc"), table, activated=table.clearSelection,
                      context=Qt.WidgetShortcut)

        # The device listing starts lazily on first show (one listing, not two).
        self._loaded_remote = False
        self._job(get_windows_drives, self._on_drives,
                  lambda msg: self.log.emit(f"[ERROR] drive list: {msg}"))
        if not _OLD_COPIES["tidied"]:
            _OLD_COPIES["tidied"] = True
            self._job(file_open.forget_old_copies)
        self.refresh_local()

    def showEvent(self, event):
        super().showEvent(event)
        # New tab only where something opens it (a device tab, not a test)
        self.btn_new_tab.setVisible(self.receivers(self.new_tab_requested) > 0)
        if not self._loaded_remote:
            self.refresh_remote()

    @staticmethod
    def _flat_button(text: str) -> QPushButton:
        """A compact ghost button that a mouse click never takes the keyboard to,
        so the file shortcuts keep working after it is clicked."""
        button = QPushButton(text)
        button.setProperty("role", "ghost")
        button.setObjectName("paneOp")
        button.setFocusPolicy(Qt.TabFocus)
        return button

    @staticmethod
    def _pane_status() -> QLabel:
        label = QLabel("")
        label.setObjectName("mutedHint")
        return label

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

        row = ToolbarFlowLayout(hspacing=2, vspacing=6)
        for text, fn in actions:
            b = FileBrowser._flat_button(text)
            glyph, tone = _PANE_OP_ICONS.get(text, ("file", "dim"))
            b.setIcon(icon(glyph, tone))
            if text in _PANE_OP_TIPS:
                b.setToolTip(_PANE_OP_TIPS[text])
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        delete = QPushButton("Delete")
        delete.setProperty("role", "danger")
        delete.setObjectName("paneOp")
        delete.setFocusPolicy(Qt.TabFocus)
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
        table.setWordWrap(False)
        # long names keep their extension ("app-rel…v2.apk"); other columns cut at the end
        table.setItemDelegateForColumn(0, _NameDelegate(table))
        table.setIconSize(QSize(18, 18))
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(26)
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setMinimumSectionSize(48)
        size_header = table.horizontalHeaderItem(1)
        if size_header is not None:
            size_header.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # Wide enough for a full date, size and permission string in the
        # table's own (style sheet) font, so none of them ends in "…".
        table.ensurePolished()
        metrics = table.fontMetrics()
        for column, sample in ((1, "999.9 MB"), (2, "Folder"), (3, "2026-09-01 10:00"),
                               (4, "drwxrwxrwx"), (5, "u0_a1234")):
            table.setColumnWidth(column, metrics.horizontalAdvance(sample) + 24)
        table.setSortingEnabled(False)
        table.setDragEnabled(True)
        # A bound method, not a lambda: PyQt holds the receiver weakly, so the
        # table does not keep a strong reference back to this page (a cycle
        # Python could collect mid-event, deleting a widget Qt still had posted
        # events for).
        table.itemSelectionChanged.connect(self._on_selection_changed)
        return table

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

    def _job(self, fn, on_done=None, on_fail=None, *, device=False):
        """*device=True*: *fn* runs adb, so it waits for an adb slot (``adb_gate``);
        PC-side listings never queue behind a slow device."""
        if device and self._adb_gate is not None:
            fn = self._adb_gate.wrap(fn)
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
        if isinstance(table, _FileTableWidget):
            table.empty_text = ""
        table.setRowCount(0)
        table.insertRow(0)
        it = QTableWidgetItem(cached_icon("refresh", "dim"), "Loading…")
        it.setFlags(Qt.ItemIsEnabled)
        table.setItem(0, 0, it)

    def _populate(self, table: QTableWidget, rows, parent_row: bool):
        file_table = isinstance(table, _FileTableWidget)
        if file_table:
            unsafe, seen = set(getattr(rows, "uncertain", ())), set()
            for row in rows:
                if row[0] in seen:
                    unsafe.add(row[0])  # two rows share a name: can't tell them apart
                seen.add(row[0])
            table.unsafe_names = frozenset(unsafe)
        # (row, date as a number): worked out once, for the date cell and the sort
        listing = [(row, row[8] if len(row) > 8 and row[8] is not None
                    else _mtime_sort_key(row[4])) for row in rows]
        if file_table:
            # in the header's order before any cell exists: building every row
            # and then taking them all apart to sort them doubled the work
            listing = table.sorted_listing(listing)
        if parent_row:
            listing.insert(0, (("..", 0, "<DIR>", "Folder", "", "", "", True), 0.0))
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(0)
            table.setRowCount(len(listing))  # all rows in one go, not an insertRow each
            local_dir = table.base_dir if table is self.local_table else ""
            for index, (row, date_key) in enumerate(listing):
                self._set_row(table, index, row[:8], date_key, local_dir)
            if file_table:
                kinds, sizes = [], []
                for index, (row, _date_key) in enumerate(listing):
                    kind = 0 if parent_row and index == 0 else 1 if row[7] else 2
                    kinds.append(kind)
                    sizes.append(row[1] if kind == 2 and isinstance(row[1], int) else 0)
                table.set_row_summary(kinds, sizes)
                table.empty_text = "This folder is empty"
        finally:
            table.setUpdatesEnabled(True)
        self._update_pane_status(table)

    def _on_selection_changed(self) -> None:
        """A selection rectangle changes the selection on every mouse move and
        autoscroll tick: the status lines follow once per pass of the event
        loop, not once per change."""
        self._status_timer.start()

    def _update_pane_statuses(self) -> None:
        for table in (self.local_table, self.remote_table):
            self._update_pane_status(table)

    def _update_pane_status(self, table) -> None:
        """"3 folders, 12 files" and, with a selection, "2 selected · 1.4 MB".

        Both come from the table's row summary and the selection's ranges, so
        the line costs the same for three rows as for 20,000."""
        local = table is getattr(self, "local_table", None)
        label = getattr(self, "local_status" if local else "remote_status", None)
        if label is None or self._closing or not isinstance(table, _FileTableWidget):
            return
        summary = table.row_summary()
        folders, files = summary.folders, summary.files
        parts = []
        if folders:
            parts.append(f"{folders} folder{'s' if folders != 1 else ''}")
        if files:
            parts.append(f"{files} file{'s' if files != 1 else ''}")
        text = ", ".join(parts)
        selected, size = table.selection_summary()
        if selected:
            text += f"   ·   {selected} selected"
            if size:
                text += f" ({_human_size(size)})"
        label.setText(text)

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
        self.local_table.setFocus(Qt.OtherFocusReason)

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
        self.local_table.setFocus(Qt.OtherFocusReason)

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
            self._local_open_row(row)  # the clicked row, not the first selected one
            return
        if name == "..":
            self._up_local()
        else:
            self._navigate_local(os.path.join(self.local_cwd, name))

    # ---- Navigation: Android Device ----
    def _jump_remote(self, path: str):
        self.remote_cwd = _normalize_remote_path(path)
        self.refresh_remote()
        self.remote_table.setFocus(Qt.OtherFocusReason)

    def _go_remote(self):
        p = self.remote_path.text().strip()
        if p:
            self.remote_cwd = _normalize_remote_path(p)
        self.refresh_remote()
        self.remote_table.setFocus(Qt.OtherFocusReason)

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
                             lambda msg: self._on_remote_list_failed(gen, path, msg),
                             device=True)

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
            self._offer_write_access(path, msg, "opening the folder",
                                     lambda: self._jump_remote(path))
            return
        self._remote_failed = True
        self.remote_table.base_dir = self.remote_cwd
        self._populate(self.remote_table, [], parent_row=True)
        self.remote_table.empty_text = "This folder could not be listed"

    def _on_remote_double_click(self, row: int, _col: int):
        it = self.remote_table.item(row, 0)
        if not it:
            return
        data = it.data(Qt.UserRole)
        if not data:
            return
        name, is_dir = data
        if not is_dir:
            self._remote_open_row(row)  # the clicked row, not the first selected one
            return
        if name == "..":
            self._up_remote()
        elif not self._refuse_unsafe(self.remote_table, [name], "Open"):
            self.remote_cwd = posixpath.join(self.remote_cwd, name)
            self.refresh_remote()

    def refresh(self):
        self.refresh_local()
        self.refresh_remote()

    def _add_row(self, table: QTableWidget, name: str, raw_size: int, sz_str: str,
                 ftype: str, mtime: str, perms: str, owner: str, is_dir: bool, raw_mtime: float = None):
        row = table.rowCount()
        table.insertRow(row)
        local_dir = table.base_dir if table is self.local_table else ""
        self._set_row(table, row, (name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir),
                      raw_mtime if raw_mtime is not None else _mtime_sort_key(mtime), local_dir)

    @staticmethod
    def _set_row(table: QTableWidget, row: int, entry, date_key: float, local_dir: str) -> None:
        """Fill *row* with the six cells of a listing *entry*; *local_dir* is the
        PC folder it is in ("" for the device)."""
        name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir = entry
        # The visible text is the exact name; the kind shows as the item's icon
        # (DecorationRole), so nothing that reads or sorts names sees a prefix.
        it_name = _FileItem(name)
        it_name.setData(Qt.UserRole, (name, is_dir))
        glyph, tone = _entry_icon_key(name, is_dir, ftype)
        # the system's own icon where it has one (a PC row by its real path, a
        # device row by its type); ICON_ROLE keeps the kind either way
        it_name.setIcon(file_icons().row_icon(name, is_dir, ftype, glyph, tone, local_dir))
        it_name.setData(ICON_ROLE, glyph)
        it_size = _FileItem(sz_str)
        it_size.setData(Qt.UserRole, raw_size)
        it_size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        it_date = _FileItem(mtime)
        it_date.setData(Qt.UserRole, date_key)

        flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDropEnabled
        if not (is_dir and name == ".."):
            flags |= Qt.ItemIsDragEnabled
        for col, it in enumerate((it_name, it_size, _FileItem(ftype), it_date, _FileItem(perms),
                                  _FileItem(owner))):
            it.setFlags(flags)
            table.setItem(row, col, it)

    # ---- Selection helpers ----
    def _select_all(self, table) -> None:
        table.select_all_entries()
        table.setFocus(Qt.OtherFocusReason)

    def _open_current(self, table) -> None:
        """Enter: open the current folder, or edit the current file."""
        row = table.currentRow()
        if row < 0 or table.item(row, 0) is None:
            return
        if table is self.local_table:
            self._on_local_double_click(row, 0)
        else:
            self._on_remote_double_click(row, 0)

    @staticmethod
    def _row_entry(table, row: int):
        """``(name, is_dir)`` of a real file row (not '..' or 'Loading…'), else None."""
        it = table.item(row, 0)
        data = it.data(Qt.UserRole) if it else None
        if not isinstance(data, (tuple, list)) or len(data) < 2 or data[0] == "..":
            return None
        return data[0], data[1]

    def _selected_rows(self, table) -> List[int]:
        if isinstance(table, _FileTableWidget):
            return table.selected_entry_rows()
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

    def _valid_new_name(self, name: str, title: str, *, local: bool) -> bool:
        """True when *name* names an entry IN the folder on screen.

        ``posixpath.join("/sdcard", "/etc/passwd")`` is ``/etc/passwd``, so a
        typed path (or ``.`` / ``..``) would create the item somewhere the user
        can't see.  Locally a backslash or a drive letter does the same.
        """
        bad = "/" in name or name in (".", "..")
        if local:
            bad = bad or os.sep in name or bool(os.path.splitdrive(name)[0])
        if bad:
            QMessageBox.warning(
                self, title,
                f"{name!r} is not a valid name.\n\nEnter a name, not a path: the item is always "
                "created in the folder shown here.")
            return False
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
        cancels = self._cancels
        self._job(lambda: _plan_push(handler, sources, dst_dir),
                  lambda plan: self._apply_transfer_plan(
                      "Push", "Pushing", dst_dir, plan[0], plan[1], plan[2], (), "on the device",
                      cancels),
                  lambda msg: self._report_errors("Push", [msg]), device=True)

    def _start_pull(self, sources, dst_dir: str):
        """Resolve links and check the local side on a worker, ask, then queue."""
        sources = [s for s in sources if s]
        if self._closing or not sources:
            return
        handler = self.handler
        cancels = self._cancels
        self._job(lambda: _plan_pull(handler, sources, dst_dir),
                  lambda plan: self._apply_transfer_plan(
                      "Pull", "Pulling", dst_dir, plan[0], plan[1], plan[2], plan[3], "on the PC",
                      cancels),
                  lambda msg: self._report_errors("Pull", [msg]), device=True)

    def _apply_transfer_plan(self, title, verb, dst_dir, jobs, collisions, errors, notes, where,
                             cancels=None):
        """*cancels*: :attr:`_cancels` when the plan was started.  Cancel stops
        what is running and queued - and a plan still being made, which would
        otherwise start copying right after the user stopped everything."""
        if cancels is not None and cancels != self._cancels:
            if jobs:
                self.log.emit(f"[INFO] {title} cancelled: nothing was copied.")
            return
        for note in notes:
            self.log.emit(note)
        if errors:
            self._report_errors(title, errors)
        if not jobs:
            return
        if collisions and not self._ask_overwrite(collisions, where):
            self.log.emit(f"[INFO] {title} cancelled: nothing was overwritten.")
            return
        self._enqueue_transfers(jobs, f"{verb} {len(jobs)} item(s) to {dst_dir}…")

    def _offer_write_access(self, folder: str, error, action: str, retry=None) -> bool:
        """When the device refused a change, ask the device tab to offer write
        access (and retry). False when this is no refusal or nobody listens."""
        from .device_access import is_permission_problem

        if self._closing or not is_permission_problem(error):
            return False
        if not self.receivers(self.write_access_needed):
            return False
        self.write_access_needed.emit(
            {"path": folder, "error": str(error), "action": action, "retry": retry})
        return True

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

    def _batch_summary(self) -> str:
        """One line for a batch that just finished, or "" when there is nothing
        to say. Said once per batch: a second drain of the same queue is quiet."""
        stats = self.transfers.stats()
        batch = self.transfers.batch
        if stats.total <= 1 or batch == self._summarised_batch:
            return ""  # a single transfer already reported itself by name
        self._summarised_batch = batch
        items = f"{stats.done} item" + ("" if stats.done == 1 else "s")
        if stats.cancelled:
            return f"[INFO] {stats.past} {items} before the transfer was cancelled"
        if stats.failed:
            return (f"[WARNING] {stats.past} {stats.done} of {stats.total} items - "
                    f"{stats.failed} failed (see the log)")
        return f"[OK] {stats.past} {items}"

    def _enqueue_transfers(self, jobs, message: str):
        """Append transfers to the queue; only one transfer thread runs at a time.

        Each job is also recorded in :attr:`transfers` (the history the panel
        shows) and sized on a worker; the queue itself keeps its plain
        ``(src, dst, direction)`` tuples."""
        if self._closing or not jobs:
            return
        busy = self._transfer is not None
        ids = self.transfers.add(jobs)
        for job, item_id in zip(jobs, ids):
            self._pending_ids.setdefault(tuple(job), deque()).append(item_id)
        self._queue.extend(jobs)
        self._measure_transfers([(tuple(job), item_id) for job, item_id in zip(jobs, ids)])
        self.transfer_panel.refresh()
        if busy:
            message += f" (queued; {len(self._queue)} waiting)"
        self.log.emit(message)
        if not busy:
            self._process_queue()

    def _take_transfer_id(self, job) -> int:
        """The history id of *job*, which was just taken off the queue.

        Identical jobs queued twice keep their order: ids wait in a FIFO per
        job. A job that reached the queue some other way is recorded now."""
        key = tuple(job)
        pending = self._pending_ids.get(key)
        if pending:
            item_id = pending.popleft()
            if not pending:
                del self._pending_ids[key]
            return item_id
        return self.transfers.add([key])[0]

    def _process_queue(self):
        if self._closing or self._transfer is not None:
            return
        if not self._queue:
            self.bar.setVisible(False)
            self.btn_cancel_transfer.setVisible(False)
            summary = self._batch_summary()
            if summary:
                self.log.emit(summary)
            self.transfer_panel.refresh()
            self.refresh_local()
            self.refresh_remote()
            refused, self._refused_transfers = self._refused_transfers, []
            if refused:
                jobs = [(src, dst, direction) for src, dst, direction, _error in refused]
                # the failed rows this retry replaces, taken now: the retry may
                # run much later, after the device was made writable
                wanted = set(jobs)
                ids = [item.id for item in self.transfers.batch_items()
                       if item.status == FAILED and item.job in wanted]
                src, dst, direction, error = refused[0]
                device_path = dst if direction == "push" else src
                self._offer_write_access(
                    posixpath.dirname(device_path.rstrip("/")) or "/", error,
                    f"the {direction} of {len(jobs)} item(s)",
                    lambda: self._retry_refused(jobs, ids))
            return

        src, dst, direction = self._queue.pop(0)
        name = transfer_name(src)
        waiting = f"  ({len(self._queue)} queued)" if self._queue else ""
        self.bar.setFormat(f"{direction} {name}: %p%{waiting}")
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.btn_cancel_transfer.setEnabled(True)
        self.btn_cancel_transfer.setVisible(True)

        t = _TransferThread(self.handler, direction, src, dst)
        self._transfer = t
        t._transfer_id = self._take_transfer_id((src, dst, direction))
        t._job = (src, dst, direction)  # as queued (the thread may rewrite its source)
        self.transfers.start(t._transfer_id)
        t.progress.connect(self.bar.setValue)
        t.progress.connect(lambda pct, t=t: self._on_transfer_progress(t, pct))
        result = getattr(t, "result", None)  # a test double may not have one
        if result is not None:
            result.connect(lambda res, t=t: self._on_transfer_result(t, res))
        t.done.connect(lambda _res, t=t: self._on_transfer_finished(t, True, ""))
        t.failed.connect(lambda err, t=t: self._on_transfer_finished(t, False, err))
        # Safety net: a thread that ends without done/failed must not wedge the queue.
        t.finished.connect(lambda t=t: self._on_transfer_finished(t, False, "transfer stopped"))
        park_thread(t)
        t.start()
        self.transfer_panel.refresh()

    def _on_transfer_progress(self, t, percent) -> None:
        if t is not self._transfer or self._closing:
            return  # a late report from a transfer that already ended
        self.transfers.progress(getattr(t, "_transfer_id", None), percent)
        self.transfer_panel.refresh()

    def _on_transfer_result(self, t, result) -> None:
        """The worker's TransferResult: the real size and time of what it copied."""
        if self._closing:
            return
        t._transfer_duration = getattr(result, "duration", None)
        size = getattr(result, "size_bytes", None)
        # Pulling ``dir/.`` merges into an existing folder, and measuring that
        # folder afterwards also counts what was already in it: keep the estimate.
        merged_pull = t.direction == "pull" and str(t.a).endswith("/.")
        if size and not merged_pull:
            self.transfers.set_size(getattr(t, "_transfer_id", None), size, exact=True,
                                    files=getattr(result, "files", None))
        self.transfer_panel.refresh()

    def _on_transfer_finished(self, t, ok: bool, message: str):
        if t is not self._transfer:
            return
        self._transfer = None
        # The job as it was queued: a run may copy a folder as "dir/." (see
        # _TransferThread._merge_source), which no longer matches its row.
        job = getattr(t, "_job", (t.a, t.b, t.direction))
        # a copy of a device file being opened in its app (see _pull_to_open)
        after = self._after_pull.pop(job, None)
        if self._closing or not _alive(self):
            if after is not None:
                after[1](None)
            return
        item_id = getattr(t, "_transfer_id", None)
        cancelled = self._cancelled_transfer is t
        if cancelled:
            self._cancelled_transfer = None
            left = "" if ok else _cancel_leftover(message)
            self.transfers.finish(item_id, CANCELLED, error=f"cancelled - {left}" if left else "")
            self.log.emit(f"[CANCELLED] {t.direction} {transfer_name(t.a)}"
                          + (f" ({left})" if left else ""))
        elif ok:
            self.transfers.finish(item_id, DONE, duration=getattr(t, "_transfer_duration", None))
            line = f"[OK] {t.direction}: {transfer_name(t.a)}"
            # one of many: keep it in the log, out of the toast
            (self.trace if self.transfers.batch_size() > 1 else self.log).emit(line)
        else:
            self.transfers.finish(item_id, FAILED, error=message)
            self.log.emit(f"[ERROR] {t.direction} {t.a}: {message}")
            from .device_access import is_permission_problem

            local = getattr(t, "local_refusal", "")
            if local:
                # this PC said no, not the device: adb root would change nothing
                self.log.emit(f"[ERROR] {t.direction} {transfer_name(t.a)}: {local} can't be "
                              f"{'read' if t.direction == 'push' else 'written'} on this PC "
                              "(the device was not the problem)")
            elif is_permission_problem(message) and after is None:
                # (an opened file's copy is not offered again: its folder goes)
                self._refused_transfers.append((*job, message))
        if after is not None and (cancelled or not ok):
            # an opened file's copy that failed: its folder is gone, so there
            # is nothing a Retry could do (the error is in the log)
            self.transfers.remove([item_id])
        self.transfer_panel.refresh()
        self._process_queue()  # first: nothing an opened file does may hold up the queue
        if after is not None:
            if ok and not cancelled:
                after[0]()
            else:
                after[1](None if cancelled else message)

    def cancel_transfers(self):
        """Stop the running transfer and drop everything still queued (and any
        push or pull still being planned)."""
        self._cancels += 1
        dropped = len(self._queue)
        for job in self._queue:
            after = self._after_pull.pop(tuple(job), None)
            if after is not None:
                after[1](None)
        self._queue.clear()
        self._pending_ids.clear()
        self.transfers.cancel_queued()  # the history says what never ran
        self.transfer_panel.refresh()
        self._refused_transfers = []
        t = self._transfer
        if t is None:
            if dropped:
                self.log.emit(f"[OK] Cleared {dropped} queued transfer(s).")
            return
        self._cancelled_transfer = t
        try:
            t.stop()
        except RuntimeError:
            pass
        self.bar.setFormat("Cancelling…")
        self.btn_cancel_transfer.setEnabled(False)
        extra = f" and {dropped} queued transfer(s)" if dropped else ""
        self.log.emit(f"Cancelling {t.direction} {transfer_name(t.a)}{extra}…")

    # ---- Transfer history (the Transfers panel) ----
    def _measure_transfers(self, pairs) -> None:
        """Size newly queued transfers on a worker - PC files for a push, the
        device for a pull - for byte-weighted progress and a time left. No
        transfer ever waits for this."""
        pushes = [(job[0], item_id) for job, item_id in pairs if job[2] == "push"]
        pulls = [(job[0], item_id) for job, item_id in pairs if job[2] == "pull"]
        if pushes:
            push_paths = [path for path, _ in pushes]
            self._job(lambda: measure_local(push_paths),
                      lambda sizes: self._apply_sizes(pushes, sizes, local=True))
        if pulls and self.handler is not None:
            handler = self.handler
            pull_paths = [path for path, _ in pulls]
            # no adb slot, like the transfers themselves: `du` on a big folder
            # can take a while and must not hold up this tab's listings
            self._job(lambda: measure_remote(handler, pull_paths),
                      lambda sizes: self._apply_sizes(pulls, sizes, local=False))

    def _apply_sizes(self, pairs, sizes, *, local: bool) -> None:
        for path, item_id in pairs:
            found = (sizes or {}).get(path)
            if not found:
                continue
            if local:
                size, files = found
                self.transfers.set_size(item_id, size, exact=True, files=files)
            else:
                size, exact = found
                self.transfers.set_size(item_id, size, exact=exact)
        self.transfer_panel.refresh()

    def _retry_refused(self, jobs, ids) -> None:
        """The device was made writable: queue the refused transfers again.

        Their failed rows go, like a Retry from the panel - left behind they
        kept "Retry N failed" on offer after the retry had succeeded, and
        using it copied the same files a second time."""
        if self._closing or not jobs:
            return
        self.transfers.remove(self.transfers.retryable(ids))
        self._enqueue_transfers(jobs, f"Retrying {len(jobs)} transfer(s)…")

    def _retry_transfers(self, ids) -> None:
        """Queue failed or cancelled transfers again: their old rows go, and
        the new attempt gets fresh ones (and its own place in the batch)."""
        if self._closing:
            return
        ids = self.transfers.retryable(ids)
        jobs = [self.transfers.get(item_id).job for item_id in ids]
        if not jobs:
            return
        self.transfers.remove(ids)
        self._enqueue_transfers(jobs, f"Retrying {len(jobs)} transfer(s)…")

    def _open_transfer_location(self, item) -> None:
        """Show where *item* went: its device folder for a push, its PC folder
        for a pull."""
        if self._closing or item is None:
            return
        if item.direction == "push":
            self._jump_remote(posixpath.dirname(item.dst.rstrip("/")) or "/")
        else:
            folder = os.path.dirname(os.path.normpath(item.dst))
            self._jump_local(folder or item.dst)

    def _on_transfer_details_toggled(self, shown: bool) -> None:
        """Opening the details gives them room; closing hands it back to the
        panes (the collapsed panel caps its own height)."""
        if not shown:
            return
        sizes = self._vsplit.sizes()
        if len(sizes) != 2:
            return
        total = sum(sizes)
        # about two fifths of the page: enough for a screenful of rows while
        # the panes stay usable; the handle moves it from there
        want = min(400, max(240, total * 2 // 5))
        if total > 0 and sizes[1] < want:
            self._vsplit.setSizes([max(1, total - want), want])

    def _request_new_tab(self) -> None:
        """Ctrl+Shift+T / New tab: another Files tab on this connection, opened
        in the folders this one shows (the device tab builds it)."""
        if not self._closing:
            self.new_tab_requested.emit(self.remote_cwd, self.local_cwd)

    # ---- File Operations: Local ----
    def _local_mkdir(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        name = name.strip() if ok else ""
        if name:
            if not self._valid_new_name(name, "New Folder", local=True):
                return
            base = self._local_dir_for_action("New Folder")
            if base is None:
                return
            target = os.path.join(base, name)
            self._local_job(f"mkdir {name}",
                            lambda: os.makedirs(target, exist_ok=True), "Error")

    def _local_newfile(self):
        name, ok = QInputDialog.getText(self, "New File", "File name:")
        name = name.strip() if ok else ""
        if name:
            if not self._valid_new_name(name, "New File", local=True):
                return
            base = self._local_dir_for_action("New File")
            if base is None:
                return
            target = os.path.join(base, name)
            self._local_job(f"create {name}", lambda: _create_empty_file(target), "Error")

    def _local_copy(self):
        items = self._selected_local()
        if items:
            self._clipboard = [p for p, _ in items]
            self._clipboard_src = "local"
            self.log.emit(f"[OK] Copied {len(self._clipboard)} local item(s) to clipboard.")

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
        if not new_name or new_name == old_name:
            return
        # "..\a.txt" or "sub/a.txt" would move the item out of sight
        if not self._valid_new_name(new_name, "Rename", local=True):
            return
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        label = f"rename {old_name}"

        def checked(state):
            if state in ("dir", "taken"):
                what = "folder" if state == "dir" else "file"
                QMessageBox.warning(self, "Rename",
                                    f"A {what} named {new_name!r} already exists here.")
                return
            replace = state == "file"
            if replace and QMessageBox.question(
                    self, "Rename",
                    f"{new_name!r} already exists here. Replace it with {old_name!r}?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            self._local_job(label, lambda: _rename_local_item(old_path, new_path, replace),
                            "Error")

        # the check touches the disk (a network drive can stall): on a worker
        self._job(lambda: _local_rename_state(old_path, new_path), checked,
                  lambda msg: self.log.emit(f"[ERROR] {label}: {msg}"))

    def _local_delete(self):
        """Delete: to the Recycle Bin on Windows (Shift+Delete skips it)."""
        if not recycle_bin_available():
            self._local_delete_permanently()
            return
        items = self._selected_local()
        if not items:
            return
        if QMessageBox.question(self, "Delete Local",
                                f"Move {len(items)} item(s) to the Recycle Bin?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        paths = [p for p, _ in items]
        self._local_job(f"move {len(paths)} local item(s) to the Recycle Bin",
                        lambda: _recycle_local_items(paths))

    def _local_delete_permanently(self):
        items = self._selected_local()
        if not items:
            return
        if QMessageBox.question(self, "Delete Local",
                                f"Permanently delete {len(items)} item(s) from Local PC?\n\n"
                                "They will not go to the Recycle Bin.",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        paths = [p for p, _ in items]
        self._local_job(f"delete {len(paths)} local item(s)", lambda: _delete_local_items(paths))

    # ---- File Operations: Remote ----
    def _remote_mkdir(self):
        name, ok = QInputDialog.getText(self, "New Device Folder", "Folder name:")
        name = name.strip() if ok else ""
        if name:
            if not self._valid_new_name(name, "New Device Folder", local=False):
                return
            base = self._remote_dir_for_action("New Device Folder")
            if base is None:
                return
            self._run_shell(f"mkdir {name}", _mkdir_cmd(posixpath.join(base, name)))

    def _remote_newfile(self):
        name, ok = QInputDialog.getText(self, "New Device File", "File name:")
        name = name.strip() if ok else ""
        if name:
            if not self._valid_new_name(name, "New Device File", local=False):
                return
            base = self._remote_dir_for_action("New Device File")
            if base is None:
                return
            self._run_shell(f"create {name}", _touch_cmd(posixpath.join(base, name)))

    def _remote_copy(self):
        items = self._selected_remote()
        if items:
            if self._refuse_unsafe(self.remote_table, [name for name, _ in items], "Copy"):
                return
            self._clipboard = [posixpath.join(self.remote_cwd, name) for name, _ in items]
            self._clipboard_src = "remote"
            self.log.emit(f"[OK] Copied {len(self._clipboard)} device item(s) to clipboard.")

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
                self.log.emit("[INFO] Paste cancelled: nothing was overwritten.")
                return
            self._run_shell_batch(commands, timeout=600, summary="Pasted")

        self._job(lambda: _plan_remote_copy(handler, sources, dst_dir), planned,
                  lambda msg: self._report_errors("Paste", [msg]), device=True)

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
        if not self._valid_new_name(new_name, "Rename", local=False):
            return
        src = posixpath.join(self.remote_cwd, old_name)
        dst = posixpath.join(self.remote_cwd, new_name)
        handler = self.handler
        self._job(lambda: handler.shell(_rename_check_cmd(src, dst), timeout=30, safe=False),
                  lambda res: self._on_rename_checked(old_name, new_name, src, dst, res),
                  lambda msg: self._report_errors("Rename", [msg]), device=True)

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
        self._delete_remote_targets([posixpath.join(self.remote_cwd, n) for n, _ in items])

    def _delete_remote_targets(self, targets):
        label = f"delete {len(targets)} device item(s)"
        handler = self.handler
        folder = self.remote_cwd

        def done(_removed):
            self.log.emit(f"[OK] {label}")
            self.refresh_remote()

        def fail(msg):
            self.log.emit(f"[ERROR] {label}: {msg}")
            self.refresh_remote()
            self._offer_write_access(folder, msg, f"deleting {len(targets)} item(s)",
                                     lambda: self._delete_remote_targets(targets))

        cancel = self._cancel

        def work():
            removed = []
            # The engine deletes one shell-sized group per `rm`; handing it
            # the groups one by one lets a closed tab stop the rest.
            for group in _chunks(targets):
                if cancel.is_set():
                    break
                removed += handler.remove(group, recursive=True, safe=False)
            return removed

        # Deletion belongs to the engine: it refuses '/', refuses a folder
        # without recursive, and keeps each `rm` shell-sized.  One unbounded
        # `rm -rf` built here did none of that.  Like a paste batch it can run
        # for minutes, so it takes no adb slot (device=False): listings keep
        # working meanwhile.
        self._job(work, done, fail)

    def _run_shell(self, label, cmd):
        self._run_shell_batch([(label, cmd)])

    def _run_shell_batch(self, commands, timeout: float = 30, summary: str = ""):
        """Run device commands in order on one worker, log each, refresh once.

        A batch of more than one command reports ONCE: the per-command lines
        stay in the log, and *summary* (a past-tense verb such as "Pasted")
        names the single toast. Without it, pasting 200 files fired 200
        toasts that replaced each other and left a file name on screen
        instead of a result. Failures always surface individually.

        Closing the tab stops the batch before its next command (the one
        running finishes: a device shell call can't be interrupted)."""
        handler = self.handler
        cancel = self._cancel

        def work():
            out = []
            for label, cmd in commands:
                if cancel.is_set():
                    break
                try:
                    res = handler.shell(cmd, timeout=timeout, safe=False)
                except Exception as exc:  # reported per command in done()
                    out.append((label, False, f"{type(exc).__name__}: {exc}"))
                    continue
                out.append((label, bool(res.ok), "" if res.ok else _result_error(res)))
            return out

        folder = self.remote_cwd

        def done(results):
            bulk = len(results) > 1
            succeeded = 0
            for label, ok, err in results:
                if ok:
                    succeeded += 1
                    # one of many: the log keeps it, the toast does not
                    (self.trace if bulk else self.log).emit(f"[OK] {label}")
                else:
                    self.log.emit(f"[ERROR] {label}: {err}")
            if bulk:
                verb = summary or "Finished"
                items = f"{succeeded} item" + ("" if succeeded == 1 else "s")
                failed = len(results) - succeeded
                if failed:
                    self.log.emit(f"[WARNING] {verb} {succeeded} of {len(results)} "
                                  f"items - {failed} failed (see the log)")
                else:
                    self.log.emit(f"[OK] {verb} {items}")
            self.refresh_remote()
            from .device_access import is_permission_problem

            refused = [(command, err) for command, (_label, ok, err) in zip(commands, results)
                       if not ok and is_permission_problem(err)]
            if refused:
                retry = [command for command, _err in refused]
                self._offer_write_access(folder, refused[0][1], refused[0][0][0],
                                         lambda: self._run_shell_batch(retry, timeout,
                                                                      summary))

        def fail(msg):
            self.log.emit(f"[ERROR] {commands[0][0] if commands else 'device command'}: {msg}")
            self.refresh_remote()

        # No adb slot: a paste batch may copy for minutes (timeout 600 s).
        self._job(work, done, fail)

    # ---- Context Menus ----
    def _context_local(self, pos):
        menu = QMenu(self)
        self._menu_action(menu, "Open\tEnter", self._local_open)
        self._menu_action(menu, "Open with…", lambda: self._local_open(choose=True))
        self._menu_action(menu, "Push to device", self.push_selected)
        self._menu_action(menu, "Edit\tF4", self._local_edit)
        menu.addSeparator()
        self._menu_action(menu, "New folder", self._local_mkdir)
        self._menu_action(menu, "New file", self._local_newfile)
        self._menu_action(menu, "Copy\tCtrl+C", self._local_copy)
        self._menu_action(menu, "Paste\tCtrl+V", self._local_paste)
        self._menu_action(menu, "Rename\tF2", self._local_rename)
        menu.addSeparator()
        self._menu_action(menu, "Select all\tCtrl+A", lambda: self._select_all(self.local_table))
        if recycle_bin_available():
            self._menu_action(menu, "Delete\tDel", self._local_delete)
            self._menu_action(menu, "Delete permanently\tShift+Del", self._local_delete_permanently)
        else:
            self._menu_action(menu, "Delete\tDel", self._local_delete)
        menu.exec_(self.local_table.viewport().mapToGlobal(pos))

    def _context_remote(self, pos):
        menu = QMenu(self)
        self._menu_action(menu, "Open\tEnter", self._remote_open)
        self._menu_action(menu, "Open with…", lambda: self._remote_open(choose=True))
        self._menu_action(menu, "Pull to this PC", self.pull_selected)
        self._menu_action(menu, "Edit\tF4", self._remote_edit)
        menu.addSeparator()
        self._menu_action(menu, "New folder", self._remote_mkdir)
        self._menu_action(menu, "New file", self._remote_newfile)
        self._menu_action(menu, "Copy\tCtrl+C", self._remote_copy)
        self._menu_action(menu, "Paste\tCtrl+V", self._remote_paste)
        self._menu_action(menu, "Rename\tF2", self._remote_rename)
        menu.addSeparator()
        self._menu_action(menu, "Select all\tCtrl+A", lambda: self._select_all(self.remote_table))
        self._menu_action(menu, "Delete\tDel", self._remote_delete)
        menu.exec_(self.remote_table.viewport().mapToGlobal(pos))

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
            self.log.emit(f"[INFO] {name} has mixed line endings; saving will use {ending} throughout.")
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
                if not _alive(dlg):
                    return
                offered = refresh == self.refresh_remote and _alive(self) and \
                    self._offer_write_access(posixpath.dirname(path) or "/", msg,
                                             f"saving {name}",
                                             lambda: _alive(dlg) and dlg._save())
                dlg.complete_async_save(False, f"Could not save {name}:\n{msg}",
                                        report=not offered)

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

    def _handle_edit_load(self, name: str, path: str, result, write, refresh, hint: str = ""):
        if result is None:
            return  # cancelled
        if result[0] == "refused":
            QMessageBox.information(self, "Edit File", f"{name} was not opened.\n\n{result[1]}"
                                    + (f"\n\n{hint}" if hint else ""))
            return
        _status, text, newline, mixed = result
        self._open_editor(name, path, text, newline, mixed, write, refresh)

    def _local_edit(self):
        rows = self._selected_rows(self.local_table)
        if rows:
            self._local_edit_row(rows[0])

    def _local_edit_row(self, row: int, hint: str = ""):
        entry = self._row_entry(self.local_table, row)
        if not entry or entry[1]:
            return
        name = entry[0]
        path = os.path.join(self.local_cwd, name)
        self._job(lambda: _edit_load_result(lambda: _read_for_edit(path)),
                  lambda result: self._handle_edit_load(
                      name, path, result, lambda payload: _save_local_text(path, payload),
                      self.refresh_local, hint),
                  lambda msg: QMessageBox.critical(self, "Edit File", f"Could not read {name}:\n{msg}"))

    def _remote_edit(self):
        rows = self._selected_rows(self.remote_table)
        if rows:
            self._remote_edit_row(rows[0])

    def _remote_edit_row(self, row: int, hint: str = ""):
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
                f"{name} was not opened.\n\n{_too_large_message(size)} Pull it to the PC instead."
                + (f"\n\n{hint}" if hint else ""))
            return
        full_path = posixpath.join(self.remote_cwd, name)
        handler = self.handler
        cancel = self._cancel
        # Filled by load(): the file a symlink points to and its permission bits.
        target = {"path": full_path, "mode": ""}
        self.log.emit(f"Fetching {name} for editing…")

        def load():
            try:
                st = handler.stat_path(full_path, safe=False)
            except Exception:
                st = None  # no usable stat: fall back to pulling the listed path
            if st is not None:
                ftype = str(st["type"])
                if not ftype.startswith("regular"):
                    return ("refused", f"It is not a regular file ({ftype}), so it can't be "
                                       "edited as text.")
                if st["size"] > _EDIT_MAX_BYTES:
                    return ("refused", f"{_too_large_message(st['size'])} Pull it to the PC instead.")
                target["path"], target["mode"] = st["real_path"], st["mode"]
            elif kind in ("b", "c", "p", "s"):
                return ("refused", "It is a device node, pipe or socket, so it can't be edited as text.")
            elif kind == "l" and not _links_to_a_file(handler, full_path):
                # pulling a link to a pipe never ends, and holds an adb slot meanwhile
                return ("refused", "It is a link to something other than a regular file, so it "
                                   "can't be edited as text.")
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
            fd, tmp_path = tempfile.mkstemp(prefix="turboadb-edit-")
            os.close(fd)
            try:
                write_text_file(tmp_path, payload)  # newline="" keeps line endings exactly
                # the engine's save, as `turboadb edit` uses: adb push resets the
                # mode (755 -> 666), so the mode the file has right now comes back
                handler.replace_file(tmp_path, target["path"], mode=target["mode"], safe=False)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        self._job(load,
                  lambda result: self._handle_edit_load(name, target["path"], result, write,
                                                        self.refresh_remote, hint),
                  lambda msg: QMessageBox.critical(
                      self, "Edit Remote File", f"Could not pull {name} from device:\n{msg}"),
                  device=True)  # at most _EDIT_MAX_BYTES (2 MB): a short job

    # ---- Open in the app this PC has for the file (see file_open) ----
    _NO_APP_HINT = ("No app on this PC opens this kind of file. To pick one, right-click it "
                    "and choose Open with….")

    def _how_to_open(self, name: str, choose: bool, *, device: bool):
        """``("open", "")`` to open the file *name* in an app, ``("edit", hint)``
        for TurboADB's editor, or ``(None, "")`` when it is refused (the user was
        told why).  Programs never start from here, and scripts, which a double
        click would run, open in the editor; with *choose* the user picks the
        app for anything."""
        if choose:
            return "open", ""
        kind = file_open.kind_of(name)
        if kind == file_open.PROGRAM:
            ext = os.path.splitext(name)[1].lower()
            if ext in file_open.APP_PACKAGE_EXTENSIONS:
                why = "is an Android app package: install it from the Apps tab."
            elif ext in (".lnk", ".url", ".website"):
                why = "is a shortcut, so it is not followed from here."
            elif device:
                why = "is a program, so it is not started from here. Pull it to this PC to use it."
            else:
                why = ("is a program, so it is not started from here. Run it from File "
                       "Explorer if you trust it.")
            QMessageBox.information(self, "Open", f"{name} {why}")
            return None, ""
        if kind == file_open.SCRIPT:
            self.log.emit(f"[INFO] {name} is a script: it opens in TurboADB's editor, so it "
                          "can't run by accident. Use Open with… for another app.")
            return "edit", ""
        runner = file_open.runs_what_it_opens(name)
        if runner == "itself":  # a type that is its own program on this PC
            QMessageBox.information(self, "Open", f"{name} is a program on this PC, so it is "
                                                  "not started from here.")
            return None, ""
        if runner:  # this PC would RUN it with that program, not show it
            self.log.emit(f"[INFO] {name} would be run by {runner}: it opens in TurboADB's "
                          "editor instead. Use Open with… for another app.")
            return "edit", ""
        if file_open.app_for(name) is None:
            return "edit", self._NO_APP_HINT  # no app for it here: the editor, if it is text
        return "open", ""

    def _start_app(self, path: str, name: str, choose: bool) -> bool:
        """Open the PC file *path* in its app, or in the one the user picks."""
        try:
            file_open.start(path, choose=choose)
        except OSError as exc:
            reason = exc.strerror or str(exc)
            self.log.emit(f"[ERROR] Open {name}: {reason}")
            QMessageBox.warning(self, "Open", f"{name} could not be opened:\n{reason}")
            return False
        return True

    def _local_open(self, choose: bool = False):
        rows = self._selected_rows(self.local_table)
        if not rows:
            return
        entry = self._row_entry(self.local_table, rows[0])
        if entry and entry[1]:
            if not choose:
                self._on_local_double_click(rows[0], 0)  # a folder: go into it
            return
        self._local_open_row(rows[0], choose)

    def _local_open_row(self, row: int, choose: bool = False):
        entry = self._row_entry(self.local_table, row)
        if not entry or entry[1]:
            return
        name = entry[0]
        how, hint = self._how_to_open(name, choose, device=False)
        if how == "edit":
            self._local_edit_row(row, hint)
        elif how == "open":
            self._start_app(os.path.join(self.local_cwd, name), name, choose)

    def _remote_open(self, choose: bool = False):
        rows = self._selected_rows(self.remote_table)
        if not rows:
            return
        entry = self._row_entry(self.remote_table, rows[0])
        if entry and entry[1]:
            if not choose:
                self._on_remote_double_click(rows[0], 0)  # a folder: go into it
            return
        self._remote_open_row(rows[0], choose)

    def _remote_open_row(self, row: int, choose: bool = False):
        """Open a device file in its app: it is copied to this PC first, and
        each save of that copy goes back to the device (see _send_copy_back)."""
        table = self.remote_table
        entry = self._row_entry(table, row)
        if not entry or entry[1] or self.handler is None:
            return
        name = entry[0]
        if self._refuse_unsafe(table, [name], "Open"):
            return
        how, hint = self._how_to_open(name, choose, device=True)
        if how == "edit":
            self._remote_edit_row(row, hint)
            return
        if how != "open":
            return
        perms_item = table.item(row, 4)
        kind = perms_item.text()[:1] if perms_item is not None else ""
        full_path = posixpath.join(self.remote_cwd, name)
        handler = self.handler
        self.log.emit(f"Opening {name}…")

        def look():
            try:
                info = handler.stat_path(full_path, safe=False)
            except Exception:
                # no usable stat (old devices): the listed path, if it is a file
                if kind in ("b", "c", "p", "s") or (kind == "l" and not _links_to_a_file(
                        handler, full_path)):
                    return ("refused", "It is not a regular file, so it can't be opened.")
                info = {"real_path": full_path, "mode": "", "type": "regular file"}
            if not str(info["type"]).startswith("regular"):
                return ("refused", f"It is not a regular file ({info['type']}), so it can't "
                                   "be opened.")
            stamp = _device_stamp(handler, info["real_path"])
            return ("ok", info, stamp, file_open.new_copy_folder())

        def looked(result):
            if result[0] == "refused":
                QMessageBox.information(self, "Open", f"{name} was not opened.\n\n{result[1]}")
                return
            _ok, info, stamp, folder = result
            self._pull_to_open(name, info, stamp, folder, choose,
                               listed=posixpath.dirname(full_path))

        self._job(look, looked,
                  lambda msg: QMessageBox.critical(self, "Open", f"Could not open {name}:\n{msg}"),
                  device=True)

    def _pull_to_open(self, name: str, info, stamp, folder: str, choose: bool, listed: str = ""):
        """Copy the device file to *folder* through the transfer queue (a big
        video shows its progress and can be cancelled), then open the copy in
        its app and watch it for saves. *listed* is the device folder it was
        opened from, as the listing shows it (a link like /sdcard or not)."""
        if self._closing:
            shutil.rmtree(folder, ignore_errors=True)
            return
        remote = info["real_path"]
        local = os.path.join(folder, _safe_local_name(name))
        copy = file_open.DeviceCopy(name, local, remote, info.get("mode") or "", stamp,
                                    folder=listed or posixpath.dirname(remote))
        job = (remote, local, "pull")

        def copied():
            copy.synced = file_open.stat_key(local)
            if self._start_app(local, name, choose):
                self._copies.watch(copy)
                self.log.emit(f"[INFO] Opened {name} from the device. Saving it in its app "
                              f"sends it back to {remote}.")

        def not_copied(message):
            shutil.rmtree(folder, ignore_errors=True)
            if message is not None:  # None: cancelled, or the tab closed
                self.log.emit(f"[ERROR] {name} could not be copied from the device to open "
                              f"it: {message}")

        self._after_pull[job] = (copied, not_copied)
        self._enqueue_transfers([job], f"Copying {name} to this PC to open it…")

    def _send_copy_back(self, copy):
        """An app saved the copy of a device file: send it to the device,
        keeping the file's permissions — after asking, when the device file
        changed since it was copied (another app, or a log that grew)."""
        if self._closing or self.handler is None:
            return
        if copy.saving:
            copy.again = True  # sent once more when this one is done
            return
        copy.saving = True
        handler, force, expected = self.handler, copy.force, copy.stamp

        def work():
            key = file_open.stat_key(copy.local)  # what is sent, as the watcher sees it
            if key is None:
                raise OSError(f"{copy.local} can't be read right now")
            if expected is not None and not force:
                now = _device_stamp(handler, copy.remote)
                if now is not None and now != expected:
                    return ("changed", now, key)
            handler.replace_file(copy.local, copy.remote, mode=copy.mode, safe=False)
            return ("saved", _device_stamp(handler, copy.remote), key)

        # Not in self._jobs: a save under way finishes even if the tab closes.
        run_job(self._save_jobs, work, lambda result: self._copy_sent(copy, result),
                lambda msg: self._copy_not_sent(copy, msg))

    def _copy_sent(self, copy, result):
        if not _alive(self) or self._closing:
            copy.saving = False
            return
        status, stamp, key = result
        if status == "changed":
            # Still "saving" while the question is open: a save made meanwhile
            # waits for the answer instead of asking a second time.
            answer = QMessageBox.question(
                self, "Save to device",
                f"{copy.name} changed on the device since you opened it.\n\n"
                f"Replace it on the device with the copy you saved?\n{copy.remote}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            copy.saving = False
            if answer == QMessageBox.Yes:
                copy.force = True
                self._send_copy_back(copy)
            else:
                copy.synced, copy.again = key, False
                self.log.emit(f"[INFO] {copy.name} was not saved to the device: it changed "
                              "there since you opened it.")
            return
        copy.saving = False
        copy.stamp, copy.synced, copy.force = stamp, key, False
        self.log.emit(f"[OK] Saved {copy.name} to the device ({copy.remote})")
        if posixpath.normpath(copy.folder) == posixpath.normpath(self.remote_cwd or "/"):
            self.refresh_remote()
        if copy.again:  # saved again while this was sent: look at it again
            copy.again = False
            self._copies.recheck(copy)

    def _copy_not_sent(self, copy, msg):
        copy.saving = copy.again = copy.force = False
        if not _alive(self) or self._closing:
            return
        self.log.emit(f"[ERROR] Saving {copy.name} to the device: {msg}")
        folder = posixpath.dirname(copy.remote) or "/"
        if not self._offer_write_access(folder, msg, f"saving {copy.name}",
                                        lambda c=copy: self._send_copy_back(c)):
            QMessageBox.warning(self, "Save to device",
                                f"{copy.name} could not be saved to the device:\n{msg}\n\n"
                                "Your changes are still in the copy on this PC: save it "
                                "again to try again.")

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
        self._pending_ids.clear()
        self.transfer_panel.close_panel()
        self._remote_refresh_pending = False
        transfer, self._transfer = self._transfer, None
        if transfer is not None:
            try:
                transfer.stop()
            except RuntimeError:
                pass
            disconnect_signals(transfer, ("progress", "done", "failed", "result"))
            park_thread(transfer)
        close_jobs(self._jobs)
        self._ls = None
        self._release_editors()
        # Device files open in apps: their saves no longer go back from a
        # closed tab (said in the log), and copies still being made are dropped.
        still_open = [copy.name for copy in self._copies.copies()]
        if still_open:
            names = ", ".join(still_open[:5]) + ("…" if len(still_open) > 5 else "")
            self.log.emit(f"[WARNING] Saving {names} in its app no longer sends it to the "
                          "device: the Files tab it was opened from was closed.")
        self._copies.stop()
        hooks, self._after_pull = list(self._after_pull.values()), {}
        for _done, dropped in hooks:
            dropped(None)
        # Listings of big folders are thousands of table items: free them now
        # rather than whenever the closed tab is finally deleted.
        self._clipboard = []
        for table in (self.local_table, self.remote_table):
            table.setRowCount(0)
