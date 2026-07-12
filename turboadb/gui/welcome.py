"""The start screen shown in the central area when no device tab is open — a
MobaXterm-style landing page with the branding, quick actions and a couple of
getting-started hints, so the app never opens to a blank rectangle."""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QLabel, QToolButton, QScrollArea, QFrame)

from . import theme


class WelcomeScreen(QScrollArea):
    """A themed, centered welcome card. Wired to the main window's own actions so
    the buttons do exactly what the toolbar/sidebar do."""

    def __init__(self, win):
        super().__init__()
        self._win = win
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        host = QWidget()
        self.setWidget(host)
        outer = QVBoxLayout(host)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)

        card = QWidget(); card.setObjectName("welcomeCard")
        card.setMinimumWidth(460); card.setMaximumWidth(720)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(46, 40, 46, 38); cl.setSpacing(10)

        logo = QLabel("TurboADB"); logo.setObjectName("welcomeLogo")
        cl.addWidget(logo)
        tag = QLabel("Android ADB + scrcpy toolkit — automotive / IVI head units "
                     "and phones")
        tag.setObjectName("welcomeTag"); tag.setWordWrap(True)
        cl.addWidget(tag)
        cl.addSpacing(14)

        sec = QLabel("QUICK START"); sec.setObjectName("welcomeSection")
        cl.addWidget(sec)
        cl.addSpacing(4)

        grid = QGridLayout(); grid.setSpacing(11)
        tiles = [
            ("➕", "New target",
             "Add a device by USB or Wi-Fi/IP", win.new_session),
            ("▶", "Open selected",
             "Connect to the highlighted saved target", win.open_selected),
            ("📹", "Host webcam",
             "Open your PC camera — no device needed", win.open_webcam_tab),
            ("⬆", "Get / update tools",
             "Download or refresh adb & scrcpy", win.upgrade_tools_gui),
        ]
        for i, (emoji, title, sub, slot) in enumerate(tiles):
            grid.addWidget(self._tile(emoji, title, sub, slot), i // 2, i % 2)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
        cl.addLayout(grid)

        cl.addSpacing(16)
        sec2 = QLabel("GETTING STARTED"); sec2.setObjectName("welcomeSection")
        cl.addWidget(sec2)
        cl.addSpacing(2)
        for line in (
            "Plug in a device (or enable wireless debugging) — it appears under "
            "CONNECTED NOW (LIVE) on the left; double-click to open it.",
            "Saved targets keep phones and head units one click away — "
            "“＋ New target” to add one.",
            "Each device opens as a tab with Shell, Logcat, Files, Apps and a "
            "Mirror (scrcpy) — the ribbon acts on the current tab.",
        ):
            row = QHBoxLayout(); row.setSpacing(8)
            dot = QLabel("•"); dot.setStyleSheet(f"color:{theme.ACCENT};font-weight:800;")
            dot.setAlignment(Qt.AlignTop)
            txt = QLabel(line); txt.setObjectName("welcomeHint"); txt.setWordWrap(True)
            row.addWidget(dot, 0, Qt.AlignTop); row.addWidget(txt, 1)
            cl.addLayout(row)

        cl.addSpacing(16)
        self._foot = QLabel(); self._foot.setObjectName("welcomeFoot")
        self._foot.setWordWrap(True)
        cl.addWidget(self._foot)
        self.refresh_status([])

        centre = QHBoxLayout()
        centre.addStretch(1); centre.addWidget(card); centre.addStretch(1)
        outer.addLayout(centre)
        outer.addStretch(2)

    def _tile(self, emoji, title, sub, slot):
        b = QToolButton(); b.setObjectName("welcomeTile")
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        b.setIcon(theme.emoji_icon(emoji))
        from PyQt5.QtCore import QSize
        b.setIconSize(QSize(22, 22))
        b.setText(f"  {title}\n  {sub}")
        b.setCursor(Qt.PointingHandCursor)
        b.setSizePolicy(b.sizePolicy().Expanding, b.sizePolicy().Fixed)
        b.setToolTip(sub)
        b.clicked.connect(lambda _=False, fn=slot: fn())
        return b

    def refresh_status(self, devices):
        """Called when the live-device list changes, so the footer reflects what's
        actually plugged in."""
        ver = getattr(self._win, "_version", "")
        n = len(devices or [])
        if n == 0:
            state = "No device connected yet — plug one in or add a target to begin."
        elif n == 1:
            state = "1 device connected — double-click it on the left to open."
        else:
            state = f"{n} devices connected — pick one on the left to open."
        self._foot.setText(f"{state}    ·    TurboADB {ver}")
