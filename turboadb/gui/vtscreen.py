"""A VT100/xterm screen for the terminal's full-screen programs.

The shell console draws output line by line, which is what a shell and its
commands need.  Programs that address the cursor (``top``, ``watch``, ``vi``,
``less``, progress displays that move back up) need a screen instead: a grid
of character cells that their output changes in place.  :class:`Screen` is
that grid and the parser that applies a program's output to it.  It is pure
Python with no Qt import, so it is tested on its own; ``screen_view`` paints
it and the console decides when a program uses it.

Understood: the C0 controls (BEL, BS, HT, LF/VT/FF, CR, SO/SI); ``ESC`` 7 8 D
E M c H = > and the charset designations (``ESC ( 0`` draws lines); the CSI
sequences that move the cursor, erase, insert and delete characters and
lines, scroll, set scroll regions and tab stops; SGR with 16, 256 and true
colours; the modes a full-screen program sets (insert, auto-wrap, origin,
application cursor keys, cursor visibility and shape, the alternate screen,
bracketed paste) and the queries it may send (cursor position, status,
identity, size), answered through *reply*.  OSC, DCS, PM, APC and SOS strings
are read and ignored.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, List, Optional, Sequence, Tuple

# Attribute flags (the third item of an attribute).
BOLD = 1
DIM = 2
ITALIC = 4
UNDERLINE = 8
BLINK = 16
REVERSE = 32
INVISIBLE = 64
STRIKE = 128

# A colour is DEFAULT, a palette index 0-255, or TRUECOLOR | 0xRRGGBB.
DEFAULT = -1
TRUECOLOR = 1 << 24

# An attribute is (foreground, background, flags).
Attr = Tuple[int, int, int]
PLAIN: Attr = (DEFAULT, DEFAULT, 0)

# Cursor shapes (DECSCUSR).
BLOCK, UNDERLINE_CURSOR, BAR = "block", "underline", "bar"

# One token of output: a run of printable characters, a CSI, OSC or other
# string sequence, another escape, or a single control character.
_TOKEN = re.compile(
    r"(?P<text>[^\x00-\x1f\x7f-\x9f]+)"
    r"|\x1b\[(?P<csi>[0-?]*[ -/]*[@-~])"
    r"|\x1b\](?P<osc>[^\x07\x1b]*)(?:\x07|\x1b\\|(?=\x1b))"
    r"|\x1b[PX^_](?:[^\x1b]|\x1b(?!\\))*\x1b\\"
    r"|\x1b(?P<esc>[ -/]*[0-~])"
    r"|(?P<ctl>[\x00-\x1f\x7f-\x9f])"
)
# A sequence cut off at the end of what arrived: kept for the next feed.
_INCOMPLETE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*\x1b?|[PX^_](?:[^\x1b]|\x1b(?!\\))*\x1b?|[ -/]*)\Z"
)
_CARRY_MAX = 4096

# DEC special graphics (``ESC ( 0``): the line-drawing set.
_LINE_DRAWING = dict(zip(
    "_`abcdefghijklmnopqrstuvwxyz{|}~",
    " ◆▒␉␌␍␊°±␤␋┘┐┌└┼⎺⎻─⎼⎽├┤┴┬│≤≥π≠£·",
))

_WIDTHS = {}


def char_width(ch: str) -> int:
    """Cells *ch* takes: 0 for a combining mark, 2 for a wide (East Asian or
    emoji) character, else 1."""
    if ch < "̀":
        return 1
    width = _WIDTHS.get(ch)
    if width is None:
        if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            width = 0
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            width = 2
        else:
            width = 1
        _WIDTHS[ch] = width
    return width


class Line:
    """One row of cells: a character and an attribute per cell.  The right
    half of a wide character holds ``""``.  *wrapped*: the text goes on in the
    next row (auto-wrap).  *seed*: the row was copied from the scrollback
    (its index there), for the console to keep its original text."""

    __slots__ = ("chars", "attrs", "wrapped", "seed")

    def __init__(self, cols: int, attr: Attr = PLAIN):
        self.chars = [" "] * cols
        self.attrs = [attr] * cols
        self.wrapped = False
        self.seed = None

    def text(self) -> str:
        """The row's characters without trailing blanks."""
        return "".join(self.chars).rstrip(" ")

    def is_blank(self) -> bool:
        return not self.text() and all(attr[1] == DEFAULT for attr in self.attrs)

    def resized(self, cols: int) -> "Line":
        """A copy of this row with *cols* cells: cut (a wide character cut
        in half goes) or padded with blanks.  The row itself stays as it is."""
        line = Line(0)
        line.chars, line.attrs = self.chars[:cols], self.attrs[:cols]
        line.wrapped, line.seed = self.wrapped, self.seed
        have = len(self.chars)
        if cols < have:
            # a cell holds its character and the marks joined to it (an
            # emoji's VS16, a Thai vowel): its width is the character's
            if cols and line.chars[-1] and char_width(line.chars[-1][0]) == 2:
                line.chars[-1] = " "  # a wide character cut in half
        elif cols > have:
            line.chars.extend([" "] * (cols - have))
            line.attrs.extend([PLAIN] * (cols - have))
        return line


