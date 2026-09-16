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
import posixpath
import re
import shlex
import time
import weakref
from collections import deque

from PyQt5.QtCore import Qt, QTimer, QEvent, pyqtSignal
from PyQt5.QtGui import QFont, QTextCursor, QTextCharFormat, QColor, QBrush
from PyQt5.QtWidgets import QAbstractSlider, QPlainTextEdit, QMenu, QApplication

from ..results import strip_ansi
from . import settings as settings_mod, theme
from .scrollback import Scrollback

_ANSI = theme.ANSI_FG
_BG_ANSI = theme.ANSI_BG
_LOCAL_PS_PROMPT = re.compile(r"(?m)^(PS )([^\r\n>]*)(> ?)")
_LOCAL_CMD_PROMPT = re.compile(r"(?m)^([A-Za-z]:[^\r\n>]*)(> ?)")

# Complete escape sequences (CSI, OSC/DCS/APC/PM/SOS strings, nF/Fp/Fe escapes)
# removed from the plain-text archive.
_ARCHIVE_ESC_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[PX^_][^\x1b]*\x1b\\"
    r"|[ -/]*[0-~])"
)
# An escape sequence cut off at the end of a chunk (its rest arrives next read).
_ESC_TAIL_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*"
    r"|[\]PX^_](?:[^\x07\x1b]|\x1b(?!\\))*\x1b?"
    r"|[ -/]*)\Z"
)
_ESC_CARRY_MAX = 4096
# xterm 256-colour cube levels (indices 16-231).
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
# A local shell that never echoes the typed command must not make the console
# swallow matching output forever.
_PENDING_ECHO_TTL_S = 2.0
_CD_FAILED_RE = re.compile(
    r"\bcd: .*(?:No such file|Not a directory|Permission denied|can't cd)", re.IGNORECASE
)
_CD_REVERT_TTL_S = 5.0
_COMMAND_SPLIT_RE = re.compile(r"\s*(?:;|&&|\|\||\|)\s*")


