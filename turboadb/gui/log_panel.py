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
from PyQt5.QtWidgets import (QGroupBox, QVBoxLayout, QHBoxLayout, QPushButton,
                             QPlainTextEdit, QFileDialog, QComboBox, QLabel)

from . import theme

DOCS_URL = "https://pypi.org/project/turboadb/"

#   name -> (rank, 4-char badge, badge colour, message colour)
_LEVELS = {
    "DEBUG":   (0, "dbg ", "#7e8896", "#8b95a3"),
    "INFO":    (1, "info", "#7fb2e8", "#cfe3f7"),
    "OK":      (1, "ok  ", "#5be39a", "#bdebcf"),
    "WARNING": (2, "warn", "#ffc34d", "#ffe1a3"),
    "ERROR":   (3, "err ", "#ff7a6e", "#ffb3aa"),
}
_ALIASES = {"SUCCESS": "OK", "WARN": "WARNING", "STDERR": "WARNING",
            "CRITICAL": "ERROR", "FATAL": "ERROR"}
_FILTERS = [("Normal", 1), ("Verbose (adb commands)", 0),
            ("Warnings + Errors", 2), ("Errors only", 3)]
_PREFIX_RE = re.compile(
    r"^\s*\[(DEBUG|INFO|OK|SUCCESS|WARNING|WARN|STDERR|CRITICAL|FATAL|ERROR)\]\s*",
    re.IGNORECASE)


class LogPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Log", parent)
        self._entries = deque(maxlen=20000)    # (ts, level, msg) — the full record
        self._min_rank = 1                     # "Normal": hide DEBUG by default

        lay = QVBoxLayout(self)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("Consolas", 9))
        self.view.setMaximumBlockCount(80000)
        # the log dock is DARK in both themes so its colour-coding stays readable
        self.view.setStyleSheet("QPlainTextEdit{background:#0b0b0d;color:#cfe3f7;"
                                "border:1px solid #2a2a2a;}")

        row = QHBoxLayout()
        lbl = QLabel("Show:"); lbl.setStyleSheet("color:#9aa4af;")
        row.addWidget(lbl)
        self.level_box = QComboBox()
        self.level_box.addItems([f[0] for f in _FILTERS])
        self.level_box.setMaximumWidth(190)
        self.level_box.setToolTip("Filter the log by level. 'Verbose' also shows "
                                  "the raw adb commands.")
        self.level_box.currentIndexChanged.connect(self._on_filter)
        row.addWidget(self.level_box)
        row.addStretch(1)
        clear = QPushButton("Clear"); clear.setProperty("role", "ghost")
        clear.setIcon(theme.emoji_icon("🧹")); clear.clicked.connect(self._clear)
        save = QPushButton("Save log…"); save.setProperty("role", "ghost")
        save.setIcon(theme.emoji_icon("💾")); save.clicked.connect(self._save)
        docs = QPushButton("Help / Docs"); docs.setProperty("role", "ghost")
        docs.setIcon(theme.emoji_icon("❓"))
        docs.clicked.connect(lambda: webbrowser.open(DOCS_URL))
        row.addWidget(clear); row.addWidget(save); row.addWidget(docs)

        lay.addWidget(self.view, 1)
        lay.addLayout(row)

    # ---- public API ----
    def append(self, text: str):
        if text is None:
            return
        for raw in str(text).split("\n"):
            if not raw.strip():
                continue
            level, msg = self._classify(raw)
            entry = (time.strftime("%H:%M:%S"), level, msg)
            self._entries.append(entry)
            if _LEVELS[level][0] >= self._min_rank:
                self._render(entry)
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---- classification ----
    @staticmethod
    def _classify(text):
        m = _PREFIX_RE.match(text)
        if m:
            lvl = m.group(1).upper()
            lvl = _ALIASES.get(lvl, lvl)
            return (lvl if lvl in _LEVELS else "INFO"), text[m.end():]
        # unprefixed: the raw command trace is DEBUG; everything else is INFO
        s = text.lstrip()
        if s.startswith("$ ") or s.startswith("-> ") or s.startswith("  -> "):
            return "DEBUG", text
        return "INFO", text

    # ---- rendering ----
    def _render(self, entry):
        ts, level, msg = entry
        _rank, badge, badge_col, msg_col = _LEVELS.get(level, _LEVELS["INFO"])
        cur = self.view.textCursor(); cur.movePosition(QTextCursor.End)
        tsfmt = QTextCharFormat(); tsfmt.setForeground(QColor("#6b7580"))
        bfmt = QTextCharFormat(); bfmt.setForeground(QColor(badge_col))
        bfmt.setFontWeight(QFont.Bold)
        mfmt = QTextCharFormat(); mfmt.setForeground(QColor(msg_col))
        cur.insertText(f"{ts} ", tsfmt)
        cur.insertText(f"{badge} ", bfmt)
        cur.insertText(msg + "\n", mfmt)

    def _rerender(self):
        self.view.clear()
        for entry in self._entries:
            if _LEVELS[entry[1]][0] >= self._min_rank:
                self._render(entry)
        sb = self.view.verticalScrollBar(); sb.setValue(sb.maximum())

    def _on_filter(self, idx):
        self._min_rank = _FILTERS[idx][1]
        self._rerender()

    # ---- clear / save ----
    def _clear(self):
        self._entries.clear()
        self.view.clear()

    def _save(self):
        default = "turboadb-log-" + time.strftime("%Y%m%d-%H%M%S") + ".log"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save log", default,
            "Log files (*.log);;Text files (*.txt);;All files (*)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            for ts, level, msg in self._entries:
                if _LEVELS[level][0] >= self._min_rank:
                    fh.write(f"{ts} {_LEVELS[level][1].strip().upper():4} {msg}\n")
        self.append(f"[OK] Log saved to {path}")
        from .fileutil import saved_dialog
        saved_dialog(self, path, "log")
