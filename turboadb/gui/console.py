"""A fast, selectable terminal console for the interactive ``adb shell``.

Built on QPlainTextEdit so it gets **native mouse text selection, copy/paste,
scrollback and smooth scrolling for free**. It runs the shell in **cooked
line-editing mode**: you type into the terminal with local echo and **one Enter
runs the command** — the whole line is sent to ``adb shell``'s stdin at once.
This is far more reliable on Windows than forcing a pseudo-terminal (which made
keystrokes need several Enters). An incremental ANSI parser colours the output
and handles carriage-return / backspace / line-erase.

Up/Down recall history; Ctrl+C interrupts; selection + copy work like any
terminal."""

from __future__ import annotations

import codecs
import time
from collections import deque

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont, QTextCursor, QTextCharFormat, QColor
from PyQt5.QtWidgets import QPlainTextEdit, QMenu, QApplication

from ..results import strip_ansi
from . import settings as settings_mod, theme
from .theme import TERM_BG
from .scrollback import Scrollback

_ANSI = {
    30: "#1c1c1c", 31: "#e25c52", 32: "#3ddc84", 33: "#d4b86a",
    34: "#4f9bea", 35: "#c678dd", 36: "#56c5d0", 37: "#cfd8e3",
    90: "#5c6370", 91: "#ff7a6e", 92: "#5be39a", 93: "#ffd479",
    94: "#6fb3ff", 95: "#d79bf0", 96: "#74dbe6", 97: "#ffffff",
}
_FG_DEFAULT = "#d7f5e3"
_PROMPT_COLOR = "#28c2d6"


