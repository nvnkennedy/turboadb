"""A fast, selectable terminal console for the interactive shells.

Built on QPlainTextEdit so it gets **native mouse text selection, copy/paste,
scrollback and smooth scrolling for free**. It runs the shell in **cooked
line-editing mode**: you type into the terminal with local echo and **one Enter
runs the command** — the whole line is sent to the shell's stdin at once. A
line never ends in a bare CR: adb.exe reads a piped stdin in text mode and
holds a trailing ``\\r`` until the next byte arrives. An incremental ANSI parser
colours the output and handles carriage-return / backspace / line-erase.

The document always ends with the *edit region*: the synthetic prompt (only for
the Android shell over a pipe) and the line being typed.  Output, notices and
echoes are written above it, so typing while a command streams never mixes
with the output, and editing the line never touches it.

Up/Down recall history; Ctrl+C interrupts; selection + copy work like any
terminal.  A multi-line paste runs line by line, each line only after the
previous one has finished, through the same path as Enter.

Colour the output does not bring itself: a device shell's plain prompt (its
parts in the theme's prompt colours) and logcat lines (by priority), see
:mod:`shell_colors`.  Cursor moves along the current line (progress bars)
are drawn in place.

On a device terminal (the owner calls :meth:`AnsiConsole.set_screen_input`),
a program that addresses the cursor (``top``, ``watch``, ``vi``, the
alternate screen, scroll regions, moves up and down) gets a screen: a
:class:`vtscreen.Screen` painted by :class:`screen_view.ScreenView` over the
scrollback, and every key goes straight to the program.  The screen goes
when the program leaves the alternate screen or the shell shows its prompt
again; a last frame on the main screen stays in the scrollback, as in a
terminal window."""

from __future__ import annotations

import codecs
import posixpath
import re
import shlex
import time
import weakref
from collections import deque

from PyQt5.QtCore import Qt, QTimer, QEvent, pyqtSignal
from PyQt5.QtGui import (
    QFont, QFontMetricsF, QKeyEvent, QTextCursor, QTextCharFormat, QColor, QBrush,
)
from PyQt5.QtWidgets import QAbstractSlider, QPlainTextEdit, QMenu, QApplication, QToolTip

from ..results import strip_ansi
from . import settings as settings_mod, shell_colors, theme, vtscreen
from .screen_view import ScreenView, screen_color
from .scrollback import Scrollback

_ANSI = theme.ANSI_FG
_BG_ANSI = theme.ANSI_BG
_LOCAL_PS_PROMPT = re.compile(r"(?m)^(PS )([^\r\n>]*)(> ?)")
_LOCAL_CMD_PROMPT = re.compile(r"(?m)^([A-Za-z]:[^\r\n>]*)(> ?)")


def _prompt_sgr(part: str) -> str:
    """The SGR escape that draws a prompt's *part* in its colour (see
    theme.TERM_PROMPT_COLORS), for the prompts the console styles itself."""
    color, bold = theme.TERM_PROMPT_COLORS[part]
    red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return f"\x1b[0;{'1;' if bold else ''}38;2;{red};{green};{blue}m"


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
# The longest OSC, DCS, SOS, PM or APC string: one that never ends (a window
# title sent without its BEL) swallowed all the output after it.  Any escape
# ends a string too, and starts a sequence of its own.
_STRING_MAX = 4096
# The fast path of AnsiConsole._process: a run of text without control
# characters, and complete sequences starting at an ESC (a CSI, a string
# ended by BEL, ST or the next escape, another escape); one cut off by the
# end of a chunk goes through the parser's states instead.
_PLAIN_RUN_RE = re.compile(r"[^\x00-\x1f]+")
_CSI_AT_RE = re.compile(r"\x1b\[([0-?]*[ -/]*)([@-~])")
_STRING_AT_RE = re.compile(
    r"\x1b[\]PX^_][^\x07\x1b]{0,%d}(?:\x07|\x1b\\|(?=\x1b[^\\]))" % _STRING_MAX
)
_ESC_AT_RE = re.compile(r"\x1b([ -/]*)([0-~])")
# A switch to or from the alternate screen (``ESC [ ? 1049 h`` ...): output
# before one may be dropped when a program's screen falls behind, never the
# switch itself (see AnsiConsole._drop_screen_backlog).
_ALT_SWITCH_RE = re.compile(r"\x1b\[\?(?:[0-9;]*;)?(?:1049|1047|47)(?:;[0-9;]*)?[hl]")
# Where output waiting for a program's screen may be cut: after a line break
# or where an escape sequence starts, never inside one.
_SCREEN_CUT_RE = re.compile(r"\n|(?=\x1b)")
# One escape sequence in a prompt or banner string; only SGR (``ESC[…m``) is applied.
_STYLE_ESC_RE = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# xterm 256-colour cube levels (indices 16-231).
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
# A local shell that never echoes the typed command must not make the console
# swallow matching output forever.
_PENDING_ECHO_TTL_S = 2.0
# The line break after a shell's echo of the submitted line.  adb.exe writes
# its stdout in text mode, so a device terminal's CR LF arrives as CR CR LF.
_ECHO_BREAK_RE = re.compile(r"\r*\n")
# How a local shell's last line ends while it waits for input: PowerShell
# (``PS C:\>``, ``>>``), CMD (``C:\>``, ``More?``), a device or Unix shell
# (``$``, ``#``) and REPLs such as Python's ``>>>``.
_PROMPT_END_RE = re.compile(r"(?:> ?|[$#] |^More\? ?)\Z")
_CD_FAILED_RE = re.compile(
    r"\bcd: .*(?:No such file|Not a directory|Permission denied|can't cd)", re.IGNORECASE
)
_CD_REVERT_TTL_S = 5.0
_COMMAND_SPLIT_RE = re.compile(r"\s*(?:;|&&|\|\||\|)\s*")


class _Marker:
    """A render-queue item that is not shell output.

    ``"text"``: our own text (a notice, a banner, a submitted line), as
    ``(text, format)`` runs and/or a document fragment.  ``"reset"``: a stream
    boundary.  ``"clear"``: a screen clear.  ``"prompt"``: the shell's prompt
    (see mark_prompt).  ``"end"``: a program's terminal is gone (see
    end_screen).  Queued with the output, so each is drawn exactly where it
    happened in the stream.
    """

    __slots__ = ("kind", "runs", "fragment", "fresh_line", "typed")

    def __init__(self, kind, runs=None, fragment=None, fresh_line=False):
        self.kind = kind
        self.runs = runs
        self.fragment = fragment
        self.fresh_line = fresh_line
        self.typed = None  # a line typed while a command ran (see _remember_typed)


def _doc_len(text: str) -> int:
    """Length of *text* in document positions (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


def _is_number(text: str) -> bool:
    """Whether *text* is a number in ASCII digits, as a CSI's parameters are:
    ``str.isdigit`` also takes ``²``, which ``int`` refuses."""
    return text.isdigit() and text.isascii()


def _count(params: str) -> int:
    """The first number of a CSI's parameters, 1 when it is missing or 0."""
    first = params.split(";", 1)[0].split(":", 1)[0]
    return int(first) if _is_number(first) and int(first) > 0 else 1


def _is_home(params: str) -> bool:
    """Whether a cursor position (CUP/HVP) is row 1, column 1."""
    parts = params.split(";")
    return len(parts) <= 2 and all(part in ("", "0", "1") for part in parts)


# Keys that send a sequence on a program's screen (see vtscreen.key_sequence).
_SCREEN_KEYS = {
    Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right",
    Qt.Key_Home: "home", Qt.Key_End: "end", Qt.Key_Insert: "insert", Qt.Key_Delete: "delete",
    Qt.Key_PageUp: "pageup", Qt.Key_PageDown: "pagedown",
    Qt.Key_Return: "enter", Qt.Key_Enter: "enter", Qt.Key_Backspace: "backspace",
    Qt.Key_Tab: "tab", Qt.Key_Backtab: "tab", Qt.Key_Escape: "escape",
    Qt.Key_F1: "f1", Qt.Key_F2: "f2", Qt.Key_F3: "f3", Qt.Key_F4: "f4", Qt.Key_F5: "f5",
    Qt.Key_F6: "f6", Qt.Key_F7: "f7", Qt.Key_F8: "f8", Qt.Key_F9: "f9", Qt.Key_F10: "f10",
    Qt.Key_F11: "f11", Qt.Key_F12: "f12",
}


