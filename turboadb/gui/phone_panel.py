"""Phone page: a dialler (dial / call / answer / end), recent calls and SMS
messages with a compose box, all driven over adb.

Nothing here places a call or sends a message on its own. Enter in the number
field only opens the device dialler pre-filled, "Call back" only fills the
number in, and composing opens the device's Messages app with a draft that the
user sends there. The call log and messages load lazily the first time the
page is shown, so connecting to a device never queries them.

On a car head unit calls come from a phone connected over Bluetooth. With no
phone connected the page says so calmly, disables the call actions and only
re-checks the connection now and then while it is visible.
"""

from __future__ import annotations

import datetime
import time

from PyQt5.QtCore import QByteArray, QPoint, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QIcon, QIconEngine, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QBoxLayout,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..results import OperationResult
from . import theme
from .icons import svg as icon_svg
from .qtutil import cached_icon, close_jobs, run_job, unwrap

# Item data role holding a row's display dict (kind, label, icon, tone, ...).
ROW_ROLE = Qt.UserRole + 1

# Characters kept in the number field; the engine keeps only + * # , ; and digits.
_NUMBER_CHARS = frozenset("0123456789+*#,;()-. ")

_KEYPAD = (
    ("1", ""), ("2", "ABC"), ("3", "DEF"),
    ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
    ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"),
    ("*", ""), ("0", "+"), ("#", ""),
)

# call log ``type`` -> (kind, label, icon, tone)
_CALL_KINDS = {
    "1": ("incoming", "Incoming", "phone-incoming", "green"),
    "2": ("outgoing", "Outgoing", "phone-outgoing", "blue"),
    "3": ("missed", "Missed", "phone-missed", "red"),
    "4": ("voicemail", "Voicemail", "phone", "purple"),
    "5": ("rejected", "Rejected", "phone-off", "amber"),
    "6": ("blocked", "Blocked", "phone-off", "dim"),
    "7": ("elsewhere", "Answered elsewhere", "phone-incoming", "teal"),
}
_CALL_OTHER = ("other", "Call", "phone", "dim")

# sms ``type`` -> (kind, label, icon, tone)
_SMS_KINDS = {
    "1": ("received", "Received", "message", "teal"),
    "2": ("sent", "Sent", "send", "blue"),
    "3": ("draft", "Draft", "edit", "dim"),
    "4": ("outbox", "Sending", "send", "amber"),
    "5": ("failed", "Failed", "alert", "red"),
    "6": ("queued", "Queued", "clock", "amber"),
}
_SMS_OTHER = ("other", "Message", "message", "dim")

# Placeholder numbers the call log uses for callers without a number.
_HIDDEN_NUMBERS = {
    "": "Unknown number",
    "null": "Unknown number",
    "-1": "Unknown number",
    "-2": "Private number",
    "-3": "Payphone",
}

# call state -> (pill text, pill state, icon, tone)
_STATE_VIEW = {
    "checking": ("Checking…", "", "clock", "dim"),
    "idle": ("Idle", "", "phone", "blue"),
    "ringing": ("Ringing", "warn", "phone-incoming", "amber"),
    "in call": ("In call", "ok", "phone", "green"),
    "none": ("No telephony", "error", "phone-off", "red"),
    # the device has no telephony at all (e.g. a head unit): expected, not a fault
    "unsupported": ("No telephony", "", "phone-off", "dim"),
    # a car head unit with no phone connected over Bluetooth: expected as well
    "no phone": ("No phone", "", "bluetooth", "dim"),
    # a car head unit calling through the phone connected over Bluetooth
    "phone": ("Phone connected", "ok", "bluetooth", "green"),
}
# states in which the page doesn't poll the call state
_NO_CALL_STATE = (None, "none", "unsupported", "no phone", "phone")

_HINT_DEFAULT = "Enter opens the dialler · Call rings the number"
_HINT_NO_TELEPHONY = "This device reports no call state — calls may be unavailable"
_HINT_UNSUPPORTED = "No SIM / telephony on this device — calls go through its own phone app"
_HINT_NO_PHONE = "No phone connected — connect a phone over Bluetooth to make and take calls"
_HINT_PHONE = "Calls go through the phone connected over Bluetooth"
_TIP_NO_PHONE = "No phone connected — connect a phone to the head unit over Bluetooth first"
_NO_PHONE_TITLE = "No phone connected"

# Call actions that need a connected phone on a car (see _refresh_tips).
_PHONE_ACTIONS = ("btn_call", "btn_dial", "btn_answer", "btn_end", "btn_compose")


def _unwrap_rows(res) -> list:
    rows = unwrap(res)
    if not isinstance(rows, list):
        raise RuntimeError("ADB did not return a list")
    return [r for r in rows if isinstance(r, dict)]


def _unwrap_flag(res):
    """True / False / None from a safe- or raw-mode result (``unwrap`` treats a
    plain False as a failed command, but "no phone connected" is an answer)."""
    if isinstance(res, OperationResult):
        if not res.success:
            raise res.error or RuntimeError(f"{res.action or 'ADB operation'} failed")
        res = res.value
    return res if isinstance(res, bool) else None


def format_when(ms, now=None) -> str:
    """``Today 14:05`` / ``Yesterday 09:12`` / ``12 Sep 14:05`` / ``12 Sep 2024``."""
    try:
        stamp = datetime.datetime.fromtimestamp(int(str(ms).strip()) / 1000.0)
    except (TypeError, ValueError, OverflowError, OSError):
        return str(ms or "")
    now = now or datetime.datetime.now()
    clock = stamp.strftime("%H:%M")
    if stamp.date() == now.date():
        return f"Today {clock}"
    if stamp.date() == now.date() - datetime.timedelta(days=1):
        return f"Yesterday {clock}"
    month = stamp.strftime("%b")
    if stamp.year == now.year:
        return f"{stamp.day} {month} {clock}"
    return f"{stamp.day} {month} {stamp.year}"


def format_duration(sec) -> str:
    """``0:45`` / ``12:03`` / ``1:02:03``; empty for zero or unparseable values."""
    try:
        total = int(str(sec).strip())
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def is_callable(number) -> bool:
    """True when *number* is a real, diallable number (not a hidden placeholder)."""
    text = str(number or "").strip()
    if text.lower() in _HIDDEN_NUMBERS:
        return False
    return any(ch.isdigit() for ch in text)


def friendly_failure(message) -> str:
    """A short, user-facing reason for a failed call-log / SMS / state query."""
    text = str(message or "").strip()
    low = text.lower()
    if "permission denial" in low or "securityexception" in low or "permission" in low:
        return "The device refused access (permission denied)."
    if "error while accessing provider" in low or "unknown uri" in low or "illegalargument" in low:
        return "This device has no such data provider."
    if "can't find service" in low:
        return "The device has no telephony service."
    if "timed out" in low or "timeout" in low:
        return "The device did not answer in time."
    if not text:
        return "Unknown error."
    first = text.splitlines()[0]
    return first if len(first) <= 160 else first[:157] + "…"


def is_denied(message) -> bool:
    """True when the device refused access (a SecurityException / permission denial)."""
    low = str(message or "").lower()
    return any(
        marker in low
        for marker in ("securityexception", "permission denial", "requires android.permission")
    )


