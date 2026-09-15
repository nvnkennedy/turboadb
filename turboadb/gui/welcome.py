"""The start screen shown in the central area when no device tab is open — a
landing page with the branding, quick actions and a couple of getting-started
hints, so the app never opens to a blank rectangle."""

from __future__ import annotations

from PyQt5.QtCore import QSize, Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QScrollArea, QFrame, QSizePolicy, QToolButton
)

from .icons import icon


def _glyph(name: str, tone: str, icon_size: int, box: int, badge: bool = False) -> QToolButton:
    """A non-interactive icon that follows live theme switches.

    ``badge=True`` gives it the tone's soft wash (the theme's tinted-button
    rule); otherwise it is a flat #iconButton with just the coloured glyph.
    Padding and corners come from theme.py (#welcomeBadge, #iconButton[flush]).
    """
    b = QToolButton()
    b.setIcon(icon(name, tone))
    b.setIconSize(QSize(icon_size, icon_size))
    b.setFixedSize(box, box)
    b.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    b.setFocusPolicy(Qt.NoFocus)
    if badge:
        b.setObjectName("welcomeBadge")
        b.setProperty("tone", tone)
    else:
        b.setObjectName("iconButton")
        b.setProperty("flush", "true")
    return b


class _WelcomeTile(QPushButton):
    """A two-line action tile: a tinted icon badge, a bold title and a subtitle."""

    def __init__(self, icon_name: str, tone: str, title: str, sub: str, slot, parent=None):
        super().__init__(parent)
        self.setObjectName("welcomeTile")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        self.setMinimumHeight(68)
        self.setToolTip(sub)
        self.clicked.connect(lambda _=False: slot())

        hl = QHBoxLayout(self)
        hl.setContentsMargins(14, 12, 16, 12)
        hl.setSpacing(12)

        # The badge's wash comes from the theme's tone rules, so it restyles
        # itself on a live theme switch (no multicolour emoji in the chrome).
        self.badge = _glyph(icon_name, tone, 22, 40, badge=True)
        hl.addWidget(self.badge, 0, Qt.AlignVCenter)

        vl = QVBoxLayout()
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)

        # font and colour come from the theme's #welcomeTileTitle / #welcomeTileSub rules
        t_lbl = QLabel(title)
        t_lbl.setObjectName("welcomeTileTitle")
        t_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        sub_lbl = QLabel(sub)
        sub_lbl.setObjectName("welcomeTileSub")
        sub_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        sub_lbl.setWordWrap(True)
        vl.addWidget(t_lbl)
        vl.addWidget(sub_lbl)
        hl.addLayout(vl, 1)

    def sizeHint(self):
        lay = self.layout()
        if lay:
            hint = lay.sizeHint()
            hint.setHeight(max(hint.height(), 68))
            return hint
        return super().sizeHint()