class AnsiConsole(QPlainTextEdit):
    def __init__(self, send_fn=None, parent=None):
        super().__init__(parent)
        self._send = send_fn                   # send_fn(bytes) -> to shell stdin
        # editable=False would hide the caret; instead we keep it editable for a
        # blinking cursor but intercept ALL keys (keyPressEvent never calls super)
        # and block drops, so the user can never actually free-edit the buffer.
        self.setUndoRedoEnabled(False)
        self.setAcceptDrops(False)
        # generous on-screen scrollback (Qt drops oldest blocks efficiently);
        # EVERYTHING is also streamed to disk so Save writes the complete log
        self._sb = Scrollback(self, display_cap=80000)
        fam = settings_mod.get("term_font") or "Consolas"
        size = int(settings_mod.get("term_font_size") or 10)
        self.setFont(QFont(fam, size))
        self.setStyleSheet(
            f"QPlainTextEdit{{background:{TERM_BG};color:{_FG_DEFAULT};"
            f"border:none;selection-background-color:#1f5f6e;}}")
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._wc = QTextCursor(self.document())
        self._wc.movePosition(QTextCursor.End)
        self._fmt = QTextCharFormat(); self._fmt.setForeground(QColor(_FG_DEFAULT))
        self._state = 0; self._csi = ""
        # cooked line-editing state
        self._line = ""
        self._need_prompt = True
        self._history = []
        self._hidx = 0
        # emulated Android shell prompt (device shell over a pipe prints no PS1)
        self._host = ""
        self._root = False
        self._cwd = "/"
        self._alive = True        # False after the shell dies (reboot/unplug)
        self._completion_fn = None    # Tab path-completion provider
        self._last_feed = 0.0         # monotonic time of the last output chunk
        self._interrupt = None        # reliable stop (set by ShellPanel)
        self._shown_prompt = ""       # the prompt text currently displayed
        # after a command's output goes idle, auto-show the next prompt (so the
        # device prompt + cursor are waiting, like a real terminal)
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self._idle_prompt)
        # ingestion is decoupled from rendering: feed() only enqueues + archives
        # (both O(1)); this timer renders within a TIME BUDGET per tick so no flood
        # can ever saturate the UI thread ("not responding")
        self._inq = deque()           # queued text chunks awaiting render
        self._inq_len = 0
        self._drain = QTimer(self)
        self._drain.setInterval(15)
        self._drain.timeout.connect(self._drain_tick)

    _TICK_BUDGET = 0.030          # seconds of rendering per tick (keeps UI live)
    _SUB = 16 * 1024             # max chars handed to _process at once
    _MAX_INQ = 8 * 1024 * 1024   # cap the ON-SCREEN backlog (disk archive is full)

    def _move_caret_end(self):
        if self.textCursor().hasSelection():
            return
        c = self.textCursor(); c.movePosition(QTextCursor.End); self.setTextCursor(c)

    def _idle_prompt(self):
        if self._alive and self._need_prompt and not self._line:
            self._prompt_if_needed()
            self._move_caret_end()

    def _consume_pending_prompt(self):
        """If a waiting prompt is currently shown and the user hasn't typed, remove
        it so newly-arrived output doesn't get appended after a stray
        ``device:/ $`` (which is what made the name appear mid-stream)."""
        if self._need_prompt or self._line or not self._shown_prompt:
            return
        self._wc.movePosition(QTextCursor.End)
        for _ in range(len(self._shown_prompt)):
            self._wc.deletePreviousChar()
        self._shown_prompt = ""
        self._need_prompt = True

    def set_interrupt_fn(self, fn):
        """Provide a reliable 'stop the running command' callback (the shell has
        no PTY, so Ctrl+C can't deliver a real SIGINT)."""
        self._interrupt = fn

    def set_completion_fn(self, fn):
        """fn(line) -> (completed_line_or_None, options_list). Bound to Tab."""
        self._completion_fn = fn

    def _do_complete(self):
        if not self._completion_fn or not self._alive:
            return
        try:
            newline, opts = self._completion_fn(self._line)
        except Exception:
            return
        if newline is not None and newline != self._line:
            self._set_line(newline)
        elif opts:
            self._echo("\n" + "   ".join(opts) + "\n")
            self._need_prompt = True
            self._prompt_if_needed()
            self._echo(self._line)
            self._move_caret_end()

    def set_alive(self, alive: bool):
        """Mark the shell connected/disconnected. When dead we stop emitting the
        fake prompt and ignore typing; on reconnect we resume with a prompt."""
        if alive and not self._alive:
            self._alive = True
            self._line = ""
            self._need_prompt = True
            self._echo("\n", _PROMPT_COLOR)
            self.show_prompt()
        elif not alive and self._alive:
            self._alive = False
            self._idle.stop()
            self._echo("\n[shell disconnected]\n", "#ff7a6e")

    # ===== prompt (mimics the real adb shell prompt) =====
    def set_prompt(self, host, root=False):
        self._host = host or ""
        self._root = bool(root)

    def _prompt_text(self):
        if not self._host:
            return "$ "
        return f"{self._host}:{self._cwd} {'#' if self._root else '$'} "

    def show_prompt(self):
        """Print the prompt now (call once the shell is open)."""
        self._prompt_if_needed()
        self._move_caret_end()

    def _apply_cd(self, cmd):
        import posixpath
        rest = cmd.strip()[2:].strip()          # after 'cd'
        arg = rest.split()[0] if rest else ""
        arg = arg.strip("'\"")
        if not arg or arg == "~":
            new = "/"
        elif arg == "-":
            new = self._cwd
        elif arg.startswith("/"):
            new = posixpath.normpath(arg)
        else:
            new = posixpath.normpath(posixpath.join(self._cwd, arg))
        self._cwd = new or "/"

    # ===== output from the shell =====
    def feed(self, data):
        """Cheap + non-blocking: capture everything to disk, queue for rendering.
        The actual (potentially expensive) document work happens in bounded slices
        in ``_drain_tick`` so the UI never freezes under a flood of output."""
        text = data if isinstance(data, str) else self._decoder.decode(data)
        if not text:
            return
        self._last_feed = time.monotonic()
        self._sb.archive(strip_ansi(text))     # COMPLETE capture, at the source
        self._idle.stop()
        self._inq.append(text)                 # O(1) enqueue (no giant string copy)
        self._inq_len += len(text)
        if self._inq_len > self._MAX_INQ:
            # extreme flood: drop the OLDEST queued text from the on-screen path
            # (the saved log already has every byte) so memory stays bounded
            while self._inq_len > self._MAX_INQ and len(self._inq) > 1:
                self._inq_len -= len(self._inq.popleft())
        if not self._drain.isActive():
            self._drain.start()

    def _drain_tick(self):
        if not self._inq:
            self._drain.stop()
            if self._alive and self._need_prompt and not self._line:
                self._idle.start(350)          # output stopped → show the prompt
            return
        sb = self.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 2
        deadline = time.monotonic() + self._TICK_BUDGET
        self.setUpdatesEnabled(False)
        try:
            # a prompt shown during a lull in output must not sit in the MIDDLE of
            # the stream — drop it before rendering the lines that just arrived
            self._consume_pending_prompt()
            # render in small slices until the per-tick time budget is spent, then
            # yield back to the event loop — so each tick is short and the UI stays
            # responsive no matter how many million lines are queued
            while self._inq and time.monotonic() < deadline:
                chunk = self._inq.popleft()
                self._inq_len -= len(chunk)
                if len(chunk) > self._SUB:
                    self._process(chunk[:self._SUB])
                    rest = chunk[self._SUB:]
                    self._inq.appendleft(rest)
                    self._inq_len += len(rest)
                else:
                    self._process(chunk)       # ANSI state persists across chunks
        finally:
            self.setUpdatesEnabled(True)
        if at_bottom:
            sb.setValue(sb.maximum())

    def _process(self, text):
        # Batch runs of plain printable text into single inserts — inserting one
        # char at a time was the slowness on large output (dumpsys, ls -R, cat …).
        run = []

        def flush():
            if run:
                self._out("".join(run))
                run.clear()

        for ch in text:
            if self._state == 0:
                if ch == "\x1b":
                    flush(); self._state = 1
                elif ch == "\r":
                    flush(); self._wc.movePosition(QTextCursor.StartOfBlock)
                elif ch == "\n":
                    flush()
                    self._wc.movePosition(QTextCursor.End)
                    self._wc.insertText("\n", self._fmt)
                elif ch == "\b":
                    flush(); self._wc.movePosition(QTextCursor.Left)
                elif ch == "\t":
                    run.append("    ")
                elif ord(ch) < 32:
                    pass
                else:
                    run.append(ch)
            elif self._state == 1:
                self._state = 2 if ch == "[" else 0
                self._csi = ""
            elif self._state == 2:
                if "\x40" <= ch <= "\x7e":
                    self._csi_dispatch(ch, self._csi); self._state = 0
                else:
                    self._csi += ch
        flush()

    def _out(self, s):
        if not s:
            return
        if self._wc.atBlockEnd():
            self._wc.insertText(s, self._fmt)          # fast path: append the run
            return
        # overwrite within the current line, then append any remainder
        rem = s
        while rem and not self._wc.atBlockEnd():
            self._wc.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor)
            self._wc.insertText(rem[0], self._fmt)
            rem = rem[1:]
        if rem:
            self._wc.insertText(rem, self._fmt)

    def _csi_dispatch(self, final, params):
        if final == "m":
            for p in (params.split(";") if params else ["0"]):
                self._sgr(p)
        elif final == "K":
            n = params or "0"
            if n in ("0", ""):
                self._wc.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                self._wc.removeSelectedText()
            elif n == "2":
                self._wc.movePosition(QTextCursor.StartOfBlock)
                self._wc.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                self._wc.removeSelectedText()

    def _sgr(self, p):
        try:
            n = int(p)
        except ValueError:
            return
        if n == 0:
            self._fmt = QTextCharFormat(); self._fmt.setForeground(QColor(_FG_DEFAULT))
        elif n == 1:
            self._fmt.setFontWeight(QFont.Bold)
        elif n == 39:
            self._fmt.setForeground(QColor(_FG_DEFAULT))
        elif n in _ANSI:
            self._fmt.setForeground(QColor(_ANSI[n]))

    # ===== local echo helpers =====
    def _echo(self, s, color=None):
        self._wc.movePosition(QTextCursor.End)
        fmt = self._fmt
        if color:
            fmt = QTextCharFormat(); fmt.setForeground(QColor(color))
        self._wc.insertText(s, fmt)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _prompt_if_needed(self):
        if self._need_prompt:
            self._shown_prompt = self._prompt_text()
            self._echo(self._shown_prompt, _PROMPT_COLOR)
            self._need_prompt = False

    def _erase_input(self):
        for _ in range(len(self._line)):
            self._wc.movePosition(QTextCursor.End)
            self._wc.deletePreviousChar()

    def _set_line(self, new):
        self._prompt_if_needed()
        self._erase_input()
        self._line = new
        self._echo(new)

    @staticmethod
    def _columnize(cmd):
        """A bare ``ls`` over a no-PTY pipe prints ONE entry per line (so 50 files
        = 50 lines). Quietly run it multi-column (``ls -C``) like a real terminal
        would — which also keeps the saved log compact. Only a plain ``ls`` /
        ``ls <paths>`` with no flags and no shell pipe/redirect is touched, so
        ``ls -l``, ``ls | grep``, ``ls > f`` etc. are left exactly as typed."""
        s = cmd.strip()
        parts = s.split()
        if not parts or parts[0] != "ls":
            return cmd
        if any(p.startswith("-") for p in parts[1:]):
            return cmd
        if any(c in s for c in "|<>;&`$()"):
            return cmd
        return "ls -C" + s[2:]

    # ===== keyboard: cooked line editing =====
    def keyPressEvent(self, event):
        if not self._send:
            return
        mods, key = event.modifiers(), event.key()
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)

        if ctrl and shift and key == Qt.Key_C:
            self.copy(); return
        if (ctrl and shift and key == Qt.Key_V) or (shift and key == Qt.Key_Insert):
            self._paste_into_line(); return
        if ctrl and key == Qt.Key_Insert:
            self.copy(); return
        if not self._alive:                 # shell is down — ignore typing
            return
        if ctrl and key == Qt.Key_C:
            if self.textCursor().hasSelection():
                self.copy(); return
            self._send(b"\x03")                 # honoured only if the device PTYs
            if self._interrupt and (time.monotonic() - self._last_feed) < 2.0:
                # output is actively flowing (e.g. logcat) and the no-PTY pipe
                # ignores ^C — fall back to a reliable device-side stop
                self._echo("^C\n")
                self._interrupt()
                return
            self._echo("^C\n"); self._line = ""; self._need_prompt = True
            self._idle.start(400); self._move_caret_end()
            return
        if ctrl and key == Qt.Key_L:
            self.clear(); return

        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._prompt_if_needed()
            self._echo("\n")
            cmd = self._line
            # record the command in the full log (the device, over a pipe, doesn't
            # echo it back, so feed() alone wouldn't capture what was typed)
            self._sb.archive(self._prompt_text() + cmd + "\n")
            if cmd.strip():
                self._history.append(cmd)
                if cmd.strip().split()[0] == "cd":
                    self._apply_cd(cmd)          # keep the prompt's path in sync
            self._hidx = len(self._history)
            self._last_feed = time.monotonic()   # treat the shell as busy now
            try:
                self._send((self._columnize(cmd) + "\n").encode("utf-8"))
            except Exception:
                pass
            self._line = ""
            self._need_prompt = True
            self._move_caret_end()
            self._idle.start(400)              # prompt returns even if no output
            return
        if key == Qt.Key_Backspace:
            if self._line:
                self._line = self._line[:-1]
                self._wc.movePosition(QTextCursor.End)
                self._wc.deletePreviousChar()
                self._move_caret_end()
            return
        if key == Qt.Key_Up:
            if self._history and self._hidx > 0:
                self._hidx -= 1
                self._set_line(self._history[self._hidx])
            return
        if key == Qt.Key_Down:
            if self._history and self._hidx < len(self._history) - 1:
                self._hidx += 1
                self._set_line(self._history[self._hidx])
            elif self._history:
                self._hidx = len(self._history)
                self._set_line("")
            return
        if key == Qt.Key_Tab:
            self._do_complete()
            return

        text = event.text()
        if text and text.isprintable():
            self._idle.stop()
            self._prompt_if_needed()
            self._line += text
            self._echo(text)
            self._move_caret_end()

    def _paste_into_line(self):
        txt = QApplication.clipboard().text()
        if not txt:
            return
        txt = txt.replace("\r\n", "\n").replace("\r", "\n")
        if "\n" in txt:
            # multi-line paste: send line by line as commands
            lines = txt.split("\n")
            for i, ln in enumerate(lines):
                if i < len(lines) - 1:
                    self._prompt_if_needed(); self._echo(ln + "\n")
                    self._sb.archive(self._prompt_text() + ln + "\n")
                    self._last_feed = time.monotonic()
                    self._send((self._columnize(ln) + "\n").encode("utf-8"))
                    self._line = ""; self._need_prompt = True
                else:
                    if ln:
                        self._prompt_if_needed(); self._line += ln; self._echo(ln)
        else:
            self._prompt_if_needed(); self._line += txt; self._echo(txt)

    def _menu(self, pos):
        ico = theme.emoji_icon
        m = QMenu(self)
        a = m.addAction(ico("📋"), "Copy"); a.setEnabled(self.textCursor().hasSelection())
        a.triggered.connect(self.copy)
        m.addAction(ico("🗂"), "Copy all", lambda: (self.selectAll(), self.copy()))
        m.addAction(ico("📥"), "Paste", self._paste_into_line)
        m.addAction(ico("🔲"), "Select All", self.selectAll)
        m.addSeparator()
        # Send key: control sequences for an interactive program in the shell
        keys = m.addMenu(ico("⌨"), "Send key")
        for label, data in (("Enter (\\n)", b"\n"), ("Tab (\\t)", b"\t"),
                            ("Esc", b"\x1b"), ("Ctrl+C", b"\x03"),
                            ("Ctrl+D (EOF)", b"\x04"), ("Ctrl+Z", b"\x1a"),
                            ("Up", b"\x1b[A"), ("Down", b"\x1b[B"),
                            ("Backspace", b"\x7f")):
            keys.addAction(label, lambda d=data: self._send(d) if self._send else None)
        m.addSeparator()
        m.addAction(ico("💾"), "Save full output to file…", self._save_output)
        m.addAction(ico("🧹"), "Clear", self.clear)
        m.exec_(self.viewport().mapToGlobal(pos))

    def _save_output(self):
        from PyQt5.QtWidgets import QFileDialog
        default = "turboadb-shell-" + time.strftime("%Y%m%d-%H%M%S") + ".log"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save terminal output", default,
            "Log files (*.log);;Text files (*.txt);;All files (*)")
        if path:
            self.save_output(path)
            from .fileutil import saved_dialog
            saved_dialog(self, path, "terminal output")

    # NOTE: saves the COMPLETE history (archived + on-screen), not just the
    # visible tail — so nothing is lost even after a long flood of output.
    def save_output(self, path):
        self._sb.save_to(path)

    def clear(self):
        super().clear()
        self._sb.reset()                 # clear forgets the archived history too
        self._inq.clear(); self._inq_len = 0; self._drain.stop()
        self._wc = QTextCursor(self.document()); self._wc.movePosition(QTextCursor.End)
        self._line = ""; self._need_prompt = True

    def close_archive(self):
        self._drain.stop()
        self._sb.close()

    paste_clipboard = _paste_into_line
