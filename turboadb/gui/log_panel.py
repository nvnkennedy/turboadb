"""Clean, level-categorized log dock.

Every line is classified as INFO / OK (success) / WARN / ERROR — or DEBUG for the
raw ``adb`` command trace, which is **hidden by default** so the log isn't noisy.
A "Show" selector reveals more (Verbose shows the adb commands; or narrow to just
Warnings/Errors). Save writes exactly what's shown, with a timestamped name."""

from __future__ import annotations

import re
import time
import webbrowser
from collections import deque

from PyQt5.QtGui import QFont, QTextCursor, QTextCharFormat, QColor
from PyQt5.QtWidgets import (
    QGroupBox,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QPlainTextEdit,
    QComboBox,
    QLabel,
    QCheckBox,
)

from . import theme
from . import settings as settings_mod

DOCS_URL = "https://pypi.org/project/turboadb/"

#   name -> (rank, 4-char badge, badge colour, message colour)
_LEVELS = {
    level: (rank, badge, *theme.LOG_LEVEL_STYLE[level])
    for level, rank, badge in (
        ("DEBUG", 0, "dbg "),
        ("INFO", 1, "info"),
        ("OK", 1, "ok  "),
        ("WARNING", 2, "warn"),
        ("ERROR", 3, "err "),
    )
}
_ALIASES = {
    "SUCCESS": "OK",
    "WARN": "WARNING",
    "STDERR": "WARNING",
    "CRITICAL": "ERROR",
    "FATAL": "ERROR",
    # "[CANCELLED] pull a.txt" (Files) is information, and says so in words.
    "CANCELLED": "INFO",
    "CANCELED": "INFO",
}
_FILTERS = [
    ("Normal", 1),
    ("Verbose (adb commands)", 0),
    ("Warnings + Errors", 2),
    ("Errors only", 3),
]
_PREFIX_RE = re.compile(
    r"^\s*\[(DEBUG|INFO|OK|SUCCESS|WARNING|WARN|STDERR|CRITICAL|FATAL|ERROR|CANCELLED|CANCELED)\]\s*",
    re.IGNORECASE,
)


def classify(text: str):
    """``(level, message, tag)`` for one log message.

    *tag* is its ``[TAG]`` prefix, upper-cased, *level* the level that tag
    names and *message* the text after it. A message without a tag (tag
    None) is INFO, except the raw ``$ adb …`` / ``-> …`` command trace,
    which is DEBUG. A cancelled step keeps the word: "[CANCELLED] pull a.txt"
    is the INFO message "Cancelled: pull a.txt".

    The one reading of these tags, for the log panel and for the main
    window's status bar and toasts; two copies of the table had already
    drifted apart (the panel showed "[CANCELLED]" as part of the text).
    """
    m = _PREFIX_RE.match(text)
    if m:
        tag = m.group(1).upper()
        level = _ALIASES.get(tag, tag)
        message = text[m.end():]
        if tag in ("CANCELLED", "CANCELED") and message.strip():
            message = f"Cancelled: {message}"
        return (level if level in _LEVELS else "INFO"), message, tag
    if text.lstrip().startswith(("$ ", "-> ")):
        return "DEBUG", text, None
    return "INFO", text, None