class WelcomeScreen(QScrollArea):
    """A themed welcome card in a centred column near the top third. Wired to
    the main window's own actions so the tiles do exactly what the menus and
    sidebar do."""

    TILES = (
        ("plus", "green", "New target", "Add a device by USB or Wi-Fi/IP"),
        ("plug", "blue", "Open target", "Connect to live device or selected saved target"),
        ("video", "red", "Host webcam", "Open your PC camera — no device needed"),
        ("download", "amber", "Get / update tools", "Download or refresh adb & scrcpy"),
    )

    def __init__(self, win):
        super().__init__()
        self._win = win
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        # No selector-less stylesheet here: it would cascade into the card and
        # tiles and wipe their #welcomeCard / #welcomeTile fills.
        host = QWidget()
        self.setWidget(host)
        outer = QVBoxLayout(host)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)

        card = QWidget()
        card.setObjectName("welcomeCard")
        # a plain QWidget only paints its #welcomeCard fill/border with this
        card.setAttribute(Qt.WA_StyledBackground, True)
        card.setMinimumWidth(480)
        card.setMaximumWidth(720)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(32, 28, 32, 24)
        cl.setSpacing(8)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        self.brand_mark = _glyph("smartphone", "accent", 30, 36)
        brand.addWidget(self.brand_mark, 0, Qt.AlignVCenter)
        logo = QLabel("TurboADB")
        logo.setObjectName("welcomeLogo")
        brand.addWidget(logo, 0, Qt.AlignVCenter)
        brand.addStretch(1)
        cl.addLayout(brand)
        tag = QLabel("Android ADB + scrcpy toolkit — automotive / IVI head units and phones")
        tag.setObjectName("welcomeTag")
        tag.setWordWrap(True)
        cl.addWidget(tag)
        cl.addSpacing(16)

        sec = QLabel("QUICK START")
        sec.setObjectName("welcomeSection")
        cl.addWidget(sec)
        cl.addSpacing(4)

        grid = QGridLayout()
        grid.setSpacing(12)
        slots = (win.new_session, self._on_open_target, win.open_webcam_tab, win.upgrade_tools_gui)
        self.tiles = []
        for i, ((icon_name, tone, title, sub), slot) in enumerate(zip(self.TILES, slots)):
            tile = _WelcomeTile(icon_name, tone, title, sub, slot, card)
            self.tiles.append(tile)
            grid.addWidget(tile, i // 2, i % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        cl.addLayout(grid)

        cl.addSpacing(16)
        sec2 = QLabel("GETTING STARTED")
        sec2.setObjectName("welcomeSection")
        cl.addWidget(sec2)
        cl.addSpacing(2)
        for icon_name, tone, line in (
            ("usb", "green",
             "Plug in a device (or enable wireless debugging) — it appears under "
             "Connected in the sidebar; double-click it to open."),
            ("plus", "blue",
             "Saved targets keep phones and head units one click away — "
             "use + in the sidebar to add one."),
            ("displays", "purple",
             "Each device opens as a tab with Terminal, Logcat, Files, Device Control, "
             "Apps and Webcam pages — toolbar actions apply to the current tab."),
        ):
            row = QHBoxLayout()
            row.setSpacing(10)
            txt = QLabel(line)
            txt.setObjectName("welcomeHint")
            txt.setWordWrap(True)
            row.addWidget(_glyph(icon_name, tone, 16, 20), 0, Qt.AlignTop)
            row.addWidget(txt, 1)
            cl.addLayout(row)

        cl.addSpacing(16)
        self._foot = QLabel()
        self._foot.setObjectName("welcomeFoot")
        self._foot.setWordWrap(True)
        cl.addWidget(self._foot)
        self.refresh_status([])
        self.set_scanning_status()

        centre = QHBoxLayout()
        centre.addStretch(1)
        centre.addWidget(card)
        centre.addStretch(1)
        outer.addLayout(centre)
        outer.addStretch(3)  # sit in the upper third, not floating mid-canvas

    def _on_open_target(self):
        """Intelligently open target: selected saved session, or first live device, or first saved target."""
        if hasattr(self._win, "_selected_name") and self._win._selected_name():
            self._win.open_selected()
            return
        live = getattr(self._win, "_live_devices", [])
        if live:
            d = live[0]
            s = {"name": d.label or d.serial, "type": "usb", "serial": d.serial}
            self._win._open_session(s, d.label or d.serial)
            return
        if hasattr(self._win, "store") and self._win.store.sessions:
            first = self._win.store.sessions[0]
            self._win._open_session(first, first.get("name", "Device"))
            return
        self._win.new_session()

    def set_scanning_status(self):
        """Show initial scanning status while ADB initializes."""
        ver = getattr(self._win, "_version", "")
        self._foot.setText(f"Detecting connected devices…    ·    TurboADB {ver}")

    def refresh_status(self, devices):
        """Called when the live-device list changes, so the footer reflects what's
        actually plugged in."""
        ver = getattr(self._win, "_version", "")
        n = len(devices or [])
        if n == 0:
            state = "No device connected yet — plug one in or add a target to begin."
        elif n == 1:
            state = "1 device connected — double-click it in the sidebar to open."
        else:
            state = f"{n} devices connected — pick one in the sidebar to open."
        self._foot.setText(f"{state}    ·    TurboADB {ver}")
