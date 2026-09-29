"""The main window's status bar: what is going on, at a glance and in colour.

On the left, the state of things (connected to a device, devices attached,
nothing yet) or the latest message, with an icon and a label in the colour
of its kind: green for done, amber for warnings, red for errors, blue for
information. On the right, chips for the ADB server, the devices and the
version, each with a dot in the colour of its state (a busy state pulses);
a click opens what belongs to it. Every colour comes from the theme at paint
time, so a theme switch needs nothing rebuilt.
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QRectF, QSize, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import QSizePolicy, QStatusBar, QWidget

from . import theme
from .icons import icon

# The kinds of state and message, and the theme colour each one is shown in.
TONE_TOKENS = {
    "ok": "ok_text", "warn": "warn_text", "error": "danger_text",
    "info": "accent_text", "accent": "accent_text", "idle": "dim",
}
# Icon tones (icons.icon) for the same kinds.
_ICON_TONES = {"ok": "ok", "warn": "warn", "error": "danger", "info": "accent",
               "accent": "accent", "idle": "dim"}
_DEFAULT_GLYPHS = {"ok": "check", "warn": "alert", "error": "x", "info": "info",
                   "accent": "info", "idle": "info"}
_LABELS = {"warn": "Warning", "error": "Error"}


def tone_color(tone: str) -> QColor:
    """The theme's colour for a kind of state (see :data:`TONE_TOKENS`)."""
    return QColor(theme.palette()[TONE_TOKENS.get(tone, "dim")])


def _blend(base: QColor, over: QColor, amount: float) -> QColor:
    return QColor(round(base.red() + (over.red() - base.red()) * amount),
                  round(base.green() + (over.green() - base.green()) * amount),
                  round(base.blue() + (over.blue() - base.blue()) * amount))


def _bar_font(widget: QWidget, bold: bool = False) -> QFont:
    font = QFont(widget.font())
    font.setPointSizeF(8.5)
    font.setWeight(QFont.DemiBold if bold else QFont.Normal)
    return font


