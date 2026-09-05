"""The start screen shown in the central area when no device tab is open — a
MobaXterm-style landing page with the branding, quick actions and a couple of
getting-started hints, so the app never opens to a blank rectangle."""

from __future__ import annotations

from PyQt5.QtCore import Qt, QSize
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QScrollArea, QFrame, QSizePolicy
)

from . import theme


class _WelcomeTile(QPushButton):
    """A clean, modern two-line action tile with icon, bold title, and subtitle."""

    def __init__(self, emoji: str, title: str, sub: str, slot, parent=None):
        super().__init__(parent)
        self.setObjectName("welcomeTile")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        self.setMinimumHeight(64)
        self.setToolTip(sub)
        self.clicked.connect(lambda _=False: slot())

        hl = QHBoxLayout(self)
        hl.setContentsMargins(14, 10, 14, 10)
        hl.setSpacing(12)

        ico = QLabel(emoji)
        ico.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        ico.setStyleSheet("font-size: 18pt; background: transparent; padding: 0px;")
        hl.addWidget(ico, 0, Qt.AlignVCenter)

        vl = QVBoxLayout()
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(3)

        t_lbl = QLabel(title)
        t_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        t_lbl.setStyleSheet("font-weight: 700; font-size: 9.5pt; background: transparent; padding: 0px;")
        sub_lbl = QLabel(sub)
        sub_lbl.setObjectName("welcomeTileSub")
        sub_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        sub_lbl.setWordWrap(True)
        sub_lbl.setStyleSheet("color: #94a3b8; font-size: 8.5pt; background: transparent; padding: 0px;")
        vl.addWidget(t_lbl)
        vl.addWidget(sub_lbl)
        hl.addLayout(vl, 1)

    def sizeHint(self):
        lay = self.layout()
        if lay:
            hint = lay.sizeHint()
            hint.setHeight(max(hint.height(), 64))
            return hint
        return super().sizeHint()


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
        host.setStyleSheet("background: transparent;")
        self.setWidget(host)
        outer = QVBoxLayout(host)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.addStretch(1)

        card = QWidget()
        card.setObjectName("welcomeCard")
        card.setMinimumWidth(480)
        card.setMaximumWidth(700)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(32, 28, 32, 28)
        cl.setSpacing(10)

        logo = QLabel("TurboADB")
        logo.setObjectName("welcomeLogo")
        cl.addWidget(logo)
        tag = QLabel("Android ADB + scrcpy toolkit — automotive / IVI head units and phones")
        tag.setObjectName("welcomeTag")
        tag.setWordWrap(True)
        cl.addWidget(tag)
        cl.addSpacing(12)

        sec = QLabel("QUICK START")
        sec.setObjectName("welcomeSection")
        cl.addWidget(sec)
        cl.addSpacing(4)

        grid = QGridLayout()
        grid.setSpacing(10)
        tiles = [
            ("➕", "New target",
             "Add a device by USB or Wi-Fi/IP", win.new_session),
            ("▶", "Open target",
             "Connect to live device or selected saved target", self._on_open_target),
            ("📹", "Host webcam",
             "Open your PC camera — no device needed", win.open_webcam_tab),
            ("⬆", "Get / update tools",
             "Download or refresh adb & scrcpy", win.upgrade_tools_gui),
        ]
        for i, (emoji, title, sub, slot) in enumerate(tiles):
            grid.addWidget(_WelcomeTile(emoji, title, sub, slot, card), i // 2, i % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        cl.addLayout(grid)

        cl.addSpacing(14)
        sec2 = QLabel("GETTING STARTED")
        sec2.setObjectName("welcomeSection")
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
            row = QHBoxLayout()
            row.setSpacing(8)
            dot = QLabel("•")
            dot.setStyleSheet(f"color:{theme.ACCENT};font-weight:800;")
            dot.setAlignment(Qt.AlignTop)
            txt = QLabel(line)
            txt.setObjectName("welcomeHint")
            txt.setWordWrap(True)
            row.addWidget(dot, 0, Qt.AlignTop)
            row.addWidget(txt, 1)
            cl.addLayout(row)

        cl.addSpacing(14)
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
        outer.addStretch(2)
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
            state = "1 device connected — double-click it on the left to open."
        else:
            state = f"{n} devices connected — pick one on the left to open."
        self._foot.setText(f"{state}    ·    TurboADB {ver}")

