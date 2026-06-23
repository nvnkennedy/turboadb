"""Device controls laid out as a UNIFORM, gap-free responsive grid that tiles the
whole panel: system keys, media, connectivity, device & power, app/web shortcuts
and a text keyboard. The groups are equal-width cards that reflow into 1/2/3
balanced columns by window width — every cell is filled (no empty regions, no
short-column void) and nothing truncates because columns are always wide enough."""

from __future__ import annotations

import math

from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QPushButton, QGroupBox, QLineEdit, QDialog,
                             QPlainTextEdit, QDialogButtonBox, QSizePolicy,
                             QScrollArea)

_BTN_MIN_H = 32        # compact enough that a 3x2 grid fits a restored window…
_BTN_MAX_H = 96        # …yet grows to fill a card when the window is large
_INPUT_H = 34
_GROUP_W = 260         # px budget per column — low enough that restored windows
                       # still get the uniform 3x2 layout (not an uneven 2x3)
_MAX_COLS = 3          # 6 cards tile cleanly as 1x6 / 2x3 / 3x2 — never leave gaps


class _Runner(QThread):
    done = pyqtSignal(str)
    fail = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(str(self.fn()))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class ControlsPanel(QWidget):
    log = pyqtSignal(str)

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self._threads = []
        self._ncols = -1
        self._ready = False          # guard: resizeEvent fires during construction

        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll = scroll

        # Six balanced cards. Each fills its grid cell (Expanding both ways), so the
        # outer grid tiles the entire panel with no gaps. Order pairs tall and short
        # groups so columns stay even.
        self._groups = [self._keys_group(), self._media_group(),
                        self._devpower_group(), self._conn_group(),
                        self._web_group(), self._text_group()]
        for g in self._groups:
            g.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        host = QWidget()
        host_lay = QVBoxLayout(host)
        host_lay.setContentsMargins(8, 8, 8, 8); host_lay.setSpacing(0)
        self._grid_host = QWidget()            # ONE persistent host; rebuilt in place
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(8); self._grid.setVerticalSpacing(8)
        host_lay.addWidget(self._grid_host, 1)
        scroll.setWidget(host)
        outer.addWidget(scroll)

        self.setMinimumWidth(500)
        self._ready = True
        self._relayout(1)

    _MAX_RC = 16        # generous bound when clearing old row/column stretches

    def _relayout(self, ncols):
        ncols = max(1, ncols)
        if ncols == self._ncols:
            return
        self._ncols = ncols
        grid = self._grid
        for g in self._groups:                # detach (kept alive by self._groups)
            grid.removeWidget(g)
            g.setParent(None)
        for i in range(self._MAX_RC):          # clear any stretches from a prior layout
            grid.setColumnStretch(i, 0)
            grid.setRowStretch(i, 0)
        nrows = math.ceil(len(self._groups) / ncols)
        for idx, g in enumerate(self._groups):
            r, c = divmod(idx, ncols)
            grid.addWidget(g, r, c)
            g.setVisible(True)
        for c in range(ncols):
            grid.setColumnStretch(c, 1)        # equal columns fill the full width
        for r in range(nrows):
            grid.setRowStretch(r, 1)           # equal rows fill the full height

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._ready:
            return
        # use the panel width (not the scrollbar-reduced viewport) so a vertical
        # scrollbar can't trap us in a too-narrow single column; cap at _MAX_COLS
        # so the 6 cards always fill a clean grid (no empty trailing cells)
        self._relayout(min(_MAX_COLS, max(1, self.width() // _GROUP_W)))

    # ---- result / run plumbing ----
    @staticmethod
    def _result_msg(label, r):
        """Turn a handler result into an honest log line: a falsy/empty result
        means the action had no effect (e.g. the app isn't installed)."""
        r = (str(r) if r is not None else "").strip()
        if r in ("True", "", "None"):
            return f"[OK] {label}"
        if r == "False":
            return (f"[WARNING] {label}: nothing happened — not available on this "
                    f"device (on an IVI the app may not be installed; try the "
                    f"Apps tab to launch what IS installed)")
        return f"[OK] {label}: {r}"

    def _run(self, label, fn):
        t = _Runner(lambda: fn(self.handler))
        t.done.connect(lambda r: self.log.emit(self._result_msg(label, r)))
        t.fail.connect(lambda m: self.log.emit(f"[ERROR] {label}: {m}"))
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t); t.start()

    def _run_info(self, label, fn):
        self.log.emit(f"{label}…")
        t = _Runner(lambda: fn(self.handler))
        t.done.connect(lambda r: self._show_info(label, r))
        t.fail.connect(lambda m: self.log.emit(f"[ERROR] {label}: {m}"))
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t); t.start()

    def _show_info(self, title, text):
        dlg = QDialog(self); dlg.setWindowTitle(title); dlg.resize(640, 460)
        v = QVBoxLayout(dlg)
        view = QPlainTextEdit(); view.setReadOnly(True); view.setPlainText(text)
        from PyQt5.QtGui import QFont
        view.setFont(QFont("Consolas", 9))
        v.addWidget(view)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject); bb.accepted.connect(dlg.accept)
        v.addWidget(bb)
        dlg.exec_()

    # ---- button factories ----
    def _btn(self, text, fn, role="ghost"):
        b = QPushButton(text); b.setProperty("role", role)
        b.setMinimumHeight(_BTN_MIN_H); b.setMaximumHeight(_BTN_MAX_H)
        b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        b.clicked.connect(lambda _=False, t=text, f=fn: self._run(t, f))
        return b

    def _info_btn(self, text, fn):
        b = QPushButton(text); b.setProperty("role", "ghost")
        b.setMinimumHeight(_BTN_MIN_H); b.setMaximumHeight(_BTN_MAX_H)
        b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        b.clicked.connect(lambda _=False, t=text, f=fn: self._run_info(t, f))
        return b

    def _local_btn(self, text, slot, role="ghost"):
        """A button bound to a local slot (input rows). Fixed, input-height — it
        sits next to a text field, so it must not balloon."""
        b = QPushButton(text); b.setProperty("role", role)
        b.setFixedHeight(_INPUT_H)
        b.clicked.connect(lambda _=False: slot())
        return b

    def _key_btn(self, text, key):
        return self._btn(text, lambda h, k=key: h.keyevent(k, safe=False))

    @staticmethod
    def _grid(ncols):
        grid = QGridLayout(); grid.setSpacing(6); grid.setContentsMargins(0, 0, 0, 0)
        for c in range(ncols):
            grid.setColumnStretch(c, 1)        # buttons share the card width
        return grid

    @staticmethod
    def _fill_rows(grid):
        for r in range(grid.rowCount()):       # stretch only rows that hold buttons
            if any(grid.itemAtPosition(r, c) for c in range(grid.columnCount())):
                grid.setRowStretch(r, 1)

    def _card(self, title):
        g = QGroupBox(title)
        v = QVBoxLayout(g); v.setContentsMargins(8, 6, 8, 8); v.setSpacing(6)
        return g, v

    # ---- groups (cards) ----
    def _keys_group(self):
        g, v = self._card("System keys")
        grid = self._grid(2)
        items = [("◀ Back", "back", 0, 0), ("⌂ Home", "home", 0, 1),
                 ("▣ Recents", "recents", 1, 0), ("⏻ Power", "power", 1, 1),
                 ("🔔 Notifs", "notifications", 2, 0)]
        for text, key, r, c in items:
            grid.addWidget(self._key_btn(text, key), r, c)
        grid.addWidget(self._btn("⚙ Settings", lambda h: h.open_settings(safe=False)), 2, 1)
        self._fill_rows(grid); v.addLayout(grid, 1)
        return g

    def _media_group(self):
        g, v = self._card("Media controls")
        grid = self._grid(3)
        grid.addWidget(self._key_btn("🔉 Vol −", "vol_down"), 0, 0)
        grid.addWidget(self._key_btn("🔇 Mute", "vol_mute"), 0, 1)
        grid.addWidget(self._key_btn("🔊 Vol +", "vol_up"), 0, 2)
        grid.addWidget(self._btn("⏮ Prev", lambda h: h.media("previous", safe=False)), 1, 0)
        grid.addWidget(self._btn("⏯ Play", lambda h: h.media("play-pause", safe=False), "ok"), 1, 1)
        grid.addWidget(self._btn("⏭ Next", lambda h: h.media("next", safe=False)), 1, 2)
        self._fill_rows(grid); v.addLayout(grid, 1)
        return g

    def _devpower_group(self):
        g, v = self._card("Device & Power")
        grid = self._grid(2)
        grid.addWidget(self._info_btn("ℹ Build info", lambda h: h.build_info(safe=False)), 0, 0)
        grid.addWidget(self._info_btn("🔋 Battery", lambda h: h.battery(safe=False)), 0, 1)
        grid.addWidget(self._btn("☀ Screen on", lambda h: h.screen_on(safe=False)), 1, 0)
        grid.addWidget(self._btn("🌙 Screen off", lambda h: h.screen_off(safe=False)), 1, 1)
        grid.addWidget(self._btn("⚙ Settings", lambda h: h.open_settings(safe=False)), 2, 0)
        grid.addWidget(self._btn("⟳ Reboot", lambda h: h.reboot(safe=False), "danger"), 2, 1)
        self._fill_rows(grid); v.addLayout(grid, 1)
        return g

    def _conn_group(self):
        g, v = self._card("Connectivity")
        grid = self._grid(2)
        defs = [("Wi-Fi On", lambda h: h.set_wifi(True, safe=False), 0, 0),
                ("Wi-Fi Off", lambda h: h.set_wifi(False, safe=False), 0, 1),
                ("BT On", lambda h: h.set_bluetooth(True, safe=False), 1, 0),
                ("BT Off", lambda h: h.set_bluetooth(False, safe=False), 1, 1),
                ("Airplane On", lambda h: h.set_airplane(True, safe=False), 2, 0),
                ("Airplane Off", lambda h: h.set_airplane(False, safe=False), 2, 1),
                ("Hotspot On", lambda h: h.set_hotspot(True, safe=False), 3, 0),
                ("Hotspot Off", lambda h: h.set_hotspot(False, safe=False), 3, 1)]
        for text, fn, r, c in defs:
            grid.addWidget(self._btn(text, fn), r, c)
        self._fill_rows(grid); v.addLayout(grid, 1)
        return g

    def _web_group(self):
        g, v = self._card("Apps & Web")
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("URL or search…")
        self.url.setMinimumWidth(60); self.url.setFixedHeight(_INPUT_H)
        self.url.returnPressed.connect(self._open_url)
        row.addWidget(self.url, 1)
        row.addWidget(self._local_btn("Open", self._open_url, "ok"))
        row.addWidget(self._local_btn("Search", self._search))
        v.addLayout(row)
        grid = self._grid(3)             # 3x3 keeps this (tallest) card short
        items = [
            ("🌐 Browser", lambda h: h.open_url("https://www.google.com", safe=False)),
            ("▶ YouTube", lambda h: h.open_url("https://www.youtube.com", safe=False)),
            ("🎵 Spotify", lambda h: h.open_url("https://open.spotify.com", safe=False)),
            ("🗺 Maps", lambda h: h.open_url("https://maps.google.com", safe=False)),
            ("🛒 Store", lambda h: h.open_url("https://play.google.com/store/apps", safe=False)),
            ("🖼 Gallery", lambda h: h.open_gallery(safe=False)),
            ("🧮 Calc", lambda h: h.open_calculator(safe=False)),
            ("📷 Camera", lambda h: h.open_camera(safe=False)),
            ("⚙ Settings", lambda h: h.open_settings(safe=False)),
        ]
        for i, (text, fn) in enumerate(items):
            grid.addWidget(self._btn(text, fn), i // 3, i % 3)
        self._fill_rows(grid); v.addLayout(grid, 1)
        return g

    def _open_url(self):
        u = self.url.text().strip()
        if u:
            self._run(f"open {u}", lambda h: h.open_url(u, safe=False))

    def _search(self):
        q = self.url.text().strip()
        if q:
            self._run(f"search {q!r}", lambda h: h.web_search(q, safe=False))

    def _text_group(self):
        g, v = self._card("Keyboard")
        g.setToolTip("Type into the device's focused field (works when the device "
                     "has no on-screen keyboard).")
        row = QHBoxLayout()
        self.text = QLineEdit()
        self.text.setPlaceholderText("type, then Send…")
        self.text.setMinimumWidth(60); self.text.setFixedHeight(_INPUT_H)
        self.text.returnPressed.connect(self._send_text)
        row.addWidget(self.text, 1)
        row.addWidget(self._local_btn("Send", self._send_text, "ok"))
        v.addLayout(row)
        grid = self._grid(3)             # 3x2 keeps the keyboard card short
        for i, (text, key) in enumerate((("⏎ Enter", "enter"), ("⌫ Backspace", "del"),
                                         ("␣ Space", "space"), ("⇥ Tab", "tab"),
                                         ("Esc", "esc"), ("🔍 Search", "search"))):
            grid.addWidget(self._key_btn(text, key), i // 3, i % 3)
        self._fill_rows(grid); v.addLayout(grid, 1)
        return g

    def _send_text(self):
        t = self.text.text()
        if not t:
            return
        self.text.clear()
        self._run(f"type {t!r}", lambda h: h.input_text(t, safe=False))

    def close_panel(self):
        for t in list(self._threads):
            t.wait(700)
