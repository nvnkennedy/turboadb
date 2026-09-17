"""Device controls laid out as a control centre.

Six titled sections: navigation (an Android-style Back / Home / Recents bar plus
Power, Notifications and Settings tiles), media & volume, quick-setting tiles
(one tile per radio with an On | Off pair), screen & power, app/web launchers and
a text keyboard. Every control carries a colour-coded vector icon from
:mod:`icons`; tiles paint their surfaces from :mod:`theme` tokens at paint time,
so a live theme switch restyles the whole panel.

The host (DeviceTab's side panel) supplies the card surface, so sections draw no
boxes of their own. They flow into 1/2/3 columns by width (masonry, so a short
section never leaves a gap), and every tile row reflows its own column count, so
the panel never scrolls horizontally in the ~340 px side pane. A column never
grows past _COL_MAX, so buttons keep a sensible width on a big monitor; the host
asks :meth:`ControlsPanel.layout_options` how wide and tall each column count is
and sizes the panel to fit instead of stretching it.
"""

from __future__ import annotations

from PyQt5.QtCore import QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                             QScrollArea, QSizePolicy, QToolButton, QVBoxLayout,
                             QWidget)

from ..results import CommandResult, OperationResult
from . import theme
from .device_commands import DeviceCommandDispatcher
from .icons import icon

_NAV_H = 44            # Back / Home / Recents bar
_TILE_H = 60           # quick-setting tiles and icon-over-label tiles
_BTN_H = 38            # normal buttons, key chips, media keys
_INPUT_H = 36          # text fields and the buttons beside them
_SEG_H = 30            # the On | Off pair inside a quick-setting tile
_SEG_W = 46
_GROUP_W = 320         # px budget per section column
_COL_MAX = 380         # a section column never grows wider than this
_COL_GAP = 20          # between section columns
_MAX_COLS = 3
_SECTION_GAP = 18
_HOST_MARGINS = (12, 6, 12, 16)  # left, top, right, bottom around the sections

# Explanations some connectivity toggles return when every method was refused
# by the device (rather than raising); surface them as warnings, not success.
_REFUSED_MARKERS = ("can't be", "permission-denied")


class _Surface(QWidget):
    """A layout-only container. Unlike a plain QWidget, a subclass never paints
    the stylesheet's window fill, so it can't leave a slab on the side panel."""


class _Section(_Surface):
    """One titled group of controls."""


class _Glyph(QWidget):
    """A small icon painted in the live theme colour (a QLabel pixmap would keep
    the colour of the theme it was created in)."""

    def __init__(self, name, tone=None, size=16, parent=None):
        super().__init__(parent)
        self._icon = icon(name, tone)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _event):
        painter = QPainter(self)
        self._icon.paint(painter, self.rect())
        painter.end()