class Screen:
    """A *rows* x *cols* terminal screen (see the module docstring).

    Feed it a program's output with :meth:`feed`.  *reply* receives the
    answers to the program's queries, as text to send to it.  Rows that
    scroll off the top of the main screen are kept for :meth:`take_scrolled_off`,
    so the owner can add them to its scrollback."""

    def __init__(self, rows: int = 24, cols: int = 80, reply: Optional[Callable[[str], None]] = None):
        self.rows = max(1, int(rows))
        self.cols = max(1, int(cols))
        self.reply = reply
        self._main = [Line(self.cols) for _ in range(self.rows)]
        self._alt = None
        self.lines = self._main
        self.alt_active = False
        self.alt_used = False          # the alternate screen was used at all
        self.wiped = False             # the whole main screen was erased
        self.scrollback_cleared = False  # ED 3 asked to forget the scrollback
        self.bell = 0
        self.version = 0               # bumped by every change, for repainting
        self._scrolled_off = []
        self._carry = ""
        self._attrs = {}
        self._reset_state()

    # ---- state -----------------------------------------------------------
    def _reset_state(self) -> None:
        self.row = 0
        self.col = 0
        self.wrap_pending = False
        self.attr = PLAIN
        self.top = 0
        self.bottom = self.rows - 1
        self.origin_mode = False
        self.autowrap = True
        self.insert_mode = False
        self.newline_mode = False
        self.cursor_visible = True
        self.cursor_shape = BLOCK
        self.app_cursor_keys = False
        self.app_keypad = False
        self.bracketed_paste = False
        self.charsets = ["B", "B"]
        self.shift = 0
        self.last_char = ""
        self._tabs = [c % 8 == 0 and c > 0 for c in range(self.cols)]
        self._saved = {False: None, True: None}

    def _intern(self, fg: int, bg: int, flags: int) -> Attr:
        key = (fg, bg, flags)
        attr = self._attrs.get(key)
        if attr is None:
            attr = self._attrs[key] = key
        return attr

    def _erase_attr(self) -> Attr:
        """What erased cells get: the current background only (xterm's BCE)."""
        bg = self.attr[1]
        return PLAIN if bg == DEFAULT else self._intern(DEFAULT, bg, 0)

    @property
    def cursor(self) -> Tuple[int, int]:
        return self.row, self.col

    def take_scrolled_off(self) -> List[Line]:
        """Rows that left the top of the main screen since the last call."""
        rows, self._scrolled_off = self._scrolled_off, []
        return rows

    def row_text(self, row: int) -> str:
        return self.lines[row].text()

    def display(self) -> List[str]:
        """Every row's text (for tests and copying)."""
        return [line.text() for line in self.lines]

    def main_lines(self) -> List[Line]:
        return self._main

    # ---- seeding -----------------------------------------------------------
    def seed(self, rows: Sequence[Sequence[Tuple[str, Attr]]], cursor: Tuple[int, int]) -> None:
        """Start from what the scrollback shows: *rows* of ``(text, attr)``
        runs fill the main screen from the top, the cursor at *cursor*."""
        for index, runs in enumerate(rows[: self.rows]):
            line = Line(self.cols)
            col = 0
            for text, attr in runs:
                attr = self._intern(*attr)
                for ch in text:
                    width = char_width(ch)
                    if width == 0:
                        continue
                    if col + width > self.cols:
                        break
                    line.chars[col] = ch
                    line.attrs[col] = attr
                    if width == 2:
                        line.chars[col + 1] = ""
                        line.attrs[col + 1] = attr
                    col += width
            line.seed = index
            self._main[index] = line
        self.row = max(0, min(self.rows - 1, cursor[0]))
        self.col = max(0, min(self.cols - 1, cursor[1]))
        self.version += 1

    def use_attr(self, fg: int, bg: int, flags: int) -> None:
        """Go on with these colours (what the output left on before the screen)."""
        self.attr = self._intern(fg, bg, flags)

    def save_cursor_at(self, row: int, col: int) -> None:
        """Save *row*, *col* as ``ESC 7`` would (it was saved before the screen)."""
        self._saved[self.alt_active] = (
            max(0, min(self.rows - 1, row)), max(0, min(self.cols - 1, col)),
            self.attr, False, False, list(self.charsets), self.shift,
        )

    def leave_alternate(self) -> None:
        """Back to the main screen and its cursor, as a program leaving the
        alternate screen does (for one that ended without doing so)."""
        if self.alt_active:
            self._switch_screen(False)
            if self._saved[False] is not None:
                self._restore_cursor()
            self.version += 1

    # ---- output ------------------------------------------------------------
    def feed(self, data: str) -> None:
        """Apply the program's output *data* (a sequence cut off at its end is
        completed by the next call)."""
        if self._carry:
            data = self._carry + data
            self._carry = ""
        if not data:
            return
        tail = _INCOMPLETE.search(data, max(0, len(data) - _CARRY_MAX))
        if tail is not None:
            self._carry = data[tail.start():]
            data = data[:tail.start()]
        for match in _TOKEN.finditer(data):
            kind = match.lastgroup
            if kind == "text":
                self._print(match.group("text"))
            elif kind == "csi":
                self._csi(match.group("csi"))
            elif kind == "ctl":
                self._control(match.group("ctl"))
            elif kind == "esc":
                self._escape(match.group("esc"))
            # osc and the other strings: read and ignored
        self.version += 1

    def forget_partial(self) -> None:
        """Forget a sequence cut off at the end of the last :meth:`feed`:
        the output that would have completed it was dropped (the console
        could not keep up with a flood), and joined to the output after
        that it would garble the next frame."""
        self._carry = ""

    def _print(self, text: str) -> None:
        charset = self.charsets[self.shift]
        if charset == "0":
            text = "".join(_LINE_DRAWING.get(ch, ch) for ch in text)
        if text.isascii():
            self._print_ascii(text)
        else:
            for ch in text:
                self._print_char(ch)
        self.last_char = text[-1]

    def _wrap(self) -> None:
        """Auto-wrap: the next character goes to the start of the next row."""
        self.lines[self.row].wrapped = True
        self._index()
        self.col = 0
        self.wrap_pending = False

    def _fix_wide(self, line: Line, start: int, end: int) -> None:
        """Cells start..end-1 are about to be overwritten: a wide character
        they cut in half is blanked."""
        chars = line.chars
        if start > 0 and chars[start] == "":
            chars[start - 1] = " "
        if end < self.cols and chars[end] == "":
            chars[end] = " "

    def _print_ascii(self, text: str) -> None:
        index, size = 0, len(text)
        attr = self.attr
        while index < size:
            if self.wrap_pending:
                if self.autowrap:
                    self._wrap()
                else:
                    self.wrap_pending = False
            line = self.lines[self.row]
            room = self.cols - self.col
            if not self.autowrap and size - index > room:
                # every character past the edge lands on the last column
                piece = text[index:index + room - 1] + text[-1]
                index = size
            else:
                piece = text[index:index + room]
                index += len(piece)
            count = len(piece)
            start = self.col
            if self.insert_mode:
                self._insert_cells(line, start, count)
            self._fix_wide(line, start, start + count)
            line.chars[start:start + count] = piece
            line.attrs[start:start + count] = [attr] * count
            if start + count >= self.cols:
                self.col = self.cols - 1
                self.wrap_pending = True
            else:
                self.col = start + count

    def _print_char(self, ch: str) -> None:
        width = char_width(ch)
        if width == 0:
            # a combining mark joins the character before the cursor
            row, col = self.row, self.col
            if not self.wrap_pending:
                col -= 1
            line = self.lines[row]
            while col > 0 and line.chars[col] == "":
                col -= 1
            if col >= 0 and line.chars[col]:
                line.chars[col] += ch
            return
        if self.wrap_pending:
            if self.autowrap:
                self._wrap()
            else:
                self.wrap_pending = False
        if width == 2 and self.col == self.cols - 1:
            if self.autowrap:
                line = self.lines[self.row]
                self._fix_wide(line, self.col, self.col + 1)
                line.chars[self.col] = " "
                self._wrap()
            elif self.cols > 1:
                self.col -= 1
        if width > self.cols:
            return
        line = self.lines[self.row]
        start = self.col
        if self.insert_mode:
            self._insert_cells(line, start, width)
        self._fix_wide(line, start, start + width)
        line.chars[start] = ch
        line.attrs[start] = self.attr
        if width == 2:
            line.chars[start + 1] = ""
            line.attrs[start + 1] = self.attr
        if start + width >= self.cols:
            self.col = self.cols - 1
            self.wrap_pending = True
        else:
            self.col = start + width

    def _insert_cells(self, line: Line, col: int, count: int) -> None:
        count = min(count, self.cols - col)
        if count <= 0:
            return
        blank = self._erase_attr()
        self._fix_wide(line, col, col)
        line.chars[col:col] = [" "] * count
        line.attrs[col:col] = [blank] * count
        del line.chars[self.cols:]
        del line.attrs[self.cols:]
        if line.chars[-1] and char_width(line.chars[-1][0]) == 2:
            line.chars[-1] = " "  # (a cell may hold combining marks too)

    # ---- controls and escapes ------------------------------------------------
    def _control(self, ch: str) -> None:
        if ch == "\n" or ch == "\x0b" or ch == "\x0c":
            self._index()
            if self.newline_mode:
                self.col = 0
        elif ch == "\r":
            self.col = 0
            self.wrap_pending = False
        elif ch == "\b":
            if self.col > 0:
                self.col -= 1
            self.wrap_pending = False
        elif ch == "\t":
            self._tab(1)
        elif ch == "\x07":
            self.bell += 1
        elif ch == "\x0e":
            self.shift = 1
        elif ch == "\x0f":
            self.shift = 0
        # every other control (and the 8-bit C1 set) is ignored

    def _escape(self, body: str) -> None:
        final = body[-1]
        intermediate = body[:-1]
        if intermediate:
            if intermediate in ("(", ")"):
                self.charsets[0 if intermediate == "(" else 1] = "0" if final == "0" else "B"
            elif intermediate == "#" and final == "8":
                self._fill("E")
            return
        if final == "7":
            self._save_cursor()
        elif final == "8":
            self._restore_cursor()
        elif final == "D":
            self._index()
        elif final == "E":
            self._index()
            self.col = 0
        elif final == "M":
            self._reverse_index()
        elif final == "c":
            self._full_reset()
        elif final == "H":
            self._tabs[self.col] = True
        elif final == "=":
            self.app_keypad = True
        elif final == ">":
            self.app_keypad = False

    def _index(self) -> None:
        """Down one row, scrolling the region at its bottom margin."""
        self.wrap_pending = False
        if self.row == self.bottom:
            self._scroll_up(self.top, self.bottom, 1)
        elif self.row < self.rows - 1:
            self.row += 1

    def _reverse_index(self) -> None:
        self.wrap_pending = False
        if self.row == self.top:
            self._scroll_down(self.top, self.bottom, 1)
        elif self.row > 0:
            self.row -= 1

    def _blank_line(self) -> Line:
        return Line(self.cols, self._erase_attr())

    def _scroll_up(self, top: int, bottom: int, count: int) -> None:
        count = min(count, bottom - top + 1)
        if count <= 0:
            return
        lines = self.lines
        gone = lines[top:top + count]
        if top == 0 and not self.alt_active:
            self._scrolled_off.extend(gone)
        del lines[top:top + count]
        for _ in range(count):
            lines.insert(bottom - count + 1, self._blank_line())

    def _scroll_down(self, top: int, bottom: int, count: int) -> None:
        count = min(count, bottom - top + 1)
        if count <= 0:
            return
        lines = self.lines
        del lines[bottom - count + 1:bottom + 1]
        for _ in range(count):
            lines.insert(top, self._blank_line())

    def _tab(self, count: int) -> None:
        # every step moves at least a column: more steps than columns only
        # stay at the edge (ESC[1000000000I kept this loop busy for a minute)
        col = self.col
        for _ in range(min(count, self.cols)):
            col += 1
            while col < self.cols - 1 and not self._tabs[col]:
                col += 1
        self.col = min(col, self.cols - 1)
        self.wrap_pending = False

    def _back_tab(self, count: int) -> None:
        col = self.col
        for _ in range(min(count, self.cols)):
            col -= 1
            while col > 0 and not self._tabs[col]:
                col -= 1
        self.col = max(col, 0)
        self.wrap_pending = False

    def _fill(self, ch: str) -> None:
        for line in self.lines:
            line.chars[:] = [ch] * self.cols
            line.attrs[:] = [PLAIN] * self.cols
        self.row = self.col = 0
        self.wrap_pending = False

    def _save_cursor(self) -> None:
        """DECSC: the position, attributes, pending wrap, origin mode and
        character sets (as xterm saves them)."""
        self._saved[self.alt_active] = (
            self.row, self.col, self.attr, self.wrap_pending, self.origin_mode,
            list(self.charsets), self.shift,
        )

    def _restore_cursor(self) -> None:
        saved = self._saved[self.alt_active]
        if saved is None:
            self.row = self.col = 0
            self.attr = PLAIN
            self.wrap_pending = False
            self.origin_mode = False
            return
        (row, col, self.attr, self.wrap_pending, self.origin_mode,
         charsets, self.shift) = saved
        self.charsets = list(charsets)
        self.row = min(row, self.rows - 1)
        self.col = min(col, self.cols - 1)

    def _full_reset(self) -> None:
        if self.alt_active:
            self._switch_screen(False)
        self._reset_state()
        self._erase_rows(0, self.rows)
        self.wiped = True

    # ---- CSI -------------------------------------------------------------
    def _csi(self, body: str) -> None:
        final = body[-1]
        middle = body[:-1]
        cut = len(middle.rstrip(" !\"#$%&'()*+,-./"))
        params, intermediate = middle[:cut], middle[cut:]
        prefix = ""
        if params[:1] in ("?", ">", "<", "="):
            prefix, params = params[0], params[1:]
        if intermediate:
            if intermediate == " " and final == "q":
                self._cursor_style(_numbers(params, 1)[0])
            elif intermediate == "!" and final == "p":
                self._soft_reset()
            return
        if prefix == "?":
            if final in "hl":
                for mode in _numbers(params, 0):
                    self._private_mode(mode, final == "h")
            elif final == "n" and params.split(";")[0] == "6":
                self._send(f"\x1b[?{self._report_row()};{self.col + 1}R")
            return
        if prefix:
            return  # DA2 (">c") and friends: no answer
        handler = self._CSI.get(final)
        if handler is not None:
            handler(self, params)

    def _send(self, text: str) -> None:
        if self.reply is not None:
            self.reply(text)

    def _report_row(self) -> int:
        return self.row - self.top + 1 if self.origin_mode else self.row + 1

    def _move_to(self, row: int, col: int) -> None:
        if self.origin_mode:
            row = max(self.top, min(self.bottom, row + self.top))
        else:
            row = max(0, min(self.rows - 1, row))
        self.row = row
        self.col = max(0, min(self.cols - 1, col))
        self.wrap_pending = False

    def _cup(self, params: str) -> None:
        values = _numbers(params, 1, 2)
        self._move_to(values[0] - 1, values[1] - 1)

    def _cuu(self, params: str) -> None:
        count = _numbers(params, 1)[0]
        limit = self.top if self.row >= self.top else 0
        self.row = max(limit, self.row - count)
        self.wrap_pending = False

    def _cud(self, params: str) -> None:
        count = _numbers(params, 1)[0]
        limit = self.bottom if self.row <= self.bottom else self.rows - 1
        self.row = min(limit, self.row + count)
        self.wrap_pending = False

    def _cuf(self, params: str) -> None:
        self.col = min(self.cols - 1, self.col + _numbers(params, 1)[0])
        self.wrap_pending = False

    def _cub(self, params: str) -> None:
        self.col = max(0, self.col - _numbers(params, 1)[0])
        self.wrap_pending = False

    def _cnl(self, params: str) -> None:
        self._cud(params)
        self.col = 0

    def _cpl(self, params: str) -> None:
        self._cuu(params)
        self.col = 0

    def _cha(self, params: str) -> None:
        self.col = max(0, min(self.cols - 1, _numbers(params, 1)[0] - 1))
        self.wrap_pending = False

    def _vpa(self, params: str) -> None:
        self._move_to(_numbers(params, 1)[0] - 1, self.col)

    def _vpr(self, params: str) -> None:
        self.row = min(self.rows - 1, self.row + _numbers(params, 1)[0])
        self.wrap_pending = False

    def _hpr(self, params: str) -> None:
        self._cuf(params)

    def _erase_cells(self, line: Line, start: int, end: int) -> None:
        start, end = max(0, start), min(self.cols, end)
        if end <= start:
            return
        self._fix_wide(line, start, end)
        line.chars[start:end] = [" "] * (end - start)
        line.attrs[start:end] = [self._erase_attr()] * (end - start)

    def _erase_rows(self, start: int, end: int) -> None:
        for row in range(max(0, start), min(self.rows, end)):
            line = self.lines[row]
            self._erase_cells(line, 0, self.cols)
            line.wrapped = False

    def _ed(self, params: str) -> None:
        mode = _numbers(params, 0)[0]
        if mode == 0:
            self._erase_cells(self.lines[self.row], self.col, self.cols)
            self.lines[self.row].wrapped = False
            self._erase_rows(self.row + 1, self.rows)
            if self.row == 0 and self.col == 0 and not self.alt_active:
                self.wiped = True
        elif mode == 1:
            self._erase_rows(0, self.row)
            self._erase_cells(self.lines[self.row], 0, self.col + 1)
        elif mode in (2, 3):
            self._erase_rows(0, self.rows)
            if not self.alt_active:
                self.wiped = True
                if mode == 3:
                    self.scrollback_cleared = True

    def _el(self, params: str) -> None:
        mode = _numbers(params, 0)[0]
        line = self.lines[self.row]
        if mode == 0:
            self._erase_cells(line, self.col, self.cols)
            line.wrapped = False
        elif mode == 1:
            self._erase_cells(line, 0, self.col + 1)
        elif mode == 2:
            self._erase_cells(line, 0, self.cols)
            line.wrapped = False

    def _ech(self, params: str) -> None:
        count = _numbers(params, 1)[0]
        self._erase_cells(self.lines[self.row], self.col, self.col + count)
        self.wrap_pending = False

    def _ich(self, params: str) -> None:
        self._insert_cells(self.lines[self.row], self.col, _numbers(params, 1)[0])
        self.wrap_pending = False

    def _dch(self, params: str) -> None:
        count = min(_numbers(params, 1)[0], self.cols - self.col)
        line = self.lines[self.row]
        col = self.col
        self._fix_wide(line, col, col + count)
        del line.chars[col:col + count]
        del line.attrs[col:col + count]
        line.chars.extend([" "] * count)
        line.attrs.extend([self._erase_attr()] * count)
        self.wrap_pending = False

    def _il(self, params: str) -> None:
        if self.top <= self.row <= self.bottom:
            self._scroll_down(self.row, self.bottom, _numbers(params, 1)[0])
            self.col = 0
            self.wrap_pending = False

    def _dl(self, params: str) -> None:
        if self.top <= self.row <= self.bottom:
            count = min(_numbers(params, 1)[0], self.bottom - self.row + 1)
            lines = self.lines
            del lines[self.row:self.row + count]
            for _ in range(count):
                lines.insert(self.bottom - count + 1, self._blank_line())
            self.col = 0
            self.wrap_pending = False

    def _su(self, params: str) -> None:
        self._scroll_up(self.top, self.bottom, _numbers(params, 1)[0])

    def _sd(self, params: str) -> None:
        if ";" in params:
            return  # xterm's mouse highlight tracking, not a scroll
        self._scroll_down(self.top, self.bottom, _numbers(params, 1)[0])

    def _rep(self, params: str) -> None:
        if self.last_char:
            self._print(self.last_char * min(_numbers(params, 1)[0], self.rows * self.cols))

    def _stbm(self, params: str) -> None:
        """DECSTBM: the scroll region (the whole screen by default); the
        cursor goes home."""
        values = _numbers(params, 0, 2)
        top = values[0] or 1
        bottom = min(self.rows, values[1] or self.rows)
        if top < bottom:
            self.top, self.bottom = top - 1, bottom - 1
            self._move_to(0, 0)

    def _tbc(self, params: str) -> None:
        mode = _numbers(params, 0)[0]
        if mode == 0:
            self._tabs[self.col] = False
        elif mode == 3:
            self._tabs = [False] * self.cols

    def _cht(self, params: str) -> None:
        self._tab(_numbers(params, 1)[0])

    def _cbt(self, params: str) -> None:
        self._back_tab(_numbers(params, 1)[0])

    def _dsr(self, params: str) -> None:
        mode = _numbers(params, 0)[0]
        if mode == 5:
            self._send("\x1b[0n")
        elif mode == 6:
            self._send(f"\x1b[{self._report_row()};{self.col + 1}R")

    def _da(self, params: str) -> None:
        if _numbers(params, 0)[0] == 0:
            self._send("\x1b[?1;2c")  # a VT100 with advanced video

    def _window(self, params: str) -> None:
        if _numbers(params, 0)[0] == 18:
            self._send(f"\x1b[8;{self.rows};{self.cols}t")

    def _mode(self, params: str, on: bool) -> None:
        for mode in _numbers(params, 0):
            if mode == 4:
                self.insert_mode = on
            elif mode == 20:
                self.newline_mode = on

    def _sm(self, params: str) -> None:
        self._mode(params, True)

    def _rm(self, params: str) -> None:
        self._mode(params, False)

    def _scosc(self, params: str) -> None:
        if not params:
            self._save_cursor()

    def _scorc(self, params: str) -> None:
        if not params:
            self._restore_cursor()

    def _cursor_style(self, style: int) -> None:
        self.cursor_shape = {3: UNDERLINE_CURSOR, 4: UNDERLINE_CURSOR, 5: BAR, 6: BAR}.get(style, BLOCK)

    def _soft_reset(self) -> None:
        self.cursor_visible = True
        self.insert_mode = False
        self.origin_mode = False
        self.autowrap = True
        self.app_cursor_keys = False
        self.app_keypad = False
        self.attr = PLAIN
        self.top, self.bottom = 0, self.rows - 1
        self.charsets = ["B", "B"]
        self.shift = 0
        self._saved[self.alt_active] = None
        self.wrap_pending = False

    def _private_mode(self, mode: int, on: bool) -> None:
        if mode == 1:
            self.app_cursor_keys = on
        elif mode == 6:
            self.origin_mode = on
            self._move_to(0, 0)
        elif mode == 7:
            self.autowrap = on
            if not on:
                self.wrap_pending = False
        elif mode == 25:
            self.cursor_visible = on
        elif mode == 2004:
            self.bracketed_paste = on
        elif mode == 1048:
            if on:
                self._save_cursor()
            else:
                self._restore_cursor()
        elif mode in (47, 1047, 1049):
            # xterm: 1049 saves the cursor and clears the alternate screen on
            # the way in; 1047 clears it on the way out; 47 does neither
            if on:
                if mode == 1049:
                    self._save_cursor()
                if not self.alt_active:
                    self._switch_screen(True, clear=mode == 1049)
            else:
                if self.alt_active:
                    if mode == 1047:
                        self._erase_rows(0, self.rows)
                    self._switch_screen(False)
                if mode == 1049:
                    self._restore_cursor()

    def _switch_screen(self, alternate: bool, clear: bool = False) -> None:
        if alternate:
            if self._alt is None or clear:
                self._alt = [Line(self.cols) for _ in range(self.rows)]
            self.lines = self._alt
            self.alt_used = True
        else:
            self.lines = self._main
        self.alt_active = alternate
        self.wrap_pending = False

    # ---- SGR -------------------------------------------------------------
    def _sgr(self, params: str) -> None:
        fg, bg, flags = self.attr
        parts = params.split(";") if params else ["0"]
        index = 0
        while index < len(parts):
            part = parts[index]
            index += 1
            if ":" in part:
                fields = part.split(":")
                code = _int(fields[0])
                if code in (38, 48) and len(fields) >= 3:
                    color = _color(_int(fields[1]), [_int(v) for v in fields[2:]][-3:]
                                   if _int(fields[1]) == 2 else [_int(fields[2])])
                    if color is not None:
                        if code == 38:
                            fg = color
                        else:
                            bg = color
                elif code == 4:
                    style = _int(fields[1]) if len(fields) > 1 else 1
                    flags = (flags | UNDERLINE) if style else (flags & ~UNDERLINE)
                continue
            code = _int(part)
            if code is None:
                continue
            if code in (38, 48, 58):
                mode = _int(parts[index]) if index < len(parts) else None
                if mode == 5 and index + 1 < len(parts):
                    values = [_int(parts[index + 1])]
                    index += 2
                elif mode == 2 and index + 3 < len(parts):
                    values = [_int(v) for v in parts[index + 1:index + 4]]
                    index += 4
                else:
                    index += 1  # a malformed colour: skip its selector
                    continue
                color = _color(mode, values)
                if color is not None and code == 38:
                    fg = color
                elif color is not None and code == 48:
                    bg = color
            elif code == 0:
                fg, bg, flags = DEFAULT, DEFAULT, 0
            elif code == 1:
                flags |= BOLD
            elif code == 2:
                flags |= DIM
            elif code == 3:
                flags |= ITALIC
            elif code in (4, 21):
                flags |= UNDERLINE
            elif code in (5, 6):
                flags |= BLINK
            elif code == 7:
                flags |= REVERSE
            elif code == 8:
                flags |= INVISIBLE
            elif code == 9:
                flags |= STRIKE
            elif code == 22:
                flags &= ~(BOLD | DIM)
            elif code == 23:
                flags &= ~ITALIC
            elif code == 24:
                flags &= ~UNDERLINE
            elif code == 25:
                flags &= ~BLINK
            elif code == 27:
                flags &= ~REVERSE
            elif code == 28:
                flags &= ~INVISIBLE
            elif code == 29:
                flags &= ~STRIKE
            elif 30 <= code <= 37:
                fg = code - 30
            elif code == 39:
                fg = DEFAULT
            elif 40 <= code <= 47:
                bg = code - 40
            elif code == 49:
                bg = DEFAULT
            elif 90 <= code <= 97:
                fg = code - 90 + 8
            elif 100 <= code <= 107:
                bg = code - 100 + 8
        self.attr = self._intern(fg, bg, flags)

    _CSI = {
        "@": _ich, "A": _cuu, "B": _cud, "C": _cuf, "D": _cub, "E": _cnl, "F": _cpl,
        "G": _cha, "H": _cup, "I": _cht, "J": _ed, "K": _el, "L": _il, "M": _dl,
        "P": _dch, "S": _su, "T": _sd, "X": _ech, "Z": _cbt, "`": _cha, "a": _hpr,
        "b": _rep, "c": _da, "d": _vpa, "e": _vpr, "f": _cup, "g": _tbc, "h": _sm,
        "l": _rm, "m": _sgr, "n": _dsr, "r": _stbm, "s": _scosc, "t": _window, "u": _scorc,
    }

    # ---- size ------------------------------------------------------------
    def resize(self, rows: int, cols: int) -> None:
        """Make the screen *rows* x *cols*.  Rows are not re-wrapped: a
        narrower screen cuts them, and a shorter one drops blank rows below
        the cursor first, then scrolls rows off the top."""
        rows, cols = max(1, int(rows)), max(1, int(cols))
        # Worked out on copies and only then put in place: a screen left half
        # resized (rows cut to the new width, the screen still the old one)
        # made every repaint read cells that were not there.
        buffers = [self._main, self._alt]
        if cols != self.cols:
            buffers = [None if lines is None else [line.resized(cols) for line in lines]
                       for lines in buffers]
        tabs = self._tabs[:cols]
        tabs.extend(c % 8 == 0 for c in range(len(tabs), cols))
        row, gone = self.row, []
        if rows != self.rows:
            for index, lines in enumerate(buffers):
                if lines is None:
                    continue
                lines = list(lines)
                active = (index == 1) == self.alt_active
                if rows < self.rows:
                    excess = self.rows - rows
                    while excess and len(lines) - 1 > (row if active else -1) \
                            and lines[-1].is_blank():
                        lines.pop()
                        excess -= 1
                    if excess:
                        if index == 0:
                            gone = lines[:excess]
                        del lines[:excess]
                        if active:
                            row = max(0, row - excess)
                else:
                    lines.extend(Line(cols) for _ in range(rows - self.rows))
                del lines[rows:]
                buffers[index] = lines
        self._main[:] = buffers[0]
        if self._alt is not None:
            self._alt[:] = buffers[1]
        self._scrolled_off.extend(gone)
        self._tabs = tabs
        self.rows, self.cols = rows, cols
        self.top, self.bottom = 0, self.rows - 1
        self.row = min(row, self.rows - 1)
        self.col = min(self.col, self.cols - 1)
        self.wrap_pending = False
        for key, saved in list(self._saved.items()):
            if saved is not None:
                self._saved[key] = (min(saved[0], self.rows - 1), min(saved[1], self.cols - 1)) + saved[2:]
        self.version += 1


