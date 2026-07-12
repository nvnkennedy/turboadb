"""Live logcat viewer: level + tag + regex filter, pause, clear, save."""

from __future__ import annotations

import re
import time
import threading

from PyQt5.QtCore import QThread, pyqtSignal, QTimer, Qt
from PyQt5.QtGui import QFont, QTextCursor, QTextCharFormat, QColor
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLineEdit, QComboBox, QCheckBox, QLabel,
                             QPlainTextEdit, QFileDialog, QMenu)

from . import theme, settings as settings_mod
from .scrollback import Scrollback


class _LogcatThread(QThread):
    # Emit BATCHES of lines, not one signal per line: over a slow/RDP link logcat
    # floods thousands of lines/sec, and a cross-thread signal + scroll per line
    # drowns the GUI thread ("not responding"). Batching keeps it smooth.
    batch = pyqtSignal(list)
    stopped = pyqtSignal()

    # Over RDP every repaint is a slow remote screen update, so flush less often
    # (≈5x/sec) in bounded chunks — keeps the GUI thread free for other tabs.
    _FLUSH_S = 0.20
    _MAX_BATCH = 1000

    def __init__(self, handler, args):
        super().__init__()
        self.handler = handler
        self.args = args
        self.proc = None
        self._stopping = False

    def run(self):
        try:
            self.proc = self.handler.popen(self.args)
        except Exception as exc:
            self.batch.emit([f"[ERROR] logcat: {exc}"])
            self.stopped.emit()
            return
        buf = b""
        pending = []
        last = time.monotonic()
        try:
            while True:
                chunk = self.proc.stdout.read(65536)
                if not chunk:
                    break                       # EOF or pipe closed by stop()
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()
                for ln in parts:
                    pending.append(ln.decode("utf-8", "replace").rstrip("\r"))
                now = time.monotonic()
                if pending and (len(pending) >= self._MAX_BATCH
                                or (now - last) >= self._FLUSH_S):
                    self.batch.emit(pending)
                    pending = []
                    last = now
        except Exception:
            pass                                # closed pipe on stop -> just end
        if pending:
            self.batch.emit(pending)
        self.stopped.emit()

    def stop(self):
        # kill the process AND close the pipe so a blocking read returns at once
        self._stopping = True
        p = self.proc
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.stdout.close()
            except Exception:
                pass


