"""The terminal's view while a full-screen program runs.

:class:`ScreenView` paints a :class:`vtscreen.Screen` in the console's font
and the terminal colours, over the console's scrollback text, cell for cell
where the program put it.  The console keeps the keyboard (keys go to the
program); the view paints, and lets the mouse select text for Copy.
"""

from __future__ import annotations

from PyQt5.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PyQt5.QtWidgets import QWidget

from . import theme
from .vtscreen import (
    BOLD, DEFAULT, DIM, INVISIBLE, ITALIC, REVERSE, STRIKE, TRUECOLOR, UNDERLINE, char_width,
)

# xterm 256-colour cube levels (indices 16-231).
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
_WORD_CHARS = frozenset("-_./~:@%+=#")


def screen_color(value: int, background: bool) -> QColor:
    """The colour of a screen cell's foreground or background *value* (see
    :mod:`vtscreen`): the terminal's own for the default, the theme's ANSI
    colours for 0-15, the xterm cube and greys for 16-255, or true colour."""
    if value == DEFAULT:
        return QColor(theme.TERM_BG if background else theme.TERM_FG)
    if value >= TRUECOLOR:
        rgb = value & 0xFFFFFF
        return QColor((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255)
    if value < 16:
        table = theme.ANSI_BG if background else theme.ANSI_FG
        base = (40 if background else 30) if value < 8 else (100 if background else 90)
        return QColor(table[base + value % 8])
    if value < 232:
        value -= 16
        return QColor(_CUBE_LEVELS[value // 36], _CUBE_LEVELS[(value // 6) % 6], _CUBE_LEVELS[value % 6])
    level = 8 + (value - 232) * 10
    return QColor(level, level, level)


def cell_colors(attr, selected: bool = False):
    """``(foreground, background)`` QColors for a cell with *attr*."""
    fg_value, bg_value, flags = attr
    fg = screen_color(fg_value, False)
    bg = screen_color(bg_value, True)
    if flags & REVERSE:
        fg, bg = bg, fg
    if flags & DIM:
        fg = QColor((fg.red() + bg.red()) // 2, (fg.green() + bg.green()) // 2,
                    (fg.blue() + bg.blue()) // 2)
    if flags & INVISIBLE:
        fg = bg
    if selected:
        fg, bg = QColor(theme.TERM_SELECTION_TEXT), QColor(theme.TERM_SELECTION)
    return fg, bg


class ScreenView(QWidget):
    """Paints the screen of *console* (an ``AnsiConsole``) over its viewport."""

    def __init__(self, console):
        super().__init__(console)
        self._console = console
        self._screen = None
        self._anchor = None      # selection: the cell where the drag started
        self._focus = None       # ... and where it is now; (row, col)
        self._fonts = {}
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setCursor(Qt.IBeamCursor)
        self.hide()

    # ---- what it shows ------------------------------------------------------
    def set_screen(self, screen) -> None:
        self._screen = screen
        self._fonts.clear()
        self.clear_selection()
        self.update()

    def screen(self):
        return self._screen

    def metrics(self):
        """``(cell width, cell height, ascent, margin)`` in the console's font,
        with the same margin as its text."""
        font = self._console.document().defaultFont()
        fm = QFontMetricsF(font)
        return (fm.horizontalAdvance("M") or 1.0, fm.lineSpacing() or 1.0, fm.ascent(),
                self._console.document().documentMargin())

    def _font(self, flags: int) -> QFont:
        key = flags & (BOLD | ITALIC | UNDERLINE | STRIKE)
        font = self._fonts.get(key)
        if font is None:
            font = QFont(self._console.document().defaultFont())
            font.setBold(bool(key & BOLD))
            font.setItalic(bool(key & ITALIC))
            font.setUnderline(bool(key & UNDERLINE))
            font.setStrikeOut(bool(key & STRIKE))
            self._fonts[key] = font
        return font

    def font_changed(self) -> None:
        self._fonts.clear()
        self.update()

    # ---- painting -------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.TERM_BG))
        screen = self._screen
        if screen is None:
            return
        advance, height, ascent, margin = self.metrics()
        area = event.rect()
        first = max(0, int((area.top() - margin) // height))
        last = min(screen.rows - 1, int((area.bottom() - margin) // height))
        span = self._selection()
        for row in range(first, last + 1):
            line = screen.lines[row]
            chars, attrs = line.chars, line.attrs
            y = margin + row * height
            picked = None
            if span is not None and span[0][0] <= row <= span[1][0]:
                start = span[0][1] if row == span[0][0] else 0
                end = span[1][1] + 1 if row == span[1][0] else screen.cols
                picked = (start, end)
            col, cols = 0, len(chars)
            while col < cols:
                attr = attrs[col]
                selected = picked is not None and picked[0] <= col < picked[1]
                start = col
                col += 1
                while col < cols and attrs[col] == attr and (
                        picked is None or (picked[0] <= col < picked[1]) == selected):
                    col += 1
                self._paint_run(painter, chars, start, col, attr, selected, y,
                                advance, height, ascent, margin)
        self._paint_cursor(painter, advance, height, ascent, margin)

    def _paint_run(self, painter, chars, start, end, attr, selected, y,
                   advance, height, ascent, margin):
        fg, bg = cell_colors(attr, selected)
        x = margin + start * advance
        if selected or attr[1] != DEFAULT or attr[2] & REVERSE:
            painter.fillRect(QRectF(x, y, (end - start) * advance, height), bg)
        text = "".join(chars[start:end])
        if not text.strip(" ") or attr[2] & INVISIBLE:
            return
        painter.setPen(fg)
        painter.setFont(self._font(attr[2]))
        if text.isascii():
            painter.drawText(QPointF(x, y + ascent), text)
            return
        # characters other than ASCII go to their own cells: a fallback
        # font's advance must not shift the rest of the row
        for offset, ch in enumerate(chars[start:end]):
            if ch and ch != " ":
                painter.drawText(QPointF(margin + (start + offset) * advance, y + ascent), ch)

    def _paint_cursor(self, painter, advance, height, ascent, margin):
        screen = self._screen
        if not screen.cursor_visible:
            return
        row, col = screen.cursor
        line = screen.lines[row]
        ch = line.chars[col]
        wide = 2 if ch and char_width(ch[0]) == 2 else 1
        rect = QRectF(margin + col * advance, margin + row * height, advance * wide, height)
        color = QColor(theme.TERM_FG)
        if not self._console.hasFocus():
            painter.setPen(color)
            painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            return
        if screen.cursor_shape == "bar":
            painter.fillRect(QRectF(rect.x(), rect.y(), 2, height), color)
        elif screen.cursor_shape == "underline":
            painter.fillRect(QRectF(rect.x(), rect.bottom() - 2, rect.width(), 2), color)
        else:
            painter.fillRect(rect, color)
            if ch and ch.strip():
                painter.setPen(QColor(theme.TERM_BG))
                painter.setFont(self._font(line.attrs[col][2]))
                painter.drawText(QPointF(rect.x(), rect.y() + ascent), ch)

    # ---- selection --------------------------------------------------------------
    def cell_at(self, pos: QPoint):
        """The ``(row, col)`` under *pos*, clamped to the screen."""
        screen = self._screen
        advance, height, _ascent, margin = self.metrics()
        row = int((pos.y() - margin) // height)
        col = int((pos.x() - margin) // advance)
        return (max(0, min(screen.rows - 1, row)), max(0, min(screen.cols - 1, col)))

    def _selection(self):
        if self._anchor is None or self._focus is None or self._anchor == self._focus:
            return None
        return (min(self._anchor, self._focus), max(self._anchor, self._focus))

    def has_selection(self) -> bool:
        return self._screen is not None and self._selection() is not None

    def clear_selection(self) -> None:
        if self._anchor is not None or self._focus is not None:
            self._anchor = self._focus = None
            self.update()

    def select_all(self) -> None:
        screen = self._screen
        if screen is not None:
            self._anchor, self._focus = (0, 0), (screen.rows - 1, screen.cols - 1)
            self.update()

    def selected_text(self) -> str:
        """The selected cells as text: one line per row (a row the program's
        output wrapped into the next is joined to it), trailing blanks off."""
        span = self._selection()
        screen = self._screen
        if span is None or screen is None:
            return ""
        (row0, col0), (row1, col1) = span
        out = []
        for row in range(row0, row1 + 1):
            line = screen.lines[row]
            start = col0 if row == row0 else 0
            end = col1 + 1 if row == row1 else screen.cols
            text = "".join(line.chars[start:end]).rstrip(" ")
            out.append(text)
            if row != row1:
                out.append("" if line.wrapped else "\n")
        return "".join(out)

    def mousePressEvent(self, event):
        self._console.setFocus(Qt.MouseFocusReason)
        if event.button() == Qt.LeftButton and self._screen is not None:
            self._anchor = self._focus = self.cell_at(event.pos())
            self.update()
        elif event.button() != Qt.RightButton:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self._anchor is not None:
            self._focus = self.cell_at(event.pos())
            self.update()

    def mouseDoubleClickEvent(self, event):
        """Select the word under the mouse."""
        if event.button() != Qt.LeftButton or self._screen is None:
            return
        row, col = self.cell_at(event.pos())
        chars = self._screen.lines[row].chars

        def word(ch):
            return bool(ch) and (ch.isalnum() or ch in _WORD_CHARS)

        if not word(chars[col]):
            return
        start = end = col
        while start > 0 and word(chars[start - 1]):
            start -= 1
        while end < len(chars) - 1 and (word(chars[end + 1]) or chars[end + 1] == ""):
            end += 1
        self._anchor, self._focus = (row, start), (row, end)
        self.update()

    def wheelEvent(self, event):
        self._console.screen_wheel(event)

    def contextMenuEvent(self, event):
        self._console.screen_menu(event.globalPos())
