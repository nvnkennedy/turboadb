"""Live logcat viewer: level + tag + regex filter, pause, clear, save."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections import deque

from PyQt5.QtCore import QThread, pyqtSignal, QTimer, Qt
from PyQt5.QtGui import QFont, QTextCursor, QTextCharFormat, QColor
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLineEdit,
    QComboBox,
    QCheckBox,
    QLabel,
    QPlainTextEdit,
    QMenu,
)

from . import icons, theme, settings as settings_mod
from .icons import icon
from .qtutil import disconnect_signals, park_thread, thread_running
from .scrollback import Scrollback

_log = logging.getLogger(__name__)

# Level picker entries: (label, icon, tone). The tone matches the level's
# colour family in the log view, so the picker reads like a legend.
_LEVELS = (
    ("Verbose (all)", "list", "dim"),
    ("Debug", "bug", "blue"),
    ("Info", "info", "green"),
    ("Warn", "alert", "amber"),
    ("Error", "x", "red"),
    ("Fatal", "zap", "purple"),
)

_HINT_START = "Press Start to stream the device log"
_HINT_DUMP = "Press Dump to print the log buffer once"
_HINT_WAIT = "Waiting for log output…"


def terminal_icon_pixmap(name: str, size: int, tone: str, ratio: float = 0.0):
    """An icon pixmap for the terminal-coloured surfaces (log view, video well).

    Those surfaces keep the same dark colours in every theme, so the icon uses
    the dark-theme hue of *tone* and never needs a refresh on theme switches.
    *ratio* is the device pixel ratio (default: the application's).
    """
    from PyQt5.QtGui import QGuiApplication, QPixmap

    if ratio <= 0:
        app = QGuiApplication.instance()
        ratio = app.devicePixelRatio() if app is not None else 1.0
    pm = QPixmap(int(round(size * ratio)), int(round(size * ratio)))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    try:
        from PyQt5.QtCore import QByteArray, QRectF
        from PyQt5.QtGui import QPainter
        from PyQt5.QtSvg import QSvgRenderer
    except ImportError:  # QtSvg missing: an empty pixmap keeps the layout intact
        return pm
    renderer = QSvgRenderer(QByteArray(icons.svg(name, theme.hue(tone, "dark")).encode("utf-8")))
    if renderer.isValid():
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
    return pm


class _EmptyLogHint(QWidget):
    """Centred icon + hint over the empty log view (mouse passes through)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        self.glyph = QLabel()
        self.glyph.setAlignment(Qt.AlignCenter)
        self.glyph.setPixmap(terminal_icon_pixmap("logcat", 40, "amber"))
        self.label = QLabel(_HINT_START)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setObjectName("logcatEmptyHint")  # colour and size: theme.py
        v.addWidget(self.glyph)
        v.addWidget(self.label)

    def text(self) -> str:
        return self.label.text()

    def set_text(self, text: str) -> None:
        self.label.setText(text)


class _LogcatThread(QThread):
    """Read ``adb logcat`` and collect decoded lines for the panel to pull.

    No per-batch cross-thread signal: over a slow/RDP link logcat floods
    thousands of lines/sec, and the panel's fixed-rate render timer drains
    :meth:`take_lines` instead.  That also means a short burst followed by
    silence is shown promptly — a time-based batch only flushed when the
    *next* chunk arrived, so the last lines of a burst could wait forever.
    """

    _READ_SIZE = 65536

    def __init__(self, handler, args, clear_first: bool = False):
        super().__init__()
        self.handler = handler
        self.args = args
        self.clear_first = clear_first
        self.proc = None
        self._stopping = False
        self._lock = threading.Lock()
        self._lines = []

    def take_lines(self) -> list:
        """All lines read since the last call (thread-safe)."""
        with self._lock:
            lines, self._lines = self._lines, []
        return lines

    def _publish(self, lines) -> None:
        with self._lock:
            self._lines.extend(lines)

    def run(self):
        if self.clear_first and not self._stopping:
            try:
                self.handler.logcat_clear()
            except Exception as exc:  # capture still starts; just say why history remains
                _log.warning("logcat clear failed: %s", exc)
        if self._stopping:
            return
        try:
            proc = self.handler.popen(self.args)
        except Exception as exc:
            self._publish([f"[ERROR] logcat: {exc}"])
            return
        with self._lock:
            # stop() may have run while popen() was starting the process: it
            # saw no proc then, so honour it here instead of leaking adb logcat.
            self.proc = proc
            stopping = self._stopping
        if stopping:
            self._kill(proc)
            return
        stdout = proc.stdout
        read = getattr(stdout, "read1", None) or stdout.read
        partial = []  # pieces of the current unterminated line (no re-splitting)
        try:
            while True:
                chunk = read(self._READ_SIZE)
                if not chunk:
                    break  # EOF or pipe closed by stop()
                nl = chunk.rfind(b"\n")
                if nl < 0:
                    partial.append(chunk)
                    continue
                partial.append(chunk[:nl])
                data = b"".join(partial)
                partial = [chunk[nl + 1:]] if nl + 1 < len(chunk) else []
                self._publish(
                    [ln.decode("utf-8", "replace").rstrip("\r") for ln in data.split(b"\n")]
                )
        except (OSError, ValueError):
            pass  # pipe closed by stop()
        tail = b"".join(partial)
        if tail:
            self._publish([tail.decode("utf-8", "replace").rstrip("\r")])
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._kill(proc)

    @staticmethod
    def _kill(proc) -> None:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except OSError:
            pass

    def stop(self):
        # kill the process AND close the pipe so a blocking read returns at once
        with self._lock:
            self._stopping = True
            proc = self.proc
        if proc is not None:
            self._kill(proc)


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
            self._bump(1)
            return
        if m & Qt.ControlModifier and k == Qt.Key_Minus:
            self._bump(-1)
            return
        super().keyPressEvent(event)

    def _bump(self, step):
        f = self.font()
        current = f.pointSize() or 10
        size = max(settings_mod.FONT_SIZE_MIN, min(settings_mod.FONT_SIZE_MAX, current + step))
        if size == current:
            return
        f.setPointSize(size)
        self.setFont(f)
        # one debounced, off-thread settings write per zoom gesture
        settings_mod.set_later("term_font_size", size)


class LogcatPanel(QWidget):
    log = pyqtSignal(str)

    _LEVEL_COLOR = theme.LOGCAT_LEVELS
    # Raw lines kept in memory for re-filtering (the complete log is on disk).
    _RECENT_MAX = 20000
    # At most this many filtered lines are redrawn when the filter changes.
    _REFILTER_MAX = 5000
    # On-screen backlog cap; the archive always receives every line.
    _PENDING_MAX = 6000

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.thread = None
        self._closed = False
        self._recent = deque(maxlen=self._RECENT_MAX)
        self._skipped = 0  # lines dropped from the on-screen backlog since the last paint
        self._paused = False
        self._use_crash_buffers = False
        self._filter_re = None
        self._hl_re = None  # keyword/regex to highlight (None = off)
        self._hl_fmt = None  # the highlight char-format (lazy)
        self._fmt_cache = {}  # color -> QTextCharFormat (reused per batch)
        self._pending = []  # lines waiting to be drawn (coalesced)
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
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # One page toolbar: capture actions left, filters in the middle (the
        # regex box stretches), secondary actions right.
        toolbar = QWidget()
        toolbar.setObjectName("pageToolbar")
        toolbar.setAttribute(Qt.WA_StyledBackground, True)
        from .flowlayout import ToolbarFlowLayout

        # Wraps instead of widening the window when the tab is narrow.
        ctrl = ToolbarFlowLayout(toolbar, hspacing=8, vspacing=6)
        ctrl.setContentsMargins(12, 8, 12, 8)
        self.level = QComboBox()
        for label, name, tone in _LEVELS:
            self.level.addItem(icon(name, tone), label)
        self.level.setToolTip("Minimum log level")
        self.level.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.level.setMinimumContentsLength(12)  # room for the level icon + "Verbose (all)"
        # a manual level change clears the crash preset (so it isn't stuck on
        # the crash buffers after the user moves on); guarded during the preset
        self._applying_preset = False
        self.level.currentIndexChanged.connect(self._on_level_changed)
        # Make the actual adb behaviour visible.  A follow mode uses ``-T``
        # before keeping the stream open; a dump mode uses the standard ``-d``
        # invocation and exits once the selected history is printed.
        self.hist = QComboBox()
        self.hist.addItem("Live from now", ("follow", 1))
        self.hist.addItem("Last 1,000 + live", ("follow", 1000))
        self.hist.addItem("Last 10,000 + live", ("follow", 10000))
        self.hist.addItem("Full buffer + live", ("follow", None))
        self.hist.addItem("Dump buffer once", ("dump", None))
        self.hist.addItem("Dump last 1,000 once", ("dump", 1000))
        self.hist.setToolTip(
            "Choose the exact standard logcat mode.\n\n"
            "Live modes keep following after their optional history (-T).\n"
            "Dump modes use -d, print the selected buffer once, then stop."
        )
        self.hist.currentIndexChanged.connect(self._sync_start_button)
        # compact in the toolbar; the popup still lists the full mode text
        self.hist.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.hist.setMinimumContentsLength(20)
        self.tag = QLineEdit()
        self.tag.setPlaceholderText("Tag")
        self.tag.setToolTip("Only show this tag (optional)")
        self.tag.setMaximumWidth(110)
        self.filt = QLineEdit()
        self.filt.setPlaceholderText("Filter (regex, live)…")
        self.filt.setMinimumWidth(120)
        self.filt.addAction(icon("filter"), QLineEdit.LeadingPosition)
        self.filt.textChanged.connect(self._set_filter)
        # a crash preset: switch to the crash buffer + Error level in one click
        self.btn_crash = QPushButton("Crashes")
        self.btn_crash.setProperty("role", "ghost")
        self.btn_crash.setIcon(icon("zap", "red"))
        self.btn_crash.setToolTip(
            "Show only crashes & ANRs: the 'crash' buffer "
            "at Error level, highlighting FATAL/ANR. Click "
            "Start after."
        )
        self.btn_crash.clicked.connect(self._crash_preset)
        self.hl = QLineEdit()
        self.hl.setPlaceholderText("Highlight (e.g. error|anr)…")
        self.hl.setToolTip(
            "Highlight matches in-line (case-insensitive regex) "
            "without hiding the rest — great for spotting "
            "errors/ANRs/your tag in a flood. The Level colours "
            "still apply; matches get a bright marker."
        )
        self.hl.setMaximumWidth(170)
        self.hl.addAction(icon("search"), QLineEdit.LeadingPosition)
        self.hl.textChanged.connect(self._set_highlight)
        self.clear_first = QCheckBox("Clear first")
        self.btn_start = QPushButton("Start")
        self.btn_start.setProperty("role", "ok")
        self.btn_start.setIcon(icon("play", "on-accent"))
        self.btn_start.clicked.connect(self.toggle)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setProperty("role", "ghost")
        self.btn_pause.setIcon(icon("pause", "amber"))
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setProperty("role", "ghost")
        self.btn_clear.setIcon(icon("eraser", "amber"))
        self.btn_clear.setToolTip("Clear the view and the captured history")
        self.btn_clear.clicked.connect(self._clear_view)
        self.btn_save = QPushButton("Save…")
        self.btn_save.setProperty("role", "ghost")
        self.btn_save.setIcon(icon("save", "teal"))
        self.btn_save.setToolTip("Save the complete logcat capture to a file")
        self.btn_save.clicked.connect(self._save)
        # primary (left)
        ctrl.addWidget(self.btn_start)
        ctrl.addWidget(self.btn_pause)
        ctrl.addSpacing(8)
        # filters (middle) — the regex filter takes the spare width
        for w in (self.level, self.hist, self.clear_first, self.tag):
            ctrl.addWidget(w)
        ctrl.addWidget(self.filt, 1)
        ctrl.addWidget(self.hl)
        ctrl.addSpacing(8)
        # secondary (right)
        for w in (self.btn_crash, self.btn_clear, self.btn_save):
            ctrl.addWidget(w)
        lay.addWidget(toolbar)

        body = QVBoxLayout()
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(0)
        lay.addLayout(body, 1)

        self.view = _ZoomEdit()
        fam = settings_mod.get("term_font") or "Consolas"
        self.view.setFont(QFont(fam, int(settings_mod.get("term_font_size") or 10)))
        # generous on-screen scrollback; trimmed lines are archived so a long
        # capture is never lost and Save writes the COMPLETE log, not the tail
        self._sb = Scrollback(self.view, display_cap=120000)
        # word-wrap off = far cheaper layout when lines pour in
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setObjectName("logcatView")  # terminal background (theme.py)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        body.addWidget(self.view, 1)
        # Empty state: a centred hint over the (terminal-coloured) view until
        # the first log line arrives; Clear brings it back.
        self._empty_hint = _EmptyLogHint(self.view.viewport())
        hint_lay = QVBoxLayout(self.view.viewport())
        hint_lay.setContentsMargins(0, 0, 0, 0)
        hint_lay.addStretch(1)
        hint_lay.addWidget(self._empty_hint, 0, Qt.AlignHCenter)
        hint_lay.addStretch(1)
        self._sync_start_button()

    def _menu(self, pos):
        m = QMenu(self.view)
        a = m.addAction(icon("copy", "blue"), "Copy")
        a.setEnabled(self.view.textCursor().hasSelection())
        a.triggered.connect(self.view.copy)
        # Only on-screen lines are selectable; "Save full logcat" has everything.
        m.addAction("Copy visible lines", lambda: (self.view.selectAll(), self.view.copy()))
        m.addAction("Select all", self.view.selectAll)
        m.addSeparator()
        m.addAction(icon("save", "teal"), "Save full logcat to file…", self._save)
        m.addAction(icon("eraser", "amber"), "Clear", self._clear_view)
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
        self.log.emit(
            "[INFO] crash preset set — press Start to stream crashes "
            "& ANRs (crash buffer, Error level)"
        )

    # --- filter ---
    def _set_filter(self, text):
        try:
            compiled = re.compile(text) if text else None
        except re.error:
            # Keep the last valid filter while a regex is still being typed —
            # a half-typed pattern must not flash the whole buffer back.
            self._mark_invalid(self.filt, True)
            return
        self._mark_invalid(self.filt, False)
        self._filter_re = compiled
        self._refilter_timer.start(300)  # debounced re-filter

    def _refilter_view(self):
        """Re-apply the regex filter to the recent raw lines.

        The source is the in-memory raw history (``_RECENT_MAX`` lines), not
        the widget text: tightening hides stale lines and loosening brings
        them back (the old view-text source lost every hidden line for good).
        Only the newest ``_REFILTER_MAX`` matches are redrawn, so a keystroke
        never re-renders a 120k-line document; Save always has everything."""
        recent = list(self._recent)
        if self._paused and self._pending:
            # lines received while paused stay queued until Resume
            recent = recent[: max(0, len(recent) - len(self._pending))]
        else:
            self._pending = []
            self._skipped = 0
        fre = self._filter_re
        if fre is not None:
            recent = [ln for ln in recent if fre.search(ln)]
        self.view.clear()
        self._draw_lines(recent[-self._REFILTER_MAX:])

    def _set_highlight(self, text):
        """Keywords/regex to mark in-line (case-insensitive). Unlike the filter,
        this never hides lines — it just makes matches pop out."""
        try:
            self._hl_re = re.compile(text, re.IGNORECASE) if text else None
        except re.error:
            self._hl_re = None  # keep typing a partial regex without errors
        self._mark_invalid(self.hl, bool(text) and self._hl_re is None)

    def _mark_invalid(self, field, invalid: bool) -> None:
        """Red text for a regex that doesn't compile (theme.py QLineEdit[invalid])."""
        value = "true" if invalid else "false"
        if field.property("invalid") != value:
            field.setProperty("invalid", value)
            self._restyle(field)

    # --- start/stop ---
    def toggle(self):
        if thread_running(self.thread):
            self.stop()
        else:
            self.start()

    def start(self):
        if self._closed or thread_running(self.thread):
            return
        _letter = {
            "Verbose": "V",
            "Debug": "D",
            "Info": "I",
            "Warn": "W",
            "Error": "E",
            "Fatal": "F",
        }
        lvl = _letter.get(self.level.currentText().split()[0], "V")
        tag = self.tag.text().strip()
        fmt = settings_mod.get("logcat_format") or "threadtime"
        mode, tailn = self.hist.currentData() or ("follow", 1)
        clear_it = self.clear_first.isChecked() and mode == "follow"
        args = ["logcat", "-v", fmt]
        if mode == "dump":
            args.append("-d")
        if self._use_crash_buffers:
            for b in self._CRASH_BUFFERS:
                args += ["-b", b]
        if tailn is not None:
            args += ["-T", str(tailn)]
        if tag and lvl != "V":
            args += [f"{tag}:{lvl}", "*:S"]
        elif tag:
            args += [f"{tag}:V", "*:S"]
        elif lvl != "V":
            args += [f"*:{lvl}"]
        if self.thread is not None:
            # a finished worker whose final lines were not painted yet
            self._on_batch(self.thread.take_lines())
        thread = _LogcatThread(self.handler, args, clear_first=clear_it)
        self.thread = thread
        thread.finished.connect(lambda t=thread: self._on_thread_finished(t))
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self.btn_start.setText("Stop")
        self.btn_start.setIcon(icon("stop", "on-danger"))
        self.btn_start.setProperty("role", "danger")
        self._restyle(self.btn_start)
        if not self._empty_hint.isHidden():
            self._empty_hint.set_text(_HINT_WAIT)
        kind = "dump" if mode == "dump" else "live capture"
        self.log.emit(f"[OK] logcat {kind} started: adb {' '.join(args)}")

    def stop(self):
        if self.thread is not None:
            self.thread.stop()

    def _on_thread_finished(self, thread):
        # The worker deletes itself (deleteLater) after this; drop our
        # reference first so a later Start/Stop never touches a dead wrapper.
        if self._closed or thread is not self.thread:
            return
        self._on_batch(thread.take_lines())
        self.thread = None
        self._sync_start_button()
        self.log.emit("[OK] logcat stopped")

    def _sync_start_button(self, *_):
        """Reflect the selected adb mode while the worker is idle."""
        if thread_running(self.thread):
            return
        mode, _tailn = self.hist.currentData() or ("follow", 1)
        is_dump = mode == "dump"
        self.btn_start.setText("Dump" if is_dump else "Start")
        self.btn_start.setIcon(icon("download", "teal") if is_dump else icon("play", "on-accent"))
        self.btn_start.setProperty("role", "ghost" if is_dump else "ok")
        self._empty_hint.set_text(_HINT_DUMP if is_dump else _HINT_START)
        self.clear_first.setEnabled(not is_dump)
        self.clear_first.setToolTip(
            "Clear device logs before starting live capture."
            if not is_dump
            else "Unavailable for a dump: clearing first would erase the history you asked to save."
        )
        self._restyle(self.btn_start)

    def _toggle_pause(self):
        self._paused = not self._paused
        self.btn_pause.setText("Resume" if self._paused else "Pause")
        self.btn_pause.setIcon(icon("play", "green") if self._paused else icon("pause", "amber"))

    def _clear_view(self):
        self.view.clear()
        self._pending = []
        self._recent.clear()
        self._skipped = 0
        self._sb.reset()  # clear forgets the archived history too
        running = thread_running(self.thread)
        self._empty_hint.set_text(_HINT_WAIT if running else self._idle_hint())
        self._empty_hint.show()

    def _idle_hint(self) -> str:
        mode, _tailn = self.hist.currentData() or ("follow", 1)
        return _HINT_DUMP if mode == "dump" else _HINT_START

    def _on_batch(self, lines):
        """Buffer lines only — the render timer paints them. Keeps the worker and
        the (slow over RDP) GUI painting fully decoupled."""
        if not lines or self._closed:
            return
        if not self._empty_hint.isHidden():
            self._empty_hint.hide()  # output arrived: the view is no longer empty
        # COMPLETE capture at the source: archive every line BEFORE any on-screen
        # dropping, so the saved log is whole even when the view skips to keep up
        self._sb.archive("\n".join(lines) + "\n")
        self._recent.extend(lines)
        # while paused, keep buffering (bounded below) — clearing here meant
        # everything logged during a pause vanished from the view on Resume
        self._pending.extend(lines)
        # hard cap the ON-SCREEN backlog so a sustained flood can never make the
        # GUI fall behind (the archive above already has every line).  The
        # count accumulates across trims, so the marker reports every skip.
        overflow = len(self._pending) - self._PENDING_MAX
        if overflow > 0:
            self._skipped += overflow
            del self._pending[:overflow]

    def _render_pending(self):
        """Pull the worker's lines, then paint what accumulated since the last
        tick — one insert, one scroll, at a fixed rate regardless of volume."""
        thread = self.thread
        if thread is not None:
            self._on_batch(thread.take_lines())
        if self._paused or not (self._pending or self._skipped):
            return
        lines, self._pending = self._pending, []
        fre = self._filter_re
        if fre is not None:
            lines = [ln for ln in lines if fre.search(ln)]
        if self._skipped:
            lines.insert(0, f"… ({self._skipped} lines skipped on screen — saved log has all)")
            self._skipped = 0
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
            color = theme.LOGCAT_DEFAULT
            head = line[:40]
            for lvl, c in self._LEVEL_COLOR.items():
                if f" {lvl} " in head or f"/{lvl}(" in head:
                    color = c
                    break
            fmt = fmt_cache.get(color)
            if fmt is None:
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(color))
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
            f.setBackground(QColor(theme.HIGHLIGHT_BG))
            f.setForeground(QColor(theme.HIGHLIGHT_FG))
            f.setFontWeight(QFont.Bold)
            self._hl_fmt = f
        return self._hl_fmt

    @staticmethod
    def _insert_highlighted(cur, line, base_fmt, hre, hl_fmt):
        """Insert *line* with matched spans in *hl_fmt* and the rest in *base_fmt*."""
        pos = 0
        for m in hre.finditer(line):
            s, e = m.start(), m.end()
            if e == s:  # skip zero-width matches
                continue
            if s > pos:
                cur.insertText(line[pos:s], base_fmt)
            cur.insertText(line[s:e], hl_fmt)
            pos = e
        cur.insertText(line[pos:] + "\n", base_fmt)

    def _save(self):
        """Save the COMPLETE log (not just the tail) off the UI thread."""
        from .fileutil import save_output

        save_output(
            self,
            "Save logcat",
            "turboadb-logcat-" + time.strftime("%Y%m%d-%H%M%S") + ".log",
            self._sb.save_job(),
            what="logcat",
            on_saved=lambda path: self.log.emit(f"[OK] logcat saved to {path}"),
        )

    @staticmethod
    def _restyle(w):
        w.style().unpolish(w)
        w.style().polish(w)

    def close_panel(self):
        """Stop capture and detach the worker without waiting on the UI thread."""
        self._closed = True
        self._render_timer.stop()
        self._refilter_timer.stop()
        thread, self.thread = self.thread, None
        if thread is not None:
            thread.stop()
            disconnect_signals(thread, ("finished",))
            park_thread(thread)  # re-adds deleteLater; referenced until it ends
        self._sb.close()  # drop the temp scrollback file