class AnsiConsole(QPlainTextEdit):
    # Emitted with the new point size whenever this console's zoom changes.
    font_size_changed = pyqtSignal(int)
    # Every live console: A+/A− or Ctrl+wheel in one terminal resizes them all,
    # so Android shell, PowerShell and CMD always share one text size.
    _live = weakref.WeakSet()

    def __init__(self, send_fn=None, parent=None):
        super().__init__(parent)
        AnsiConsole._live.add(self)
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
        # Qt reports the inherited widget font after a stylesheet is applied,
        # not necessarily the terminal's visible CSS size.  Keep the explicit
        # zoom level so A+/A−, Ctrl+plus/minus and Ctrl+wheel always move one
        # step from the *currently displayed* size.
        self._font_size = size
        font = QFont(fam, size)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        self.document().setDefaultFont(font)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._set_console_palette()
        self._update_stylesheet(fam, size)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

        self._prompt_provider_fn = None

        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._wc = QTextCursor(self.document())
        self._wc.movePosition(QTextCursor.End)
        # Output formats carry colour/weight only — never a concrete font — so
        # every character inherits the document font and a zoom is a cheap
        # default-font change instead of a whole-document format rewrite.
        self._fmt = QTextCharFormat()
        self._fmt.setForeground(QColor(self._fg_default))
        self._state = 0
        self._csi = ""
        self._osc = ""
        self._archive_carry = ""      # an escape split across output chunks
        self._feed_at_line_start = True
        # cooked line-editing state
        self._line = ""
        self._cpos = 0
        self._pending_echo = None
        self._pending_echo_at = 0.0
        self._need_prompt = True
        self._history = []
        self._hidx = 0
        # emulated Android shell prompt (device shell over a pipe prints no PS1)
        self._host = ""
        self._root = False
        self._cwd = "/"
        self._prev_cwd = "/"        # for ``cd -``
        self._cd_revert = None      # (cwd, prev_cwd, time) until a cd is known to work
        self._alive = True          # False after the shell dies (reboot/unplug)
        # ``Latest`` is a persistent preference, not a scroll lock.  A user
        # must always be able to inspect earlier output while it remains on.
        self._follow_latest = False
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

    def showEvent(self, event):
        """Reopen a checked terminal at its newest output after view changes."""
        super().showEvent(event)
        if self._follow_latest:
            QTimer.singleShot(0, self._pin_to_latest_if_following)

    def _set_console_palette(self):
        """Use the shared terminal colours.

        A terminal stays charcoal in both themes: a light terminal was jarring
        and made shell output harder to scan (see theme.py).
        """
        self._term_bg = theme.TERM_BG
        self._fg_default = theme.TERM_FG
        self._prompt_color = theme.TERM_PROMPT
        self._selection_bg = theme.TERM_SELECTION

    def refresh_theme(self):
        """Refresh inline terminal colours after a live theme switch."""
        self._set_console_palette()
        self._update_stylesheet()
        self._fmt.setForeground(QColor(self._fg_default))

    def _update_stylesheet(self, fam=None, size=None):
        if fam is None:
            fam = self.font().family() or "Consolas"
        if size is None:
            size = self._font_size
        self.setStyleSheet(
            f"QPlainTextEdit {{"
            f"font-family: '{fam}', 'Cascadia Code', 'Consolas', monospace;"
            f"font-size: {size}pt;"
            f"background: {self._term_bg};"
            f"color: {self._fg_default};"
            f"border: none;"
            f"selection-background-color: {self._selection_bg};"
            f"selection-color: {theme.TERM_SELECTION_TEXT};"
            f"}}"
        )

    def bump_font(self, step):
        current = self._font_size
        size = max(settings_mod.FONT_SIZE_MIN, min(settings_mod.FONT_SIZE_MAX, current + step))
        if size == current:
            return
        for console in list(AnsiConsole._live):
            try:
                console.set_font_size(size)
            except RuntimeError:  # its C++ widget was already deleted
                AnsiConsole._live.discard(console)
        # One debounced, off-thread write however fast the wheel spins.
        settings_mod.set_later("term_font_size", size)

    def set_font_size(self, size: int) -> None:
        """Show this console's text at *size* points (no settings write)."""
        if size == self._font_size:
            return
        self._font_size = size
        f = self.font()
        f.setPointSize(size)
        self.setFont(f)
        # Character formats never pin a font size (see ``_fmt``), so changing
        # the document default font resizes all existing ANSI-coloured output
        # without rewriting every character's format per zoom notch.
        self.document().setDefaultFont(f)
        self._update_stylesheet(size=size)
        self.font_size_changed.emit(size)

    def font_size(self) -> int:
        """Return the visible terminal zoom level in points."""
        return int(self._font_size)

    def _move_caret_end(self):
        if self.textCursor().hasSelection():
            return
        c = self.textCursor()
        c.movePosition(QTextCursor.End)
        self.setTextCursor(c)

    def _idle_prompt(self):
        if not self._emulate_prompt:
            return
        if self._alive and self._need_prompt and not self._line:
            self._prompt_if_needed()
            self._move_caret_end()

    def _consume_pending_prompt(self):
        """If a waiting prompt is currently shown and the user hasn't typed, remove
        it so newly-arrived output doesn't get appended after a stray prompt."""
        if not self._emulate_prompt:
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

    # QPlainTextEdit installs its own standard clipboard shortcuts.  This
    # terminal owns input editing, so claim those shortcuts before Qt can route
    # them to an inherited read/edit action.  That makes Ctrl+C/Ctrl+V, the
    # context menu and the toolbar behave identically in Android Shell as well
    # as local PowerShell/CMD.
    def event(self, event):
        if event.type() == QEvent.ShortcutOverride:
            mods, key = event.modifiers(), event.key()
            ctrl = bool(mods & Qt.ControlModifier)
            shift = bool(mods & Qt.ShiftModifier)
            if (
                (ctrl and key in (Qt.Key_C, Qt.Key_V, Qt.Key_X, Qt.Key_A, Qt.Key_Insert))
                or (ctrl and shift and key in (Qt.Key_C, Qt.Key_V))
                or (shift and key == Qt.Key_Insert)
            ):
                event.accept()
                return True
        return super().event(event)

    def copy(self):
        """Copy selected output through the terminal-safe clipboard path."""
        return self._copy_selection()

    def cut(self):
        """Cut the editable current command, or safely copy old output."""
        return self._cut_selection()

    def paste(self):
        """Paste into the cooked command line rather than editing scrollback."""
        self._paste_into_line()

    def scroll_to_latest(self):
        """Move the viewport to the most recent terminal output on request.

        Output normally follows only while the user is already at the bottom;
        this explicit action is the safe way to return after reviewing older
        output without changing the current command line or selection.
        """
        scrollbar = self.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        # Toolbar clicks otherwise leave focus on the button, which makes the
        # next keystroke appear to be lost.  Returning focus does not move the
        # text cursor or alter a selected block of output.
        self.setFocus(Qt.OtherFocusReason)

    def set_follow_latest(self, enabled: bool):
        """Select whether this terminal should reopen at its newest output.

        Selecting Latest jumps to the tail once.  It deliberately does *not*
        trap the scrollbar there: scrolling upward is how a user reviews a
        command's output.  Fresh output follows naturally only while the
        viewport is already at the bottom.
        """
        self._follow_latest = bool(enabled)
        if self._follow_latest:
            self.scroll_to_latest()

    def _pin_to_latest_if_following(self):
        if self._follow_latest:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

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

        self._apply_completion_result(newline, opts)

    def apply_completion_result(self, query: str, newline, opts) -> bool:
        """Apply an asynchronous completion only if its input is still current.

        Remote Android completion deliberately runs off the GUI thread.  A
        result that arrives after the user has typed something else is stale and
        must never overwrite that newer input line.
        """
        if not self._alive or query != self._line:
            return False
        self._apply_completion_result(newline, opts)
        return True

    def _apply_completion_result(self, newline, opts):
        """Render one completion-provider result for the current input line."""

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

            # Multiple options: apply the provider's normalised active token
            # even when it is shorter.  For example the local terminals accept
            # ``cd /t`` as a friendly relative prefix and normalise it to
            # ``cd t`` before showing matching folders.  The old length-only
            # test left the slash in place, so the next Tab inserted a malformed
            # Windows path and completion appeared broken in CMD/PowerShell.
            if newline is not None and newline != self._line:
                self._set_line(newline)

            # Display options cleanly
            formatted = self._format_columns(opts)
            self._echo("\n" + formatted + "\n")
            if self._emulate_prompt:
                self._need_prompt = True
                self._prompt_if_needed()
            elif self._prompt_provider_fn:
                prompt = self._prompt_provider_fn()
                if prompt:
                    self._echo(prompt, self._prompt_color)
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


    def set_alive(self, alive: bool, *, show_disconnect_notice: bool = True):
        """Mark the shell connected/disconnected.

        ``show_disconnect_notice`` is false for a deliberate ADB-server
        restart: the caller can then show one concise recovery message instead
        of an alarming red disconnect line followed by a second status line.
        """
        if alive:
            self._alive = True
            self._line = ""
            if self._emulate_prompt:
                self._need_prompt = True
                self.show_prompt()
            else:
                self._need_prompt = False
            self._move_caret_end()
        elif not alive and self._alive:
            self._alive = False
            self._idle.stop()
            if show_disconnect_notice:
                self._echo("\n[shell disconnected]\n", theme.ECHO_ERROR)

    def set_emulate_prompt(self, enabled: bool):
        """Enable or disable synthetic Android shell prompt emulation."""
        self._emulate_prompt = bool(enabled)
        if not self._emulate_prompt:
            self._need_prompt = False
            self._shown_prompt = ""

    def set_prompt(self, identity, root=False):
        """Show *identity* — the device's ``user@host``, or just a host — in the prompt."""
        self._host = identity or ""
        self._root = bool(root)

    def _prompt_text(self):
        """Return the device's own compact prompt: ``user@host:cwd $``.

        The shell already tells the user where they are.  A clock and a second
        decorated path made every prompt long and obscured command output.
        """
        cwd = self._cwd or "/"
        marker = "#" if self._root else "$"
        user, _, host = (self._host or "android").rpartition("@")
        user = user or ("root" if self._root else "shell")
        return (
            f"\x1b[1;95m{user}@\x1b[1;96m{host or 'android'}"
            f"\x1b[90m:\x1b[1;93m{cwd} "
            f"\x1b[1;92m{marker}\x1b[0m "
        )

    @staticmethod
    def _style_local_prompts(text: str) -> str:
        """Colour the real local-shell prompt without replacing its semantics."""
        def powershell(match):
            return (
                f"\x1b[1;96m{match.group(1)}\x1b[1;93m{match.group(2)}"
                f"\x1b[1;92m{match.group(3)}\x1b[0m"
            )

        def cmd(match):
            return (
                f"\x1b[1;93m{match.group(1)}\x1b[1;92m{match.group(2)}\x1b[0m"
            )

        text = _LOCAL_PS_PROMPT.sub(powershell, text)
        return _LOCAL_CMD_PROMPT.sub(cmd, text)

    def _style_local_prompts_stream(self, text: str) -> str:
        """Style prompts in a streamed chunk.

        ``(?m)^`` treats the start of every chunk as a line start; when the
        previous chunk ended mid-line, the first (partial) line of this chunk
        is not a prompt and must be left alone."""
        if self._feed_at_line_start:
            styled = self._style_local_prompts(text)
        else:
            nl = text.find("\n")
            styled = text if nl < 0 else text[: nl + 1] + self._style_local_prompts(text[nl + 1:])
        self._feed_at_line_start = text.endswith("\n")
        return styled

    def _archive_text(self, text: str) -> str:
        """Plain text for the history archive, escape-safe across chunks.

        ``strip_ansi`` per chunk leaked the halves of an escape sequence that
        was split between two reads into the saved log."""
        if self._archive_carry:
            text = self._archive_carry + text
            self._archive_carry = ""
        tail = _ESC_TAIL_RE.search(text)
        if tail is not None and len(text) - tail.start() <= _ESC_CARRY_MAX:
            self._archive_carry = text[tail.start():]
            text = text[: tail.start()]
        return strip_ansi(_ARCHIVE_ESC_RE.sub("", text))

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
        """Track the device working directory for the emulated prompt.

        Handles quoted names (``cd "My Dir"``), ``cd -``, option flags, and a
        compound line (``cd foo; ls`` / ``cd foo && ls``: only the leading cd
        matters).  A failed cd is reverted when the shell reports the error
        (see ``_check_cd_failure``)."""
        first = _COMMAND_SPLIT_RE.split(cmd.strip(), maxsplit=1)[0]
        try:
            tokens = shlex.split(first)
        except ValueError:  # unbalanced quote: fall back to whitespace split
            tokens = first.split()
        if not tokens or tokens[0] != "cd":
            return
        arg = ""
        args = tokens[1:]
        for index, token in enumerate(args):
            if token == "--":
                arg = args[index + 1] if index + 1 < len(args) else ""
                break
            if token.startswith("-") and token != "-":
                continue  # -P / -L
            arg = token
            break
        old = self._cwd
        if not arg or arg == "~":
            new = "/"
        elif arg == "-":
            new = self._prev_cwd or old
        elif arg.startswith("~/"):
            new = posixpath.normpath("/" + arg[2:])
        elif arg.startswith("/"):
            new = posixpath.normpath(arg)
        else:
            new = posixpath.normpath(posixpath.join(old, arg))
        new = new or "/"
        self._cd_revert = (old, self._prev_cwd, time.monotonic())
        if new != old:
            self._prev_cwd = old
        self._cwd = new

    def _check_cd_failure(self, text):
        """Undo the tracked cwd if the shell rejected the last ``cd``."""
        old, prev, when = self._cd_revert
        if time.monotonic() - when > _CD_REVERT_TTL_S:
            self._cd_revert = None
            return
        if _CD_FAILED_RE.search(strip_ansi(text)):
            self._cwd, self._prev_cwd = old, prev
            self._cd_revert = None

    def feed(self, data):
        """Enqueue text and flush to disk archive; renders asynchronously in slices."""
        text = data if isinstance(data, str) else self._decoder.decode(data)
        if not text:
            return
        pe = self._pending_echo
        if pe and time.monotonic() - self._pending_echo_at > _PENDING_ECHO_TTL_S:
            pe = self._pending_echo = None
        if pe:
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
            else:
                # The shell did not echo the command: stop waiting, or later
                # output that happens to start with it would be swallowed.
                self._pending_echo = None
        if not text:
            return
        if self._cd_revert is not None:
            self._check_cd_failure(text)
        if not self._emulate_prompt:
            text = self._style_local_prompts_stream(text)
        self._last_feed = time.monotonic()
        self._sb.archive(self._archive_text(text))
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
                    # A terminal backspace stops at column 0; it never wraps
                    # onto the end of the previous line.
                    if not self._wc.atBlockStart():
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
                elif ch in "]PX^_":
                    # OSC, or a DCS/SOS/PM/APC string: ignored up to BEL / ST
                    self._state = 3
                    self._osc = ""
                elif " " <= ch <= "/":
                    # an intermediate byte (e.g. ``ESC ( B`` charset select):
                    # the sequence ends at the following final byte
                    self._state = 5
                else:
                    self._state = 0
            elif self._state == 5:
                if not (" " <= ch <= "/"):
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
            self._sgr_params(params)
        elif final == "K":
            n = params or "0"
            if n in ("0", ""):
                self._wc.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                self._wc.removeSelectedText()
            elif n == "1":
                # erase from the start of the line through the cursor, in place
                pos = self._wc.position()
                block = self._wc.block()
                start = block.position()
                count = min(pos - start + 1, block.length() - 1)
                if count > 0:
                    eraser = QTextCursor(self.document())
                    eraser.setPosition(start)
                    eraser.setPosition(start + count, QTextCursor.KeepAnchor)
                    eraser.insertText(" " * count, QTextCharFormat())
                self._wc.setPosition(pos)
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

    def _sgr_params(self, params):
        """Apply one SGR parameter list in order.

        ``38;5;n`` / ``48;5;n`` (256 colours) and ``38;2;r;g;b`` (truecolour),
        plus their ITU colon forms (``38:5:n``, ``38:2::r:g:b``), are single
        attributes — applying their numbers one by one used to turn e.g. the
        ``5`` or a blue value of ``1`` into unrelated attributes."""
        parts = params.split(";") if params else ["0"]
        i = 0
        while i < len(parts):
            part = parts[i]
            if ":" in part:
                self._sgr_colon(part)
                i += 1
                continue
            n = self._sgr_int(part)
            if n in (38, 48, 58):
                mode = self._sgr_int(parts[i + 1]) if i + 1 < len(parts) else None
                if mode == 5 and i + 2 < len(parts):
                    self._extended_color(n, 5, [self._sgr_int(parts[i + 2])])
                    i += 3
                    continue
                if mode == 2 and i + 4 < len(parts):
                    self._extended_color(n, 2, [self._sgr_int(p) for p in parts[i + 2:i + 5]])
                    i += 5
                    continue
                i += 2  # malformed selector: skip it rather than misapply it
                continue
            if n is not None:
                self._sgr(n)
            i += 1

    @staticmethod
    def _sgr_int(value):
        if value in ("", None):
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _sgr_colon(self, part):
        fields = part.split(":")
        head = self._sgr_int(fields[0])
        if head in (38, 48, 58) and len(fields) >= 3:
            mode = self._sgr_int(fields[1])
            if mode == 5:
                self._extended_color(head, 5, [self._sgr_int(fields[2])])
            elif mode == 2 and len(fields) >= 5:
                # 38:2:<colour-space>:r:g:b — the colour-space id is optional
                self._extended_color(head, 2, [self._sgr_int(v) for v in fields[-3:]])
        elif head is not None:
            self._sgr(head)  # e.g. 4:3 (curly underline) — keep the base attribute

    @staticmethod
    def _palette_256(index, background):
        if index < 16:
            table = _BG_ANSI if background else _ANSI
            if index < 8:
                key = (40 if background else 30) + index
            else:
                key = (100 if background else 90) + index - 8
            return QColor(table[key]) if key in table else None
        if index < 232:
            index -= 16
            return QColor(
                _CUBE_LEVELS[index // 36], _CUBE_LEVELS[(index // 6) % 6], _CUBE_LEVELS[index % 6]
            )
        level = 8 + (index - 232) * 10
        return QColor(level, level, level)

    def _extended_color(self, target, mode, values):
        if target == 58 or any(v is None for v in values):  # underline colour: unsupported
            return
        background = target == 48
        if mode == 5:
            if not 0 <= values[0] <= 255:
                return
            color = self._palette_256(values[0], background)
        else:
            r, g, b = (max(0, min(255, v)) for v in values)
            color = QColor(r, g, b)
        if color is None:
            return
        if background:
            self._fmt.setBackground(color)
        else:
            self._fmt.setForeground(color)

    def _sgr(self, p):
        n = self._sgr_int(p)
        if n is None:
            return
        if n == 0:
            self._fmt = QTextCharFormat()
            self._fmt.setForeground(QColor(self._fg_default))
            self._fmt.setBackground(QBrush(Qt.NoBrush))
        elif n == 1:
            self._fmt.setFontWeight(QFont.Bold)
        elif n == 22:
            self._fmt.setFontWeight(QFont.Normal)
        elif n == 39:
            self._fmt.setForeground(QColor(self._fg_default))
        elif n == 49:
            self._fmt.setBackground(QBrush(Qt.NoBrush))
        elif n in _ANSI:
            self._fmt.setForeground(QColor(_ANSI[n]))
        elif n in _BG_ANSI:
            self._fmt.setBackground(QColor(_BG_ANSI[n]))

    def _echo(self, s, color=None):
        scrollbar = self.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
        self._wc.movePosition(QTextCursor.End)
        fmt = self._fmt
        if color:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
        self._wc.insertText(s, fmt)
        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _prompt_if_needed(self):
        if not self._emulate_prompt:
            return
        if self._need_prompt:
            scrollbar = self.verticalScrollBar()
            at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
            p_ansi = self._prompt_text()
            self._shown_prompt = strip_ansi(p_ansi)
            if not self._wc.atBlockStart():
                self._process("\n")
            self._process(p_ansi)
            self._wc.movePosition(QTextCursor.End)
            if at_bottom:
                scrollbar.setValue(scrollbar.maximum())
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

    def _copy_selection(self) -> bool:
        """Copy selected terminal text and report whether there was a selection."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return False
        # Use the application's clipboard explicitly.  Calling the inherited
        # ``copy`` is normally equivalent, but this remains reliable when the
        # console owns key handling and does not delegate to QPlainTextEdit.
        QApplication.clipboard().setText(cursor.selectedText().replace("\u2029", "\n"))
        return True

    def _cut_selection(self) -> bool:
        """Cut only editable command input; terminal history stays immutable.

        Ctrl+X on historical output acts like a safe copy.  If the selection is
        wholly inside the command currently being edited, it is also removed
        from that command, matching the usual Cut shortcut without allowing a
        user to accidentally erase received terminal output.
        """
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return False
        self._copy_selection()

        input_end = self.document().characterCount() - 1
        input_start = input_end - len(self._line)
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        if start < input_start or end > input_end:
            self._move_caret_end()
            return True

        old_len = len(self._line)
        rel_start = start - input_start
        rel_end = end - input_start
        self._line = self._line[:rel_start] + self._line[rel_end:]
        self._cpos = rel_start
        self._redraw_line(old_len)
        return True

    def _scroll_key(self, key, ctrl) -> bool:
        """PageUp/PageDown and Ctrl+Home/End scroll the scrollback (they used to
        be swallowed because key handling never reaches QPlainTextEdit)."""
        actions = {
            Qt.Key_PageUp: QAbstractSlider.SliderPageStepSub,
            Qt.Key_PageDown: QAbstractSlider.SliderPageStepAdd,
        }
        if ctrl:
            actions[Qt.Key_Home] = QAbstractSlider.SliderToMinimum
            actions[Qt.Key_End] = QAbstractSlider.SliderToMaximum
        action = actions.get(key)
        if action is None:
            return False
        self.verticalScrollBar().triggerAction(action)
        return True

    def _send_interrupt(self):
        """Stop the running command: the owner's reliable interrupt when there
        is one (keyboard Ctrl+C and the context menu share this path)."""
        if self._interrupt:
            self._interrupt()
        else:
            if self._send:
                self._send(b"\x03")
            self._echo("^C\n")
        self._line = ""
        self._cpos = 0
        self._need_prompt = True
        self._idle.start(40)
        self._move_caret_end()

    def keyPressEvent(self, event):
        mods, key = event.modifiers(), event.key()
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        if self._scroll_key(key, ctrl):
            return
        if not self._send:
            return

        # Standard clipboard bindings must take priority over terminal control
        # keys.  Ctrl+C still sends SIGINT only when there is no selection.
        if (ctrl and shift and key == Qt.Key_C) or (ctrl and key == Qt.Key_Insert):
            self._copy_selection()
            return
        if (ctrl and shift and key == Qt.Key_V) or (ctrl and key == Qt.Key_V) or (shift and key == Qt.Key_Insert):
            self._paste_into_line()
            return
        if ctrl and key == Qt.Key_X:
            self._cut_selection()
            return
        if ctrl and key == Qt.Key_A:
            self.selectAll()
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
            if self._copy_selection():
                return
            # An embedded process uses redirected pipes, so a bare ``\x03``
            # is not a reliable Ctrl+C on Windows.  Shell owners provide a
            # real interrupt that ends/reopens the process; retain the byte
            # only for consoles without such an owner.
            self._send_interrupt()
            return
        if ctrl and key == Qt.Key_L:
            self.clear()
            return

        if key in (Qt.Key_Return, Qt.Key_Enter):
            emulate = self._emulate_prompt
            cmd = self._line

            if cmd.strip():
                self._history.append(cmd)
            self._hidx = len(self._history)
            self._last_feed = time.monotonic()

            if emulate:
                self._prompt_if_needed()
                self._echo("\n")
                self._sb.archive(strip_ansi(self._prompt_text()) + cmd + "\n")
                self._cd_revert = None
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
                self._pending_echo = cmd if cmd else "\r\n"
                self._pending_echo_at = time.monotonic()
                self._feed_at_line_start = True
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
            if self._emulate_prompt:
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
        emulate = self._emulate_prompt
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
        # Only the on-screen scrollback is selectable; the complete history is
        # in "Save full output to file…".
        m.addAction(ico("🗂"), "Copy visible output", lambda: (self.selectAll(), self.copy()))
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
            if data == b"\x03":
                # same reliable stop as the keyboard shortcut
                keys.addAction(label, lambda *_: self._send_interrupt() if self._alive else None)
            else:
                keys.addAction(label, lambda *_, d=data: self._send(d) if self._send else None)

        m.addSeparator()
        m.addAction(ico("💾"), "Save full output to file…", self._save_output)
        m.addAction(ico("🧹"), "Clear", self.clear)
        m.exec_(self.viewport().mapToGlobal(pos))

    def _save_output(self):
        """Save the complete history off the UI thread (toast or real error)."""
        from .fileutil import save_output

        save_output(
            self,
            "Save terminal output",
            "turboadb-shell-" + time.strftime("%Y%m%d-%H%M%S") + ".log",
            self._sb.save_job(),
            what="terminal output",
        )

    def save_output(self, path):
        """Synchronously write the complete history to *path* (raises OSError)."""
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
        self._archive_carry = ""
        self._feed_at_line_start = True
        self._need_prompt = True

    def close_archive(self):
        self._drain.stop()
        self._sb.close()