def is_unsupported(message, automotive=False) -> bool:
    """True when a failure only means the device lacks the feature altogether
    (no call-log / SMS provider, no telephony service) — normal on head units.

    On a car head unit (*automotive*) a refused provider is just as expected:
    its call log and messages belong to the phone app, not to adb. On a phone
    a denial stays a real problem worth a warning."""
    low = str(message or "").lower()
    if automotive and is_denied(message):
        return True
    return any(
        marker in low
        for marker in (
            "error while accessing provider",
            "unknown uri",
            "unknown authority",
            "can't find service",
            "no such provider",
            "could not find provider",
        )
    )


def call_row(d: dict) -> dict:
    kind, label, icon_name, tone = _CALL_KINDS.get(str(d.get("type", "")).strip(), _CALL_OTHER)
    number = str(d.get("number") or "").strip()
    title = number if is_callable(number) else _HIDDEN_NUMBERS.get(number.lower(), number)
    return {
        "kind": kind,
        "label": label,
        "icon": icon_name,
        "tone": tone,
        "number": number,
        "title": title or "Unknown number",
        "when": format_when(d.get("date")),
        "detail": format_duration(d.get("duration")),
        "body": "",
    }


def sms_row(d: dict) -> dict:
    kind, label, icon_name, tone = _SMS_KINDS.get(str(d.get("type", "")).strip(), _SMS_OTHER)
    address = str(d.get("address") or "").strip()
    body = " ".join(str(d.get("body") or "").split())
    prefix = {"sent": "You: ", "draft": "Draft: ", "failed": "Not sent: "}.get(kind, "")
    return {
        "kind": kind,
        "label": label,
        "icon": icon_name,
        "tone": tone,
        "number": address,
        "title": address or "Unknown sender",
        "when": format_when(d.get("date")),
        "detail": prefix + (body or "(no text)"),
        "body": str(d.get("body") or ""),
    }


def _text_width(fm, text) -> int:
    try:
        return fm.horizontalAdvance(text)
    except AttributeError:  # Qt < 5.11
        return fm.width(text)


def _repolish(widget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class _TokenIconEngine(QIconEngine):
    """Draws an ``icons.py`` glyph in a palette token colour read at paint time.

    Icon tones cover hues and text roles, but not the text colour of filled
    buttons (``on_danger``); a tone-coloured icon on the red End button turned
    near-black in light themes.
    """

    _renderers = {}

    def __init__(self, name, token):
        super().__init__()
        self.name = name
        self.token = token

    def paint(self, painter, rect, mode, state):
        tokens = theme.palette()
        colour = tokens["frame"] if mode == QIcon.Disabled else tokens.get(self.token, tokens["text"])
        key = (self.name, colour)
        renderer = self._renderers.get(key)
        if renderer is None:
            try:
                from PyQt5.QtSvg import QSvgRenderer
            except Exception:  # QtSvg missing: no icon, the label still reads
                return
            renderer = QSvgRenderer(QByteArray(icon_svg(self.name, colour).encode("utf-8")))
            self._renderers[key] = renderer
        side = min(rect.width(), rect.height())
        target = QRectF(
            rect.x() + (rect.width() - side) / 2.0, rect.y() + (rect.height() - side) / 2.0, side, side
        )
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter, target)
        painter.restore()

    def pixmap(self, size, mode, state):
        pm = QPixmap(size)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        self.paint(painter, QRect(QPoint(0, 0), size), mode, state)
        painter.end()
        return pm

    def clone(self):
        return _TokenIconEngine(self.name, self.token)


class _ElidedLabel(QLabel):
    """A one-line hint that elides with "…" instead of clipping or forcing width."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, _event):
        painter = QPainter(self)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, rect.width())
        self.style().drawItemText(
            painter, rect, int(self.alignment()), self.palette(), self.isEnabled(), text,
            self.foregroundRole(),
        )
        painter.end()


class _IconLabel(QWidget):
    """A fixed-size vector icon that repaints in the active theme's colours."""

    def __init__(self, name, tone=None, size=16, parent=None):
        super().__init__(parent)
        self._name = name
        self._tone = tone
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def icon_name(self):
        return self._name, self._tone

    def set_icon(self, name, tone=None):
        if (name, tone) != (self._name, self._tone):
            self._name, self._tone = name, tone
            self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        cached_icon(self._name, self._tone).paint(painter, self.rect())
        painter.end()


class _KeyButton(QPushButton):
    """A dialler key: a large digit with its letters underneath.

    ``key`` emits the digit on click, or ``long_key`` (``+`` on the 0 key)
    when the button is held down.
    """

    key = pyqtSignal(str)
    LONG_PRESS_MS = 550

    def __init__(self, digit, letters="", long_key="", parent=None):
        super().__init__("", parent)
        self.digit = digit
        self.letters = letters
        self.long_key = long_key
        self._long_fired = False
        self.setFocusPolicy(Qt.NoFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(50)
        self.setAccessibleName(digit)
        if long_key:
            self.setToolTip(f"{digit} — hold for {long_key}")
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(self.LONG_PRESS_MS)
        self._hold.timeout.connect(self._on_hold)
        self.pressed.connect(self._on_pressed)
        self.released.connect(self._hold.stop)
        self.clicked.connect(self._on_clicked)

    def sizeHint(self):
        return QSize(80, 50)

    def minimumSizeHint(self):
        return QSize(52, 50)

    def _on_pressed(self):
        self._long_fired = False
        if self.long_key:
            self._hold.start()

    def _on_hold(self):
        if self.isDown():
            self._long_fired = True
            self.key.emit(self.long_key)

    def _on_clicked(self):
        if self._long_fired:
            self._long_fired = False
            return
        self.key.emit(self.digit)

    def paintEvent(self, event):
        super().paintEvent(event)  # the themed button background (text is empty)
        tokens = theme.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing)
        rect = self.rect()
        digit_font = QFont(self.font())
        digit_font.setPointSizeF(19.0 if self.digit == "*" else 14.5)
        digit_font.setWeight(QFont.DemiBold)
        painter.setFont(digit_font)
        painter.setPen(QColor(tokens["text"] if self.isEnabled() else tokens["placeholder"]))
        split = int(rect.height() * 0.62)
        if self.letters:
            painter.drawText(
                QRect(0, 0, rect.width(), split + 3), Qt.AlignHCenter | Qt.AlignBottom, self.digit
            )
            small = QFont(self.font())
            small.setPointSizeF(6.5)
            small.setWeight(QFont.Bold)
            small.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
            painter.setFont(small)
            painter.setPen(QColor(tokens["dim"]))
            painter.drawText(
                QRect(0, split + 1, rect.width(), rect.height() - split - 1),
                Qt.AlignHCenter | Qt.AlignTop,
                self.letters,
            )
        else:
            offset = 5 if self.digit == "*" else 0  # Segoe's asterisk sits high
            painter.drawText(rect.adjusted(0, offset, 0, offset), Qt.AlignCenter, self.digit)
        painter.end()