class _TileGrid(_Surface):
    """Equal-width buttons in a grid whose column count follows its width.

    *choices* are the allowed column counts, widest first (e.g. ``(4, 2)`` keeps
    eight launchers in full rows). The widest count whose columns still fit every
    label is used, so labels never clip and the grid never forces horizontal
    scrolling: its minimum width is one narrow column.
    """

    def __init__(self, buttons, choices, spacing=6, parent=None):
        super().__init__(parent)
        self.buttons = list(buttons)
        self._choices = tuple(sorted(set(choices), reverse=True)) or (1,)
        self._ncols = -1
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)
        for button in self.buttons:
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumWidth(1)
        self._place(self._choices[0])

    @property
    def columns(self):
        return self._ncols

    def _needed_width(self):
        need = 1
        for b in self.buttons:
            width = b.sizeHint().width()
            if (isinstance(b, QToolButton) and b.text()
                    and b.toolButtonStyle() != Qt.ToolButtonIconOnly):
                # QToolButton pads its label with two spaces of slack; the style
                # padding is already in the hint, so the label shows unclipped
                # without that slack
                width -= b.fontMetrics().horizontalAdvance(" ") * 2
            need = max(need, width)
        return need

    def columns_for(self, width):
        spacing = self._grid.horizontalSpacing()
        need = self._needed_width()
        for n in self._choices:
            if n * need + (n - 1) * spacing <= width:
                return n
        return self._choices[-1]

    def height_for(self, width=None):
        """Height at *width* (default: as placed now): the row count follows
        the width. Only a prediction, for ControlsPanel.layout_options; a real
        resize re-places the buttons."""
        if not self.buttons:
            return 0
        columns = self._ncols if width is None else self.columns_for(width)
        rows = -(-len(self.buttons) // max(1, columns))
        row_h = max(max(b.sizeHint().height(), b.minimumHeight()) for b in self.buttons)
        return rows * row_h + (rows - 1) * self._grid.verticalSpacing()

    def _place(self, ncols):
        if ncols == self._ncols:
            return
        self._ncols = ncols
        grid = self._grid
        for button in self.buttons:
            grid.removeWidget(button)
        for c in range(max(self._choices) + 1):
            grid.setColumnStretch(c, 0)
        for index, button in enumerate(self.buttons):
            row, col = divmod(index, ncols)
            grid.addWidget(button, row, col)
        for c in range(ncols):
            grid.setColumnStretch(c, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place(self.columns_for(self.width()))


class _QuickTile(QWidget):
    """One quick setting: a tinted icon badge, the name and a short caption,
    then an On | Off pair. The surface, badge and text are painted from theme
    tokens at paint time; the two buttons are ordinary tinted tool buttons."""

    _PAD = 10
    _BADGE = 36

    def __init__(self, name, caption, icon_name, tone, on_label="On", off_label="Off",
                 parent=None):
        super().__init__(parent)
        self.name = name
        self.caption = caption
        self._tone = tone
        self._icon = icon(icon_name, tone)
        self.setFixedHeight(_TILE_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName(name)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(self._PAD, 0, self._PAD, 0)
        lay.setSpacing(4)
        lay.addStretch(1)
        self.btn_on = self._segment(on_label, tone, f"Turn {name} on")
        self.btn_off = self._segment(off_label, None, f"Turn {name} off")
        lay.addWidget(self.btn_on)
        lay.addWidget(self.btn_off)

    @staticmethod
    def _segment(text, tone, tip):
        b = QToolButton()
        b.setObjectName("ctlSeg")
        b.setText(text)
        b.setToolTip(tip)
        b.setAccessibleName(tip)
        if tone:
            b.setProperty("tone", tone)
        b.setFixedSize(_SEG_W, _SEG_H)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def minimumSizeHint(self):
        # badge + a short name + the pair; longer names elide instead of
        # forcing the side panel to scroll sideways
        return QSize(self._PAD * 2 + self._BADGE + 10 + 64 + 4 + 2 * _SEG_W, _TILE_H)

    def sizeHint(self):
        return QSize(max(self.minimumSizeHint().width(), 280), _TILE_H)

    def paintEvent(self, _event):
        c = theme.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        outer = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(c["border"]), 1))
        p.setBrush(QColor(c["raised"]))
        p.drawRoundedRect(outer, 10, 10)

        top = (self.height() - self._BADGE) / 2.0
        badge = QRectF(self._PAD, top, self._BADGE, self._BADGE)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.tint(self._tone)))
        p.drawRoundedRect(badge, 10, 10)
        self._icon.paint(p, badge.adjusted(8, 8, -8, -8).toRect())

        text_x = int(badge.right()) + 10
        text_w = max(0, self.btn_on.x() - 8 - text_x)
        name_font = QFont(self.font())
        name_font.setWeight(QFont.DemiBold)
        cap_font = QFont(self.font())
        if cap_font.pointSizeF() > 0:
            cap_font.setPointSizeF(cap_font.pointSizeF() - 0.5)
        name_fm, cap_fm = QFontMetrics(name_font), QFontMetrics(cap_font)
        block = name_fm.height() + cap_fm.height()
        y = (self.height() - block) // 2
        p.setFont(name_font)
        p.setPen(QColor(c["text"]))
        p.drawText(text_x, y + name_fm.ascent(),
                   name_fm.elidedText(self.name, Qt.ElideRight, text_w))
        p.setFont(cap_font)
        p.setPen(QColor(c["dim"]))
        p.drawText(text_x, y + name_fm.height() + cap_fm.ascent(),
                   cap_fm.elidedText(self.caption, Qt.ElideRight, text_w))
        p.end()