class StatusChip(QWidget):
    """A pill on the status bar: a dot in the colour of the state and a short
    text. A busy state (``busy=True``) makes the dot pulse."""

    clicked = pyqtSignal()
    HEIGHT = 28   # the pill is 22 high, with room around it
    MARGIN = 3

    def __init__(self, text: str = "", tone: str = "idle", parent=None):
        super().__init__(parent)
        self._text, self._tone, self._busy = text, tone, False
        self._hover = self._clickable = False
        self._glow = 1.0
        self._pulse = QVariantAnimation(self)
        self._pulse.setStartValue(1.0)
        self._pulse.setKeyValueAt(0.5, 0.3)
        self._pulse.setEndValue(1.0)
        self._pulse.setDuration(1100)
        self._pulse.setLoopCount(-1)
        self._pulse.valueChanged.connect(self._on_pulse)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_Hover)

    # ---- state ----------------------------------------------------------------
    def set_state(self, text: str, tone: str, *, tooltip: Optional[str] = None,
                  busy: bool = False) -> None:
        changed = text != self._text
        self._text, self._tone = text, tone
        if tooltip is not None:
            self.setToolTip(tooltip)
        if busy != self._busy:
            self._busy = busy
            if busy:
                self._pulse.start()
            else:
                self._pulse.stop()
                self._glow = 1.0
        if changed:
            self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._text

    def tone(self) -> str:
        return self._tone

    def busy(self) -> bool:
        return self._busy

    def set_clickable(self, clickable: bool = True) -> None:
        self.setCursor(Qt.PointingHandCursor if clickable else Qt.ArrowCursor)
        self._clickable = clickable

    def _on_pulse(self, value) -> None:
        self._glow = float(value)
        self.update()

    # ---- geometry and painting -------------------------------------------------
    def sizeHint(self) -> QSize:
        fm = QFontMetrics(_bar_font(self, bold=True))
        return QSize(fm.horizontalAdvance(self._text) + 33 + 2 * self.MARGIN, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _event) -> None:
        c = theme.palette()
        tone = tone_color(self._tone)
        base = QColor(c["chrome"])
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        m = self.MARGIN + 0.5
        rect = QRectF(self.rect()).adjusted(m, m, -m, -m)
        radius = rect.height() / 2
        solid = self._tone == "error"  # a problem stands out as a filled pill
        if solid:
            fill = QColor(c["danger"])
            p.setPen(QPen(fill.lighter(125) if self._hover else fill, 1))
            p.setBrush(fill)
            dot, text = QColor(c["on_danger"]), QColor(c["on_danger"])
        else:
            p.setPen(QPen(_blend(base, tone, 0.62 if self._hover else 0.46), 1))
            p.setBrush(_blend(base, tone, 0.28 if self._hover else 0.18))
            dot, text = QColor(tone), QColor(c["text"])
        p.drawRoundedRect(rect, radius, radius)
        dot.setAlphaF(max(0.0, min(1.0, self._glow)))
        p.setPen(Qt.NoPen)
        p.setBrush(dot)
        p.drawEllipse(QRectF(rect.left() + 9, rect.center().y() - 4, 8, 8))
        p.setPen(text)
        p.setFont(_bar_font(self, bold=True))
        p.drawText(QRectF(rect.left() + 22.5, 0, rect.width() - 28, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self._text)

    def enterEvent(self, event) -> None:
        self._hover = self._clickable
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if (event.button() == Qt.LeftButton and self._clickable
                and self.rect().contains(event.pos())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class StatusMessage(QWidget):
    """The left of the status bar: an icon in the colour of the message's
    kind, a label ("Error", "Warning") in that colour, and the text, cut
    with "…" when the bar is narrow (the tooltip has all of it)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text, self._tone, self._glyph, self._label = "", "idle", "info", ""
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_message(self, text: str, tone: str = "info", glyph: Optional[str] = None,
                    label: Optional[str] = None) -> None:
        self._text, self._tone = text, tone
        self._glyph = glyph or _DEFAULT_GLYPHS.get(tone, "info")
        self._label = _LABELS.get(tone, "") if label is None else label
        self.setToolTip(text if len(text) > 60 else "")
        self.update()

    def text(self) -> str:
        return self._text

    def tone(self) -> str:
        return self._tone

    def label(self) -> str:
        return self._label

    def sizeHint(self) -> QSize:
        return QSize(200, StatusChip.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return QSize(40, StatusChip.HEIGHT)

    def paintEvent(self, _event) -> None:
        if not self._text:
            return
        c = theme.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        if self._tone in ("error", "warn") and self._label:
            # a problem gets a tinted banner as long as its text
            fm_bold = QFontMetrics(_bar_font(self, bold=True))
            fm = QFontMetrics(_bar_font(self))
            want = 30 + fm_bold.horizontalAdvance(self._label) + 8 + fm.horizontalAdvance(self._text) + 12
            banner = QRectF(1.5, 3.5, min(want, self.width() - 3), h - 7)
            tone = tone_color(self._tone)
            base = QColor(c["chrome"])
            p.setPen(QPen(_blend(base, tone, 0.45), 1))
            p.setBrush(_blend(base, tone, 0.15))
            p.drawRoundedRect(banner, 6, 6)
        icon(self._glyph, _ICON_TONES.get(self._tone, "dim")).paint(p, 8, (h - 16) // 2, 16, 16)
        x = 32
        if self._label:
            p.setFont(_bar_font(self, bold=True))
            p.setPen(tone_color(self._tone))
            fm = QFontMetrics(p.font())
            p.drawText(QRectF(x, 0, fm.horizontalAdvance(self._label) + 2, h),
                       Qt.AlignVCenter | Qt.AlignLeft, self._label)
            x += fm.horizontalAdvance(self._label) + 8
        p.setFont(_bar_font(self))
        p.setPen(QColor(c["text"]))
        fm = QFontMetrics(p.font())
        room = max(0, self.width() - x - 6)
        p.drawText(QRectF(x, 0, room, h), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(self._text, Qt.ElideRight, room))


class StatusBar(QStatusBar):
    """A QStatusBar whose message area shows the kind of each message.

    :meth:`set_summary` is the standing state; :meth:`show_message` puts a
    message in front of it for *timeout* ms, after which the summary comes
    back (and ``messageChanged("")`` is emitted, as Qt does). ``showMessage``
    keeps Qt's signature for callers that don't know about kinds: its
    "Error: " / "Warning: " prefix picks the colour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(False)
        self.message = StatusMessage(self)
        self.addWidget(self.message, 1)
        self._summary = ("", "idle", None, "")
        self._current = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._restore)

    def set_summary(self, text: str, tone: str = "idle", glyph: Optional[str] = None,
                    label: str = "") -> None:
        """The standing state: *label* in the colour of *tone* ("Connected"),
        then *text*."""
        self._summary = (text, tone, glyph, label)
        if not self._timer.isActive():
            self.message.set_message(text, tone, glyph, label=label)

    def summary(self):
        return self._summary

    def show_message(self, text: str, tone: str = "info", timeout: int = 0,
                     glyph: Optional[str] = None) -> None:
        self._current = text
        self.message.set_message(text, tone, glyph)
        if timeout > 0:
            self._timer.start(timeout)
        else:
            self._timer.stop()
        self.messageChanged.emit(text)

    def showMessage(self, text: str, timeout: int = 0) -> None:  # noqa: N802 (Qt's name)
        tone = "info"
        for prefix, kind in (("Error: ", "error"), ("Warning: ", "warn")):
            if text.startswith(prefix):
                text, tone = text[len(prefix):], kind
                break
        self.show_message(text, tone, timeout)

    def currentMessage(self) -> str:  # noqa: N802
        return self._current

    def clearMessage(self) -> None:  # noqa: N802
        self._timer.stop()
        self._restore()

    def _restore(self) -> None:
        self._current = ""
        text, tone, glyph, label = self._summary
        self.message.set_message(text, tone, glyph, label=label)
        self.messageChanged.emit("")