# ---- keys ------------------------------------------------------------------
_CURSOR_KEYS = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}
_FUNCTION_KEYS = {"f1": "P", "f2": "Q", "f3": "R", "f4": "S"}
_TILDE_KEYS = {
    "insert": 2, "delete": 3, "pageup": 5, "pagedown": 6, "f5": 15, "f6": 17, "f7": 18,
    "f8": 19, "f9": 20, "f10": 21, "f11": 23, "f12": 24,
}
# Ctrl with a digit, as xterm sends it (Ctrl+2 is NUL ... Ctrl+8 is DEL)
_CTRL_DIGITS = {"2": "\x00", "3": "\x1b", "4": "\x1c", "5": "\x1d", "6": "\x1e", "7": "\x1f", "8": "\x7f"}


def key_sequence(name: str, *, shift: bool = False, alt: bool = False, ctrl: bool = False,
                 app_cursor: bool = False) -> str:
    """What an xterm sends for the key *name* (``"up"``, ``"home"``,
    ``"pagedown"``, ``"f5"``, ``"enter"``, ``"backspace"``, ``"tab"``,
    ``"escape"`` ...) with these modifiers; *app_cursor*: the program asked
    for application cursor keys (``ESC [ ? 1 h``).  ``""`` for another name.

    Enter is a line feed: adb.exe holds a CR that ends what it reads until
    more comes, and a device terminal in raw mode takes LF for Enter."""
    modifier = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
    if name in _CURSOR_KEYS:
        final = _CURSOR_KEYS[name]
        if modifier > 1:
            return f"\x1b[1;{modifier}{final}"
        return ("\x1bO" if app_cursor else "\x1b[") + final
    if name in _FUNCTION_KEYS:
        final = _FUNCTION_KEYS[name]
        return f"\x1b[1;{modifier}{final}" if modifier > 1 else "\x1bO" + final
    if name in _TILDE_KEYS:
        code = _TILDE_KEYS[name]
        return f"\x1b[{code};{modifier}~" if modifier > 1 else f"\x1b[{code}~"
    meta = "\x1b" if alt else ""
    if name == "enter":
        return meta + "\n"
    if name == "backspace":
        return meta + ("\x08" if ctrl else "\x7f")
    if name == "tab":
        return "\x1b[Z" if shift else meta + "\t"
    if name == "escape":
        return "\x1b"
    return ""