class AnsiConsole(QPlainTextEdit):
    # Emitted with the new point size whenever this console's zoom changes.
    font_size_changed = pyqtSignal(int)
    # A program's screen shows (True) or went (False); see set_screen_input.
    screen_changed = pyqtSignal(bool)
    # (columns, rows): the screen was resized with the view.
    screen_resized = pyqtSignal(int, int)
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
        size = int(settings_mod.get("term_font_size") or settings_mod.DEFAULTS["term_font_size"])
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
        self._string_len = 0          # characters of an OSC/DCS string so far (see _STRING_MAX)
        self._esc_mid = ""            # an escape's intermediate bytes (``ESC ( 0``)
        self._skip_break = False      # a form feed cleared the screen: drop the next line break
        self._cr_pending = False      # a CR not applied yet (see _process)
        self._archive_carry = ""      # an escape split across output chunks
        self._feed_at_line_start = True
        # cooked line-editing state
        self._line = ""
        self._cpos = 0
        # The edit region ending the document, as drawn, in characters: the
        # line break before a synthetic prompt (output stopped mid-line), that
        # prompt, and the typed line.  _out_end() is where output goes.
        self._edit_sep = 0
        self._prompt_len = 0
        self._line_len = 0
        self._pending_echo = None
        self._pending_echo_at = 0.0
        self._echo_held = ""          # output held back while it matches _pending_echo
        self._at_prompt = None        # the owner's verdict (set_shell_at_prompt); None: guess
        self._send_key_fn = None      # context-menu keys (set_send_key_fn)
        self._key_offered_fn = None   # ... and which of them mean something now
        self._need_prompt = True
        # Lines drawn when typed while a command ran, which the shell may yet
        # read as commands and echo after its prompt: [text, cursor, marker].
        self._typed_ahead = deque()
        self._typing_ahead = False    # the owner's shell is not reading yet (set_typing_ahead)
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
        self._shown_prompt = ""     # the synthetic prompt currently displayed
        self._emulate_prompt = True
        self._feed_count = 0        # output chunks shown so far (echo excluded)
        self._submitted_at = 0.0    # monotonic time the last line was sent
        self._submitted_feeds = 0   # _feed_count when the last line was sent
        self._submitting = False    # inside send_fn for a submitted line
        self._submit_count = 0
        # True while the paste timer edits: leave the user's selection, caret
        # and scroll position alone, as streaming output does
        self._keep_view = False

        # multi-line paste: lines still to run, one at a time (see _pump_paste)
        self._paste_lines = deque()
        self._paste_tail = ""       # text after the last line break (edit line)
        self._paste_keys = deque()  # keys typed meanwhile: (key, modifiers, text)
        self._pasting = False
        self._paste_multi = False   # the paste spans more than one line
        self._paste_block_watch = False  # complete a PowerShell ``>>`` block
        self._paste_tail_open = False    # Enter on the left-over line ends the paste
        self._paste_timer = QTimer(self)
        self._paste_timer.setInterval(self._PASTE_TICK_MS)
        self._paste_timer.timeout.connect(self._pump_paste)

        # after a command's output goes idle, auto-show the next prompt
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self._idle_prompt)

        # ingestion decoupled from rendering
        self._inq = deque()         # queued output chunks (str) and _Markers awaiting render
        self._inq_len = 0           # characters of output in _inq
        self._dropped = 0           # characters dropped from the queue, not yet announced
        self._render_rate = self._RENDER_RATE_GUESS  # characters drawn per second (measured)
        self._drain = QTimer(self)
        self._drain.setInterval(15)
        self._drain.timeout.connect(self._drain_tick)

        # After Ctrl+C on a device terminal, output that arrives before the
        # terminal's echo of it waits here (see interrupt_output).
        self._held = []
        self._held_until = 0.0
        self._held_timer = QTimer(self)
        self._held_timer.setSingleShot(True)
        self._held_timer.timeout.connect(self._release_held)

        # The line being drawn (see _line_format and _colour_prompt): whether it
        # is plain text so far, in the default colours, and how it starts.
        self._logcat = shell_colors.LogcatLines()
        self._line_fmt = None       # its logcat colour, shown before it ended
        self._line_fmt_end = None   # (block, column) the text in that colour ends at
        self._line_clean = True
        self._line_head = ""
        self._fmt_plain = True
        self._level_formats = {}
        self._pad = 0               # columns the cursor stands past its line's end
        self._saved_column = None   # (block, column) of ESC 7 / CSI s
        self._home_pending = False  # a cursor-home that ED 2 would make a clear
        self._modes = {}            # private modes set before a screen was needed

        # A full-screen program's screen (see set_screen_input).
        self._screen_input = None
        self._screen = None
        self._screen_view = None
        self._screen_base = None    # where the rows it took over start in the document
        self._screen_seed = []      # (fragment, chars, attrs) of each row it took over
        self._screen_scrolled = []  # rows that scrolled off its top, not written yet
        self._screen_notes = []     # our own text meanwhile, drawn once it is gone
        self._screen_painted = -1

    _HISTORY_MAX = 1000           # lines Up/Down can recall
    _TYPED_AHEAD_MAX = 20         # typed-ahead lines followed (see forget_typed_line)
    _TICK_BUDGET = 0.030          # seconds of rendering per tick (keeps UI live)
    _SUB = 8 * 1024               # max chars handed to _process at once (~20 ms of work)
    # The ON-SCREEN backlog: output waiting to be drawn is capped at about
    # _MAX_LAG_S of drawing (at the rate this console draws), so the view
    # never falls far behind a flood; older undrawn output goes (the saved
    # history has all of it).  Never less than _MIN_INQ, never more than _MAX_INQ.
    _MAX_LAG_S = 1.0
    _MIN_INQ = 256 * 1024
    _MAX_INQ = 8 * 1024 * 1024
    _RENDER_RATE_GUESS = 1000000  # characters per second, until measured
    # After Ctrl+C on a device terminal: how long output waits for the
    # terminal's echo of it (``^C``) before it is drawn after all.
    _CTRL_C_ECHO_S = 1.0
    # Output that Ctrl+C would drop but that takes no longer than this to
    # draw is drawn after all: it never keeps anything scrolling.
    _CTRL_C_KEEP_S = 0.1

    # Paste pacing: how quiet the output must be before the next pasted line
    # runs, and the most one line may hold up the rest.
    _PASTE_TICK_MS = 20
    _PASTE_IDLE_S = 0.25          # Android shell (no echo, no prompt of its own)
    _PASTE_LOCAL_IDLE_S = 0.06    # PowerShell / CMD, after their prompt is back
    _PASTE_MAX_WAIT_S = 2.5
    # Quiet output after which the synthetic prompt shows (pipe mode).  Shorter
    # made it flicker between the lines of a streaming command.
    _PROMPT_IDLE_S = _PASTE_IDLE_S

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
        self._level_formats.clear()
        if self._screen_view is not None:
            self._screen_view.update()

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
        if self._screen_view is not None:
            self._screen_view.font_changed()
            self._place_screen()
        self.font_size_changed.emit(size)

    def font_size(self) -> int:
        """Return the visible terminal zoom level in points."""
        return int(self._font_size)

    def _move_caret_end(self):
        if self._keep_view or self.textCursor().hasSelection():
            return
        c = self.textCursor()
        c.movePosition(QTextCursor.End)
        self.setTextCursor(c)

    def _idle_prompt(self):
        """The output went quiet: show the synthetic prompt (pipe mode only).

        Never in front of text the user is typing (a running command must not
        look finished) and never while output still waits to be drawn."""
        if not (self._emulate_prompt and self._alive and self._need_prompt):
            return
        if self._line or self._inq:
            return
        wait = self._PROMPT_IDLE_S - (time.monotonic() - max(self._last_feed, self._submitted_at))
        if wait > 0.005:
            self._idle.start(int(wait * 1000) + 1)
            return
        self._prompt_if_needed()

    def _prompt_due(self):
        """Whether the synthetic prompt would show now: quiet long enough, nothing queued."""
        return (
            self._emulate_prompt and self._alive and self._need_prompt
            and not self._prompt_len and not self._inq
            and time.monotonic() - max(self._last_feed, self._submitted_at) >= self._PROMPT_IDLE_S
        )

    def _prompt_idle_ms(self) -> int:
        return int(self._PROMPT_IDLE_S * 1000)

    def _consume_pending_prompt(self):
        """Output arrived after all: take the synthetic prompt off the screen.

        The line break drawn before it (when the output had stopped mid-line)
        goes too, so the output continues exactly where it stopped.  The line
        being typed stays at the end."""
        if not self._prompt_len:
            return
        start = self._out_end()
        self._remove_range(start, start + self._edit_sep + self._prompt_len)
        self._edit_sep = self._prompt_len = 0
        self._shown_prompt = ""
        self._need_prompt = True

    # ---- the edit region (see the module docstring) ----
    def _edit_len(self) -> int:
        return self._edit_sep + self._prompt_len + self._line_len

    def _out_end(self) -> int:
        """Document position where output ends and the edit region starts."""
        return self.document().characterCount() - 1 - self._edit_len()

    def _output_tail_line(self) -> str:
        """The last line of output, without the edit region."""
        pos = self._out_end()
        block = self.document().findBlock(pos)
        return block.text()[: pos - block.position()]

    def _remove_range(self, start, end):
        if end > start:
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()

    def _plain_fmt(self, color=None):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color or self._fg_default))
        return fmt

    def _styled_runs(self, text):
        """``(text, format)`` runs for a string coloured with SGR escapes (a prompt,
        a banner).  Drawn with these formats, never through the stream's parser,
        so a half-received escape or a colour a command left on survives it."""
        saved = self._fmt
        self._fmt = self._plain_fmt()
        runs = []
        try:
            pos = 0
            for match in _STYLE_ESC_RE.finditer(text):
                if match.start() > pos:
                    runs.append((text[pos:match.start()], QTextCharFormat(self._fmt)))
                if match.group(3) == "m" and not match.group(2):
                    self._sgr_params(match.group(1))
                pos = match.end()
            if pos < len(text):
                runs.append((text[pos:], QTextCharFormat(self._fmt)))
        finally:
            self._fmt = saved
        return runs

    def _fix_edit_sep(self):
        """Keep the line break before a shown synthetic prompt only while the
        output above it ends mid-line (a notice may just have ended the line)."""
        if not self._prompt_len:
            return
        pos = self._out_end()
        cursor = QTextCursor(self.document())
        cursor.setPosition(pos)
        want = 0 if cursor.atBlockStart() else 1
        if want == self._edit_sep:
            return
        wpos = self._wc.position()
        if want:
            cursor.insertText("\n", self._plain_fmt())
        else:
            self._remove_range(pos, pos + 1)
        self._wc.setPosition(wpos)
        self._edit_sep = want

    def _commit_edit(self, suffix="\n"):
        """Make the shown prompt and edit line part of the output, then write *suffix*.

        Output still waiting to be drawn goes first, so the screen keeps the
        order in which things happened.  The edit region is empty afterwards."""
        prompt_shown = bool(self._prompt_len)
        if prompt_shown:
            self._need_prompt = True
        if not self._inq and self._screen is None:
            self._edit_sep = self._prompt_len = self._line_len = 0
            self._shown_prompt = ""
            self._wc.setPosition(self._out_end())
            self._cr_pending = False
            self._pad = 0
            if suffix:
                scrollbar = self.verticalScrollBar()
                at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
                self._wc.insertText(suffix, self._plain_fmt())
                if at_bottom:
                    scrollbar.setValue(scrollbar.maximum())
            if self._wc.atBlockStart():
                self._start_line()
            else:
                self._line_clean = False
            return
        end = self.document().characterCount() - 1
        start = end - self._line_len - self._prompt_len  # the separator is redrawn if needed
        fragment = None
        if end > start:
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            fragment = cursor.selection()
        self._remove_range(end - self._edit_len(), end)
        self._edit_sep = self._prompt_len = self._line_len = 0
        self._shown_prompt = ""
        runs = [(suffix, self._plain_fmt())] if suffix else None
        self._enqueue(_Marker("text", runs, fragment, fresh_line=prompt_shown))

    def set_interrupt_fn(self, fn):
        """Provide a reliable 'stop the running command' callback."""
        self._interrupt = fn

    def set_send_key_fn(self, fn):
        """``fn(data: bytes) -> bool`` sends a key picked from the context menu's
        *Send key*, or refuses it by returning False.

        An owner refuses bytes its session cannot take: 0x1A (Ctrl+Z) is end of
        file to adb.exe's text-mode stdin, which then stops forwarding input for
        good."""
        self._send_key_fn = fn

    def set_key_offered_fn(self, fn):
        """``fn(data: bytes) -> bool``: whether *Send key* offers *data* now.

        Tab, Esc, the arrows, Backspace and Ctrl+D mean something to a
        terminal (a device terminal).  A shell reading a pipe takes them as
        characters of the next command line (``Up`` made cmd run ``←[A
        dir``), so an owner offers them only while a terminal reads them."""
        self._key_offered_fn = fn

    def _key_offered(self, data: bytes) -> bool:
        fn = self._key_offered_fn
        if fn is None:
            return True
        try:
            return bool(fn(data))
        except Exception:
            return False

    def send_key(self, data: bytes) -> bool:
        """Send one control key (the context menu's *Send key*); True if it was sent."""
        if not (self._send and self._alive):
            return False
        if self._screen_takes_keys():
            return self._screen_send(data)  # a program's screen: straight to it
        if not self._key_offered(data):
            return False  # a pipe would take it as part of the next line
        if self._send_key_fn is not None:
            try:
                return bool(self._send_key_fn(data))
            except Exception:
                return False
        self._send(data)
        return True

    def set_shell_at_prompt(self, at_prompt):
        """Tell the console whether the shell waits at its own prompt.

        ``True``: a submitted line is a command, which the shell echoes (that
        copy is hidden).  ``False``: a command is running, so a line is input
        for it: a non-empty one is drawn once and no echo is expected, an empty
        one never adds a blank line.  ``None`` (the default): unknown, the
        console guesses from what it shows.  Owners that can recognise their
        shell's prompt should update this after every output chunk they
        inspect; the console sets it to False itself when it submits a line."""
        self._at_prompt = None if at_prompt is None else bool(at_prompt)

    def shell_at_prompt(self) -> bool:
        """Whether the shell seems to wait at its own prompt: the owner's
        verdict when it gave one, else the console's guess."""
        if self._at_prompt is None and self._emulate_prompt:
            return bool(self._prompt_len)
        return self._at_shell_prompt()

    def set_typing_ahead(self, on: bool) -> None:
        """Tell the console that the shell has not shown its first prompt yet
        (it is starting): a line submitted now is read when that prompt is
        out and echoed after it, so the console does not draw it itself (it
        showed twice: once with no prompt, once in the echo).  Not for the
        emulated prompt, whose shell never echoes."""
        self._typing_ahead = bool(on)

    def typing_ahead(self) -> bool:
        return self._typing_ahead and not self._emulate_prompt

    def has_typed_ahead(self) -> bool:
        """Whether a line drawn as input for a running command may still be
        read by the shell as a command (see :meth:`forget_typed_line`)."""
        return bool(self._typed_ahead)

    def forget_typed_line(self, text: str) -> bool:
        """The shell read *text*, drawn when it was typed while a command
        ran, as a command after all, and echoed it after its prompt: take the
        early copy off the screen, so the line shows once, where it ran.
        False when *text* is no such line (or its copy is gone already)."""
        text = text.strip()
        entry = next((e for e in self._typed_ahead if e[0].strip() == text), None)
        if entry is None:
            return False
        self._typed_ahead.remove(entry)
        drawn, start, marker = entry
        if start is None:
            if marker is None:
                return False
            marker.runs = marker.fragment = None  # still queued: never drawn
            return True
        # *start* moved with the edits before it; the copy is the line and
        # its break from there, unless the view was trimmed or cleared
        at, size = start.position(), _doc_len(drawn) + 1
        if at + size > self.document().characterCount() - 1:
            return False
        copy = QTextCursor(self.document())
        copy.setPosition(at)
        copy.setPosition(at + size, QTextCursor.KeepAnchor)
        if copy.selectedText() != drawn + "\u2029":
            return False
        copy.removeSelectedText()
        return True

    def _line_start_cursor(self, end: int, text: str) -> QTextCursor:
        """A cursor where *text* and the line break ending at *end* start: it
        moves with the text drawn before it, never with the output after."""
        cursor = QTextCursor(self.document())
        cursor.setPosition(max(0, end - _doc_len(text) - 1))
        return cursor

    def drop_typed_ahead(self) -> None:
        """Stop following typed-ahead lines: the shell has read all of them
        (it waited at its prompt), or it is gone."""
        self._typed_ahead.clear()

    def _remember_typed(self, text: str) -> None:
        """*text* was just drawn as input for a running command: keep hold of
        where its copy starts (see :meth:`forget_typed_line`)."""
        entry = [text, None, None]
        queued = self._inq[-1] if self._inq else None
        if isinstance(queued, _Marker) and queued.kind == "text":
            queued.typed = entry  # drawn when the queue gets there
            entry[2] = queued
        else:
            entry[1] = self._line_start_cursor(self._out_end(), text)
        self._typed_ahead.append(entry)
        while len(self._typed_ahead) > self._TYPED_AHEAD_MAX:
            self._typed_ahead.popleft()

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
    # as local PowerShell/CMD.  The window's shortcuts would take keys the
    # console handles too (see _claims_key).
    def event(self, event):
        if event.type() == QEvent.ShortcutOverride and self._claims_key(event):
            event.accept()
            return True
        return super().event(event)

    # Ctrl with these keys does something in line mode: the clipboard, select
    # all, clear (L), delete a word (W) and the text size
    _LINE_CTRL_KEYS = frozenset((
        Qt.Key_C, Qt.Key_V, Qt.Key_X, Qt.Key_A, Qt.Key_Insert, Qt.Key_L, Qt.Key_W,
        Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus,
    ))

    def _claims_key(self, event) -> bool:
        """Whether a key is the console's even where the window has a
        shortcut for it.  A shortcut takes its key before the console sees
        it: Ctrl+W closed the whole device tab instead of deleting a word.

        In line mode the console keeps the keys it handles itself, and the
        window the others (Ctrl+T, Ctrl+N, Ctrl+B, Ctrl+S, Ctrl+Q, F1).  A
        program that has the screen gets every key with Ctrl or Alt and
        every key that sends a sequence (Ctrl+W in nano, F1 in htop)."""
        mods, key = event.modifiers(), event.key()
        ctrl = bool(mods & Qt.ControlModifier)
        if self._screen_takes_keys():
            return ctrl or bool(mods & Qt.AltModifier) or key in _SCREEN_KEYS
        return (ctrl and key in self._LINE_CTRL_KEYS) or (
            bool(mods & Qt.ShiftModifier) and key == Qt.Key_Insert
        )

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

    def _clear_viewport(self, keep_prompt_line=False):
        """Clear the screen (``ESC[2J``, a form feed, cls); the saved history keeps everything.

        The edit region is drawn again at the top, so a synthetic prompt that
        was showing and the command being typed survive.  With
        *keep_prompt_line* the shell's own prompt on the last output line
        survives too (the Clear button, Clear-Host)."""
        keep = self._prompt_line_fragment() if keep_prompt_line else None
        had_prompt = bool(self._prompt_len)
        super().clear()
        self._wc = QTextCursor(self.document())
        self._cr_pending = False
        self._edit_sep = self._prompt_len = self._line_len = 0
        self._shown_prompt = ""
        self._home_pending = False
        self._start_line()
        if self._screen is not None:
            # a program's screen stays; the rows it took over are gone
            self._screen_base = QTextCursor(self.document())
            self._screen_seed = []
            self._screen_scrolled = []
        if keep is not None:
            self._wc.insertFragment(keep)
        if had_prompt:
            self._need_prompt = True
            self._prompt_if_needed()
        if self._line:
            self._redraw_line()
        self._clear_tab_cycle()

    def _prompt_line_fragment(self):
        """The last output line, formatted, when it is the shell's prompt."""
        tail = self._output_tail_line()
        if not tail or not self._at_shell_prompt():
            return None
        end = self._out_end()
        cursor = QTextCursor(self.document())
        cursor.setPosition(end - len(tail))
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        return cursor.selection()

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
        if not self._alive or self._pasting or query != self._line:
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

            # Display options cleanly: as in bash, the prompt and the line stay
            # above the list and are drawn again below it.
            had_prompt = bool(self._prompt_len)
            self._commit_edit("\n")
            self._echo(self._format_columns(opts) + "\n")
            if self._emulate_prompt:
                if had_prompt:
                    self._prompt_if_needed()
            elif self._prompt_provider_fn:
                prompt = self._prompt_provider_fn()
                if prompt:
                    self._echo(prompt, self._prompt_color)
            self._redraw_line()

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
        if not self._submitting:
            # Stop, a reconnect or a dead shell: lines still waiting from a
            # paste were meant for the old shell.  (A local shell that starts
            # on its first command reports alive from inside send_fn.)
            self._cancel_paste()
        if alive:
            self._alive = True
            self._erase_input()
            if self._emulate_prompt:
                # at most one prompt: the owner may have shown it already
                if not self._prompt_len:
                    self._need_prompt = True
                    self._prompt_if_needed()
            else:
                self._need_prompt = False
            self._move_caret_end()
        elif not alive and self._alive:
            self._alive = False
            self._idle.stop()
            # what was on the edit line stays visible above the notice
            self._commit_edit("")
            self._line = ""
            self._cpos = 0
            if show_disconnect_notice:
                self.notice("[shell disconnected]", theme.ECHO_ERROR)

    def set_emulate_prompt(self, enabled: bool):
        """Enable or disable synthetic Android shell prompt emulation."""
        self._emulate_prompt = bool(enabled)
        if not self._emulate_prompt:
            self._need_prompt = False
            if self._prompt_len:
                start = self._out_end()
                self._remove_range(start, start + self._edit_sep + self._prompt_len)
                self._edit_sep = self._prompt_len = 0
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
            f"{_prompt_sgr('user')}{user}@{_prompt_sgr('host')}{host or 'android'}"
            f"{_prompt_sgr('colon')}:{_prompt_sgr('path')}{cwd} "
            f"{_prompt_sgr('root' if self._root else 'mark')}{marker}\x1b[0m "
        )

    @staticmethod
    def _style_local_prompts(text: str) -> str:
        """Colour the real local-shell prompt without replacing its semantics."""
        def powershell(match):
            return (
                f"{_prompt_sgr('host')}{match.group(1)}{_prompt_sgr('path')}{match.group(2)}"
                f"{_prompt_sgr('mark')}{match.group(3)}\x1b[0m"
            )

        def cmd(match):
            return f"{_prompt_sgr('path')}{match.group(1)}{_prompt_sgr('mark')}{match.group(2)}\x1b[0m"

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
        """Show a pre-formatted welcome banner after what is already on screen or queued.

        It starts on a fresh line, never with a blank one.  Its colours are
        applied directly, so the stream's parser and colour are not disturbed."""
        text = (text or "").lstrip("\r\n")
        if not text:
            return
        self._sb.archive(strip_ansi(text))
        self._write_text(self._styled_runs(text), fresh_line=True)

    def show_prompt(self):
        """Show the synthetic prompt now: the owner knows the shell is ready.

        A prompt that is showing already stays; there is never a second one."""
        self._prompt_if_needed()
        self._move_caret_end()

    # ---- ordered output of our own: notices, stream boundaries, clears ----
    def notice(self, text, color=None, *, new_stream=True):
        """Show a status line (Stop, reopen, reconnect, disconnect, restart).

        It is drawn after every output chunk already received, starts on a
        fresh line and adds no blank line when the output ended with a line
        break.  *new_stream* (the default) marks a stream boundary as well: see
        :meth:`reset_stream`.  Pass False for a notice in the middle of a
        stream that carries on (a local shell while the ADB server restarts)."""
        if new_stream:
            self.reset_stream()
        text = (text or "").strip("\r\n")
        if text:
            self._write_text([(text + "\n", self._plain_fmt(color))], fresh_line=True)

    def reset_stream(self):
        """Mark a stream boundary: the shell was stopped, reopened or reconnected.

        Output from the old stream that is still queued is drawn first; the
        next output starts on a fresh line with the escape parser, colours,
        UTF-8 decoder and pending echo reset, so a sequence or a character cut
        off by the old stream cannot garble the new one."""
        self.clear_pending_echo()
        self._decoder.reset()
        self._archive_carry = ""
        self._feed_at_line_start = True
        self._drop_held()
        self._enqueue(_Marker("reset"))

    def discard_output(self):
        """Drop output received but not drawn yet (Stop).  Queued notices stay.

        So does no count of output an over-full queue dropped before: that
        was part of what goes now, and a note about it after the Stop only
        told half of it (the saved history has all of it)."""
        self._inq = deque(item for item in self._inq if isinstance(item, _Marker))
        self._inq_len = 0
        self._drop_held()
        self._dropped = 0

    def interrupt_output(self, *, until_echo=True) -> bool:
        """Ctrl+C went to the running command: stop drawing what it printed.

        Output received but not drawn yet goes (queued notices stay), so the
        scrolling stops at once instead of drawing seconds of backlog; one
        note says how much went, and the saved history has all of it.  With
        *until_echo* (a device terminal) the output that arrives until the
        terminal echoes the Ctrl+C (``^C``) goes too: the command printed it
        before it was stopped, and it was already on its way.  That echo, and
        what follows it (the command's last words, the prompt), is drawn as
        usual; without one (the terminal echoes nothing) the output shows
        after :attr:`_CTRL_C_ECHO_S` or with the shell's prompt.  A little
        output, drawn in a moment (:attr:`_CTRL_C_KEEP_S`), is drawn anyway.

        While a program has the screen, Ctrl+C is only a key to it (``top``
        quits on it, ``vi`` does not), but a flood it printed stops all the
        same (see :meth:`_drop_screen_backlog`): nothing waits for an echo
        there (a program reading its keys gives none), and the program
        redraws its screen.  Returns whether the output was stopped."""
        if self._screen is not None:
            if self._inq_len > self._little_output():
                self._drop_screen_backlog()
            return True
        self._drop_held()
        if self._inq_len > self._little_output():
            kept = deque()
            for item in self._inq:
                if isinstance(item, _Marker):
                    kept.append(item)
                else:
                    self._dropped += len(item)
            # what went may have ended in the middle of a sequence
            self._state = 0
            self._csi = ""
            self._inq = kept
            self._inq_len = 0
        if until_echo:
            self._held_until = time.monotonic() + self._CTRL_C_ECHO_S
            self._held_timer.start(int(self._CTRL_C_ECHO_S * 1000) + 10)
        if self._dropped and not self._drain.isActive():
            self._drain.start()  # the note, at once
        return True

    def _through_ctrl_c(self, text: str) -> str:
        """*text* arrived while output waits for the echo of a Ctrl+C: what
        to queue now ("" while it waits)."""
        if time.monotonic() >= self._held_until:
            self._release_held()
            return text
        held = self._held
        if held and held[-1].endswith("^") and text.startswith("C"):
            at = -1  # the echo starts at the end of the last read
        else:
            found = [i for i in (text.find("^C"), text.find("\x03")) if i >= 0]
            if not found:
                held.append(text)
                return ""
            at = min(found)
        skipped = sum(len(part) for part in held) + at
        self._held = []
        self._held_until = 0.0
        self._held_timer.stop()
        if skipped <= self._little_output():
            for part in held:  # drawn in a moment: no gap in the output
                self._queue_text(part)
            return text
        self._dropped += skipped
        return "^" + text if at < 0 else text[at:]

    def _little_output(self) -> int:
        """How much output is drawn in :attr:`_CTRL_C_KEEP_S`."""
        return int(self._render_rate * self._CTRL_C_KEEP_S)

    def _release_held(self):
        """Stop waiting for the echo of a Ctrl+C: draw what waited."""
        held, self._held = self._held, []
        self._held_until = 0.0
        self._held_timer.stop()
        for text in held:
            self._queue_text(text)

    def _drop_held(self):
        """Forget output waiting for the echo of a Ctrl+C (a new stream, a
        clear); the saved history has it."""
        self._dropped += sum(len(text) for text in self._held)
        self._held = []
        self._held_until = 0.0
        self._held_timer.stop()

    def mark_prompt(self):
        """The output received so far ends with the shell's own prompt (the
        owner recognised it): it is drawn in the prompt colours, unless it
        brings colours of its own, and a program's screen that is still
        showing goes (the program has ended).  Output held back after a
        Ctrl+C shows now, if the terminal never echoed it."""
        if self._held_until:
            self._release_held()
        if self._inq:
            self._enqueue(_Marker("prompt"))
        else:
            self._render_prompt_marker()
            self._fix_edit_sep()

    def set_screen_input(self, fn):
        """``fn(data: bytes) -> bool`` sends keys and answers to a program on
        a device terminal; None when the shell reads a pipe.

        With it, a program that addresses the cursor gets a screen (see the
        module docstring): its keys go to it as a terminal sends them, and its
        questions (cursor position, status) are answered.  Over a pipe none
        of that means anything, and neither is done.  A screen still showing
        goes where the stream says so (the shell's prompt, a new stream, see
        :meth:`end_screen`); without *fn* the keys go to the edit line
        meanwhile, never into a terminal that is gone."""
        self._screen_input = fn
        if fn is None:
            self._home_pending = False

    def end_screen(self):
        """The terminal a program's screen belongs to is gone (the adb shell
        it ran in ended: the device went away, adb restarted): the screen
        goes where the output received so far ends, as when the program
        leaves it itself.  Unlike the shell's prompt (:meth:`mark_prompt`)
        this ends the alternate screen too: ``vi`` left there by a lost
        device would never leave it, and hid the shell's own prompt."""
        if self._inq:
            self._enqueue(_Marker("end"))
        else:
            self._leave_screen()

    def _screen_takes_keys(self) -> bool:
        """Whether a program's screen shows and its terminal takes keys."""
        return self._screen is not None and self._screen_input is not None

    def clear_screen(self):
        """Clear the screen for ``cls`` / ``Clear-Host``, in order with the output.

        For shells that clear only their own console (PowerShell's Clear-Host
        writes nothing to a pipe; cmd's cls writes a form feed, which clears by
        itself).  Unlike :meth:`clear` the saved history keeps everything.  The
        shell's prompt line and the command being typed are drawn again at the
        top."""
        self._enqueue(_Marker("clear"))

    def _enqueue(self, marker):
        self._idle.stop()
        self._inq.append(marker)
        if not self._drain.isActive():
            self._drain.start()

    def _write_text(self, runs, *, fragment=None, fresh_line=False):
        """Write our own ``(text, format)`` runs as output: now, or after the
        queued output (or once a program's screen has gone)."""
        if self._inq:
            self._enqueue(_Marker("text", runs, fragment, fresh_line))
            return
        if self._screen is not None:
            self._screen_notes.append(_Marker("text", runs, fragment, fresh_line))
            return
        scrollbar = self.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
        self._render_text(runs, fragment, fresh_line)
        self._fix_edit_sep()
        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _render_text(self, runs, fragment, fresh_line):
        if fresh_line:
            self._break_line()
        else:
            self._wc.setPosition(self._out_end())
            self._cr_pending = False
            self._pad = 0
        if fragment is not None:
            self._wc.insertFragment(fragment)
        for text, fmt in runs or ():
            self._wc.insertText(text, fmt)
        if self._wc.atBlockStart():
            self._start_line()
        else:
            self._line_clean = False  # our own text: the output line goes on after it

    def _render_marker(self, marker):
        if marker.kind == "prompt":
            self._render_prompt_marker()
        elif marker.kind == "end":
            self._leave_screen()  # the program's terminal is gone (see end_screen)
        elif self._screen is not None and marker.kind == "text":
            self._screen_notes.append(marker)  # drawn once the screen has gone
            return
        elif marker.kind == "reset":
            self._leave_screen()  # the program's stream has ended
            self._reset_parser()
            self._break_line()
        elif marker.kind == "clear":
            self._leave_screen()
            self._clear_viewport(keep_prompt_line=True)
        else:
            self._render_text(marker.runs, marker.fragment, marker.fresh_line)
            entry = marker.typed
            if entry is not None and entry[1] is None and (marker.runs or marker.fragment):
                # a typed-ahead line drawn now: its text and line break end here
                entry[1] = self._line_start_cursor(self._wc.position(), entry[0])
        self._fix_edit_sep()

    def _render_prompt_marker(self):
        """The shell's prompt ends the output drawn so far (see mark_prompt).

        A program on the alternate screen leaves it itself when it ends; a
        line there that only looks like a prompt (a file in ``vi``) must not
        take the screen away from it."""
        if self._screen is not None:
            if self._screen.alt_active:
                return
            self._leave_screen()
        self._logcat.reset()
        self._colour_prompt()

    def _break_line(self):
        """Move the output cursor to the end of the output, on a fresh line."""
        self._wc.setPosition(self._out_end())
        self._cr_pending = False
        if not self._wc.atBlockStart():
            self._wc.insertText("\n", self._plain_fmt())
        self._start_line()

    def _reset_parser(self):
        self._state = 0
        self._csi = ""
        self._skip_break = False
        self._cr_pending = False
        self._sgr(0)
        self._fmt_plain = True
        self._home_pending = False
        self._saved_column = None
        self._modes.clear()
        self._logcat.reset()
        self._start_line()

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
        if text and self._pending_echo is not None:
            text = self._strip_echo(text)
        self._ingest(text)

    def clear_pending_echo(self):
        """Stop waiting for the shell to echo the last submitted line.

        For owners that know a line was input for a running program, which does
        not echo it: that program's output must not be hidden when it starts
        with the same text.  Output held back while it matched part of the
        line is shown; a complete echo that only lacked its line break is not."""
        held = self._echo_held
        complete = self._pending_echo in ("\r\n", "\n")
        self._pending_echo = None
        self._echo_held = ""
        if not complete:
            self._ingest(held)

    def _strip_echo(self, text):
        """Hide the shell's echo of the submitted line (and its line break) in *text*.

        The echo can arrive in pieces (adb.exe forwards a device terminal's
        echo byte by byte).  What matched so far is held back; it is shown after
        all when anything but the line break follows it."""
        pe = self._pending_echo
        held = self._echo_held
        awaiting_break = pe in ("\r\n", "\n")
        if not (awaiting_break and held) and time.monotonic() - self._pending_echo_at > _PENDING_ECHO_TTL_S:
            self._pending_echo = None
            self._echo_held = ""
            return held + text
        if awaiting_break:
            # the command text is through (or none was typed): its line break is next
            return self._strip_echo_break(text)
        if text.startswith(pe):
            self._echo_held = held + pe
            return self._strip_echo_break(text[len(pe):])
        if pe.startswith(text):
            self._pending_echo = pe[len(text):]
            self._echo_held = held + text
            return ""
        # The shell did not echo the command: stop waiting, or later output
        # that happens to start with it would be swallowed.
        self._pending_echo = None
        self._echo_held = ""
        return held + text

    def _strip_echo_break(self, rest):
        """*rest* follows the echoed command: drop one line break (``\\n``, ``\\r\\n``
        or adb.exe's ``\\r\\r\\n``), or wait while only CRs have come."""
        match = _ECHO_BREAK_RE.match(rest)
        if match:
            self._pending_echo = None
            self._echo_held = ""
            return rest[match.end():]
        if not rest.strip("\r"):
            # PowerShell and adb.exe can write the echo and its line break
            # separately: wait for the break rather than print a blank line.
            self._pending_echo = "\n"
            self._echo_held += rest
            return ""
        # Something other than a line break follows: that was output, not the echo.
        held = self._echo_held
        self._pending_echo = None
        self._echo_held = ""
        return held + rest

    def _ingest(self, text):
        """Archive shell output and queue it for drawing (nothing is drawn here)."""
        if not text:
            return
        if self._cd_revert is not None:
            self._check_cd_failure(text)
        if not self._emulate_prompt:
            text = self._style_local_prompts_stream(text)
        self._last_feed = time.monotonic()
        self._feed_count += 1
        self._sb.archive(self._archive_text(text))
        self._idle.stop()
        if self._held_until:
            text = self._through_ctrl_c(text)
            if not text:
                return
        self._queue_text(text)

    def _queue_text(self, text):
        """Queue archived output for drawing (see _drain_tick)."""
        self._inq.append(text)
        self._inq_len += len(text)
        if self._inq_len > self._backlog_cap():
            self._drop_backlog()
        if not self._drain.isActive():
            self._drain.start()

    def _backlog_cap(self) -> int:
        """How much output may wait to be drawn: about :attr:`_MAX_LAG_S` of
        drawing at the rate this console draws."""
        return min(self._MAX_INQ, max(self._MIN_INQ, int(self._render_rate * self._MAX_LAG_S)))

    def _drop_backlog(self):
        """The on-screen backlog is capped; the disk archive already has every
        character.  Count what is dropped so the view says so instead of
        silently splicing two unrelated points together.  Notices stay.

        The oldest output goes first.  A reader batch (up to a megabyte) more
        than twice the cap on its own loses its start, up to a line break.
        A program's screen is capped too (see _drop_screen_backlog): a
        flood it could not keep up with once queued 40 MB."""
        cap = self._backlog_cap()
        if self._screen is not None:
            self._drop_screen_backlog(self._inq_len - cap)
            return
        kept = []
        while self._inq_len > cap and len(self._inq) > 1:
            item = self._inq.popleft()
            if isinstance(item, _Marker):
                kept.append(item)
                continue
            self._inq_len -= len(item)
            self._dropped += len(item)
        if self._inq_len > 2 * cap and len(self._inq) == 1 and not isinstance(self._inq[0], _Marker):
            item = self._inq[0]
            excess = len(item) - cap
            cut = item.find("\n", excess)
            cut = excess if cut < 0 else cut + 1
            self._inq[0] = item[cut:]
            self._inq_len -= cut
            self._dropped += cut
        self._inq.extendleft(reversed(kept))

    def _drop_screen_backlog(self, count=None):
        """Drop the oldest output waiting for a program's screen: about
        *count* characters of it, or all it can (None: Ctrl+C, Stop).  The
        newest stays, and nothing is said: the program redraws its screen,
        so a frame it drew meanwhile is only skipped (the saved history has
        all of it).

        Only what is surely the program's goes: never a queued notice or
        prompt (what follows one may be the shell's) nor a switch to or
        from the alternate screen, which decides what the screen is.  What
        is left starts at a line break or an escape sequence, and the start
        of a sequence the screen holds from the last read goes too: joined
        to what comes next, a sequence cut in half garbled the next frame."""
        queue = self._inq
        dropped = 0
        while queue and not isinstance(queue[0], _Marker):
            item = queue[0]
            switch = _ALT_SWITCH_RE.search(item)
            stop = len(item) if switch is None else switch.start()
            want = stop if count is None else min(stop, max(0, count - dropped))
            if want == len(item):
                queue.popleft()  # all of it
                dropped += want
                continue
            if dropped or want:
                boundary = _SCREEN_CUT_RE.search(item, want)
                cut = want if boundary is None else boundary.end()
                if cut < len(item):
                    queue[0] = item[cut:]
                else:
                    queue.popleft()
                dropped += cut
            break
        if dropped:
            self._inq_len -= dropped
            self._screen.forget_partial()

    def _drain_tick(self):
        if not self._inq:
            self._drain.stop()
            if self._emulate_prompt and self._alive and self._need_prompt and not self._line:
                self._idle.start(self._prompt_idle_ms())
            return
        sb = self.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 2
        started = time.perf_counter()
        deadline = started + self._TICK_BUDGET
        drawn = 0
        self.setUpdatesEnabled(False)
        try:
            if self._dropped and self._screen is None:
                self._consume_pending_prompt()
                self._announce_drop()
            while self._inq and time.perf_counter() < deadline:
                item = self._inq.popleft()
                if isinstance(item, _Marker):
                    self._render_marker(item)
                    continue
                if len(item) > self._SUB:
                    # The reader hands over a flood in batches of up to a
                    # megabyte.  Cut one into slices once (cutting the rest
                    # again per slice copied it over and over), so the time
                    # budget is checked every few milliseconds and a key
                    # typed meanwhile is never kept waiting long.
                    sub = self._SUB
                    self._inq.extendleft(reversed([item[i:i + sub] for i in range(sub, len(item), sub)]))
                    item = item[:sub]
                self._inq_len -= len(item)
                drawn += len(item)
                if self._screen is not None:
                    self._screen_feed(item)
                    continue
                self._consume_pending_prompt()
                self._process(item)
        finally:
            self.setUpdatesEnabled(True)
        spent = time.perf_counter() - started
        if drawn >= 32 * 1024 and spent > 0.002:
            # how fast this console draws (see _backlog_cap)
            self._render_rate = 0.8 * self._render_rate + 0.2 * (drawn / spent)
        screen = self._screen
        if screen is not None:
            if screen.version != self._screen_painted:
                self._screen_painted = screen.version
                self._screen_view.update()
        elif at_bottom:
            sb.setValue(sb.maximum())

    def _announce_drop(self):
        """Mark the hole left by an over-full input queue, like the log view.

        The escape parser is restarted as well: the dropped text can end in the
        middle of a sequence, and a half-parsed CSI would then swallow the
        output that follows it.
        """
        dropped, self._dropped = self._dropped, 0
        self._state = 0
        self._csi = ""
        self._home_pending = False
        self._logcat.reset()
        self._break_line()
        self._wc.insertText(
            f"… ({dropped} characters skipped on screen — saved log has all)\n", self._fmt
        )
        self._start_line()
        self._fix_edit_sep()

    def _process(self, text):
        """Draw *text* on the output lines.

        Runs of plain text go in whole and complete escape sequences are
        read in one go; a sequence cut off by the end of *text* goes through
        the parser's states, which the next call resumes.  When the output
        addresses the cursor in a way lines cannot show (see _csi_dispatch),
        the rest of *text* goes to a screen instead (_enter_screen)."""
        run = []

        def flush(fmt=None):
            if run:
                chunk = "".join(run)
                run.clear()
                if fmt is None and self._line_fmt is not None and self._fmt_plain:
                    fmt = self._line_fmt  # more of a line shown in its logcat colour
                self._out(chunk, fmt)
                if fmt is not None and fmt is self._line_fmt:
                    self._line_fmt_end = (self._wc.block(), self._wc.positionInBlock())
                if fmt is None and not self._fmt_plain:
                    self._line_clean = False  # the line brings colours of its own
                if self._line_clean and len(self._line_head) < 256:
                    self._line_head += chunk

        def newline(fmt):
            if self._wc.position() == self._out_end():
                # Appending: one insert for the text and its break.  A
                # lone newline in front of a typed line costs Qt a
                # relayout that grows with the document.  Blanks a cursor
                # move left past the line's end stay only for text that
                # follows them ("abc", 2 right, "d" is "abc  d").
                if not run:
                    self._pad = 0
                run.append("\n")
                flush(fmt)
            else:
                flush(fmt)
                self._wc.setPosition(self._out_end())
                self._wc.insertText("\n", self._fmt)

        if self._wc.position() > self._out_end():
            self._wc.setPosition(self._out_end())  # output never goes into the edit region
        # A CR is applied lazily: "\r\n" (and adb.exe's "\r\r\n") is only a line
        # break, and the text before it can go in with the break as ONE insert.
        cr = self._cr_pending
        self._cr_pending = False
        index, size = 0, len(text)
        while index < size:
            state = self._state
            if state == 0:
                if self._skip_break:
                    # cls wrote a form feed and then its usual line break: the
                    # next prompt belongs on the first line, not below a blank one
                    ch = text[index]
                    if ch == "\r":
                        index += 1
                        continue
                    self._skip_break = False
                    if ch == "\n":
                        index += 1
                        continue
                if self._home_pending and text[index] != "\x1b":
                    # the cursor went home and something else than a clear follows
                    flush()
                    self._enter_screen(self._pending_home() + text[index:])
                    return
                match = _PLAIN_RUN_RE.match(text, index)
                if match is not None:
                    if cr:
                        flush()
                        self._carriage_return()
                        cr = False
                    run.append(match.group())
                    index = match.end()
                    continue
                ch = text[index]
                index += 1
                if ch == "\r":
                    cr = True
                    continue
                if ch == "\n":
                    cr = False
                    fmt, words = self._line_format(run)
                    if words:
                        # the line and its words' colours in one edit, laid out
                        # once: a flood of "Permission denied" stays as fast
                        line = self._wc.block()
                        self._wc.beginEditBlock()
                        try:
                            newline(fmt)
                            self._colour_words(line)
                        finally:
                            self._wc.endEditBlock()
                    else:
                        newline(fmt)
                    self._start_line()
                    continue
                if cr:
                    flush()
                    self._carriage_return()
                    cr = False
                if ch == "\x1b":
                    flush()
                    end = self._escape_at(text, index - 1)
                    if end is None:
                        return  # a screen has the rest
                    index = end
                elif ch == "\b":
                    flush()
                    self._backspace()
                elif ch == "\t":
                    run.append("    ")
                elif ch == "\x0c" and not self._emulate_prompt:
                    # A form feed is what cmd's cls writes to a pipe: clear the
                    # screen (the saved history keeps everything).
                    flush()
                    self._clear_viewport()
                    self._skip_break = True
                # every other control character is ignored
                continue
            ch = text[index]
            index += 1
            if state == 1:
                if ch == "[":
                    self._state = 2
                    self._csi = ""
                elif ch in "]PX^_":
                    # OSC, or a DCS/SOS/PM/APC string: ignored up to BEL / ST
                    self._state = 3
                    self._string_len = 0
                elif " " <= ch <= "/":
                    # an intermediate byte (e.g. ``ESC ( B`` charset select):
                    # the sequence ends at the following final byte
                    self._state = 5
                    self._esc_mid = ch
                else:
                    self._state = 0
                    if self._esc_dispatch("", ch):
                        self._enter_screen(self._pending_home() + "\x1b" + ch + text[index:])
                        return
            elif state == 5:
                if " " <= ch <= "/":
                    self._esc_mid += ch
                else:
                    self._state = 0
                    if self._esc_dispatch(self._esc_mid, ch):
                        self._enter_screen(
                            self._pending_home() + "\x1b" + self._esc_mid + ch + text[index:]
                        )
                        return
            elif state == 2:
                if "\x40" <= ch <= "\x7e":
                    self._state = 0
                    if self._csi_dispatch(ch, self._csi):
                        self._enter_screen(
                            self._pending_home() + "\x1b[" + self._csi + ch + text[index:]
                        )
                        return
                else:
                    self._csi += ch
            elif state == 3:
                if ch == "\x07":
                    self._state = 0
                elif ch == "\x1b":
                    self._state = 4
                else:
                    self._string_len += 1
                    if self._string_len >= _STRING_MAX:
                        self._state = 0  # it never ends: what follows is output
            elif state == 4:
                if ch == "\\":
                    self._state = 0
                else:
                    # any other escape ends the string as well, and is a
                    # sequence of its own (``ESC ] 0 ; title`` without its
                    # BEL swallowed the prompts and output after it)
                    self._state = 1
                    index -= 1
        flush(self._partial_line_format(run))
        self._cr_pending = cr

    def _escape_at(self, text, index):
        """Apply the escape sequence at ``text[index]`` when all of it is
        there; the index after it, or None when a screen took the rest of
        *text*.  A sequence cut off by the end of *text* is left to the
        parser's states."""
        after = text[index + 1:index + 2]
        match = None
        if after == "[":
            match = _CSI_AT_RE.match(text, index)
            if match is not None and self._csi_dispatch(match.group(2), match.group(1)):
                self._enter_screen(self._pending_home() + text[index:])
                return None
        elif after and after in "]PX^_":
            match = _STRING_AT_RE.match(text, index)  # read and ignored
        elif after:
            match = _ESC_AT_RE.match(text, index)
            if match is not None and self._esc_dispatch(match.group(1), match.group(2)):
                self._enter_screen(self._pending_home() + text[index:])
                return None
        if match is not None:
            return match.end()
        self._state = 1
        return index + 1

    def _start_line(self):
        """The output continues on a fresh line."""
        self._line_fmt = None  # the line before keeps its colour
        self._line_clean = True
        self._line_head = ""
        self._pad = 0

    @property
    def _line_clean(self) -> bool:
        """Whether the line being drawn is plain text so far, in the default
        colours (see _line_format).  Once it is not, a logcat colour it was
        shown in before it ended is taken back (see _partial_line_format)."""
        return self._line_plain

    @_line_clean.setter
    def _line_clean(self, clean: bool):
        if not clean and self._line_fmt is not None:
            self._uncolour_line()
        self._line_plain = clean

    def _partial_line_format(self, run):
        """The format for the end of a line that has not ended yet: its logcat
        priority's once its start shows it (the priority comes before the
        message), so a line that arrives in pieces is not drawn in the default
        colour first; else None.  The line's end settles it (_line_format)."""
        if not run or not self._line_clean or not self._fmt_plain:
            return None
        if self._line_fmt is None:
            head = self._line_head
            for piece in run:
                if len(head) >= 256:
                    break
                head += piece
            fmt = self._level_format(shell_colors.logcat_level(head))
            if fmt is None:
                return None
            self._line_fmt = fmt
            self._line_fmt_end = (self._wc.block(), self._wc.positionInBlock())
            if self._line_head:  # its start, drawn earlier in the default colour
                start = QTextCursor(self.document())
                start.setPosition(self._wc.block().position())
                start.setPosition(self._wc.position(), QTextCursor.KeepAnchor)
                start.setCharFormat(self._line_fmt)
        return self._line_fmt

    def _uncolour_line(self):
        """The line shown in a logcat colour before it ended brings colours of
        its own after all, or is written over: its text so far goes back to
        the default colour."""
        block, column = self._line_fmt_end
        self._line_fmt = None
        if block.isValid():
            start = block.position()
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.setPosition(start + min(column, block.length() - 1), QTextCursor.KeepAnchor)
            cursor.setCharFormat(self._plain_fmt())

    def _carriage_return(self):
        if self._pad or not self._wc.atBlockStart():
            self._line_clean = False  # the line is written over from its start
        self._wc.movePosition(QTextCursor.StartOfBlock)
        self._pad = 0

    def _backspace(self):
        self._line_clean = False
        if self._pad:
            self._pad -= 1
        elif not self._wc.atBlockStart():
            # A terminal backspace stops at column 0; it never wraps onto
            # the end of the previous line.
            self._wc.movePosition(QTextCursor.Left)

    def _line_format(self, run):
        """How the line that ends now is coloured when all of it was plain
        text in the default colours (see shell_colors): ``(fmt, words)``.

        *fmt* is its logcat priority's format for a warning, error or fatal
        logcat line, else None (the stream's own); the start of the line,
        drawn earlier when it came in an earlier read, is recoloured to
        match.  *words* is whether it is other output (no logcat line, no
        prompt and the command after it) with words to look at once it is
        drawn (see _colour_words)."""
        if not self._line_clean or (run and not self._fmt_plain):
            self._logcat.reset()
            return None, False
        head = self._line_head
        for piece in run:
            if len(head) >= 256:
                break
            head += piece
        level = self._logcat.level(head)
        if level is None:
            # a head cut short may leave the words out: all of it is looked at
            words = len(head) >= 256 or shell_colors.may_mean(head)
            return None, words and shell_colors.prompt_spans(head) is None
        fmt = self._level_format(level)
        if fmt is not None and self._line_head:
            start = QTextCursor(self.document())
            start.setPosition(self._wc.block().position())
            start.setPosition(self._wc.position(), QTextCursor.KeepAnchor)
            start.setCharFormat(fmt)
        return fmt, False

    def _level_format(self, level):
        """The format of a logcat line of priority *level*: the Logcat tab's
        colours for warnings, errors and fatal lines (fatal ones bold), None
        for the rest (and for no priority), which keep the default colour."""
        if level is None or level not in shell_colors.COLOURED_LEVELS:
            return None
        fmt = self._level_formats.get(level)
        if fmt is None:
            fmt = QTextCharFormat()
            color = theme.LOGCAT_LEVELS.get("F" if level == "A" else level, theme.LOGCAT_DEFAULT)
            fmt.setForeground(QColor(color))
            if level in ("F", "A"):
                fmt.setFontWeight(QFont.Bold)
            self._level_formats[level] = fmt
        return fmt

    def _colour_words(self, block):
        """Colour the words of the output line *block* that say something
        went wrong or was refused, needs attention or worked
        (theme.TERM_MEANING_COLORS; see shell_colors.meaning_spans).  The rest
        of the line keeps the default colour."""
        if not block.isValid():
            return
        spans = shell_colors.meaning_spans(block.text())
        if not spans:
            return
        start = block.position()
        painter = QTextCursor(block)
        for first, last, kind in spans:
            painter.setPosition(start + first)
            painter.setPosition(start + last, QTextCursor.KeepAnchor)
            painter.setCharFormat(self._meaning_format(kind))

    def _meaning_format(self, kind):
        key = ("meaning", kind)
        fmt = self._level_formats.get(key)
        if fmt is None:
            color, bold = theme.TERM_MEANING_COLORS[kind]
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Bold)
            self._level_formats[key] = fmt
        return fmt

    def _prompt_format(self, part):
        key = ("prompt", part)
        fmt = self._level_formats.get(key)
        if fmt is None:
            color, bold = theme.TERM_PROMPT_COLORS[part]
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Bold)
            self._level_formats[key] = fmt
        return fmt

    def _is_plain(self, fmt) -> bool:
        """Whether text in *fmt* shows in the terminal's default colours."""
        brush = fmt.foreground()
        if brush.style() != Qt.NoBrush and brush.color().name() != QColor(self._fg_default).name():
            return False
        return (
            fmt.background().style() == Qt.NoBrush and fmt.fontWeight() <= QFont.Normal
            and not fmt.fontItalic() and not fmt.fontUnderline()
        )

    def _colour_prompt(self):
        """Draw the shell's prompt that ends the output (see mark_prompt) in
        the prompt colours (theme.TERM_PROMPT_COLORS).  A prompt that brought
        colours of its own keeps them, and the command after it is never
        touched."""
        end = self._out_end()
        block = self.document().findBlock(end)
        start = block.position()
        spans = shell_colors.prompt_spans(block.text()[: end - start])
        if not spans:
            return
        stop = start + spans[-1][1]
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid() and fragment.position() < stop and not self._is_plain(fragment.charFormat()):
                return
            it += 1
        painter = QTextCursor(self.document())
        for first, last, part in spans:
            painter.setPosition(start + first)
            painter.setPosition(start + last, QTextCursor.KeepAnchor)
            painter.setCharFormat(self._prompt_format(part))

    def _line_end(self) -> int:
        """End of the output on the output cursor's line (never into the edit region)."""
        block = self._wc.block()
        return min(block.position() + block.length() - 1, self._out_end())

    def _column(self) -> int:
        """The output cursor's column on its line."""
        return self._wc.positionInBlock() + self._pad

    def _move_to_column(self, column):
        """Put the output cursor in *column* of its line (progress bars and
        prompts that redraw one line).  Past the end of the line's text it
        stands in blank columns, which are filled once text comes there."""
        cols = self.cell_size()[0]
        column = max(0, min(column, cols - 1) if cols > 0 else column)
        start = self._wc.block().position()
        length = self._line_end() - start
        if column <= length:
            self._wc.setPosition(start + column)
            self._pad = 0
        else:
            self._wc.setPosition(start + length)
            self._pad = column - length

    def _erase_chars(self, count):
        """ECH: blank *count* characters from the cursor on; nothing moves."""
        pos = self._wc.position()
        end = min(pos + count, self._line_end())
        if self._pad or end <= pos:
            return
        eraser = QTextCursor(self.document())
        eraser.setPosition(pos)
        eraser.setPosition(end, QTextCursor.KeepAnchor)
        eraser.insertText(" " * (end - pos), QTextCharFormat())
        self._wc.setPosition(pos)

    def _delete_chars(self, count):
        """DCH: remove *count* characters at the cursor; the rest moves left."""
        pos = self._wc.position()
        end = min(pos + count, self._line_end())
        if not self._pad and end > pos:
            self._remove_range(pos, end)
            self._wc.setPosition(pos)

    def _insert_blanks(self, count):
        """ICH: *count* blanks at the cursor; the rest moves right.  Never
        more than the view is wide (``ESC[1000000000@`` made a gigabyte of
        blanks)."""
        pos = self._wc.position()
        if not self._pad and pos < self._line_end():
            cols = self.cell_size()[0]
            if cols > 0:
                count = min(count, cols)
            self._wc.insertText(" " * count, QTextCharFormat())
            self._wc.setPosition(pos)

    def _erase_line_start(self):
        """EL 1: blank the line from its start through the cursor, in place."""
        pos = self._wc.position()
        start = self._wc.block().position()
        count = min(pos - start + 1, self._line_end() - start)
        if count > 0:
            eraser = QTextCursor(self.document())
            eraser.setPosition(start)
            eraser.setPosition(start + count, QTextCursor.KeepAnchor)
            eraser.insertText(" " * count, QTextCharFormat())
        self._wc.setPosition(pos)

    def _save_column(self):
        self._saved_column = (self._wc.blockNumber(), self._column())

    def _restore_column(self) -> bool:
        """ESC 8 / CSI u: back to the saved cursor.  True when that is on
        another line (only a screen can go there)."""
        saved = self._saved_column
        if saved is None:
            return False
        if saved[0] == self._wc.blockNumber():
            self._move_to_column(saved[1])
            return False
        return self._screen_input is not None

    def _out(self, s, fmt=None):
        if not s:
            return
        if fmt is None:
            fmt = self._fmt
        if self._pad:
            # the cursor stands past the end of its line: blanks up to it
            self._wc.insertText(" " * self._pad, self._plain_fmt())
            self._pad = 0
        pos = self._wc.position()
        room = 0 if pos >= self._out_end() else self._line_end() - pos
        if room <= 0:
            self._wc.insertText(s, fmt)
            return
        # Overwrite mode (the cursor went back over existing text after a \r):
        # replace everything the new text covers with ONE selection and ONE
        # insert.  Doing it per character — each with an O(n) ``rem = rem[1:]``
        # slice and its own edit — made progress-style output stutter.
        overlap = min(len(s), room)
        self._wc.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, overlap)
        self._wc.insertText(s[:overlap], fmt)
        if len(s) > overlap:
            self._wc.insertText(s[overlap:], fmt)

    # CSI finals that move the cursor to another line or change lines: output
    # drawn line after line can show none of them (see _csi_dispatch)
    _SCREEN_FINALS = frozenset("ABEFHLMSTdefr")

    def _csi_dispatch(self, final, params):
        """Apply one CSI sequence to the output lines.

        True when it needs a screen: on a device terminal (see
        set_screen_input) a cursor position on another line, a move up or
        down, lines inserted, deleted or scrolled, a scroll region or the
        alternate screen.  Over a pipe they mean nothing and are ignored.
        The cursor going home is a clear when ED 2 follows (``clear``), so it
        waits for the next sequence (_home_pending); on a view with nothing
        on it, it is where the cursor is already."""
        if self._home_pending and not (final == "J" and params in ("2", "3")):
            return True
        if final == "m":
            # colours alone leave the line as it is: text drawn in them does not
            self._sgr_params(params)
            self._fmt_plain = self._is_plain(self._fmt)
            return False
        self._line_clean = False
        if params[:1] in ("?", ">", "<", "="):
            return self._private_csi(final, params)
        if final == "K":
            n = params or "0"
            if n in ("0", ""):
                end = self._line_end()
                if end > self._wc.position():
                    self._wc.setPosition(end, QTextCursor.KeepAnchor)
                    self._wc.removeSelectedText()
            elif n == "1":
                self._erase_line_start()
            elif n == "2":
                self._pad = 0
                self._wc.movePosition(QTextCursor.StartOfBlock)
                end = self._line_end()
                if end > self._wc.position():
                    self._wc.setPosition(end, QTextCursor.KeepAnchor)
                    self._wc.removeSelectedText()
        elif final == "J":
            n = params or "0"
            if n in ("2", "3"):
                self._home_pending = False
                self._clear_viewport()
            elif n in ("0", ""):
                end = self._out_end()
                if end > self._wc.position():
                    self._wc.setPosition(end, QTextCursor.KeepAnchor)
                    self._wc.removeSelectedText()
            elif n == "1":
                # From the top of the screen through the cursor: the lines
                # above are the scrollback here, which this deleted whole.
                # Only the cursor's line is blanked, as EL 1 does.
                self._erase_line_start()
        elif final in "Ca":
            self._move_to_column(self._column() + _count(params))
        elif final == "D":
            self._move_to_column(self._column() - _count(params))
        elif final in "G`":
            self._move_to_column(_count(params) - 1)
        elif final == "X":
            self._erase_chars(_count(params))
        elif final == "P":
            self._delete_chars(_count(params))
        elif final == "@":
            self._insert_blanks(_count(params))
        elif final == "n":
            if params == "5":
                self._screen_reply("\x1b[0n")
            elif params == "6":
                self._screen_reply(f"\x1b[{self._report_row()};{self._column() + 1}R")
        elif final == "c":
            if params in ("", "0"):
                self._screen_reply("\x1b[?1;2c")
        elif final == "s":
            if not params:
                self._save_column()
        elif final == "u":
            if not params:
                return self._restore_column()
        elif final in self._SCREEN_FINALS and self._screen_input is not None:
            if final in "Hf" and _is_home(params):
                if self._out_end() == 0:
                    return False  # nothing on the view: the cursor is home already
                self._home_pending = True
                return False
            return True
        return False

    def _private_csi(self, final, params):
        """A CSI with a private parameter prefix (``?25l``, ``?1049h`` ...)."""
        if params[0] != "?":
            return False  # ">c" (which terminal is this?) and friends: no answer
        modes = [int(part) for part in params[1:].split(";") if _is_number(part)]
        if final == "n":
            if modes[:1] == [6]:
                self._screen_reply(f"\x1b[?{self._report_row()};{self._column() + 1}R")
            return False
        if final not in "hl":
            return False
        on = final == "h"
        if on and self._screen_input is not None and any(m in (47, 1047, 1049) for m in modes):
            return True
        for mode in modes:
            if mode in (1, 7, 25, 2004):
                self._modes[mode] = on  # cursor keys, wrap, cursor, paste: for a screen to come
        return False

    def _esc_dispatch(self, mid, final):
        """Apply ``ESC`` *mid* *final* to the output lines.  True when it needs
        a screen (up a line: ``ESC M``; back to a cursor saved on another line)."""
        if self._home_pending:
            return True
        self._line_clean = False
        if mid:
            return False  # charset designations and the like: nothing to draw
        if final == "7":
            self._save_column()
        elif final == "8":
            return self._restore_column()
        elif final == "M":
            return self._screen_input is not None
        elif final == "c":
            # a full reset (``reset``): colours off and a clean view
            self._sgr(0)
            self._fmt_plain = True
            self._modes.clear()
            self._clear_viewport()
        return False

    def _report_row(self) -> int:
        """The cursor's row for a position report on the output lines: its
        line, counted from the top of a view that is not full yet."""
        rows = max(1, self.cell_size()[1])
        return min(rows, self._wc.blockNumber() + 1)

    def _screen_reply(self, text):
        """Answer a program's question (cursor position, status) on a device
        terminal; over a pipe nobody asks."""
        send = self._screen_input
        if send is not None:
            try:
                send(text.encode("utf-8"))
            except Exception:
                pass

    # ---- a full-screen program's screen (see the module docstring) ---------
    # Rows that scrolled off a program's screen, kept for the scrollback: a
    # few thousand (80,000 of them took seconds to write when it went).
    _SCREEN_SCROLLBACK = 5000

    def has_screen(self) -> bool:
        """Whether a program's screen is showing."""
        return self._screen is not None

    def screen(self):
        """The :class:`vtscreen.Screen` showing, or None."""
        return self._screen

    def cell_size(self):
        """``(columns, rows)`` of whole character cells the view shows: what a
        device terminal's ``stty cols/rows`` should say.  The margins and
        the vertical scroll bar are left out, whether the bar shows yet or
        not, so the number stays put as output fills the view and a line of
        that many characters never scrolls sideways.  (Neither is a
        sideways scroll bar, which only a longer line brings.)"""
        return self._cells(self.contentsRect().height())

    def _cells(self, height):
        fm = QFontMetricsF(self.document().defaultFont())
        advance = fm.horizontalAdvance("M") or 1.0
        line = fm.lineSpacing() or 1.0
        margin = self.document().documentMargin()
        width = self.contentsRect().width() - self.verticalScrollBar().sizeHint().width()
        cols = int((width - 2 * margin) // advance)  # a line may end at the very edge
        rows = int((height - 2 * margin) // line)
        return max(0, cols), max(0, rows)

    def _screen_size(self):
        """``(columns, rows)`` for a program's screen: :meth:`cell_size`, less
        a sideways scroll bar that shows (an earlier line was that long)."""
        bar = self.horizontalScrollBar()
        if bar.isVisible():
            return self._cells(self.contentsRect().height() - bar.height())
        return self.cell_size()

    def _pending_home(self) -> str:
        if self._home_pending:
            self._home_pending = False
            return "\x1b[H"
        return ""

    def _attr_of(self, fmt):
        """The screen attribute (see vtscreen) of text drawn in *fmt*."""
        fg = bg = vtscreen.DEFAULT
        brush = fmt.foreground()
        if brush.style() != Qt.NoBrush and brush.color().name() != QColor(self._fg_default).name():
            fg = vtscreen.TRUECOLOR | (brush.color().rgb() & 0xFFFFFF)
        brush = fmt.background()
        if brush.style() != Qt.NoBrush:
            bg = vtscreen.TRUECOLOR | (brush.color().rgb() & 0xFFFFFF)
        flags = 0
        if fmt.fontWeight() > QFont.Normal:
            flags |= vtscreen.BOLD
        if fmt.fontItalic():
            flags |= vtscreen.ITALIC
        if fmt.fontUnderline():
            flags |= vtscreen.UNDERLINE
        if fmt.fontStrikeOut():
            flags |= vtscreen.STRIKE
        return fg, bg, flags

    def _format_of(self, attr):
        """The text format of screen cells with *attr* (see vtscreen)."""
        key = ("cell", attr)
        fmt = self._level_formats.get(key)
        if fmt is None:
            fg, bg = screen_color(attr[0], False), screen_color(attr[1], True)
            flags = attr[2]
            if flags & vtscreen.REVERSE:
                fg, bg = bg, fg
            if flags & vtscreen.DIM:
                fg = QColor((fg.red() + bg.red()) // 2, (fg.green() + bg.green()) // 2,
                            (fg.blue() + bg.blue()) // 2)
            if flags & vtscreen.INVISIBLE:
                fg = bg
            fmt = QTextCharFormat()
            fmt.setForeground(fg)
            if attr[1] != vtscreen.DEFAULT or flags & vtscreen.REVERSE:
                fmt.setBackground(bg)
            if flags & vtscreen.BOLD:
                fmt.setFontWeight(QFont.Bold)
            fmt.setFontItalic(bool(flags & vtscreen.ITALIC))
            fmt.setFontUnderline(bool(flags & vtscreen.UNDERLINE))
            fmt.setFontStrikeOut(bool(flags & vtscreen.STRIKE))
            self._level_formats[key] = fmt
        return fmt

    def _add_row(self, runs, line, keep=0):
        """Add the screen row *line* to *runs* (see _write_rows): blanks at its
        end are left off, except the first *keep* cells (up to the cursor on
        its row)."""
        chars, attrs = line.chars, line.attrs
        end = len(chars)
        while end > keep and chars[end - 1] in (" ", "") and attrs[end - 1][1] == vtscreen.DEFAULT \
                and not attrs[end - 1][2] & vtscreen.REVERSE:
            end -= 1
        start = 0
        while start < end:
            attr = attrs[start]
            stop = start + 1
            while stop < end and attrs[stop] == attr:
                stop += 1
            self._add_run(runs, "".join(chars[start:stop]), attr)
            start = stop

    @staticmethod
    def _add_run(runs, text, attr):
        """Add *text* in the screen attribute *attr* to *runs*, a list of
        ``[pieces, attr]``: text in the attribute of the run before joins it."""
        if not text:
            return
        if runs and runs[-1][1] == attr:
            runs[-1][0].append(text)
        else:
            runs.append([[text], attr])

    def _write_runs(self, cursor, runs):
        """Insert *runs* at *cursor* and empty the list."""
        for pieces, attr in runs:
            cursor.insertText("".join(pieces), self._format_of(attr))
        del runs[:]

    def _write_rows(self, cursor, rows, seeds, col):
        """Write a program's screen *rows* at *cursor*, the last one at
        least up to column *col*.  A row the program never touched goes back
        exactly as it was (its fragment in *seeds*); the others as text, one
        insert for everything in one format, however many rows it spans (an
        insert per row and line break took seconds for many rows)."""
        runs = []
        for index, line in enumerate(rows):
            if index:
                self._add_run(runs, "\n", vtscreen.PLAIN)
            seed = line.seed
            if (seed is not None and seed < len(seeds) and seeds[seed][1] == line.chars
                    and seeds[seed][2] == line.attrs):
                self._write_runs(cursor, runs)
                cursor.insertFragment(seeds[seed][0])  # untouched: exactly as it was
            else:
                self._add_row(runs, line, keep=col if index == len(rows) - 1 else 0)
        self._write_runs(cursor, runs)

    def _enter_screen(self, data):
        """A program on a device terminal addressed the cursor: show a screen
        for it, and draw *data* on it.  The screen starts as the view was: its
        rows are the last lines of the output, the cursor where the output's
        cursor was, in the colours and modes the output left on."""
        cols, rows = self._screen_size()
        cols, rows = max(cols, 2), max(rows, 2)
        document = self.document()
        end = self._out_end()
        last = document.findBlock(end)
        blocks = [last]
        while len(blocks) < rows and blocks[0].previous().isValid():
            blocks.insert(0, blocks[0].previous())
        seeds, fragments = [], []
        for block in blocks:
            stop = end if block == last else block.position() + block.length() - 1
            runs = []
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid() and fragment.position() < stop:
                    text = fragment.text()[: stop - fragment.position()]
                    runs.append((text, self._attr_of(fragment.charFormat())))
                it += 1
            seeds.append(runs)
            cursor = QTextCursor(document)
            cursor.setPosition(block.position())
            cursor.setPosition(stop, QTextCursor.KeepAnchor)
            fragments.append(cursor.selection())
        here = self._wc.block()
        row = next((index for index, block in enumerate(blocks) if block == here), len(blocks) - 1)
        screen = vtscreen.Screen(rows, cols, reply=self._screen_reply)
        screen.seed(seeds, (row, self._wc.positionInBlock() + self._pad))
        self._screen_seed = [
            (fragment, list(line.chars), list(line.attrs))
            for fragment, line in zip(fragments, screen.main_lines())
        ]
        screen.use_attr(*self._attr_of(self._fmt))
        if self._saved_column is not None:
            saved = next((index for index, block in enumerate(blocks)
                          if block.blockNumber() == self._saved_column[0]), None)
            if saved is not None:
                screen.save_cursor_at(saved, self._saved_column[1])
        for mode, on in self._modes.items():
            screen.feed(f"\x1b[?{mode}{'h' if on else 'l'}")
        base = QTextCursor(document)
        base.setPosition(blocks[0].position())
        self._screen = screen
        self._screen_base = base
        self._screen_scrolled = []
        self._screen_notes = []
        self._screen_painted = -1
        self._home_pending = False
        self._state = 0
        self._csi = ""
        self._pad = 0
        self._start_line()
        self._logcat.reset()
        view = self._screen_view
        if view is None:
            view = self._screen_view = ScreenView(self)
        view.set_screen(screen)
        view.setGeometry(self.viewport().geometry())
        view.show()
        view.raise_()
        self.screen_changed.emit(True)
        self._screen_feed(data)

    def _screen_feed(self, data):
        """Draw a program's output on its screen."""
        screen = self._screen
        screen.feed(data)
        self._keep_scrolled(screen.take_scrolled_off())
        if screen.alt_used and not screen.alt_active:
            self._leave_screen()  # the program left the alternate screen: it is done

    def _keep_scrolled(self, rows):
        """Keep *rows*, which scrolled off the top of a program's screen, for
        the scrollback: the newest :attr:`_SCREEN_SCROLLBACK` of them."""
        if rows:
            self._screen_scrolled.extend(rows)
            excess = len(self._screen_scrolled) - self._SCREEN_SCROLLBACK
            if excess > 0:
                del self._screen_scrolled[:excess]

    def _leave_screen(self):
        """The program's screen goes; the view shows the output lines again.

        Rows that scrolled off the screen's top and its rows down to the
        cursor take the place of the lines it started from: the last frame
        of the main screen stays, as in a terminal window, and what was on
        the alternate screen goes with it.  Rows the program never touched
        keep their text and colours as they were.  A screen that was wiped
        and holds nothing above its cursor (``printf '\\e[H\\e[J'`` and the
        prompt) clears the view as ``clear`` does."""
        screen = self._screen
        if screen is None:
            return
        screen.leave_alternate()
        self._screen = None
        row, col = screen.cursor
        lines = screen.main_lines()
        rows = self._screen_scrolled + lines[: row + 1]
        seeds, self._screen_seed = self._screen_seed, []
        self._screen_scrolled = []
        wipe = screen.scrollback_cleared or (
            screen.wiped and len(rows) == row + 1 and all(line.is_blank() for line in rows[:-1])
        )
        updates = self.updatesEnabled()
        self.setUpdatesEnabled(False)
        try:
            document = self.document()
            end = self._out_end()
            base = 0 if wipe or self._screen_base is None else min(self._screen_base.position(), end)
            self._screen_base = None
            cursor = QTextCursor(document)
            cursor.setPosition(base)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            cursor.beginEditBlock()  # one edit: the rows are laid out once, not per insert
            try:
                cursor.removeSelectedText()
                self._write_rows(cursor, rows, seeds, col)
            finally:
                cursor.endEditBlock()
            # the output goes on where the program left the cursor
            block = cursor.block()
            length = cursor.position() - block.position()
            column = _doc_len("".join(rows[-1].chars[:col])) if rows else 0
            self._wc = QTextCursor(document)
            self._wc.setPosition(block.position() + min(column, length))
            self._pad = max(0, column - length)
            self._fmt = QTextCharFormat(self._format_of(screen.attr)) \
                if screen.attr != vtscreen.PLAIN else self._plain_fmt()
            self._fmt_plain = screen.attr == vtscreen.PLAIN
            self._line_clean = False
            self._line_head = ""
            self._cr_pending = False
            view = self._screen_view
            view.hide()
            view.set_screen(None)
            notes, self._screen_notes = self._screen_notes, []
            for marker in notes:
                self._render_text(marker.runs, marker.fragment, marker.fresh_line)
            self._fix_edit_sep()
        finally:
            self.setUpdatesEnabled(updates)
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.screen_changed.emit(False)

    def _drop_screen(self):
        """Forget a program's screen without writing it anywhere (the view is
        being cleared or closed)."""
        if self._screen is None:
            return
        self._screen = None
        self._screen_base = None
        self._screen_seed = []
        self._screen_scrolled = []
        self._screen_notes = []
        if self._screen_view is not None:
            self._screen_view.hide()
            self._screen_view.set_screen(None)
        self.screen_changed.emit(False)

    def _place_screen(self):
        """Keep the screen over the view, as big as the view."""
        screen, view = self._screen, self._screen_view
        if screen is None or view is None:
            return
        view.setGeometry(self.viewport().geometry())
        cols, rows = self._screen_size()
        if cols >= 2 and rows >= 2 and (cols, rows) != (screen.cols, screen.rows):
            screen.resize(rows, cols)
            self._keep_scrolled(screen.take_scrolled_off())
            view.update()
            self.screen_resized.emit(cols, rows)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_screen()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self._screen is not None:
            self._screen_view.update()  # a solid cursor again

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if self._screen is not None:
            self._screen_view.update()

    def _screen_key(self, event):
        """A key while a program has the screen: to the program, as a terminal
        sends it.  Copy, paste and the text size keep their shortcuts, and
        Ctrl+C copies a selection instead of reaching the program."""
        mods, key = event.modifiers(), event.key()
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        alt = bool(mods & Qt.AltModifier)
        if (ctrl and shift and key == Qt.Key_C) or (ctrl and key == Qt.Key_Insert):
            self._copy_selection()
            return
        if (ctrl and key == Qt.Key_V) or (shift and key == Qt.Key_Insert):
            self._paste_into_line()
            return
        if ctrl and not alt and key in (Qt.Key_Plus, Qt.Key_Equal):
            self.bump_font(1)
            return
        if ctrl and not alt and key == Qt.Key_Minus:
            self.bump_font(-1)
            return
        if ctrl and key == Qt.Key_C and self._copy_selection():
            return
        if not (self._send and self._alive):
            return
        text = self._screen_key_text(event, ctrl, shift, alt)
        if text:
            self._screen_send(text.encode("utf-8"))

    def _screen_key_text(self, event, ctrl, shift, alt):
        key, text = event.key(), event.text()
        name = _SCREEN_KEYS.get(key)
        if name is not None:
            return vtscreen.key_sequence(
                name, shift=shift or key == Qt.Key_Backtab, alt=alt, ctrl=ctrl,
                app_cursor=self._screen.app_cursor_keys,
            )
        if ctrl and alt and text and text.isprintable():
            return text  # AltGr (Ctrl+Alt on Windows) typed a character
        if ctrl and 0x20 <= key < 0x7F:
            control = vtscreen.control_character(chr(key))
            if control:
                return ("\x1b" if alt else "") + control
        if text and text.isprintable():
            return ("\x1b" if alt else "") + text
        return ""

    def _screen_send(self, data: bytes) -> bool:
        """Send *data* to the program that has the screen.  Never 0x1A: it is
        end of input to adb.exe, which then forwards nothing more.  Never a
        bare CR at the end either, which adb.exe holds until more comes:
        Ctrl+M is Enter, a line feed, as the Enter key sends it.  Ctrl+C
        stops a flood the program printed as it does in line mode (see
        :meth:`interrupt_output`), and still goes to the program."""
        if b"\x1a" in data:
            data = data.replace(b"\x1a", b"")
            self._screen_note("Ctrl+Z is not sent: adb would stop reading this terminal's input for good")
        if data.endswith(b"\r"):
            data = data[:-1] + b"\n"
        send = self._screen_input
        if send is None or not data:
            return False
        if data == b"\x03":
            self.interrupt_output(until_echo=False)
        if self._screen_view is not None:
            self._screen_view.clear_selection()
        try:
            return send(data) is not False
        except Exception:
            return False

    def _screen_note(self, text):
        """A short note over the screen (it hides the output lines, where a
        notice would go)."""
        view = self._screen_view
        if view is not None and view.isVisible():
            QToolTip.showText(view.mapToGlobal(view.rect().center()), text, view)

    def _screen_paste(self, text):
        """Paste goes to the program as typed text (in bracketed-paste
        markers when it asked for them); line breaks as LF."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if self._screen.bracketed_paste:
            text = "\x1b[200~" + text.replace("\x1b[201~", "") + "\x1b[201~"
        self._screen_send(text.encode("utf-8"))

    def screen_wheel(self, event):
        """The wheel over a program's screen: Ctrl zooms, as everywhere; on
        the alternate screen (``less``, ``vi``) it scrolls the program with
        the arrow keys, three lines a notch."""
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.ControlModifier:
            if delta:
                self.bump_font(1 if delta > 0 else -1)
            return
        screen = self._screen
        if screen is None or not screen.alt_active or not delta:
            return
        key = vtscreen.key_sequence("up" if delta > 0 else "down", app_cursor=screen.app_cursor_keys)
        self._screen_send((key * max(1, abs(delta) // 40)).encode("utf-8"))

    def screen_menu(self, global_pos):
        """The context menu, over a program's screen."""
        menu = self._build_menu()
        menu.exec_(global_pos)
        menu.deleteLater()

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
        """Write our own text (a completion list, ``^C``) as output, after
        anything still queued; the edit region stays below it."""
        fmt = self._plain_fmt(color) if color else QTextCharFormat(self._fmt)
        self._write_text([(s, fmt)])

    def _prompt_if_needed(self):
        """Draw the synthetic prompt at the start of the edit region, if one is due."""
        if not self._emulate_prompt or not self._need_prompt or self._prompt_len:
            return
        scrollbar = self.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
        runs = self._styled_runs(self._prompt_text())
        cursor = QTextCursor(self.document())
        cursor.setPosition(self._out_end())
        # output that stopped mid-line keeps its line; this break is part of
        # the edit region and goes again with the prompt
        sep = 0 if cursor.atBlockStart() else 1
        wpos = self._wc.position()
        if sep:
            cursor.insertText("\n", self._plain_fmt())
        for text, fmt in runs:
            cursor.insertText(text, fmt)
        self._wc.setPosition(wpos)
        self._edit_sep = sep
        self._shown_prompt = "".join(text for text, _fmt in runs)
        self._prompt_len = len(self._shown_prompt)
        self._need_prompt = False
        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _sync_cursor(self):
        """Put the caret at ``_cpos`` in the edit line and scroll to it."""
        if self._keep_view:
            return
        cursor = QTextCursor(self.document())
        end = self.document().characterCount() - 1
        cursor.setPosition(end - self._line_len + max(0, min(self._cpos, self._line_len)))
        self.setTextCursor(cursor)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _redraw_line(self):
        """Show ``_line`` at the end of the edit region; nothing else changes."""
        end = self.document().characterCount() - 1
        cursor = QTextCursor(self.document())
        cursor.setPosition(end - self._line_len)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        wpos = self._wc.position()
        if self._line:
            cursor.insertText(self._line, self._plain_fmt())
        else:
            cursor.removeSelectedText()
        self._wc.setPosition(wpos)
        self._line_len = len(self._line)
        if not self._line:
            # the line a paste left for review was cleared (Esc, Backspace …)
            self._paste_tail_open = False
            if (self._emulate_prompt and self._alive and self._need_prompt
                    and not self._idle.isActive()):
                self._idle.start(self._prompt_idle_ms())  # the prompt may be due now
        self._sync_cursor()

    def _erase_input(self):
        self._line = ""
        self._cpos = 0
        self._redraw_line()

    def _set_line(self, new):
        self._line = new
        self._cpos = len(new)
        self._redraw_line()

    def _insert_text(self, text):
        """Insert *text* into the edit line at the caret (typing, a one-line paste, IME)."""
        self._line = self._line[:self._cpos] + text + self._line[self._cpos:]
        self._cpos += len(text)
        self._redraw_line()

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
        from .fileutil import copy_to_clipboard

        if self._screen is not None:
            view = self._screen_view
            if not view.has_selection():
                return False
            copy_to_clipboard(self, view.selected_text())
            return True
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return False
        # Use the application's clipboard explicitly.  Calling the inherited
        # ``copy`` is normally equivalent, but this remains reliable when the
        # console owns key handling and does not delegate to QPlainTextEdit.
        copy_to_clipboard(self, cursor.selectedText().replace("\u2029", "\n"))
        return True

    def _has_selection(self) -> bool:
        if self._screen is not None:
            return self._screen_view.has_selection()
        return self.textCursor().hasSelection()

    def _select_all(self):
        if self._screen is not None:
            self._screen_view.select_all()
        else:
            self.selectAll()

    def _cut_selection(self) -> bool:
        """Cut only editable command input; terminal history stays immutable.

        Ctrl+X on historical output acts like a safe copy.  If the selection is
        wholly inside the command currently being edited, it is also removed
        from that command, matching the usual Cut shortcut without allowing a
        user to accidentally erase received terminal output.
        """
        if self._screen is not None:
            return self._copy_selection()  # the screen is not editable: a copy
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return False
        self._copy_selection()

        input_end = self.document().characterCount() - 1
        input_start = input_end - self._line_len
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        if start < input_start or end > input_end:
            self._move_caret_end()
            return True

        rel_start = start - input_start
        rel_end = end - input_start
        self._line = self._line[:rel_start] + self._line[rel_end:]
        self._cpos = rel_start
        self._redraw_line()
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
        is one (keyboard Ctrl+C and the context menu share this path).

        The line being typed is dropped, as Ctrl+C does in a terminal.  An
        owner draws whatever else it needs (a stop notice, the prompt through
        ``set_alive``), so the prompt is left alone here: drawing one as well
        printed two prompts."""
        self._cancel_paste()
        if self._interrupt:
            self._erase_input()
            self._interrupt()
        else:
            self._commit_edit("^C\n")
            self._line = ""
            self._cpos = 0
            if self._send:
                self._send(b"\x03")
            if self._emulate_prompt:
                self._idle.start(self._prompt_idle_ms())
        self._move_caret_end()

    def keyPressEvent(self, event):
        if self._screen_takes_keys():
            self._screen_key(event)  # a program has the screen: every key is its
            return
        # A screen whose terminal is gone takes no keys: they go to the edit
        # line, which shows once the screen has gone (see end_screen).
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
        if self._pasting:
            self._paste_type_ahead(event)
            return

        if key in (Qt.Key_Return, Qt.Key_Enter):
            ends_paste = self._paste_tail_open
            self._paste_tail_open = False
            self._submit_line(self._line)
            if ends_paste:
                # This was the last line of a multi-line paste, left for review.
                self._watch_paste_block()
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
            self._line = self._line[:p] + self._line[self._cpos:]
            self._cpos = p
            self._redraw_line()
            return

        if key == Qt.Key_Backspace:
            if self._cpos > 0:
                self._line = self._line[:self._cpos - 1] + self._line[self._cpos:]
                self._cpos -= 1
                self._redraw_line()
            return

        if key == Qt.Key_Delete:
            if self._cpos < len(self._line):
                self._line = self._line[:self._cpos] + self._line[self._cpos + 1:]
                self._redraw_line()
            return

        if key == Qt.Key_Escape:
            self._erase_input()
            return

        if key in (Qt.Key_Up, Qt.Key_Down):
            self._paste_tail_open = False  # history replaces the edit line
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
            # Typing never draws the synthetic prompt: a command still running
            # would look finished (the prompt shows once the output is quiet).
            self._insert_text(text)
            return

    def inputMethodEvent(self, event):
        """Committed input-method text (CJK input, compose and dead keys) goes
        into the command line like typed keys, never straight into the output."""
        text = event.commitString()
        event.accept()
        if text and self._send and self._alive:
            if self._screen_takes_keys():
                self._screen_send(text.encode("utf-8"))  # typed, not pasted
                return
            self._clear_tab_cycle()
            self._paste_text(text)

    def canInsertFromMimeData(self, source):
        return source is not None and source.hasText()

    def insertFromMimeData(self, source):
        """A paste Qt performs itself (an X11 middle-click) goes through the
        command line like Ctrl+V, never straight into the output."""
        if source is not None and source.hasText() and self._send and self._alive:
            self._paste_text(source.text())

    def _submit_line(self, cmd, *, typeahead=False):
        """Run *cmd* as if it had been typed and Enter pressed.

        Enter and every pasted line come through here, so echo suppression,
        history, ``cd`` tracking, the archive and the prompt stay identical.

        What the line becomes on screen depends on whether the shell waits at
        its own prompt (see :meth:`set_shell_at_prompt`).  At the prompt the
        line is a command: it is drawn at once, and a local shell's echo of it
        is hidden.  Otherwise it is input for the running command: a non-empty
        line is drawn once and no echo is expected (a program reading its
        stdin does not echo it); an empty line only ends a line the command
        left open, so it never puts a blank line into the output (the shell
        prints its own line break and prompt when it reads the line).

        *typeahead* is for a pasted line that has to go to a local shell while
        its previous command is still running (see ``_paste_ready``).  The shell
        echoes the line itself, after its prompt, when it finally reads it, so
        the line is neither drawn nor archived here and no echo is suppressed.
        The Android shell never echoes, so it always draws the line.
        """
        emulate = self._emulate_prompt
        typeahead = (typeahead or self._typing_ahead) and not emulate
        if emulate and not self._line and self._prompt_due():
            # a pasted line runs once the output went quiet: at the prompt
            self._prompt_if_needed()
        if not typeahead and cmd != self._line:
            self._set_line(cmd)

        if self._at_prompt is not None:
            at_prompt = self._at_prompt
        elif emulate:
            at_prompt = bool(self._prompt_len)
        elif cmd:
            at_prompt = True  # unknown: a typed command goes to the shell, as always
        else:
            at_prompt = self._at_shell_prompt()
        if emulate and at_prompt and not self._prompt_len:
            self._need_prompt = True
            self._prompt_if_needed()

        if cmd.strip():
            self._history.append(cmd)
            if len(self._history) > self._HISTORY_MAX:
                del self._history[:-self._HISTORY_MAX]  # a long paste ran every line
        self._hidx = len(self._history)
        self._last_feed = time.monotonic()

        if typeahead:
            shown = False
        elif cmd or at_prompt:
            shown = True
        else:
            # An empty line for a running command ends a line it left open
            # (its own input prompt), but never adds a blank line to its output.
            shown = bool(self._output_tail_line())
        if shown and not emulate:
            # output held back as a possible echo of the last line came first
            self.clear_pending_echo()
        prompt = self._shown_prompt if self._prompt_len else ""
        if shown:
            self._commit_edit("\n")
            self._sb.archive(prompt + cmd + "\n")
            if cmd and not at_prompt and not emulate:
                # input for the running command, unless the shell reads it
                # later as a command: then its echo shows it (see forget_typed_line)
                self._remember_typed(cmd)
        elif self._line_len:
            self._erase_input()  # typed ahead: the shell's echo shows it
        self._line = ""
        self._cpos = 0
        if emulate:
            self._cd_revert = None
            if cmd.strip() and cmd.strip().split()[0] == "cd":
                self._apply_cd(cmd)
            to_send = (self._columnize(cmd) + "\n").encode("utf-8")
        else:
            # Local shell (PowerShell or CMD)
            if shown:
                if at_prompt:
                    self._pending_echo = cmd if cmd else "\r\n"
                    self._pending_echo_at = time.monotonic()
                self._feed_at_line_start = True
            to_send = (cmd + "\r\n").encode("utf-8")
        if self._at_prompt is not None:
            self._at_prompt = False  # the shell is busy with this line now

        self._submit_count += 1
        self._submitted_at = time.monotonic()
        self._submitted_feeds = self._feed_count
        self._submitting = True
        try:
            self._send(to_send)
        except Exception:
            pass
        finally:
            self._submitting = False
        if typeahead or (shown and not at_prompt and not emulate):
            # send_fn may name a rewritten command's echo; this line has none
            # to hide: the shell echoes a typed-ahead line itself, and a
            # program reading an answer does not echo it.
            self.clear_pending_echo()

        if emulate:
            self._need_prompt = not self._prompt_len
            self._idle.start(self._prompt_idle_ms())
        else:
            self._need_prompt = False
        self._move_caret_end()

    def _paste_into_line(self):
        txt = QApplication.clipboard().text()
        if txt:
            self._paste_text(txt)

    def _paste_text(self, txt):
        """Paste *txt* at the cursor like a terminal would.

        Text without a line break is inserted into the edit line.  Otherwise
        the text goes in at the cursor of what is already typed and every
        complete line runs, in order and exactly once, each only after the
        previous one had its turn (``_pump_paste``).  Whatever follows the last
        line break stays in the edit line for review, so a trailing newline
        runs the last line too.
        """
        txt = txt.replace("\r\n", "\n").replace("\r", "\n")
        if not txt:
            return
        if self._screen_takes_keys():
            if self._send and self._alive:
                self._screen_paste(txt)
            return
        if self._pasting:
            if self._paste_keys:
                # Keys typed meanwhile are waiting: this paste comes after them.
                self._paste_keys.append((None, None, txt))
                return
            # An earlier paste is still running: this one queues behind it.
            lines = (self._paste_tail + txt).split("\n")
            self._paste_tail = lines.pop()
            if lines:
                self._paste_lines.extend(lines)
                self._paste_multi = True
                self._paste_block_watch = False  # its last line is now a later one
            return
        if "\n" not in txt:
            self._insert_text(txt)  # the caret stays right after the text
            return
        if not (self._send and self._alive):
            return

        lines = (self._line[:self._cpos] + txt + self._line[self._cpos:]).split("\n")
        tail = lines.pop()
        self._clear_tab_cycle()
        self._paste_lines = deque(lines)
        self._paste_tail = tail
        self._paste_multi = len(lines) + (1 if tail else 0) > 1
        self._paste_tail_open = False
        self._paste_block_watch = False
        self._pasting = True
        self._submit_line(self._paste_lines.popleft())
        if not (self._paste_lines or tail or self._paste_multi):
            self._pasting = False  # one line and its newline: exactly Enter
            return
        self._paste_timer.start()

    def _paste_type_ahead(self, event):
        """A key pressed while a paste runs.

        Esc drops the rest of the paste.  Any other key waits behind the
        pasted lines, as a terminal's type-ahead does, and is replayed in order
        once they have run and the left-over text is in the edit line.
        """
        if event.key() == Qt.Key_Escape:
            self._cancel_paste()
        else:
            self._paste_keys.append((event.key(), event.modifiers(), event.text()))

    def _replay_paste_keys(self):
        """Replay the keys typed during the paste; True if there were any.

        A key that runs a line (Enter) or starts another paste ends the replay
        for this turn: the keys after it wait for that line, as they would in a
        terminal.
        """
        replayed = False
        while self._paste_keys and not self._pasting:
            key, mods, text = self._paste_keys.popleft()
            replayed = True
            submitted = self._submit_count
            if key is None:
                self._paste_text(text)
            else:
                self.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, mods, text))
            if self._submit_count != submitted and self._paste_keys and not self._pasting:
                self._pasting = True
        return replayed

    def _cancel_paste(self):
        """Forget pasted lines that have not run yet (Ctrl+C, Stop, Esc)."""
        self._paste_lines.clear()
        self._paste_keys.clear()
        self._paste_tail = ""
        self._pasting = False
        self._paste_multi = False
        self._paste_block_watch = False
        self._paste_tail_open = False
        self._paste_timer.stop()

    def _watch_paste_block(self):
        """After a paste's last line, complete a PowerShell block if one is open.

        PowerShell reading a pipe answers every line of a ``foreach``/``if``
        block, even the closing ``}``, with its ``>>`` continuation prompt and
        runs the block only after an empty line, so the paste looked hung.  The
        empty line is sent only when that prompt is really showing: an ordinary
        paste gets no stray extra prompt, and CMD (which has no ``>>``) never
        receives one."""
        if not self._emulate_prompt:
            self._paste_block_watch = True
            self._paste_timer.start()

    def _at_continuation_prompt(self):
        return self._output_tail_line().rstrip() == ">>"

    def _at_shell_prompt(self):
        """True when the shell waits for input: the owner says so, or the last
        output line looks like a prompt.

        Output that merely pauses mid-line (progress dots, a block-buffered
        program) is not one: running the next line then would put it inside
        that output."""
        if self._at_prompt is not None:
            return self._at_prompt
        return bool(_PROMPT_END_RE.search(self._output_tail_line()))

    def _paste_ready(self):
        """Whether the last submitted line has had its turn.

        Returns ``"idle"`` once it finished, ``"timeout"`` after
        ``_PASTE_MAX_WAIT_S`` and ``None`` while it still runs.  The bound
        keeps a command that never goes quiet (``logcat``, ``ping -t``) from
        holding up the rest of a paste; shells read stdin in order, so the next
        line then simply waits in the shell's input.
        """
        now = time.monotonic()
        if now - self._submitted_at >= self._PASTE_MAX_WAIT_S:
            return "timeout"
        if self._inq:
            return None  # the previous line's output is still being drawn
        quiet = now - max(self._submitted_at, self._last_feed)
        if self._emulate_prompt:
            # Over a pipe the Android shell prints no echo and no prompt: quiet
            # output is the sign a command finished, as for the emulated prompt.
            return "idle" if quiet >= self._PASTE_IDLE_S else None
        # PowerShell and CMD echo the line, run it, then print their prompt
        # without a newline.  Anything else waits for the bounded timeout.
        if (
            self._pending_echo is None
            and self._feed_count > self._submitted_feeds
            and quiet >= self._PASTE_LOCAL_IDLE_S
            and self._at_shell_prompt()
        ):
            return "idle"
        return None

    def _pump_paste(self):
        """Paste timer: run the next pasted line once the previous one is done.

        One line per tick at most, so even thousands of lines never hold up
        the UI thread.  A line run by the timer leaves the user's selection
        and scroll position alone, like streaming output, and follows only a
        view that was already at the bottom; so Ctrl+C on a selection made
        meanwhile still copies instead of interrupting the shell."""
        scrollbar = self.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
        self._keep_view = True
        try:
            changed = self._paste_step()
        finally:
            self._keep_view = False
        if changed and at_bottom:
            if self.textCursor().hasSelection():
                scrollbar.setValue(scrollbar.maximum())
            else:
                self._sync_cursor()

    def _paste_step(self):
        """One turn of the paste timer; True when it changed the terminal."""
        if not (self._pasting or self._paste_block_watch):
            self._paste_timer.stop()
            return False
        if not self._alive:
            self._cancel_paste()
            return False
        ready = self._paste_ready()
        if not ready:
            return False
        timed_out = ready == "timeout"
        if self._pasting and self._paste_lines:
            # Only the paste's last line may complete a PowerShell block.
            self._paste_block_watch = False
            self._submit_line(self._paste_lines.popleft(), typeahead=timed_out)
            if not self._paste_lines and not self._paste_tail and self._paste_multi:
                self._watch_paste_block()
            return True
        changed = False
        if self._paste_block_watch:
            self._paste_block_watch = False
            if not timed_out and not self._line and self._at_continuation_prompt():
                self._submit_line("")
                changed = True
                if self._pasting:
                    return True  # the block runs now; place the edit line after it
        if self._pasting:
            tail, multi = self._paste_tail, self._paste_multi
            self._pasting = False
            self._paste_tail = ""
            self._paste_multi = False
            if tail:
                if self._emulate_prompt and self._prompt_due():
                    self._prompt_if_needed()  # the left-over line sits at a prompt
                self._set_line(tail)
                self._paste_tail_open = multi
                changed = True
            changed = self._replay_paste_keys() or changed
        if not (self._pasting or self._paste_block_watch):
            self._paste_timer.stop()
        return changed

    def _menu(self, pos):
        menu = self._build_menu()
        menu.exec_(self.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def _build_menu(self) -> QMenu:
        """The context menu (see :meth:`_menu`)."""
        ico = theme.emoji_icon
        m = QMenu(self)
        a = m.addAction(ico("📋"), "Copy")
        a.setEnabled(self._has_selection())
        a.triggered.connect(self.copy)
        # Only the on-screen scrollback is selectable; the complete history is
        # in "Save full output to file…".
        m.addAction(ico("🗂"), "Copy visible output", lambda: (self._select_all(), self.copy()))
        m.addAction(ico("📥"), "Paste", self._paste_into_line)
        m.addAction(ico("🔲"), "Select All", self._select_all)
        m.addSeparator()

        keys = m.addMenu(ico("⌨"), "Send key")
        left_out = False
        for label, data in (
            ("Enter (\\n)", b"\n"), ("Tab (\\t)", b"\t"),
            ("Esc", b"\x1b"), ("Ctrl+C", b"\x03"),
            ("Ctrl+D (EOF)", b"\x04"), ("Ctrl+Z", b"\x1a"),
            ("Up", b"\x1b[A"), ("Down", b"\x1b[B"),
            ("Backspace", b"\x7f")
        ):
            if data == b"\x03" and not self._screen_takes_keys():
                # same reliable stop as the keyboard shortcut (on a program's
                # screen Ctrl+C is a key for the program, as there)
                keys.addAction(label, lambda *_: self._send_interrupt() if self._alive else None)
            elif self._screen_takes_keys() or self._key_offered(data):
                # through the owner, which may refuse a byte (see set_send_key_fn)
                keys.addAction(label, lambda *_, d=data: self.send_key(d))
            else:
                left_out = True
        if left_out:
            keys.addSeparator()
            note = keys.addAction("The other keys need a device terminal (an adb shell)")
            note.setEnabled(False)

        m.addSeparator()
        m.addAction(ico("💾"), "Save full output to file…", self._save_output)
        m.addAction(ico("🧹"), "Clear", self.clear)
        return m

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

    def clear(self, *, keep_prompt=True):
        """Clear the terminal and forget its saved history (Clear, Ctrl+L).

        Output still waiting to be drawn goes too.  The shell's prompt line and
        the command being typed stay, so the terminal is ready at once; an
        owner replacing the shell itself passes ``keep_prompt=False``."""
        self._sb.reset()
        self._inq.clear()
        self._inq_len = 0
        self._drop_held()
        self._dropped = 0
        self._typed_ahead.clear()
        self._drain.stop()
        self._state = 0
        self._csi = ""
        self._skip_break = False
        self._archive_carry = ""
        self._feed_at_line_start = True
        self._paste_tail_open = False
        if self._screen is not None:
            self._screen.forget_partial()  # its rest was in what went
        if not keep_prompt:
            self._drop_screen()  # the shell itself is being replaced
            self._line = ""
            self._cpos = 0
            self._edit_sep = self._prompt_len = self._line_len = 0
            self._shown_prompt = ""
            self._pending_echo = None
            self._echo_held = ""
        self._clear_viewport(keep_prompt_line=keep_prompt)
        if self._emulate_prompt and not self._prompt_len:
            self._need_prompt = True
            if self._alive:
                # The output it would have followed is gone: once the shell is
                # quiet the prompt comes back by itself, not only after a key.
                self._idle.start(self._prompt_idle_ms())

    def close_archive(self):
        self._cancel_paste()
        self._drain.stop()
        self._idle.stop()
        self._held_timer.stop()
        self._screen_input = None
        self._drop_screen()
        self._sb.close()