class LogPanel(QGroupBox):
    def __init__(self, parent=None):
        # The dock already titles this panel "Log"; a second group-box title
        # repeated it.
        super().__init__("", parent)
        self.setObjectName("logPanel")
        self._entries = deque(maxlen=20000)  # (ts, level, msg) — the full record
        self._min_rank = 1  # "Normal": hide DEBUG by default

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setSpacing(6)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("Consolas", 9))
        self.view.setMaximumBlockCount(80000)
        # the log dock is DARK in both themes so its colour-coding stays readable
        self.view.setObjectName("logBox")  # terminal colours (theme.py)

        row = QHBoxLayout()
        lbl = QLabel("Show:")
        row.addWidget(lbl)
        self.level_box = QComboBox()
        self.level_box.addItems([f[0] for f in _FILTERS])
        self.level_box.setMaximumWidth(190)
        self.level_box.setToolTip(
            "Filter the log by level. 'Verbose' also shows the raw adb commands."
        )
        self.level_box.currentIndexChanged.connect(self._on_filter)
        row.addWidget(self.level_box)
        self.chk_silent = QCheckBox("Silent (suppress popups while log open)")
        self.chk_silent.setToolTip(
            "When checked, critical error popups will not interrupt you when the log dock is open."
        )
        self.chk_silent.setChecked(bool(settings_mod.get("mute_popups_with_log", True)))
        self.chk_silent.toggled.connect(self._on_silent_toggled)
        row.addWidget(self.chk_silent)
        row.addStretch(1)
        from .icons import icon

        clear = QPushButton("Clear")
        clear.setProperty("role", "ghost")
        clear.setIcon(icon("eraser", "amber"))
        clear.clicked.connect(self._clear)
        save = QPushButton("Save log…")
        save.setProperty("role", "ghost")
        save.setIcon(icon("save", "teal"))
        save.clicked.connect(self._save)
        docs = QPushButton("Help")
        docs.setProperty("role", "ghost")
        docs.setIcon(icon("help", "blue"))
        docs.clicked.connect(lambda: webbrowser.open(DOCS_URL))
        row.addWidget(clear)
        row.addWidget(save)
        row.addWidget(docs)

        lay.addWidget(self.view, 1)
        lay.addLayout(row)

    def _on_silent_toggled(self, checked: bool):
        try:
            settings_mod.set("mute_popups_with_log", bool(checked))
        except Exception as exc:
            # The box still works until TurboADB closes; only saving it failed.
            self.append(f"[WARNING] Could not save the Silent choice: {exc}")

    # ---- public API ----
    def append(self, text: str):
        if text is None:
            return
        sb = self.view.verticalScrollBar()
        # Follow new entries only when already at the bottom; someone reading
        # older lines must not be yanked away by every new message.
        at_bottom = sb.value() >= sb.maximum() - 4
        # Only a message's first line carries its [TAG]: the lines after it
        # (a traceback, a command's stderr) keep that level, or "Errors only"
        # and a saved log showed "Unexpected error:" without the error. Lines
        # of a message without a tag are read one by one, so an untagged adb
        # command trace stays DEBUG.
        header = None
        for raw in str(text).split("\n"):
            if not raw.strip():
                continue
            level, msg, tag = classify(raw)
            if tag is not None:
                header = level
            elif header is not None:
                level, msg = header, raw
            entry = (time.strftime("%H:%M:%S"), level, msg)
            self._entries.append(entry)
            if _LEVELS[level][0] >= self._min_rank:
                self._render(entry)
        if at_bottom:
            sb.setValue(sb.maximum())

    # ---- rendering ----
    def _render(self, entry):
        ts, level, msg = entry
        _rank, badge, badge_col, msg_col = _LEVELS.get(level, _LEVELS["INFO"])
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.End)
        tsfmt = QTextCharFormat()
        tsfmt.setForeground(QColor(theme.LOG_TIMESTAMP))
        bfmt = QTextCharFormat()
        bfmt.setForeground(QColor(badge_col))
        bfmt.setFontWeight(QFont.Bold)
        mfmt = QTextCharFormat()
        mfmt.setForeground(QColor(msg_col))
        cur.insertText(f"{ts} ", tsfmt)
        cur.insertText(f"{badge} ", bfmt)
        cur.insertText(msg + "\n", mfmt)

    def _rerender(self):
        self.view.clear()
        for entry in self._entries:
            if _LEVELS[entry[1]][0] >= self._min_rank:
                self._render(entry)
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_filter(self, idx):
        self._min_rank = _FILTERS[idx][1]
        self._rerender()

    # ---- clear / save ----
    def _clear(self):
        self._entries.clear()
        self.view.clear()

    def _save(self):
        """Save exactly what is shown, off the UI thread, reporting real errors."""
        from .fileutil import save_output, write_text_file

        text = "".join(
            f"{ts} {_LEVELS[level][1].strip().upper():4} {msg}\n"
            for ts, level, msg in self._entries
            if _LEVELS[level][0] >= self._min_rank
        )
        save_output(
            self,
            "Save log",
            "turboadb-log-" + time.strftime("%Y%m%d-%H%M%S") + ".log",
            lambda path: write_text_file(path, text, newline=None),
            what="log",
            on_saved=lambda path: self.append(f"[OK] Log saved to {path}"),
        )
