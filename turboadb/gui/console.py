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
from PyQt5.QtGui import QFont, QTextCursor, QTextCharFormat, QColor, QBrush
from PyQt5.QtWidgets import QPlainTextEdit, QMenu, QApplication

from ..results import strip_ansi
from . import settings as settings_mod, theme
from .theme import TERM_BG
from .scrollback import Scrollback

_ANSI = {
    30: "#0f172a", 31: "#ef4444", 32: "#22c55e", 33: "#f59e0b",
    34: "#3b82f6", 35: "#a855f7", 36: "#06b6d4", 37: "#e2e8f0",
    90: "#64748b", 91: "#f87171", 92: "#4ade80", 93: "#fbbf24",
    94: "#60a5fa", 95: "#c084fc", 96: "#38bdf8", 97: "#ffffff",
}
_BG_ANSI = {
    40: "#0f172a", 41: "#dc2626", 42: "#16a34a", 43: "#ca8a04",
    44: "#2563eb", 45: "#9333ea", 46: "#0891b2", 47: "#cbd5e1",
    100: "#475569", 101: "#ef4444", 102: "#22c55e", 103: "#f59e0b",
    104: "#3b82f6", 105: "#a855f7", 106: "#06b6d4", 107: "#f8fafc",
}
_FG_DEFAULT = "#f1f5f9"
_PROMPT_COLOR = "#38bdf8"


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
        font = QFont(fam, size)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        self.document().setDefaultFont(font)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._update_stylesheet(fam, size)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

        self._prompt_provider_fn = None

        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._wc = QTextCursor(self.document())
        self._wc.movePosition(QTextCursor.End)
        self._fmt = QTextCharFormat()
        self._fmt.setFont(self.font())
        self._fmt.setForeground(QColor(_FG_DEFAULT))
        self._state = 0
        self._csi = ""
        self._osc = ""
        # cooked line-editing state
        self._line = ""
        self._cpos = 0
        self._pending_echo = None
        self._need_prompt = True
        self._history = []
        self._hidx = 0
        # emulated Android shell prompt (device shell over a pipe prints no PS1)
        self._host = ""
        self._root = False
        self._cwd = "/"
        self._alive = True          # False after the shell dies (reboot/unplug)
        self._completion_fn = None  # Tab path-completion provider
        self._tab_cycle_opts = []   # Candidate completions for active tab cycling
        self._tab_cycle_idx = -1
        self._tab_replace_idx = 0
        self._tab_base_line = ""
        self._last_feed = 0.0       # monotonic time of the last output chunk
        self._interrupt = None      # reliable stop (set by ShellPanel)
        self._shown_prompt = ""     # the prompt text currently displayed
        self._emulate_prompt = True


        # after a command's output goes idle, auto-show the next prompt
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self._idle_prompt)

        # ingestion decoupled from rendering
        self._inq = deque()         # queued text chunks awaiting render
        self._inq_len = 0
        self._drain = QTimer(self)
        self._drain.setInterval(15)
        self._drain.timeout.connect(self._drain_tick)

    _TICK_BUDGET = 0.030          # seconds of rendering per tick (keeps UI live)
    _SUB = 16 * 1024              # max chars handed to _process at once
    _MAX_INQ = 8 * 1024 * 1024    # cap the ON-SCREEN backlog

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self.bump_font(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def _update_stylesheet(self, fam=None, size=None):
        if fam is None:
            fam = self.font().family() or "Consolas"
        if size is None:
            size = self.font().pointSize() or 10
        self.setStyleSheet(
            f"QPlainTextEdit {{"
            f"font-family: '{fam}', 'Cascadia Code', 'Consolas', monospace;"
            f"font-size: {size}pt;"
            f"background: {TERM_BG};"
            f"color: {_FG_DEFAULT};"
            f"border: none;"
            f"selection-background-color: #2563eb;"
            f"selection-color: #ffffff;"
            f"}}"
        )

    def bump_font(self, step):
        f = self.font()
        size = max(6, min(40, (f.pointSize() or 10) + step))
        if size == f.pointSize():
            return
        f.setPointSize(size)
        self.setFont(f)
        self.document().setDefaultFont(f)
        self._update_stylesheet(size=size)
        try:
            data = settings_mod.load()
            data["term_font_size"] = size
            settings_mod.save(data)
        except Exception:
            pass

    def _move_caret_end(self):
        if self.textCursor().hasSelection():
            return
        c = self.textCursor()
        c.movePosition(QTextCursor.End)
        self.setTextCursor(c)

    def _idle_prompt(self):
        if not getattr(self, "_emulate_prompt", True):
            return
        if self._alive and self._need_prompt and not self._line:
            self._prompt_if_needed()
            self._move_caret_end()

    def _consume_pending_prompt(self):
        """If a waiting prompt is currently shown and the user hasn't typed, remove
        it so newly-arrived output doesn't get appended after a stray prompt."""
        if not getattr(self, "_emulate_prompt", True):
            return
        if self._need_prompt or self._line or not self._shown_prompt:
            return
        self._wc.movePosition(QTextCursor.End)
        for _ in range(len(self._shown_prompt)):
            self._wc.deletePreviousChar()
        self._shown_prompt = ""
        self._need_prompt = True

    def set_interrupt_fn(self, fn):
        """Provide a reliable 'stop the running command' callback."""
        self._interrupt = fn

    def set_completion_fn(self, fn):
        """fn(line) -> (completed_line_or_None, options_list). Bound to Tab."""
        self._completion_fn = fn

    def set_prompt_provider_fn(self, fn):
        """fn() -> prompt string (e.g. for local shells)."""
        self._prompt_provider_fn = fn

    @staticmethod
    def _format_columns(items, width=80):
        if not items:
            return ""
        display_items = []
        for s in items:
            s_str = str(s)
            clean = s_str.strip('"\'')
            if clean.endswith("\\") or clean.endswith("/"):
                display_items.append("📁 " + s_str)
            else:
                display_items.append(s_str)
        max_len = max(len(s) for s in display_items)
        eff_width = max(width, 24)
        col_width = min(eff_width, max_len + 3)
        cols = max(1, eff_width // max(1, col_width))
        lines = []
        for i in range(0, len(display_items), cols):
            row = display_items[i : i + cols]
            lines.append("".join(s.ljust(col_width) for s in row).rstrip())
        return "\n".join(lines)

    def paste_clipboard(self):
        """Public alias for pasting clipboard text into the active input line."""
        self._paste_into_line()

    def _clear_tab_cycle(self):
        self._tab_cycle_opts.clear()
        self._tab_cycle_idx = -1
        self._tab_replace_idx = 0
        self._tab_base_line = ""

    def _clear_viewport(self):
        super().clear()
        self._wc = QTextCursor(self.document())
        self._wc.movePosition(QTextCursor.End)
        self._line = ""
        self._cpos = 0
        self._pending_echo = None
        self._state = 0
        self._csi = ""
        self._osc = ""
        self._clear_tab_cycle()

    def _do_complete(self, reverse: bool = False):
        if not self._completion_fn or not self._alive:
            return

        # 1. If user presses Tab repeatedly, cycle through previous matches
        if self._tab_cycle_opts:
            step = -1 if reverse else 1
            self._tab_cycle_idx = (self._tab_cycle_idx + step) % len(self._tab_cycle_opts)
            cand = self._tab_cycle_opts[self._tab_cycle_idx]
            new_line = self._tab_base_line[: self._tab_replace_idx] + cand
            self._set_line(new_line)
            return

        # 2. First tab press: query completion provider
        try:
            newline, opts = self._completion_fn(self._line)
        except Exception:
            return

        if not opts and newline is not None and newline != self._line:
            self._set_line(newline)
            return

        if opts:
            # Single option: complete directly
            if len(opts) == 1:
                if newline is not None and newline != self._line:
                    self._set_line(newline)
                else:
                    cand = opts[0]
                    tokens = self._line.split()
                    if tokens and not self._line.endswith(" "):
                        last_tok = tokens[-1]
                        idx = self._line.rfind(last_tok)
                        self._set_line(self._line[:idx] + cand)
                    else:
                        self._set_line(self._line + cand)
                return

            # Multiple options: apply common prefix if it extends current line
            if newline is not None and len(newline) > len(self._line):
                self._set_line(newline)

            # Display options cleanly
            formatted = self._format_columns(opts)
            self._echo("\n" + formatted + "\n")
            if getattr(self, "_emulate_prompt", True):
                self._need_prompt = True
                self._prompt_if_needed()
            elif hasattr(self, "_prompt_provider_fn") and self._prompt_provider_fn:
                prompt = self._prompt_provider_fn()
                if prompt:
                    self._echo(prompt, _PROMPT_COLOR)
            self._echo(self._line)
            self._move_caret_end()

            # Store options for cycling on subsequent tabs
            self._tab_cycle_opts = list(opts)
            self._tab_cycle_idx = -1
            self._tab_base_line = self._line
            tokens = self._line.split()
            if tokens and not self._line.endswith(" "):
                last_tok = tokens[-1]
                self._tab_replace_idx = self._line.rfind(last_tok)
            else:
                self._tab_replace_idx = len(self._line)


    def set_alive(self, alive: bool):
        """Mark the shell connected/disconnected."""
        if alive:
            self._alive = True
            self._line = ""
            if getattr(self, "_emulate_prompt", True):
                self._need_prompt = True
                self.show_prompt()
            else:
                self._need_prompt = False
            self._move_caret_end()
        elif not alive and self._alive:
            self._alive = False
            self._idle.stop()
            self._echo("\n[shell disconnected]\n", "#ff7a6e")

    def set_emulate_prompt(self, enabled: bool):
        """Enable or disable synthetic Android shell prompt emulation."""
        self._emulate_prompt = bool(enabled)
        if not self._emulate_prompt:
            self._need_prompt = False
            self._shown_prompt = ""

    def set_prompt(self, host, root=False):
        self._host = host or ""
        self._root = bool(root)

    def _prompt_text(self):
        import datetime
        now = datetime.datetime.now()
        d_s = now.strftime("%m-%d")
        t_s = now.strftime("%H:%M")
        cwd = getattr(self, "_cwd", "/") or "/"
        arrow = "\u25b6"
        return (
            f"\x1b[30;46m 📅 {d_s} "
            f"\x1b[36;42m{arrow}"
            f"\x1b[30;42m 🕒 {t_s} "
            f"\x1b[32;43m{arrow}"
            f"\x1b[30;43m 📁 {cwd} "
            f"\x1b[33;49m{arrow}\x1b[0m "
        )

    def banner(self, text):
        """Render a pre-formatted welcome banner synchronously."""
        if not text:
            return
        self._sb.archive(strip_ansi(text))
        self._process(text)
        self._wc.movePosition(QTextCursor.End)
        if "\u25b6" in text and "📁" in text:
            last_line = text.splitlines()[-1] if text.splitlines() else ""
            self._shown_prompt = strip_ansi(last_line)
            self._need_prompt = False

    def show_prompt(self):
        """Print the prompt now (call once the shell is open)."""
        self._prompt_if_needed()
        self._move_caret_end()

    def _apply_cd(self, cmd):
        import posixpath
        rest = cmd.strip()[2:].strip()
        tokens = rest.split() if rest else []
        non_flags = [t for t in tokens if not t.startswith("-") or t in ("-", "--")]
        arg = non_flags[0] if non_flags else ""
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

    def feed(self, data):
        """Enqueue text and flush to disk archive; renders asynchronously in slices."""
        text = data if isinstance(data, str) else self._decoder.decode(data)
        if not text:
            return
        if getattr(self, "_pending_echo", None):
            pe = self._pending_echo
            if pe in ("\r\n", "\n"):
                if text.startswith("\r\n"):
                    text = text[2:]
                elif text.startswith("\n"):
                    text = text[1:]
                self._pending_echo = None
            elif text.startswith(pe):
                text = text[len(pe):]
                if text.startswith("\r\n"):
                    text = text[2:]
                elif text.startswith("\n"):
                    text = text[1:]
                self._pending_echo = None
            elif pe.startswith(text):
                self._pending_echo = pe[len(text):]
                text = ""
        if not text:
            return
        self._last_feed = time.monotonic()
        self._sb.archive(strip_ansi(text))
        self._idle.stop()
        self._inq.append(text)
        self._inq_len += len(text)
        if self._inq_len > self._MAX_INQ:
            while self._inq_len > self._MAX_INQ and len(self._inq) > 1:
                self._inq_len -= len(self._inq.popleft())
        if not self._drain.isActive():
            self._drain.start()

    def _drain_tick(self):
        if not self._inq:
            self._drain.stop()
            if self._alive and self._need_prompt and not self._line:
                self._idle.start(35)
            return
        sb = self.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 2
        deadline = time.monotonic() + self._TICK_BUDGET
        self.setUpdatesEnabled(False)
        try:
            self._consume_pending_prompt()
            while self._inq and time.monotonic() < deadline:
                chunk = self._inq.popleft()
                self._inq_len -= len(chunk)
                if len(chunk) > self._SUB:
                    self._process(chunk[:self._SUB])
                    rest = chunk[self._SUB:]
                    self._inq.appendleft(rest)
                    self._inq_len += len(rest)
                else:
                    self._process(chunk)
        finally:
            self.setUpdatesEnabled(True)
        if at_bottom:
            sb.setValue(sb.maximum())

    def _process(self, text):
        run = []

        def flush():
            if run:
                self._out("".join(run))
                run.clear()

        for ch in text:
            if self._state == 0:
                if ch == "\x1b":
                    flush()
                    self._state = 1
                elif ch == "\r":
                    flush()
                    self._wc.movePosition(QTextCursor.StartOfBlock)
                elif ch == "\n":
                    flush()
                    self._wc.movePosition(QTextCursor.End)
                    self._wc.insertText("\n", self._fmt)
                elif ch == "\b":
                    flush()
                    self._wc.movePosition(QTextCursor.Left)
                elif ch == "\t":
                    run.append("    ")
                elif ord(ch) < 32:
                    pass
                else:
                    run.append(ch)
            elif self._state == 1:
                if ch == "[":
                    self._state = 2
                    self._csi = ""
                elif ch == "]":
                    self._state = 3
                    self._osc = ""
                else:
                    self._state = 0
            elif self._state == 2:
                if "\x40" <= ch <= "\x7e":
                    self._csi_dispatch(ch, self._csi)
                    self._state = 0
                else:
                    self._csi += ch
            elif self._state == 3:
                if ch == "\x07":
                    self._state = 0
                elif ch == "\x1b":
                    self._state = 4
                else:
                    self._osc += ch
            elif self._state == 4:
                if ch == "\\":
                    self._state = 0
                else:
                    self._state = 3
        flush()

    def _out(self, s):
        if not s:
            return
        if self._wc.atBlockEnd():
            self._wc.insertText(s, self._fmt)
            return
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
        elif final == "J":
            n = params or "0"
            if n in ("2", "3"):
                self._clear_viewport()
            elif n in ("0", ""):
                self._wc.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
                self._wc.removeSelectedText()
            elif n == "1":
                self._wc.movePosition(QTextCursor.Start, QTextCursor.KeepAnchor)
                self._wc.removeSelectedText()

    def _sgr(self, p):
        try:
            n = int(p)
        except ValueError:
            return
        if n == 0:
            self._fmt = QTextCharFormat()
            self._fmt.setFont(self.font())
            self._fmt.setForeground(QColor(_FG_DEFAULT))
            self._fmt.setBackground(QBrush(Qt.NoBrush))
        elif n == 1:
            f = self.font()
            f.setBold(True)
            self._fmt.setFont(f)
        elif n == 22:
            f = self.font()
            f.setBold(False)
            self._fmt.setFont(f)
        elif n == 39:
            self._fmt.setForeground(QColor(_FG_DEFAULT))
        elif n == 49:
            self._fmt.setBackground(QBrush(Qt.NoBrush))
        elif n in _ANSI:
            self._fmt.setForeground(QColor(_ANSI[n]))
        elif n in _BG_ANSI:
            self._fmt.setBackground(QColor(_BG_ANSI[n]))

    def _echo(self, s, color=None):
        self._wc.movePosition(QTextCursor.End)
        fmt = self._fmt
        if color:
            fmt = QTextCharFormat()
            fmt.setFont(self.font())
            fmt.setForeground(QColor(color))
        self._wc.insertText(s, fmt)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _prompt_if_needed(self):
        if not getattr(self, "_emulate_prompt", True):
            return
        if self._need_prompt:
            p_ansi = self._prompt_text()
            self._shown_prompt = strip_ansi(p_ansi)
            if not self._wc.atBlockStart():
                self._process("\n")
            self._process(p_ansi)
            self._wc.movePosition(QTextCursor.End)
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
            self._need_prompt = False

    def _sync_cursor(self):
        self._wc.movePosition(QTextCursor.End)
        offset = len(self._line) - self._cpos
        for _ in range(offset):
            self._wc.movePosition(QTextCursor.Left)
        self.setTextCursor(self._wc)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _redraw_line(self, old_len: int):
        self._wc.movePosition(QTextCursor.End)
        for _ in range(old_len):
            self._wc.deletePreviousChar()
        self._wc.insertText(self._line, self._fmt)
        self._sync_cursor()

    def _erase_input(self):
        old_len = len(self._line)
        self._line = ""
        self._cpos = 0
        self._redraw_line(old_len)

    def _set_line(self, new):
        self._prompt_if_needed()
        old_len = len(self._line)
        self._line = new
        self._cpos = len(new)
        self._redraw_line(old_len)

    @staticmethod
    def _columnize(cmd):
        s = cmd.strip()
        parts = s.split()
        if not parts or parts[0] != "ls":
            return cmd
        if any(p.startswith("-") for p in parts[1:]):
            return cmd
        if any(c in s for c in "|<>;&`$()"):
            return cmd
        return "ls -C" + s[2:]

    def keyPressEvent(self, event):
        if not self._send:
            return
        mods, key = event.modifiers(), event.key()
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)

        if ctrl and shift and key == Qt.Key_C:
            self.copy()
            return
        if (ctrl and shift and key == Qt.Key_V) or (ctrl and key == Qt.Key_V) or (shift and key == Qt.Key_Insert):
            self._paste_into_line()
            return
        if ctrl and key == Qt.Key_Insert:
            self.copy()
            return
        if ctrl and key in (Qt.Key_Plus, Qt.Key_Equal):
            self.bump_font(1)
            return
        if ctrl and key == Qt.Key_Minus:
            self.bump_font(-1)
            return
        if not self._alive:
            return
        if ctrl and key == Qt.Key_C:
            if self.textCursor().hasSelection():
                self.copy()
                return
            self._send(b"\x03")
            self._echo("^C\n")
            if self._interrupt and (time.monotonic() - self._last_feed) < 2.0:
                self._interrupt()
            self._line = ""
            self._cpos = 0
            self._need_prompt = True
            self._idle.start(40)
            self._move_caret_end()
            return
        if ctrl and key == Qt.Key_L:
            self.clear()
            return

        if key in (Qt.Key_Return, Qt.Key_Enter):
            emulate = getattr(self, "_emulate_prompt", True)
            cmd = self._line

            if cmd.strip():
                self._history.append(cmd)
            self._hidx = len(self._history)
            self._last_feed = time.monotonic()

            if emulate:
                self._prompt_if_needed()
                self._echo("\n")
                self._sb.archive(strip_ansi(self._prompt_text()) + cmd + "\n")
                if cmd.strip() and cmd.strip().split()[0] == "cd":
                    self._apply_cd(cmd)
                to_send = (self._columnize(cmd) + "\n").encode("utf-8")
                try:
                    self._send(to_send)
                except Exception:
                    pass
                self._line = ""
                self._cpos = 0
                self._need_prompt = True
                self._idle.start(40)
            else:
                # Local shell (PowerShell or CMD)
                self._echo("\n")
                self._sb.archive(cmd + "\n")
                self._pending_echo = cmd
                self._pending_echo = cmd if cmd else "\r\n"
                try:
                    self._send((cmd + "\r\n").encode("utf-8"))
                except Exception:
                    pass
                self._line = ""
                self._cpos = 0
                self._need_prompt = False

            self._move_caret_end()
            return

        if key == Qt.Key_Left:
            if ctrl:
                p = self._cpos
                while p > 0 and self._line[p - 1].isspace():
                    p -= 1
                while p > 0 and not self._line[p - 1].isspace():
                    p -= 1
                self._cpos = p
            else:
                if self._cpos > 0:
                    self._cpos -= 1
            self._sync_cursor()
            return

        if key == Qt.Key_Right:
            if ctrl:
                p = self._cpos
                while p < len(self._line) and not self._line[p].isspace():
                    p += 1
                while p < len(self._line) and self._line[p].isspace():
                    p += 1
                self._cpos = p
            else:
                if self._cpos < len(self._line):
                    self._cpos += 1
            self._sync_cursor()
            return

        if key == Qt.Key_Home:
            self._cpos = 0
            self._sync_cursor()
            return

        if key == Qt.Key_End:
            self._cpos = len(self._line)
            self._sync_cursor()
            return

        if ctrl and key == Qt.Key_W:
            p = self._cpos
            while p > 0 and self._line[p - 1].isspace():
                p -= 1
            while p > 0 and not self._line[p - 1].isspace():
                p -= 1
            old_len = len(self._line)
            self._line = self._line[:p] + self._line[self._cpos:]
            self._cpos = p
            self._redraw_line(old_len)
            return

        if key == Qt.Key_Backspace:
            if self._cpos > 0:
                old_len = len(self._line)
                self._line = self._line[:self._cpos - 1] + self._line[self._cpos:]
                self._cpos -= 1
                self._redraw_line(old_len)
            return

        if key == Qt.Key_Delete:
            if self._cpos < len(self._line):
                old_len = len(self._line)
                self._line = self._line[:self._cpos] + self._line[self._cpos + 1:]
                self._redraw_line(old_len)
            return

        if key == Qt.Key_Escape:
            old_len = len(self._line)
            self._line = ""
            self._cpos = 0
            self._redraw_line(old_len)
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
        if key in (Qt.Key_Tab, Qt.Key_Backtab):
            self._do_complete(reverse=(shift or key == Qt.Key_Backtab))
            return

        self._clear_tab_cycle()

        text = event.text()

        if text and text.isprintable():
            self._idle.stop()
            if getattr(self, "_emulate_prompt", True):
                self._prompt_if_needed()
            old_len = len(self._line)
            self._line = self._line[:self._cpos] + text + self._line[self._cpos:]
            self._cpos += len(text)
            self._redraw_line(old_len)
            return

    def _paste_into_line(self):
        txt = QApplication.clipboard().text()
        if not txt:
            return
        txt = txt.replace("\r\n", "\n").replace("\r", "\n")
        emulate = getattr(self, "_emulate_prompt", True)
        if "\n" in txt:
            lines = txt.split("\n")
            for i, ln in enumerate(lines):
                if i < len(lines) - 1:
                    if emulate:
                        self._prompt_if_needed()
                        self._echo(ln + "\n")
                        self._sb.archive(strip_ansi(self._prompt_text()) + ln + "\n")
                        self._last_feed = time.monotonic()
                        self._send((self._columnize(ln) + "\n").encode("utf-8"))
                        self._need_prompt = True
                    else:
                        self._echo(ln + "\n")
                        self._sb.archive(ln + "\n")
                        self._last_feed = time.monotonic()
                        self._send((ln + "\r\n").encode("utf-8"))
                        self._need_prompt = False
                    self._line = ""
                    self._cpos = 0
                else:
                    if ln:
                        if emulate:
                            self._prompt_if_needed()
                        old_len = len(self._line)
                        self._line = self._line[:self._cpos] + ln + self._line[self._cpos:]
                        self._cpos += len(ln)
                        self._redraw_line(old_len)
        else:
            if emulate:
                self._prompt_if_needed()
            old_len = len(self._line)
            self._line = self._line[:self._cpos] + txt + self._line[self._cpos:]
            self._cpos += len(txt)
            self._redraw_line(old_len)
        self._move_caret_end()

    def _menu(self, pos):
        ico = theme.emoji_icon
        m = QMenu(self)
        a = m.addAction(ico("📋"), "Copy")
        a.setEnabled(self.textCursor().hasSelection())
        a.triggered.connect(self.copy)
        m.addAction(ico("🗂"), "Copy all", lambda: (self.selectAll(), self.copy()))
        m.addAction(ico("📥"), "Paste", self._paste_into_line)
        m.addAction(ico("🔲"), "Select All", self.selectAll)
        m.addSeparator()

        keys = m.addMenu(ico("⌨"), "Send key")
        for label, data in (
            ("Enter (\\n)", b"\n"), ("Tab (\\t)", b"\t"),
            ("Esc", b"\x1b"), ("Ctrl+C", b"\x03"),
            ("Ctrl+D (EOF)", b"\x04"), ("Ctrl+Z", b"\x1a"),
            ("Up", b"\x1b[A"), ("Down", b"\x1b[B"),
            ("Backspace", b"\x7f")
        ):
            keys.addAction(label, lambda *_, d=data: self._send(d) if self._send else None)

        m.addSeparator()
        m.addAction(ico("💾"), "Save full output to file…", self._save_output)
        m.addAction(ico("🧹"), "Clear", self.clear)
        m.exec_(self.viewport().mapToGlobal(pos))

    def _save_output(self):
        from PyQt5.QtWidgets import QFileDialog
        from .fileutil import download_path, saved_dialog
        default = download_path("turboadb-shell-" + time.strftime("%Y%m%d-%H%M%S") + ".log")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save terminal output", default,
            "Log files (*.log);;Text files (*.txt);;All files (*)"
        )
        if path:
            self.save_output(path)
            saved_dialog(self, path, "terminal output")

    def save_output(self, path):
        self._sb.save_to(path)

    def clear(self):
        super().clear()
        self._sb.reset()
        self._inq.clear()
        self._inq_len = 0
        self._drain.stop()
        self._wc = QTextCursor(self.document())
        self._wc.movePosition(QTextCursor.End)
        self._line = ""
        self._cpos = 0
        self._pending_echo = None
        self._state = 0
        self._csi = ""
        self._osc = ""
        self._need_prompt = True

    def close_archive(self):
        self._drain.stop()
        self._sb.close()