class ControlsPanel(QWidget):
    log = pyqtSignal(str)

    def __init__(self, handler, compact=False, on_reboot=None, parent=None, dispatcher=None,
                 display_provider=None):
        """*compact=True* (the Control + Mirror side pane) allows a narrower
        minimum width so the mirror keeps most of the space — the sections
        simply reflow into a single column there."""
        super().__init__(parent)
        self.handler = handler
        self._compact = compact
        self._on_reboot = on_reboot
        self._owns_dispatcher = dispatcher is None
        # Which logical display keys and typing go to (None = the default one).
        self._display_provider = display_provider
        self._dispatcher = dispatcher or DeviceCommandDispatcher()
        self._ncols = -1
        self._strip = False  # True: laid out as a wide strip under the screen
        self._ready = False          # guard: resizeEvent fires during construction
        self.quick_tiles = {}
        # Button geometry (#ctlNav, #ctlTile, …) and the section-title padding
        # are rules in theme.stylesheet(); a panel-wide stylesheet here made
        # every live theme switch re-resolve the whole panel separately.
        self.setObjectName("controlsPanel")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll = scroll

        self._groups = [self._nav_group(), self._media_group(), self._quick_group(),
                        self._power_group(), self._web_group(), self._text_group()]
        for g in self._groups:
            g.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        host = _Surface()
        host_lay = QVBoxLayout(host)
        host_lay.setContentsMargins(*_HOST_MARGINS)
        host_lay.setSpacing(0)
        self._grid_host = _Surface()           # ONE persistent host; rebuilt in place
        self._card_grid = QGridLayout(self._grid_host)
        self._card_grid.setContentsMargins(0, 0, 0, 0)
        self._card_grid.setHorizontalSpacing(_COL_GAP)
        self._card_grid.setVerticalSpacing(0)
        self._columns = []
        # Columns stop at _COL_MAX; a wider panel centres them rather than
        # stretching every button across the spare width.
        centre = QHBoxLayout()
        centre.setContentsMargins(0, 0, 0, 0)
        centre.setSpacing(0)
        centre.addStretch(1)
        centre.addWidget(self._grid_host, 1000)
        centre.addStretch(1)
        host_lay.addLayout(centre)
        host_lay.addStretch(1)                 # sections keep their natural height
        scroll.setWidget(host)
        outer.addWidget(scroll)

        # set_strip changes the minimum width, so the host resizes us right
        # after; relayout once from the event loop, at the width we end up with.
        self._strip_timer = QTimer(self)
        self._strip_timer.setSingleShot(True)
        self._strip_timer.timeout.connect(self._relayout_now)

        self.setMinimumWidth(340 if compact else 500)
        self._ready = True
        self._relayout(1)

    def _relayout(self, ncols):
        """Flow the sections into *ncols* columns. Each column stacks its own
        sections, and each section goes to the currently shortest column, so a
        short section never leaves a hole beside a tall one."""
        ncols = max(1, ncols)
        if ncols == self._ncols:
            return
        self._ncols = ncols
        self._grid_host.setMaximumWidth(ncols * _COL_MAX + (ncols - 1) * _COL_GAP)
        grid = self._card_grid
        for g in self._groups:                # detach (kept alive by self._groups)
            g.setParent(None)
        for column in self._columns:
            grid.removeWidget(column)
            column.setParent(None)
            column.deleteLater()
        for c in range(max(_MAX_COLS, self.STRIP_COLS) + 1):
            grid.setColumnStretch(c, 0)
        self._columns, layouts, heights = [], [], []
        for c in range(ncols):
            column = _Surface()
            lay = QVBoxLayout(column)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(_SECTION_GAP)
            self._columns.append(column)
            layouts.append(lay)
            heights.append(0)
            grid.addWidget(column, 0, c, Qt.AlignTop)
            grid.setColumnStretch(c, 1)        # equal columns fill the full width
        column_w = self._column_width(self.width(), ncols)
        for g in self._groups:
            c = heights.index(min(heights))
            layouts[c].addWidget(g)
            g.setVisible(True)
            heights[c] += self._section_height(g, column_w) + _SECTION_GAP
        for lay in layouts:
            lay.addStretch(1)

    def _relayout_now(self) -> None:
        """Flow the sections into the column count the current width allows."""
        if self._ready:
            # use the panel width (not the scrollbar-reduced viewport) so a
            # vertical scrollbar can't trap us in a too-narrow single column
            self._relayout(self._columns_for(self.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout_now()

    STRIP_COLS = 6  # a strip under the screen is wide: up to six section columns

    def _columns_for(self, width, strip=None):
        strip = self._strip if strip is None else strip
        return min(self.STRIP_COLS if strip else _MAX_COLS, max(1, int(width) // _GROUP_W))

    def set_strip(self, strip: bool) -> None:
        """Lay the sections out as a wide, short strip (under the device screen)
        instead of a side panel; the side panel keeps its 340 px minimum.

        The flip comes with a new width from the splitter, which arrives as a
        resize just after this call. Relayouting here would rebuild every
        section at the OLD width and the resize would rebuild them again, so
        the work is deferred and the two collapse into one rebuild.
        """
        strip = bool(strip)
        if strip == self._strip:
            return
        self._strip = strip
        self.setMinimumWidth(1 if strip else (340 if self._compact else 500))
        if self._ready:
            self._strip_timer.start(0)

    def content_height(self, width, strip=None) -> int:
        """Height the sections need when flowed into the columns *width* allows
        (packed like _relayout: each section into the shortest column)."""
        return self.columns_height(self._columns_for(width, strip), width)

    @staticmethod
    def _column_width(width, ncols) -> int:
        """One section column's width in a panel *width* px wide."""
        ncols = max(1, int(ncols))
        inner = int(width) - _HOST_MARGINS[0] - _HOST_MARGINS[2] - (ncols - 1) * _COL_GAP
        return max(1, min(_COL_MAX, inner // ncols))

    @staticmethod
    def _section_height(group, column_width) -> int:
        """A section's height in a column *column_width* px wide: its rows
        added up, with each tile grid at the row count that width gives.
        (Not the section's size hint: right after a resize that is still the
        one from before its grids were re-placed.)"""
        lay = group.layout()
        if lay is None:
            return max(1, group.sizeHint().height())
        margins = lay.contentsMargins()
        height, rows = margins.top() + margins.bottom(), 0
        for index in range(lay.count()):
            item = lay.itemAt(index)
            if item is None or item.isEmpty():
                continue
            widget = item.widget()
            if isinstance(widget, _TileGrid):
                height += widget.height_for(column_width)
            else:
                height += item.sizeHint().height()
            rows += 1
        return max(1, height + max(0, lay.spacing()) * max(0, rows - 1))

    def columns_height(self, ncols, width=None) -> int:
        """Height of the sections packed into *ncols* columns of a panel
        *width* px wide (default: the narrowest panel showing that many)."""
        ncols = max(1, int(ncols))
        if width is None:
            width = self.width_range(ncols)[0]
        column_w = self._column_width(width, ncols)
        heights = [0] * ncols
        for g in self._groups:
            c = heights.index(min(heights))
            heights[c] += self._section_height(g, column_w) + _SECTION_GAP
        # the last section's gap is not drawn; the host's margins are
        return max(heights) - _SECTION_GAP + _HOST_MARGINS[1] + _HOST_MARGINS[3]

    def width_range(self, ncols):
        """``(narrowest, widest)`` panel widths that lay the sections out in
        *ncols* columns: from where that many first fit, to where each column
        reaches _COL_MAX (wider only adds side margins)."""
        ncols = max(1, int(ncols))
        side_min = 340 if self._compact else 500
        low = max(side_min if ncols == 1 else 0, ncols * _GROUP_W)
        high = (ncols * _COL_MAX + (ncols - 1) * _COL_GAP
                + _HOST_MARGINS[0] + _HOST_MARGINS[2] + self._scrollbar_allowance())
        return low, max(low, high)

    def _scrollbar_allowance(self) -> int:
        bar = self._scroll.verticalScrollBar()
        return max(0, bar.sizeHint().width()) if bar is not None else 0

    def layout_options(self, max_columns=_MAX_COLS):
        """``[(columns, narrowest, widest, height), …]`` for 1..*max_columns*
        section columns beside the device screen; the host picks one."""
        return [
            (n,) + self.width_range(n) + (self.columns_height(n),)
            for n in range(1, max(1, int(max_columns)) + 1)
        ]

    # ---- result / run plumbing ----
    @staticmethod
    def _result_msg(label, r):
        """Turn a handler result into an honest log line: a failed operation is
        an error, and a falsy result means the action had no effect (e.g. the
        app isn't installed)."""
        if isinstance(r, OperationResult):
            if not r.success:
                return f"[ERROR] {label}: {r.error or 'failed'}"
            r = r.value
        if isinstance(r, CommandResult):
            if not r.ok:
                detail = (r.stderr or r.text or f"exit {r.exit_code}").strip()
                return f"[ERROR] {label}: {detail}"
            r = r.text
        if r is False or (isinstance(r, str) and r.strip() == "False"):
            return (f"[WARNING] {label}: nothing happened — not available on this "
                    f"device (on an IVI the app may not be installed; try the "
                    f"Apps tab to launch what IS installed)")
        text = "" if r is None or r is True else str(r).strip()
        if text in ("", "True", "None"):
            return f"[OK] {label}"
        if any(marker in text for marker in _REFUSED_MARKERS):
            return f"[WARNING] {label}: {text}"
        return f"[OK] {label}: {text}"

    def _run(self, label, fn):
        self._dispatcher.submit(
            lambda: fn(self.handler),
            on_done=lambda result: self.log.emit(self._result_msg(label, result)),
            on_fail=lambda message: self.log.emit(f"[ERROR] {label}: {message}"),
        )

    def _run_info(self, label, fn):
        self.log.emit(f"[INFO] {label}…")
        self._dispatcher.submit(
            lambda: fn(self.handler),
            on_done=lambda result: self._show_info_result(label, result),
            on_fail=lambda message: self.log.emit(f"[ERROR] {label}: {message}"),
        )

    def _show_info_result(self, label, result):
        if isinstance(result, OperationResult):
            if not result.success:
                self.log.emit(f"[ERROR] {label}: {result.error or 'failed'}")
                return
            result = result.value
        if isinstance(result, CommandResult):
            result = result.text
        self._show_info(label, "" if result is None else str(result))

    def _show_info(self, title, text):
        from .report_dialog import show_report_dialog

        stem = "build-report" if "build" in title.lower() else "device-info"
        show_report_dialog(
            self,
            title,
            text,
            stem,
            "Copy the result, save a complete text report, or render the entire report as a PNG.",
        )

    # ---- button factories ----
    @staticmethod
    def _button(text, icon_name, tone=None, *, kind="ctlAction", height=_BTN_H,
                style=Qt.ToolButtonTextBesideIcon, icon_size=18, tip=None):
        """A tool button with a colour-coded icon. *tone* tints its fill too."""
        b = QToolButton()
        b.setObjectName(kind)
        b.setText(text)
        b.setIcon(icon(icon_name, tone or "accent"))
        b.setIconSize(QSize(icon_size, icon_size))
        b.setToolButtonStyle(style)
        if tone:
            b.setProperty("tone", tone)
        b.setFixedHeight(height)
        b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        b.setToolTip(tip or text)
        b.setAccessibleName(text)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _btn(self, text, icon_name, fn, tone=None, **kwargs):
        b = self._button(text, icon_name, tone, **kwargs)
        b.clicked.connect(lambda _=False, t=text, f=fn: self._run(t, f))
        return b

    def _key_btn(self, text, icon_name, key, tone=None, **kwargs):
        b = self._button(text, icon_name, tone, **kwargs)
        b.clicked.connect(lambda _=False, t=text, k=key: self._send_key(t, k))
        return b

    def _target_display(self):
        """The display the device screen shows — read on the UI thread, when the
        button is pressed, never from the worker that sends the key."""
        if self._display_provider is None:
            return None
        try:
            return self._display_provider()
        except Exception:
            return None

    def _display_kwargs(self):
        """``{"display_id": N}`` for a secondary display, else nothing, so the
        default display keeps the plain command."""
        display_id = self._target_display()
        return {} if display_id is None else {"display_id": display_id}

    def _send_key(self, label, key):
        extra = self._display_kwargs()
        self._run(label, lambda h: h.keyevent(key, safe=True, **extra))

    def _icon_key(self, text, icon_name, key, tone, *, kind, height, icon_size):
        """An icon-only device key (nav bar, media): the label stays as the
        tooltip, accessible name and log text."""
        return self._key_btn(text, icon_name, key, tone, kind=kind, height=height,
                             style=Qt.ToolButtonIconOnly, icon_size=icon_size)

    def _tile(self, text, icon_name, fn, tone, tip=None):
        """An icon-over-label tile (launchers, Power/Notifications/Settings)."""
        return self._btn(text, icon_name, fn, tone, kind="ctlTile", height=_TILE_H,
                         style=Qt.ToolButtonTextUnderIcon, icon_size=22, tip=tip)

    def _callback_btn(self, text, icon_name, callback, tone=None, **kwargs):
        """A button whose action is owned by the parent workflow."""
        b = self._button(text, icon_name, tone, **kwargs)
        b.clicked.connect(lambda _=False: callback())
        return b

    def _request_reboot(self):
        if callable(self._on_reboot):
            self._on_reboot()
        else:
            # Kept for standalone embedding, where there is no DeviceTab to
            # coordinate recovery. The main TurboADB UI always supplies the
            # callback above.
            self._run("Reboot", lambda h: h.reboot(safe=True))

    def _info_btn(self, text, icon_name, fn, tone=None, **kwargs):
        b = self._button(text, icon_name, tone, **kwargs)
        b.clicked.connect(lambda _=False, t=text, f=fn: self._run_info(t, f))
        return b

    def _local_btn(self, text, icon_name, slot, tone=None):
        """A button bound to a local slot beside a text field (input height)."""
        b = self._button(text, icon_name, tone, height=_INPUT_H, icon_size=16)
        # natural width normally; it may give way (rather than force sideways
        # scrolling) only when the panel is narrower than the row's labels
        b.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        b.setMinimumWidth(_INPUT_H)
        b.clicked.connect(lambda _=False: slot())
        return b

    def _quick_tile(self, key, name, caption, icon_name, tone, setter):
        tile = _QuickTile(name, caption, icon_name, tone)
        tile.btn_on.clicked.connect(
            lambda _=False: self._run(f"{name} on", lambda h: setter(h, True)))
        tile.btn_off.clicked.connect(
            lambda _=False: self._run(f"{name} off", lambda h: setter(h, False)))
        self.quick_tiles[key] = tile
        return tile

    @staticmethod
    def _field(placeholder):
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        field.setMinimumWidth(60)
        field.setFixedHeight(_INPUT_H)
        return field

    def _card(self, title, icon_name, tone="accent"):
        """A section: an icon + small title over the section's controls."""
        g = _Section()
        v = QVBoxLayout(g)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(7)
        head.addWidget(_Glyph(icon_name, tone, 15))
        heading = QLabel(title)
        # the side panel pads its own caption; section titles align with the
        # buttons (#controlsPanel QLabel#cardTitle in theme.py)
        heading.setObjectName("cardTitle")
        head.addWidget(heading, 1)
        v.addLayout(head)
        return g, v

    # ---- sections ----
    def _nav_group(self):
        g, v = self._card("Navigation", "smartphone")
        bar = _TileGrid(
            [self._icon_key(text, name, key, "blue", kind="ctlNav", height=_NAV_H,
                            icon_size=22)
             for text, name, key in (("Back", "back", "back"), ("Home", "home", "home"),
                                     ("Recents", "recents", "recents"))],
            (3,), spacing=8)
        v.addWidget(bar)
        v.addWidget(_TileGrid([
            self._key_btn("Power", "power", "power", "red", kind="ctlTile",
                          height=_TILE_H, style=Qt.ToolButtonTextUnderIcon, icon_size=22,
                          tip="Power key (sleep / wake)"),
            self._tile(
                "Notifications",
                "bell",
                lambda h: (
                    h.expand_notifications(safe=True)
                    if hasattr(h, "expand_notifications")
                    else h.keyevent("notifications", safe=True)
                ),
                "amber",
                tip="Pull down the notification shade",
            ),
            # The one "Settings" shortcut (it used to be repeated on three cards).
            self._tile("Settings", "settings", lambda h: h.open_settings(safe=True), "teal",
                       tip="Open the device's Settings app"),
        ], (3, 1), spacing=6))
        return g

    def _media_group(self):
        g, v = self._card("Media & volume", "music")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        groups = (
            ("purple", (("Volume down", "volume-down", "vol_down"),
                        ("Mute", "mute", "vol_mute"),
                        ("Volume up", "volume-up", "vol_up"))),
            ("pink", (("Previous track", "skip-back", "previous"),
                      ("Play / pause", "play-pause", "play-pause"),
                      ("Next track", "skip-forward", "next"))),
        )
        for tone, items in groups:
            seg = QHBoxLayout()
            seg.setSpacing(4)
            for text, name, action in items:
                if tone == "purple":
                    b = self._icon_key(text, name, action, tone, kind="ctlMedia",
                                       height=_BTN_H + 2, icon_size=20)
                else:
                    b = self._btn(text, name, lambda h, a=action: h.media(a, safe=True),
                                  tone, kind="ctlMedia", height=_BTN_H + 2,
                                  style=Qt.ToolButtonIconOnly, icon_size=20)
                b.setMinimumWidth(34)
                seg.addWidget(b, 1)
            row.addLayout(seg, 1)
        v.addLayout(row)
        return g

    def _quick_group(self):
        g, v = self._card("Quick settings", "sliders")
        tiles = (
            ("wifi", "Wi-Fi", "Wireless network", "wifi", "blue",
             lambda h, on: h.set_wifi(on, safe=True)),
            ("bluetooth", "Bluetooth", "Nearby devices", "bluetooth", "purple",
             lambda h, on: h.set_bluetooth(on, safe=True)),
            ("mobile_data", "Mobile data", "Cellular connection", "signal", "green",
             lambda h, on: h.set_mobile_data(on, safe=True)),
            ("airplane", "Airplane mode", "All radios off", "airplane", "orange",
             lambda h, on: h.set_airplane(on, safe=True)),
            ("hotspot", "Hotspot", "Share the connection", "hotspot", "teal",
             lambda h, on: h.set_hotspot(on, safe=True)),
        )
        stack = QVBoxLayout()
        stack.setSpacing(6)
        for key, name, caption, icon_name, tone, setter in tiles:
            stack.addWidget(self._quick_tile(key, name, caption, icon_name, tone, setter))
        v.addLayout(stack)
        return g

    def _power_group(self):
        g, v = self._card("Screen & power", "power")
        v.addWidget(self._quick_tile(
            "screen", "Screen", "Wake or sleep", "screen-on", "amber",
            lambda h, on: h.screen_on(safe=True) if on else h.screen_off(safe=True)))
        v.addWidget(_TileGrid([
            self._info_btn("Battery", "battery", lambda h: h.battery(safe=True), "green",
                           tip="Show battery level, health and charging state"),
            self._callback_btn("Reboot", "reboot", self._request_reboot, "red",
                               tip="Reboot the device"),
        ], (2, 1)))
        return g

    def _web_group(self):
        g, v = self._card("Apps & web", "apps")
        # the field gets a full row so the placeholder never truncates in the
        # side pane; its two actions share the row below
        self.url = self._field("URL or search terms…")
        self.url.addAction(icon("globe", "dim"), QLineEdit.LeadingPosition)
        self.url.returnPressed.connect(self._open_url)
        v.addWidget(self.url)
        v.addWidget(_TileGrid([
            self._local_btn("Open URL", "external", self._open_url, "blue"),
            self._local_btn("Web search", "search", self._search),
        ], (2, 1)))
        items = [
            ("Browser", "globe", "blue",
             lambda h: h.open_url("https://www.google.com", safe=True)),
            ("YouTube", "youtube", "red",
             lambda h: h.open_url("https://www.youtube.com", safe=True)),
            ("Spotify", "music", "green",
             lambda h: h.open_url("https://open.spotify.com", safe=True)),
            ("Maps", "map", "teal",
             lambda h: h.open_url("https://maps.google.com", safe=True)),
            ("Play Store", "store", "orange",
             lambda h: h.open_url("https://play.google.com/store/apps", safe=True)),
            ("Gallery", "image", "pink", lambda h: h.open_gallery(safe=True)),
            ("Calculator", "calculator", "amber", lambda h: h.open_calculator(safe=True)),
            ("Camera", "camera", "purple", lambda h: h.open_camera(safe=True)),
        ]
        # compact coloured launchers: 2x4 in the side pane, one row of 4 when wide
        v.addWidget(_TileGrid(
            [self._btn(text, name, fn, tone, height=_BTN_H + 2, icon_size=20,
                       tip=f"Open {text}")
             for text, name, tone, fn in items],
            (4, 2)))
        return g

    def _open_url(self):
        u = self.url.text().strip()
        if u:
            self._run(f"open {u}", lambda h: h.open_url(u, safe=True))

    def _search(self):
        q = self.url.text().strip()
        if q:
            self._run(f"search {q!r}", lambda h: h.web_search(q, safe=True))

    def _text_group(self):
        g, v = self._card("Keyboard", "keyboard")
        g.setToolTip("Type into the device's focused field (works when the device "
                     "has no on-screen keyboard).")
        row = QHBoxLayout()
        row.setSpacing(6)
        self.text = self._field("Type text, then Send…")
        self.text.returnPressed.connect(self._send_text)
        row.addWidget(self.text, 1)
        row.addWidget(self._local_btn("Send", "send", self._send_text, "green"))
        v.addLayout(row)
        keys = (("Enter", "enter", "enter"), ("Backspace", "backspace", "del"),
                ("Space", "space", "space"), ("Tab", "tab", "tab"),
                ("Esc", "x", "esc"), ("Search", "search", "search"))
        v.addWidget(_TileGrid(
            [self._key_btn(text, name, key, kind="ctlKey", height=_BTN_H - 2, icon_size=16,
                           tip=f"{text} key")
             for text, name, key in keys],
            (6, 3, 2)))
        return g

    def _send_text(self):
        t = self.text.text()
        if not t:
            return
        self.text.clear()
        extra = self._display_kwargs()
        self._run(f"type {t!r}", lambda h: h.input_text(t, safe=True, **extra))

    def close_panel(self):
        # Commands go through the dispatcher; a shared (device-tab) dispatcher
        # is stopped by its owner.
        if self._owns_dispatcher:
            from .qtutil import park_thread

            self._dispatcher.stop()
            park_thread(self._dispatcher)