class _RowDelegate(QStyledItemDelegate):
    """Paints a call / message row: a tinted direction badge, a title with the
    time on the right, and a secondary line (coloured direction label for calls,
    an elided body preview for messages)."""

    def __init__(self, height, show_label, parent=None):
        super().__init__(parent)
        self._height = height
        self._show_label = show_label

    def sizeHint(self, option, index):
        return QSize(max(0, option.rect.width()), self._height)

    def paint(self, painter, option, index):
        row = index.data(ROW_ROLE) or {}
        tokens = theme.palette()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(option.rect).adjusted(4, 2, -4, -2)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(tokens["sel"] if selected else tokens["raised"]))
            painter.drawRoundedRect(r, 7, 7)

        tone = row.get("tone") or "dim"
        side = 34.0
        badge = QRectF(r.left() + 8, r.center().y() - side / 2, side, side)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.tint(tone, extra=0.08)))
        painter.drawEllipse(badge)
        glyph = badge.adjusted(8.5, 8.5, -8.5, -8.5).toRect()
        cached_icon(row.get("icon") or "phone", tone).paint(painter, glyph)

        base = QFont(option.font)
        title_font = QFont(base)
        title_font.setPointSizeF(10.0)
        title_font.setWeight(QFont.DemiBold)
        small_font = QFont(base)
        small_font.setPointSizeF(8.5)
        fm_title = QFontMetrics(title_font)
        fm_small = QFontMetrics(small_font)

        left = badge.right() + 12
        right = r.right() - 12
        gap = 3
        block = fm_title.height() + gap + fm_small.height()
        top = r.center().y() - block / 2.0
        title_rect = QRectF(left, top, max(0.0, right - left), fm_title.height())
        line2 = QRectF(left, top + fm_title.height() + gap, max(0.0, right - left), fm_small.height())

        when = row.get("when") or ""
        when_w = _text_width(fm_small, when) if when else 0
        painter.setFont(small_font)
        painter.setPen(QColor(tokens["sel_text"] if selected else tokens["dim"]))
        if when:
            painter.drawText(title_rect, Qt.AlignRight | Qt.AlignVCenter, when)

        painter.setFont(title_font)
        painter.setPen(QColor(tokens["sel_text"] if selected else tokens["text"]))
        title_w = max(0, int(title_rect.width() - when_w - 12))
        painter.drawText(
            title_rect,
            Qt.AlignLeft | Qt.AlignVCenter,
            fm_title.elidedText(row.get("title") or "", Qt.ElideRight, title_w),
        )

        painter.setFont(small_font)
        x = line2.left()
        if self._show_label and row.get("label"):
            label = row["label"]
            painter.setPen(QColor(theme.hue(tone)))
            painter.drawText(line2, Qt.AlignLeft | Qt.AlignVCenter, label)
            x += _text_width(fm_small, label)
            if row.get("detail"):
                sep = "  ·  "
                painter.setPen(QColor(tokens["dim"]))
                painter.drawText(QRectF(x, line2.top(), line2.right() - x, line2.height()),
                                 Qt.AlignLeft | Qt.AlignVCenter, sep)
                x += _text_width(fm_small, sep)
        detail = row.get("detail") or ""
        if detail:
            painter.setPen(QColor(tokens["sel_text"] if selected else tokens["dim"]))
            rest = QRectF(x, line2.top(), max(0.0, line2.right() - x), line2.height())
            painter.drawText(
                rest,
                Qt.AlignLeft | Qt.AlignVCenter,
                fm_small.elidedText(detail, Qt.ElideRight, int(rest.width())),
            )
        painter.restore()