def control_character(key: str) -> str:
    """What Ctrl+*key* sends (Ctrl+A is ``\\x01``, Ctrl+[ is ESC, Ctrl+Space
    NUL, Ctrl+? DEL), or ``""`` when it is no control character."""
    upper = key.upper()
    if len(upper) != 1:
        return ""
    if "@" <= upper <= "_":
        return chr(ord(upper) - 64)
    if upper == " ":
        return "\x00"
    if upper == "?":
        return "\x7f"
    return _CTRL_DIGITS.get(upper, "")


def _int(value) -> Optional[int]:
    if value in ("", None):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _numbers(params: str, default: int, count: int = 1) -> List[int]:
    """The numeric parameters of a CSI, at least *count* of them (0 or
    missing ones are *default*)."""
    values = []
    for part in params.split(";") if params else []:
        number = _int(part.split(":")[0])
        values.append(number if number else default)
    while len(values) < count:
        values.append(default)
    return values


def _color(mode, values) -> Optional[int]:
    """A 256-colour (mode 5) or true colour (mode 2) SGR value, or None."""
    if any(v is None for v in values):
        return None
    if mode == 5 and values and 0 <= values[0] <= 255:
        return values[0]
    if mode == 2 and len(values) == 3:
        r, g, b = (max(0, min(255, v)) for v in values)
        return TRUECOLOR | (r << 16) | (g << 8) | b
    return None