class _ZoomEdit(QPlainTextEdit):
    """A read-only log view whose font zooms with Ctrl+wheel / Ctrl+± — the
    persisted size is shared with the terminal (same ``term_font_size``)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self._bump(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event):
        m, k = event.modifiers(), event.key()
        if m & Qt.ControlModifier and k in (Qt.Key_Plus, Qt.Key_Equal):
            self._bump(1); return
        if m & Qt.ControlModifier and k == Qt.Key_Minus:
            self._bump(-1); return
        super().keyPressEvent(event)

    def _bump(self, step):
        f = self.font()
        size = max(6, min(40, (f.pointSize() or 10) + step))
        if size == f.pointSize():
            return
        f.setPointSize(size); self.setFont(f)
        try:
            data = settings_mod.load()
            data["term_font_size"] = size
            settings_mod.save(data)
        except Exception:
            pass


class LogcatPanel(QWidget):
    log = pyqtSignal(str)

    _LEVEL_COLOR = {"E": "#ff7a6e", "F": "#ff5e8a", "W": "#ffc34d",
                    "I": "#7ee2a4", "D": "#9fb4c9", "V": "#8b95a3"}

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.thread = None
        self._paused = False
        self._use_crash_buffers = False
        self._filter_re = None
        self._hl_re = None            # keyword/regex to highlight (None = off)
        self._hl_fmt = None           # the highlight char-format (lazy)
        self._fmt_cache = {}          # color -> QTextCharFormat (reused per batch)
        self._pending = []            # lines waiting to be drawn (coalesced)
        # A GUI-side timer paints at a FIXED low rate, decoupled from how fast
        # logcat arrives. Over RDP each repaint is a slow remote screen update,
        # so this is what stops the window going "not responding" under a flood.
        self._render_timer = QTimer(self)
        self._render_timer.timeout.connect(self._render_pending)
        self._render_timer.start(350)
        # debounce the on-screen re-filter so holding a key doesn't re-render the
        # (large) buffer on every character
        self._refilter_timer = QTimer(self)
        self._refilter_timer.setSingleShot(True)
        self._refilter_timer.timeout.connect(self._refilter_view)

        lay = QVBoxLayout(self)
        ctrl = QHBoxLayout()
        self.level = QComboBox()
        self.level.addItems(["Verbose (all)", "Debug", "Info", "Warn",
                            "Error", "Fatal"])
        # a manual level change clears the crash preset (so it isn't stuck on
        # the crash buffers after the user moves on); guarded during the preset
        self._applying_preset = False
        self.level.currentIndexChanged.connect(self._on_level_changed)
        # How much of the device's ALREADY-BUFFERED (cached) log to show before
        # following live. Plain `adb logcat` dumps its whole in-memory buffer
        # first — often hundreds of thousands of OLD lines in seconds — which
        # looked like a runaway stream. Default = live only (-T 1).
        self.hist = QComboBox()
        self.hist.addItem("Live only", 1)
        self.hist.addItem("Last 1,000 lines", 1000)
        self.hist.addItem("Last 10,000 lines", 10000)
        self.hist.addItem("Full buffer (all cached)", None)
        self.hist.setToolTip(
            "How much of the device's already-buffered log to include before "
            "streaming live.\n\n'Live only' starts from NOW — no cached "
            "backlog. Without this, adb dumps the device's entire in-memory "
            "log cache first (easily 100,000+ old lines), which floods the "
            "view the moment you press Start.")
        self.tag = QLineEdit(); self.tag.setPlaceholderText("tag (optional)")
        self.tag.setMaximumWidth(160)
        self.filt = QLineEdit(); self.filt.setPlaceholderText("regex filter (live)…")
        self.filt.textChanged.connect(self._set_filter)
        # a crash preset: switch to the crash buffer + Error level in one click
        self.btn_crash = QPushButton(" Crashes")
        self.btn_crash.setProperty("role", "ghost")
        self.btn_crash.setToolTip("Show only crashes & ANRs: the 'crash' buffer "
                                  "at Error level, highlighting FATAL/ANR. Click "
                                  "Start after.")
        self.btn_crash.clicked.connect(self._crash_preset)
        self.hl = QLineEdit()
        self.hl.setPlaceholderText("highlight (e.g. error|anr|crash)…")
        self.hl.setToolTip("Highlight matches in-line (case-insensitive regex) "
                           "without hiding the rest — great for spotting "
                           "errors/ANRs/your tag in a flood. The Level colours "
                           "still apply; matches get a bright marker.")
        self.hl.setMaximumWidth(220)
        self.hl.textChanged.connect(self._set_highlight)
        self.clear_first = QCheckBox("clear first")
        self.btn_start = QPushButton(" Start"); self.btn_start.setProperty("role", "ok")
        self.btn_start.setIcon(theme.emoji_icon("▶"))
        self.btn_start.clicked.connect(self.toggle)
        self.btn_pause = QPushButton(" Pause"); self.btn_pause.setProperty("role", "ghost")
        self.btn_pause.setIcon(theme.emoji_icon("⏸"))
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_clear = QPushButton(" Clear"); self.btn_clear.setProperty("role", "ghost")
        self.btn_clear.setIcon(theme.emoji_icon("🧹"))
        self.btn_clear.clicked.connect(self._clear_view)
        self.btn_save = QPushButton(" Save…"); self.btn_save.setProperty("role", "ghost")
        self.btn_save.setIcon(theme.emoji_icon("💾"))
        self.btn_save.clicked.connect(self._save)
        for w in (QLabel("Level:"), self.level, QLabel("History:"), self.hist,
                  self.tag, self.filt, self.hl, self.btn_crash,
                  self.clear_first, self.btn_start, self.btn_pause,
                  self.btn_clear, self.btn_save):
            ctrl.addWidget(w)
        ctrl.setStretch(5, 1)                  # the regex-filter box stretches
        lay.addLayout(ctrl)

        self.view = _ZoomEdit()
        fam = settings_mod.get("term_font") or "Consolas"
        self.view.setFont(QFont(fam, int(settings_mod.get("term_font_size") or 10)))
        # generous on-screen scrollback; trimmed lines are archived so a long
        # capture is never lost and Save writes the COMPLETE log, not the tail
        self._sb = Scrollback(self.view, display_cap=120000)
        # word-wrap off = far cheaper layout when lines pour in
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setStyleSheet(f"background:{theme.TERM_BG}; border:none;")
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        lay.addWidget(self.view, 1)

    def _menu(self, pos):
        ico = theme.emoji_icon
        m = QMenu(self.view)
        a = m.addAction(ico("📋"), "Copy")
        a.setEnabled(self.view.textCursor().hasSelection())
        a.triggered.connect(self.view.copy)
        m.addAction(ico("🗂"), "Copy all",
                    lambda: (self.view.selectAll(), self.view.copy()))
        m.addAction(ico("🔲"), "Select All", self.view.selectAll)
        m.addSeparator()
        m.addAction(ico("💾"), "Save full logcat to file…", self._save)
        m.addAction(ico("🧹"), "Clear", self._clear_view)
        m.exec_(self.view.viewport().mapToGlobal(pos))

    _CRASH_BUFFERS = ["crash", "main", "system"]

    def _on_level_changed(self, *_):
        if not self._applying_preset:
            self._use_crash_buffers = False

    def _crash_preset(self):
        """One-click crash/ANR view: crash buffer, Error level, FATAL/ANR
        highlighted. Applied to the controls; the user presses Start."""
        self._applying_preset = True
        self.level.setCurrentText("Error")
        self._applying_preset = False
        self.tag.clear()
        self.hl.setText("FATAL|ANR|Exception|crash")
        self._use_crash_buffers = True
        self.log.emit("[INFO] crash preset set — press Start to stream crashes "
                      "& ANRs (crash buffer, Error level)")

    # --- filter ---
    def _set_filter(self, text):
        try:
            self._filter_re = re.compile(text) if text else None
        except re.error:
            self._filter_re = None
        self._refilter_timer.start(300)          # debounced re-filter

    def _refilter_view(self):
        """Re-apply the live regex filter to what's ALREADY on screen, so
        tightening the filter also hides stale non-matching lines (it used to
        only affect newly-arriving lines).

        Reads the ON-SCREEN text (bounded by the display cap), never the full
        disk archive — so this stays cheap even during a huge capture. (Loosening
        the filter therefore doesn't resurrect already-scrolled-off lines; the
        complete log is always in Save.)"""
        fre = self._filter_re
        try:
            text = self.view.toPlainText()
        except Exception:
            return
        if not text:
            return
        lines = text.splitlines()
        if fre is not None:
            lines = [ln for ln in lines if fre.search(ln)]
        self.view.clear()
        self._draw_lines(lines)

    def _set_highlight(self, text):
        """Keywords/regex to mark in-line (case-insensitive). Unlike the filter,
        this never hides lines — it just makes matches pop out."""
        try:
            self._hl_re = re.compile(text, re.IGNORECASE) if text else None
        except re.error:
            self._hl_re = None       # keep typing a partial regex without errors
        self.hl.setStyleSheet("" if self._hl_re or not text
                              else "QLineEdit{color:%s;}" % theme.DANGER)

    # --- start/stop ---
    def toggle(self):
        if self.thread and self.thread.isRunning():
            self.stop()
        else:
            self.start()

    def start(self):
        if self.thread and self.thread.isRunning():
            return
        _letter = {"Verbose": "V", "Debug": "D", "Info": "I", "Warn": "W",
                   "Error": "E", "Fatal": "F"}
        lvl = _letter.get(self.level.currentText().split()[0], "V")
        tag = self.tag.text().strip()
        fmt = settings_mod.get("logcat_format") or "threadtime"
        if self.clear_first.isChecked():
            try:
                self.handler.logcat_clear()
            except Exception:
                pass
        args = ["logcat", "-v", fmt]
        if self._use_crash_buffers:
            for b in self._CRASH_BUFFERS:
                args += ["-b", b]
        tailn = self.hist.currentData()
        if tailn:                              # None = full cached buffer
            args += ["-T", str(tailn)]
        if tag and lvl != "V":
            args += [f"{tag}:{lvl}", "*:S"]
        elif tag:
            args += [f"{tag}:V", "*:S"]
        elif lvl != "V":
            args += [f"*:{lvl}"]
        self.thread = _LogcatThread(self.handler, args)
        self.thread.batch.connect(self._on_batch)
        self.thread.stopped.connect(self._on_stopped)
        self.thread.start()
        self.btn_start.setText("Stop"); self.btn_start.setProperty("role", "danger")
        self._restyle(self.btn_start)
        self.log.emit("[OK] logcat started")

    def stop(self):
        if self.thread:
            self.thread.stop()

    def _on_stopped(self):
        self.btn_start.setText("Start"); self.btn_start.setProperty("role", "ok")
        self._restyle(self.btn_start)
        self.log.emit("[OK] logcat stopped")

    def _toggle_pause(self):
        self._paused = not self._paused
        self.btn_pause.setText("Resume" if self._paused else "Pause")

    def _clear_view(self):
        self.view.clear()
        self._sb.reset()                 # clear forgets the archived history too

    def _on_batch(self, lines):
        """Buffer lines only — the render timer paints them. Keeps the worker and
        the (slow over RDP) GUI painting fully decoupled."""
        if not lines:
            return
        # COMPLETE capture at the source: archive every line BEFORE any on-screen
        # dropping, so the saved log is whole even when the view skips to keep up
        self._sb.archive("\n".join(lines) + "\n")
        # while paused, keep buffering (bounded below) — clearing here meant
        # everything logged during a pause vanished from the view on Resume
        self._pending.extend(lines)
        # hard cap the ON-SCREEN backlog so a sustained flood can never make the
        # GUI fall behind (the archive above already has every line)
        if len(self._pending) > 6000:
            drop = len(self._pending) - 6000
            self._pending = ([f"… ({drop} lines skipped on screen — saved log has all)"]
                             + self._pending[-6000:])

    def _render_pending(self):
        """Paint whatever has accumulated since the last tick — one insert, one
        scroll. Runs at a fixed rate regardless of logcat volume."""
        if self._paused or not self._pending:
            return
        lines, self._pending = self._pending, []
        fre = self._filter_re
        if fre is not None:
            lines = [ln for ln in lines if fre.search(ln)]
            if not lines:
                return
        self._draw_lines(lines)

    def _draw_lines(self, lines):
        """Append *lines* to the view with level colouring + highlight marking.
        Shared by live rendering and the re-filter re-render."""
        if not lines:
            return
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.End)
        fmt_cache = self._fmt_cache
        hre = self._hl_re
        hl_fmt = self._highlight_fmt() if hre is not None else None
        for line in lines:
            color = "#cfd8e3"
            head = line[:40]
            for lvl, c in self._LEVEL_COLOR.items():
                if f" {lvl} " in head or f"/{lvl}(" in head:
                    color = c
                    break
            fmt = fmt_cache.get(color)
            if fmt is None:
                fmt = QTextCharFormat(); fmt.setForeground(QColor(color))
                fmt_cache[color] = fmt
            # Fast path (the common case): no highlight, or this line has no match
            # — a single insertText keeps the flood cheap. Only split a line into
            # segments when it actually contains a match.
            if hre is not None and hre.search(line):
                self._insert_highlighted(cur, line, fmt, hre, hl_fmt)
            else:
                cur.insertText(line + "\n", fmt)
        if at_bottom:
            sb.setValue(sb.maximum())

    def _highlight_fmt(self):
        """The in-line marker format (bright amber, bold) — built once."""
        if self._hl_fmt is None:
            f = QTextCharFormat()
            f.setBackground(QColor("#ffd54a"))
            f.setForeground(QColor("#1a1a1a"))
            f.setFontWeight(QFont.Bold)
            self._hl_fmt = f
        return self._hl_fmt

    @staticmethod
    def _insert_highlighted(cur, line, base_fmt, hre, hl_fmt):
        """Insert *line* with matched spans in *hl_fmt* and the rest in *base_fmt*."""
        pos = 0
        for m in hre.finditer(line):
            s, e = m.start(), m.end()
            if e == s:                       # skip zero-width matches
                continue
            if s > pos:
                cur.insertText(line[pos:s], base_fmt)
            cur.insertText(line[s:e], hl_fmt)
            pos = e
        cur.insertText(line[pos:] + "\n", base_fmt)

    def _save(self):
        from .fileutil import download_path
        default = download_path("turboadb-logcat-"
                                + time.strftime("%Y%m%d-%H%M%S") + ".log")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save logcat", default,
            "Log files (*.log);;Text files (*.txt);;All files (*)")
        if path:
            self._sb.save_to(path)           # COMPLETE log, not just the tail
            self.log.emit(f"[OK] logcat saved to {path}")
            from .fileutil import saved_dialog
            saved_dialog(self, path, "logcat")

    @staticmethod
    def _restyle(w):
        w.style().unpolish(w); w.style().polish(w)

    def close_panel(self):
        self.stop()
        if self.thread:
            self.thread.wait(700)
        self._sb.close()                     # drop the temp scrollback file