class _EmptyState(QWidget):
    """Centered icon + title + detail shown in place of an empty or failed list."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(6)
        lay.addStretch(1)
        self.glyph = _IconLabel("clock", "dim", 36)
        lay.addWidget(self.glyph, 0, Qt.AlignHCenter)
        lay.addSpacing(4)
        self.title = QLabel("")
        self.title.setObjectName("cardTitle")
        self.title.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.title)
        self.detail = QLabel("")
        self.detail.setObjectName("emptyState")
        self.detail.setAlignment(Qt.AlignCenter)
        self.detail.setWordWrap(True)
        lay.addWidget(self.detail)
        lay.addStretch(2)

    def set_state(self, title, detail="", icon_name="clock", tone="dim", tip=""):
        self.glyph.set_icon(icon_name, tone)
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.setToolTip(tip)


class PhonePanel(QWidget):
    log = pyqtSignal(str)

    CALL_LIMIT = 100
    SMS_LIMIT = 100
    WIDE_MIN = 900  # side-by-side columns from this width, stacked below it
    POLL_MS = 4000  # call-state refresh while the page is visible
    PHONE_CHECK_MS = 15000  # a car's phone-connection re-check while the page is visible
    STATE_AFTER_ACTION_MS = 1500

    PAGE_CALLS = 0
    PAGE_MESSAGES = 1

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self._jobs = []
        self._closed = False
        self._loaded = False  # load lazily on first view (keeps connect fast)
        self._wide = None
        self._call_state = None
        self._state_busy = False
        self._generation = {"calls": 0, "sms": 0}
        self._warned = {"state": None, "calls": None, "sms": None}
        self._counts = {"calls": None, "sms": None}
        # What the device offers (phone_support): None until checked.
        self._support = None
        self._dial_missing = self._call_missing = self._sms_missing = False
        self._phone_apps = []
        # A car head unit (phone_support "automotive") and whether a phone is
        # connected to it over Bluetooth: True / False, None = can't tell.
        self._car = False
        self._phone_link = None
        self._link_busy = False
        self._link_reload = False
        self._link_checked_at = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_toolbar())

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        self._columns = QBoxLayout(QBoxLayout.TopToBottom, body)
        self._columns.setContentsMargins(12, 12, 12, 12)
        self._columns.setSpacing(12)
        self.dialler_card = self._build_dialler()
        self.history_card = self._build_history()
        self._columns.addWidget(self.dialler_card, 0)
        self._columns.addWidget(self.history_card, 1)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll, 1)

        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_MS)
        self._poll.timeout.connect(lambda: self._load_state(quiet=True))
        self._state_soon = QTimer(self)
        self._state_soon.setSingleShot(True)
        self._state_soon.setInterval(self.STATE_AFTER_ACTION_MS)
        self._state_soon.timeout.connect(lambda: self._load_state(quiet=True))
        # On a car, re-check the phone connection now and then, only while the
        # page is visible; nothing else is queried while no phone is connected.
        self._link_poll = QTimer(self)
        self._link_poll.setInterval(self.PHONE_CHECK_MS)
        self._link_poll.timeout.connect(lambda: self._check_phone_link())
        self._default_tips = {name: getattr(self, name).toolTip() for name in _PHONE_ACTIONS}

        self._set_state("checking")
        self.calls_empty.set_state("Loading recent calls…")
        self.sms_empty.set_state("Loading messages…")
        self._show_list(self.calls_stack, False)
        self._show_list(self.sms_stack, False)
        self.show_page(self.PAGE_CALLS)
        self._apply_width(self.width())
        self._sync_actions()

    # ------------------------------------------------------------------ build
    @staticmethod
    def _button(text, icon_name, icon_tone=None, *, tone=None, role=None, tip="", slot=None):
        b = QPushButton(text)
        if icon_name:
            b.setIcon(cached_icon(icon_name, icon_tone))
            b.setIconSize(QSize(16, 16))
        if tone:
            b.setProperty("tone", tone)
        if role:
            b.setProperty("role", role)
        if tip:
            b.setToolTip(tip)
        if slot is not None:
            b.clicked.connect(lambda _=False: slot())
        return b

    @staticmethod
    def _card():
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        return card, lay

    @staticmethod
    def _card_header():
        header = QWidget()
        header.setObjectName("cardHeader")
        header.setAttribute(Qt.WA_StyledBackground, True)
        lay = QHBoxLayout(header)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)
        return header, lay

    @staticmethod
    def _divider():
        """A 1px themed hairline (the card-header rule on an empty strip)."""
        line = QWidget()
        line.setObjectName("cardHeader")
        line.setAttribute(Qt.WA_StyledBackground, True)
        line.setFixedHeight(1)
        return line

    @staticmethod
    def _transparent_stack():
        stack = QStackedWidget()
        # A QStackedWidget is a QFrame subclass that would paint the page colour
        # as a dark slab inside the card; theme.py's #phoneStack rule clears it.
        stack.setObjectName("phoneStack")
        return stack

    def _build_toolbar(self):
        toolbar = QWidget()
        toolbar.setObjectName("pageToolbar")
        toolbar.setAttribute(Qt.WA_StyledBackground, True)
        lay = QHBoxLayout(toolbar)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)
        self.state_icon = _IconLabel("clock", "dim", 18)
        self.state_pill = QLabel("")
        self.state_pill.setObjectName("statePill")
        self.hint = _ElidedLabel(_HINT_DEFAULT)
        self.hint.setObjectName("mutedHint")
        self.btn_refresh = self._button(
            "Refresh", "refresh", "blue", role="ghost",
            tip="Reload the call state, recent calls and messages", slot=self.refresh,
        )
        lay.addWidget(self.state_icon)
        lay.addWidget(self.state_pill)
        lay.addSpacing(6)
        lay.addWidget(self.hint, 1)
        lay.addWidget(self.btn_refresh)
        return toolbar

    def _build_dialler(self):
        card, lay = self._card()
        header, h = self._card_header()
        h.addWidget(_IconLabel("dialpad", "blue", 16))
        title = QLabel("Dialler")
        title.setObjectName("cardTitle")
        h.addWidget(title)
        h.addStretch(1)
        lay.addWidget(header)

        inner = QWidget()
        inner.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._dialler_inner = inner
        v = QVBoxLayout(inner)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(12)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.number = QLineEdit()
        self.number.setObjectName("phoneNumber")
        self.number.setPlaceholderText("Enter a number")
        self.number.setAlignment(Qt.AlignCenter)
        self.number.setMinimumHeight(48)  # large type: theme.py #phoneNumber
        self.number.setToolTip("Type or paste a number · Enter opens the device dialler")
        self.number.returnPressed.connect(self._dial)
        self.number.textEdited.connect(self._sanitize_number)
        self.number.textChanged.connect(lambda _t: self._sync_actions())
        self.btn_backspace = QToolButton()
        self.btn_backspace.setObjectName("iconButton")
        self.btn_backspace.setIcon(cached_icon("backspace", "dim"))
        self.btn_backspace.setIconSize(QSize(22, 22))
        self.btn_backspace.setAutoRepeat(True)
        self.btn_backspace.setFocusPolicy(Qt.NoFocus)
        self.btn_backspace.setToolTip("Delete the last digit (hold to keep deleting)")
        self.btn_backspace.clicked.connect(lambda _=False: self._backspace())
        row.addWidget(self.number, 1)
        row.addWidget(self.btn_backspace)
        v.addLayout(row)

        # Shown when the device has no standard phone app (customised head units).
        self.app_notice = QWidget()
        notice = QVBoxLayout(self.app_notice)
        notice.setContentsMargins(0, 0, 0, 0)
        notice.setSpacing(6)
        self.app_notice_text = QLabel("")
        self.app_notice_text.setObjectName("mutedHint")
        self.app_notice_text.setWordWrap(True)
        self.app_notice_text.setAlignment(Qt.AlignCenter)
        self.btn_phone_app = self._button(
            "Open phone app", "phone", "green", role="ghost",
            tip="Open this device's own phone app", slot=self._open_phone_app,
        )
        notice.addWidget(self.app_notice_text)
        notice.addWidget(self.btn_phone_app)
        self.app_notice.hide()
        v.addWidget(self.app_notice)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.keys = {}
        for i, (digit, letters) in enumerate(_KEYPAD):
            key = _KeyButton(digit, letters, long_key="+" if digit == "0" else "")
            key.key.connect(self._press_key)
            self.keys[digit] = key
            grid.addWidget(key, i // 3, i % 3)
        v.addLayout(grid)

        actions = QGridLayout()
        actions.setHorizontalSpacing(8)
        actions.setVerticalSpacing(8)
        self.btn_call = self._button(
            "Call", "phone", "green", tone="green",
            tip="Place a call to this number on the device", slot=self._call,
        )
        # Answer and End stay usable without telephony on this device: they send
        # the standard call key events, which a head unit forwards to the phone
        # paired over Bluetooth.  Nothing to hang up is reported as a hint, not
        # as an error (see ``_action``).
        self.btn_end = self._button(
            "End", "phone-off", "text", role="danger",
            tip="Hang up / reject the current call — sends the End-call key event to the device",
            slot=self._end_call,
        )
        self.btn_end.setIcon(QIcon(_TokenIconEngine("phone-off", "on_danger")))
        self.btn_answer = self._button(
            "Answer", "phone-incoming", "green", tone="green",
            tip="Answer the ringing call — sends the Answer key event to the device "
                "(works on a head unit with a phone paired over Bluetooth)",
            slot=self._answer_call,
        )
        self.btn_dial = self._button(
            "Open in dialler", "dialpad", "accent", role="ghost",
            tip="Open the device dialler with this number filled in (does not call)",
            slot=self._dial,
        )
        for b in (self.btn_call, self.btn_end):
            b.setMinimumHeight(38)
        for b in (self.btn_answer, self.btn_dial):
            b.setMinimumHeight(32)
        actions.addWidget(self.btn_call, 0, 0)
        actions.addWidget(self.btn_end, 0, 1)
        actions.addWidget(self.btn_answer, 1, 0)
        actions.addWidget(self.btn_dial, 1, 1)
        v.addLayout(actions)

        self.dial_hint = QLabel("Hold 0 for +. Call rings straight away; Open in dialler only fills it in.")
        self.dial_hint.setObjectName("mutedHint")
        self.dial_hint.setWordWrap(True)
        self.dial_hint.setAlignment(Qt.AlignCenter)
        v.addWidget(self.dial_hint)
        v.addStretch(1)

        lay.addWidget(inner, 1, Qt.AlignHCenter)
        return card

    def _build_history(self):
        card, lay = self._card()
        header, h = self._card_header()
        header.layout().setContentsMargins(8, 6, 12, 6)
        self.seg_calls = self._button("Recent calls", "clock", "blue")
        self.seg_messages = self._button("Messages", "message", "purple")
        self._seg_group = QButtonGroup(self)
        self._seg_group.setExclusive(True)
        for index, b in enumerate((self.seg_calls, self.seg_messages)):
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, i=index: self.show_page(i))
            self._seg_group.addButton(b, index)
            h.addWidget(b)
        h.addStretch(1)
        self.count = QLabel("")
        self.count.setObjectName("mutedHint")
        h.addWidget(self.count)
        lay.addWidget(header)

        self.pages = self._transparent_stack()
        self.pages.addWidget(self._build_calls_page())
        self.pages.addWidget(self._build_sms_page())
        lay.addWidget(self.pages, 1)
        return card

    def _make_list(self, row_height, show_label):
        view = QListWidget()
        view.setItemDelegate(_RowDelegate(row_height, show_label, view))
        view.setMouseTracking(True)
        view.setUniformItemSizes(True)
        view.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setSelectionMode(QListWidget.SingleSelection)
        view.setContextMenuPolicy(Qt.CustomContextMenu)
        view.customContextMenuRequested.connect(lambda pos, w=view: self._row_menu(w, pos))
        return view

    def _build_calls_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(6, 6, 6, 0)
        v.setSpacing(0)
        self.calls = self._make_list(54, True)
        self.calls.currentItemChanged.connect(lambda *_: self._sync_actions())
        self.calls.itemActivated.connect(self._call_back_item)
        self.calls_empty = _EmptyState()
        self.calls_stack = self._transparent_stack()
        self.calls_stack.addWidget(self.calls)
        self.calls_stack.addWidget(self.calls_empty)
        v.addWidget(self.calls_stack, 1)

        v.addWidget(self._divider())
        foot = QHBoxLayout()
        foot.setContentsMargins(6, 8, 6, 8)
        foot.setSpacing(6)
        self.btn_call_back = self._button(
            "Call back", "phone-outgoing", "green", tone="green",
            tip="Put this number in the dialler — press Call to ring it", slot=self._call_back,
        )
        self.btn_message = self._button(
            "Message", "message", "purple", role="ghost",
            tip="Write a message to this number", slot=self._message_selected_call,
        )
        self.btn_copy = self._button(
            "Copy", "copy", "dim", role="ghost", tip="Copy the number", slot=self._copy_selected_call,
        )
        foot.addWidget(self.btn_call_back)
        foot.addWidget(self.btn_message)
        foot.addWidget(self.btn_copy)
        foot.addStretch(1)
        hint = _ElidedLabel("Double-click a call to use its number")
        hint.setObjectName("mutedHint")
        hint.setToolTip("Double-click a call to put its number in the dialler (it does not call)")
        hint.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        foot.addWidget(hint, 1)
        v.addLayout(foot)
        return page

    def _build_sms_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(6, 6, 6, 0)
        v.setSpacing(0)
        self.sms = self._make_list(58, False)
        self.sms.currentItemChanged.connect(self._on_sms_selected)
        self.sms.itemActivated.connect(self._reply_item)
        self.sms_empty = _EmptyState()
        self.sms_stack = self._transparent_stack()
        self.sms_stack.addWidget(self.sms)
        self.sms_stack.addWidget(self.sms_empty)
        v.addWidget(self.sms_stack, 1)

        v.addWidget(self._divider())
        compose = QVBoxLayout()
        compose.setContentsMargins(8, 10, 8, 10)
        compose.setSpacing(8)
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title_row.addWidget(_IconLabel("send", "purple", 15))
        title = QLabel("New message")
        title.setObjectName("cardTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        compose.addLayout(title_row)

        fields = QHBoxLayout()
        fields.setSpacing(8)
        self.sms_to = QLineEdit()
        self.sms_to.setPlaceholderText("To (number)")
        self.sms_to.addAction(cached_icon("user", "dim"), QLineEdit.LeadingPosition)
        self.sms_to.setMinimumWidth(120)
        self.sms_to.setMaximumWidth(200)
        self.sms_to.textChanged.connect(lambda _t: self._sync_actions())
        self.sms_body = QLineEdit()
        self.sms_body.setPlaceholderText("Message…")
        self.sms_body.addAction(cached_icon("message", "dim"), QLineEdit.LeadingPosition)
        self.sms_body.returnPressed.connect(self._compose)
        fields.addWidget(self.sms_to)
        fields.addWidget(self.sms_body, 1)
        compose.addLayout(fields)

        send_row = QHBoxLayout()
        send_row.setSpacing(8)
        note = QLabel("Opens Messages on the device with this draft — you press Send there.")
        note.setObjectName("mutedHint")
        note.setWordWrap(True)
        self.btn_compose = self._button(
            "Compose in Messages", "send", "purple", tone="purple",
            tip="Open the device's Messages app with this draft (it is not sent automatically)",
            slot=self._compose,
        )
        send_row.addWidget(note, 1)
        send_row.addWidget(self.btn_compose)
        compose.addLayout(send_row)
        v.addLayout(compose)
        return page

    # ------------------------------------------------------------ layout/state
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_width(event.size().width())

    def _apply_width(self, width):
        wide = width >= self.WIDE_MIN
        if wide == self._wide:
            return
        self._wide = wide
        if wide:
            self._columns.setDirection(QBoxLayout.LeftToRight)
            self.dialler_card.setMinimumWidth(330)
            self.dialler_card.setMaximumWidth(380)
            self.history_card.setMinimumHeight(0)
            self._dialler_inner.setMaximumWidth(16777215)
        else:
            self._columns.setDirection(QBoxLayout.TopToBottom)
            self.dialler_card.setMinimumWidth(0)
            self.dialler_card.setMaximumWidth(16777215)
            self.history_card.setMinimumHeight(460)
            self._dialler_inner.setMaximumWidth(440)

    def is_wide(self) -> bool:
        return bool(self._wide)

    def show_page(self, index):
        self.pages.setCurrentIndex(index)
        for i, (b, tone) in enumerate(((self.seg_calls, "blue"), (self.seg_messages, "purple"))):
            active = i == index
            b.setChecked(active)
            b.setProperty("tone", tone if active else None)
            b.setProperty("role", None if active else "ghost")
            _repolish(b)
        self._update_count()

    def _update_count(self):
        key = "calls" if self.pages.currentIndex() == self.PAGE_CALLS else "sms"
        n = self._counts[key]
        if n is None:
            self.count.setText("")
        elif key == "calls":
            self.count.setText(f"{n} call{'s' if n != 1 else ''}")
        else:
            self.count.setText(f"{n} message{'s' if n != 1 else ''}")

    @staticmethod
    def _show_list(stack, has_rows):
        stack.setCurrentIndex(0 if has_rows else 1)

    def _set_state(self, key, tip=""):
        text, pill_state, icon_name, tone = _STATE_VIEW[key]
        self._call_state = key
        self.state_pill.setText(text)
        self.state_pill.setToolTip(tip or f"Call state: {text}")
        self.state_icon.set_icon(icon_name, tone)
        if (self.state_pill.property("state") or "") != pill_state:
            self.state_pill.setProperty("state", pill_state)
            _repolish(self.state_pill)
        hints = {
            "none": _HINT_NO_TELEPHONY,
            "unsupported": _HINT_UNSUPPORTED,
            "no phone": _HINT_NO_PHONE,
            "phone": _HINT_PHONE,
        }
        hint = hints.get(key, _HINT_DEFAULT)
        self.hint.setText(hint)
        self.hint.setToolTip(tip if key in hints and tip else hint)

    def call_state_text(self) -> str:
        return self.state_pill.text()

    def _no_phone(self) -> bool:
        """A car head unit that positively has no phone connected."""
        return self._car and self._phone_link is False

    def _sync_actions(self):
        has_number = bool(self._number_text())
        no_phone = self._no_phone()
        self.btn_call.setEnabled(has_number and not self._call_missing and not no_phone)
        self.btn_dial.setEnabled(has_number and not self._dial_missing and not no_phone)
        self.btn_answer.setEnabled(not no_phone)
        self.btn_end.setEnabled(not no_phone)
        row = self._current_row(self.calls)
        usable = bool(row and is_callable(row.get("number")))
        for b in (self.btn_call_back, self.btn_message, self.btn_copy):
            b.setEnabled(usable)
        self.btn_compose.setEnabled(
            is_callable(self.sms_to.text()) and not self._sms_missing and not no_phone
        )

    def _refresh_tips(self):
        """Say why a call action is disabled: no phone connected (a car) or no
        app for it on the device; otherwise the button keeps its own tip."""
        no_phone = self._no_phone()
        for name, missing, tip in (
            ("btn_call", self._call_missing, "No app on this device places phone calls"),
            ("btn_dial", self._dial_missing, "This device has no dialler app"),
            ("btn_compose", self._sms_missing, "This device has no messaging app"),
            ("btn_answer", False, ""),
            ("btn_end", False, ""),
        ):
            button = getattr(self, name)
            if no_phone:
                button.setToolTip(_TIP_NO_PHONE)
            elif missing:
                button.setToolTip(tip)
            else:
                button.setToolTip(self._default_tips[name])

    def _needs_phone(self) -> bool:
        """True (and a hint under the keypad) when a car has no phone to call through."""
        if self._no_phone():
            self.dial_hint.setText("No phone connected — connect a phone over Bluetooth first.")
            return True
        return False

    # ---------------------------------------------------------------- dialler
    def _number_text(self) -> str:
        text = self.number.text().strip()
        return text if any(ch.isdigit() or ch in "*#" for ch in text) else ""

    def _sanitize_number(self, text):
        clean = "".join(ch for ch in text if ch in _NUMBER_CHARS)
        if clean == text:
            return
        pos = self.number.cursorPosition()
        kept_before = sum(1 for ch in text[:pos] if ch in _NUMBER_CHARS)
        self.number.setText(clean)
        self.number.setCursorPosition(kept_before)

    def _press_key(self, ch):
        self.number.insert(ch)

    def _backspace(self):
        if self.number.hasSelectedText():
            self.number.del_()
        elif self.number.cursorPosition() == 0 and self.number.text():
            self.number.end(False)
            self.number.backspace()
        else:
            self.number.backspace()

    def _need_number(self):
        number = self._number_text()
        if not number:
            self.number.setFocus()
            self.dial_hint.setText("Type a number first.")
        return number

    def _dial(self):
        if self._needs_phone():
            return
        if self._dial_missing:
            self.dial_hint.setText("This device has no dialler app — use its own phone app.")
            return
        number = self._need_number()
        if number:
            self._action(
                f"dial {number}",
                lambda h: h.dial(number, safe=True),
                f"[OK] Dialler opened with {number}",
            )

    def _call(self):
        if self._needs_phone():
            return
        if self._call_missing:
            self.dial_hint.setText("No app on this device places calls — use its own phone app.")
            return
        number = self._need_number()
        if number:
            self._action(
                f"call {number}",
                lambda h: h.call(number, safe=True),
                f"[OK] Calling {number}",
                then_state=True,
            )

    def _answer_call(self):
        if self._needs_phone():
            return
        self._action("answer", lambda h: h.answer_call(safe=True), "[OK] Answer sent",
                     then_state=True, expected_without_telephony=True)

    def _end_call(self):
        if self._needs_phone():
            return
        self._action("end call", lambda h: h.end_call(safe=True), "[OK] End call sent",
                     then_state=True, expected_without_telephony=True)

    def use_number(self, number):
        """Fill the dialler with *number* (never places the call)."""
        self.number.setText(str(number or "").strip())
        self.number.setFocus()
        self.dial_hint.setText("Number ready — press Call to ring it.")

    # ------------------------------------------------------------ call history
    @staticmethod
    def _current_row(view):
        item = view.currentItem()
        data = item.data(ROW_ROLE) if item is not None else None
        return data if isinstance(data, dict) else None

    def _call_back_item(self, item):
        data = item.data(ROW_ROLE) if item is not None else None
        if isinstance(data, dict) and is_callable(data.get("number")):
            self.use_number(data["number"])

    def _call_back(self):
        self._call_back_item(self.calls.currentItem())

    def _message_selected_call(self):
        row = self._current_row(self.calls)
        if row and is_callable(row.get("number")):
            self.compose_to(row["number"])

    def _copy_selected_call(self):
        row = self._current_row(self.calls)
        if row and is_callable(row.get("number")):
            QApplication.clipboard().setText(row["number"])
            self.log.emit(f"[OK] Copied {row['number']}")

    def compose_to(self, number):
        self.show_page(self.PAGE_MESSAGES)
        self.sms_to.setText(str(number or "").strip())
        self.sms_body.setFocus()

    def _on_sms_selected(self, current, _previous=None):
        data = current.data(ROW_ROLE) if current is not None else None
        if isinstance(data, dict) and is_callable(data.get("number")):
            self.sms_to.setText(data["number"])

    def _reply_item(self, item):
        data = item.data(ROW_ROLE) if item is not None else None
        if isinstance(data, dict) and is_callable(data.get("number")):
            self.compose_to(data["number"])

    def _row_menu(self, view, pos):
        item = view.itemAt(pos)
        if item is None:
            return
        view.setCurrentItem(item)
        row = item.data(ROW_ROLE) or {}
        number = row.get("number") or ""
        menu = QMenu(self)
        if is_callable(number):
            menu.addAction(cached_icon("dialpad", "green"), "Call back (fill the dialler)",
                           lambda: self.use_number(number))
            menu.addAction(cached_icon("message", "purple"), "Message…",
                           lambda: self.compose_to(number))
            menu.addAction(cached_icon("copy", "dim"), "Copy number",
                           lambda: QApplication.clipboard().setText(number))
        body = row.get("body") or ""
        if body:
            menu.addAction(cached_icon("copy", "dim"), "Copy message",
                           lambda: QApplication.clipboard().setText(body))
        if not menu.isEmpty():
            menu.exec_(view.viewport().mapToGlobal(pos))

    def _compose(self):
        to = self.sms_to.text().strip()
        body = self.sms_body.text()
        if not is_callable(to):
            self.sms_to.setFocus()
            return
        if self._sms_missing or self._no_phone():
            return
        self._action(
            f"compose to {to}",
            lambda h: h.send_sms(to, body, safe=True),
            f"[OK] Messages opened with a draft to {to} — press Send on the device",
        )

    # ----------------------------------------------------------------- jobs
    def _action(self, label, fn, ok_text, *, then_state=False,
                expected_without_telephony=False):
        """Run one device action on a worker and report the outcome.

        *expected_without_telephony* marks an action (Answer / End) that is
        offered on purpose even where there is no call state: a device that has
        nothing to answer is a normal condition, so it is reported as a hint
        instead of an error the user has to investigate.
        """
        if self._closed:
            return
        handler = self.handler

        def done(_result):
            if self._closed:
                return
            self.log.emit(ok_text)
            if then_state:
                self._state_soon.start()

        def failed(message):
            if self._closed:
                return
            expected = expected_without_telephony and (
                self._call_state in ("none", "unsupported", "no phone", "phone")
                or is_unsupported(message, automotive=self._car)
            )
            if expected:
                self.log.emit(f"[INFO] {label}: {friendly_failure(message)} "
                              "(this device has no call state of its own)")
            else:
                self.log.emit(f"[ERROR] {label}: {message}")

        run_job(self._jobs, lambda: unwrap(fn(handler)), done, failed)

    def showEvent(self, event):
        super().showEvent(event)
        # Don't query the device on connect — only when the user opens this
        # page, so the initial connect stays fast (esp. over a remote link).
        if not self._loaded:
            self.refresh()
        elif self._call_state not in _NO_CALL_STATE:
            self._poll.start()
        if self._loaded and self._car and self._phone_link is not None:
            last = self._link_checked_at
            if last is None or (time.monotonic() - last) * 1000 >= self.PHONE_CHECK_MS:
                self._check_phone_link()  # back on the page after a while: look now
        self._update_link_poll()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._poll.stop()
        self._link_poll.stop()

    def refresh(self):
        if self._closed:
            return
        self._loaded = True
        probe = getattr(self.handler, "phone_support", None)
        if self._support is None and callable(probe):
            # First find out what the device has: a customised head unit may have
            # no dialler, no call log and no telephony, none of which is an error.
            self._set_state("checking")
            run_job(
                self._jobs,
                lambda: unwrap(probe(safe=True)),
                self._support_loaded,
                lambda _message: self._support_loaded(None),
            )
            return
        if self._car and self._phone_link is not None and callable(
            getattr(self.handler, "phone_connected", None)
        ):
            # A car: see whether a phone is connected now, then load for that.
            self._check_phone_link(reload=True)
            return
        self._load_everything()

    def _load_everything(self):
        if self._no_phone():
            self._show_no_phone()
            return
        if self._support and self._support.get("telephony") is False:
            self._poll.stop()
            if self._car and self._phone_link:
                self._set_state(
                    "phone",
                    tip="Calls and messages come from the phone connected to this head "
                    "unit over Bluetooth.",
                )
            else:
                self._set_state(
                    "unsupported",
                    tip="This device has no telephony feature (no SIM). Calls through a paired "
                    "phone are handled by the device's own phone app.",
                )
        else:
            self._load_state()
        self._load_calls()
        self._load_sms()
        self._update_link_poll()

    def _show_no_phone(self):
        """A car with no phone connected: a calm state in place of the lists, the
        call actions disabled, and nothing queried until a phone connects."""
        self._poll.stop()
        self._state_soon.stop()
        for key in ("calls", "sms"):
            self._generation[key] += 1  # drop answers still on their way
            self._counts[key] = None
            self._warned[key] = None
        self._set_state("no phone", tip=_TIP_NO_PHONE)
        self.calls.clear()
        self.sms.clear()
        self.calls_empty.set_state(
            _NO_PHONE_TITLE,
            "Connect a phone over Bluetooth to see its calls and messages.",
            "bluetooth", "dim",
        )
        self.sms_empty.set_state(
            _NO_PHONE_TITLE,
            "Connect a phone over Bluetooth to see its messages.",
            "bluetooth", "dim",
        )
        self._show_list(self.calls_stack, False)
        self._show_list(self.sms_stack, False)
        self._update_count()
        self._sync_actions()
        self._update_link_poll()

    def _update_link_poll(self):
        """Poll a car's phone connection only while the page is visible and the
        answer is known; there is nothing to poll anywhere else."""
        if self._closed or not self._car or self._phone_link is None or not self.isVisible():
            self._link_poll.stop()
        elif not self._link_poll.isActive():
            self._link_poll.start()

    def _check_phone_link(self, reload=False):
        """Ask a car whether a phone is connected now (``phone_connected``: a
        dumpsys or two, no call log or messages). *reload* loads the page for
        the answer even when it hasn't changed (the Refresh button)."""
        if self._closed:
            return
        probe = getattr(self.handler, "phone_connected", None)
        if not callable(probe):
            self._link_poll.stop()
            return
        self._link_reload = self._link_reload or reload
        if self._link_busy:
            return
        if not self._link_reload and not self.isVisible():
            self._link_poll.stop()
            return
        self._link_busy = True
        # Raw mode: a failed check means "can't tell", which safe mode would
        # log as an engine error.
        run_job(
            self._jobs,
            lambda: _unwrap_flag(probe(safe=False)),
            self._link_checked,
            lambda _message: self._link_checked(None),
        )

    def _link_checked(self, value):
        self._link_busy = False
        if self._closed:
            return
        reload, self._link_reload = self._link_reload, False
        self._link_checked_at = time.monotonic()
        changed = isinstance(value, bool) and value != self._phone_link
        if changed:
            self._phone_link = value
            self._refresh_tips()
            self._sync_actions()
            self.log.emit(
                "[INFO] Phone connected to the head unit — loading its calls and messages"
                if value else "[INFO] Phone disconnected from the head unit"
            )
        if changed or reload:
            self._load_everything()
        else:
            self._update_link_poll()

    def _support_loaded(self, info):
        if self._closed:
            return
        self._apply_support(info)
        self._load_everything()

    def _apply_support(self, info):
        """Adapt the page to what the device has: disable what it can't do and
        offer its own phone app instead."""
        info = info if isinstance(info, dict) else {}
        self._support = info  # {} = checked, but the device couldn't say
        self._dial_missing = info.get("dialer") == ""
        self._call_missing = info.get("caller") == ""
        self._sms_missing = info.get("messages") == ""
        self._phone_apps = [str(p) for p in info.get("phone_apps") or [] if p]
        self._car = bool(info.get("automotive"))
        link = info.get("phone_connected") if self._car else None
        self._phone_link = link if isinstance(link, bool) else None
        self._link_checked_at = time.monotonic() if self._car else None
        if self._dial_missing and self._call_missing:
            if self._phone_apps:
                text = (
                    "This device has no standard phone app. Customised head units "
                    "use their own, often over Bluetooth:"
                )
            else:
                text = (
                    "This device has no phone app TurboADB can open. Answer and End "
                    "still send the call keys."
                )
            self.app_notice_text.setText(text)
            self.btn_phone_app.setVisible(bool(self._phone_apps))
            if len(self._phone_apps) == 1:
                self.btn_phone_app.setMenu(None)
                self.btn_phone_app.setText(f"Open {self._phone_apps[0]}")
            elif self._phone_apps:
                menu = QMenu(self.btn_phone_app)
                for package in self._phone_apps:
                    menu.addAction(package, lambda p=package: self._open_phone_app(p))
                self.btn_phone_app.setMenu(menu)
                self.btn_phone_app.setText("Open phone app")
            self.app_notice.show()
            self.dial_hint.setText("Place calls in the device's own phone app.")
        else:
            self.app_notice.hide()
        self._refresh_tips()
        self._sync_actions()

    def _open_phone_app(self, package=None):
        package = package or (self._phone_apps[0] if self._phone_apps else "")
        if package:
            self._action(
                f"open {package}",
                lambda h: h.start_app(package, safe=True),
                f"[OK] Opened {package}",
            )

    def _load_state(self, quiet=False):
        if self._closed or self._state_busy or self._no_phone():
            return
        if self._car and (self._support or {}).get("telephony") is False:
            return  # a car without telephony has no call state (e.g. after Answer / End)
        self._state_busy = True
        if not quiet:
            self._set_state("checking")
        handler = self.handler
        run_job(
            self._jobs,
            lambda: unwrap(handler.call_state(safe=True)),
            self._state_loaded,
            self._state_failed,
        )

    def _state_loaded(self, value):
        self._state_busy = False
        if self._closed or self._no_phone():  # the phone went away meanwhile
            return
        state = str(value or "").strip().lower()
        if state == "offhook":
            state = "in call"
        if state in ("idle", "ringing", "in call"):
            self._warned["state"] = None
            self._set_state(state)
            if self.isVisible():
                self._poll.start()
            return
        self._no_telephony("The device reports no call state (telephony.registry has none).")

    def _state_failed(self, message):
        self._state_busy = False
        if self._closed or self._no_phone():
            return
        if is_unsupported(message, automotive=self._car):
            # no telephony service at all (a head unit): expected, so no warning
            self._poll.stop()
            self._set_state("unsupported", tip=friendly_failure(message))
            return
        self._no_telephony(friendly_failure(message))

    def _no_telephony(self, reason):
        self._poll.stop()
        self._set_state("none", tip=reason)
        if self._warned["state"] != reason:
            self._warned["state"] = reason
            self.log.emit(f"[WARNING] Call state unavailable — {reason}")

    def _load(self, key, fn, on_done, on_failed):
        self._generation[key] += 1
        generation = self._generation[key]
        handler = self.handler

        def current(g=generation):
            return not self._closed and g == self._generation[key]

        run_job(
            self._jobs,
            lambda: _unwrap_rows(fn(handler)),
            lambda rows: on_done(rows) if current() else None,
            lambda message: on_failed(message) if current() else None,
        )

    def _load_calls(self):
        if self.calls.count() == 0:
            self.calls_empty.set_state("Loading recent calls…")
            self._show_list(self.calls_stack, False)
        limit = self.CALL_LIMIT
        # On a car a refused or missing call log is expected: raw mode keeps the
        # engine from logging it as an error, and the page shows it calmly.
        safe = not self._car
        self._load("calls", lambda h: h.call_log(limit, safe=safe),
                   self._fill_calls, self._calls_failed)

    def _load_sms(self):
        if self.sms.count() == 0:
            self.sms_empty.set_state("Loading messages…")
            self._show_list(self.sms_stack, False)
        limit = self.SMS_LIMIT
        safe = not self._car
        self._load("sms", lambda h: h.sms_list(limit, safe=safe),
                   self._fill_sms, self._sms_failed)

    @staticmethod
    def _fill(view, rows, make_row):
        view.clear()
        for d in rows:
            data = make_row(d)
            item = QListWidgetItem(f"{data['label']} — {data['title']} — {data['when']}")
            item.setData(ROW_ROLE, data)
            tip = f"{data['label']} · {data['title']}"
            if data["when"]:
                tip += f" · {data['when']}"
            if data["body"]:
                tip += "\n\n" + data["body"][:500]
            elif data["detail"]:
                tip += f" · {data['detail']}"
            item.setToolTip(tip)
            view.addItem(item)

    def _fill_calls(self, rows):
        self._warned["calls"] = None
        self._fill(self.calls, rows, call_row)
        self._counts["calls"] = len(rows)
        if rows:
            self._show_list(self.calls_stack, True)
        else:
            self.calls_empty.set_state(
                "No recent calls", "Calls made or received on the device show up here.",
                "phone", "blue",
            )
            self._show_list(self.calls_stack, False)
        self._update_count()
        self._sync_actions()
        self.log.emit(f"[OK] {len(rows)} recent call{'s' if len(rows) != 1 else ''}")

    def _calls_failed(self, message):
        reason = friendly_failure(message)
        self.calls.clear()
        self._counts["calls"] = None
        if is_unsupported(message, automotive=self._car):
            # normal on head units: there is no call log at all, so no warning
            if self._car:
                self.calls_empty.set_state(
                    "Call history not available on this head unit",
                    "It doesn't share call history over adb — its own phone app shows the calls.",
                    "phone", "dim", message,
                )
            else:
                self.calls_empty.set_state(
                    "No call history on this device",
                    "It keeps no call log — a customised head unit shows calls in its own phone app.",
                    "phone", "dim", message,
                )
            self._show_list(self.calls_stack, False)
            self._update_count()
            self._sync_actions()
            return
        self.calls_empty.set_state("Recent calls unavailable", reason, "alert", "amber", message)
        self._show_list(self.calls_stack, False)
        self._update_count()
        self._sync_actions()
        if self._warned["calls"] != reason:
            self._warned["calls"] = reason
            self.log.emit(f"[WARNING] Recent calls unavailable — {reason}")

    def _fill_sms(self, rows):
        self._warned["sms"] = None
        self._fill(self.sms, rows, sms_row)
        self._counts["sms"] = len(rows)
        if rows:
            self._show_list(self.sms_stack, True)
        else:
            self.sms_empty.set_state(
                "No messages", "SMS messages on the device show up here.", "message", "purple",
            )
            self._show_list(self.sms_stack, False)
        self._update_count()
        self.log.emit(f"[OK] {len(rows)} message{'s' if len(rows) != 1 else ''}")

    def _sms_failed(self, message):
        reason = friendly_failure(message)
        self.sms.clear()
        self._counts["sms"] = None
        if is_unsupported(message, automotive=self._car):
            if self._car:
                self.sms_empty.set_state(
                    "Messages not available on this head unit",
                    "It doesn't share messages over adb — its own apps show them.",
                    "message", "dim", message,
                )
            else:
                self.sms_empty.set_state(
                    "No messages on this device",
                    "It keeps no SMS store — messages, if any, live in its own apps.",
                    "message", "dim", message,
                )
            self._show_list(self.sms_stack, False)
            self._update_count()
            return
        self.sms_empty.set_state("Messages unavailable", reason, "alert", "amber", message)
        self._show_list(self.sms_stack, False)
        self._update_count()
        if self._warned["sms"] != reason:
            self._warned["sms"] = reason
            self.log.emit(f"[WARNING] Messages unavailable — {reason}")

    def close_panel(self):
        """Stop timers and detach running jobs (no waiting on the UI thread)."""
        self._closed = True
        self._poll.stop()
        self._state_soon.stop()
        self._link_poll.stop()
        close_jobs(self._jobs)
