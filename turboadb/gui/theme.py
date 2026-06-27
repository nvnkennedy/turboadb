"""TurboADB themes — a clean BLACK dark theme (default, neutral grays, no blue
tint) and a soft light theme. Applied at the QApplication level so every window
and dialog is styled. Accent is Android green with an automotive amber redline."""

from __future__ import annotations

# brand accents (Android green + speedometer amber)
ACCENT = "#28c2d6"        # cyan-teal (clean, not greenish)
ACCENT_2 = "#48d6e8"      # brighter cyan
ACCENT_DARK = "#0a5b66"   # deep teal — high-contrast TEXT on a light background
DANGER = "#ff6b5e"        # coral/redline
WARN = "#ffc34d"          # amber
TERM_BG = "#000000"       # true black terminal


def accent_text(name: str = "dark") -> str:
    """Accent colour to use for TEXT (group titles, section labels…). The bright
    green is fine on dark, but needs a darker shade to be readable on light."""
    return ACCENT if name != "light" else ACCENT_DARK

LOG_COLORS = {
    "ERROR": "#ff7a6e", "WARNING": "#ffc34d", "stderr": "#ffb37a",
    "OK": "#5be39a", "INFO": "#cfe3f7",
    # logcat level tags
    " E ": "#ff7a6e", " W ": "#ffc34d", " F ": "#ff5e8a",
    " I ": "#7ee2a4", " D ": "#9fb4c9", " V ": "#8b95a3",
}

# dark = near-black, neutral grays (no blue tint), green accent. "dim" lifted for
# readability (was too faint for low-vision / glasses); "line" = a lighter,
# clearly-visible outline for inputs & checkboxes.
_DARK = {
    "win": "#0a0a0a", "panel": "#141414", "raised": "#1d1d1d",
    "border": "#3a3a3a", "line": "#5a5f66", "text": "#f0f0f0", "dim": "#aeb4bb",
    "sel": "#2a2a2a", "input": "#101010", "tab": "#171717", "ribbon": "#0c0c0c",
}
# light = soft, not glaring; darker dim/line for stronger contrast
_LIGHT = {
    "win": "#dcdfe2", "panel": "#e9ecef", "raised": "#f4f6f8",
    "border": "#aab2bb", "line": "#7b838c", "text": "#161b21", "dim": "#444c54",
    "sel": "#cdeadd", "input": "#f8fafb", "tab": "#d4d9de", "ribbon": "#d4d9de",
}
THEMES = {"dark": _DARK, "light": _LIGHT}


def _checkmark_png(color: str = "#ffffff") -> str:
    """Generate (once) a small ✓ image and return its path, so a ticked checkbox
    / menu item shows a clear checkmark instead of just a filled square."""
    import os, tempfile
    cached = _CHECK_CACHE.get(color)
    if cached and os.path.exists(cached):
        return cached
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap, QPainter, QPen, QColor
    pm = QPixmap(18, 18)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color)); pen.setWidthF(2.4)
    pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawLine(4, 9, 8, 13)
    p.drawLine(8, 13, 14, 5)
    p.end()
    path = os.path.join(tempfile.gettempdir(),
                        f"turboadb-check-{color.lstrip('#')}.png")
    pm.save(path)
    _CHECK_CACHE[color] = path
    return path


_CHECK_CACHE = {}
_ARROW_CACHE = {}


def _down_arrow_png(color: str = "#aeb4bb") -> str:
    """Paint (once) a small down-chevron PNG and return its path. Qt's stylesheet
    engine can't draw a CSS border-triangle for a subcontrol — it renders as a tiny
    square/'dot' — so QComboBox::down-arrow points at a real image instead."""
    import os, tempfile
    cached = _ARROW_CACHE.get(color)
    if cached and os.path.exists(cached):
        return cached
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap, QPainter, QPen, QColor
    pm = QPixmap(14, 9)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color)); pen.setWidthF(1.8)
    pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawLine(3, 3, 7, 7)           # a clean "v" chevron
    p.drawLine(7, 7, 11, 3)
    p.end()
    path = os.path.join(tempfile.gettempdir(),
                        f"turboadb-arrow-{color.lstrip('#')}.png")
    pm.save(path)
    _ARROW_CACHE[color] = path
    return path


def stylesheet(name: str = "dark") -> str:
    c = THEMES.get(name, _DARK)
    atext = accent_text(name)        # accent that's readable as text on this bg
    try:
        check = _checkmark_png("#ffffff").replace("\\", "/")
    except Exception:
        check = ""
    try:
        arrow = _down_arrow_png(c['dim']).replace("\\", "/")
    except Exception:
        arrow = ""
    return f"""
    QWidget {{ background: {c['win']}; color: {c['text']}; font-size: 10.5pt; }}
    QMainWindow::separator {{ background: {c['border']}; width: 1px; height: 1px; }}
    QToolBar {{
        background: {c['ribbon']}; border: none;
        border-bottom: 1px solid {c['border']}; spacing: 3px; padding: 5px;
    }}
    QToolButton {{
        background: transparent; border: 1px solid transparent; border-radius: 8px;
        padding: 4px 8px; color: {c['text']};
    }}
    QToolButton:hover {{ background: {c['raised']}; border: 1px solid {c['border']}; }}
    QToolBar#ribbon QToolButton {{ padding: 3px 5px; }}   /* tight ribbon */
    QToolBar#ribbon {{ spacing: 1px; }}
    QToolButton:pressed {{ background: {ACCENT}; color: #042830; }}
    QToolButton[role="ok"] {{ background: {ACCENT}; color: #042830; border-radius: 8px;
        padding: 6px 12px; font-weight: 700; }}
    QToolButton[role="ok"]:hover {{ background: {ACCENT_2}; border: 1px solid {ACCENT_2}; }}
    QToolButton[role="ghost"] {{ background: {c['raised']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 8px; padding: 6px 12px; font-weight: 600; }}
    QToolButton[role="ghost"]:hover {{ border: 1px solid {ACCENT}; }}
    QToolButton[role="danger"] {{ background: {DANGER}; color: white; border-radius: 8px;
        padding: 6px 12px; font-weight: 700; }}
    QToolButton[role="ok"]:disabled, QToolButton[role="ghost"]:disabled {{
        background: {c['border']}; color: {c['dim']}; border-color: {c['border']}; }}
    QToolButton::menu-indicator {{ subcontrol-position: right center;
        subcontrol-origin: padding; right: 5px; }}
    QDockWidget {{ color: {c['dim']}; }}
    QDockWidget::title {{
        background: {c['ribbon']}; padding: 6px 10px;
        border-bottom: 1px solid {c['border']}; font-weight: 600;
    }}
    QListWidget, QTreeWidget {{
        background: {c['raised']}; border: 1px solid {c['border']}; border-radius: 8px;
        outline: 0;
    }}
    QListWidget::item {{ padding: 5px 8px; border-radius: 6px; }}
    QListWidget::item:selected {{ background: {ACCENT}; color: #042830; }}
    QTableWidget, QTableView {{
        background: {c['raised']}; alternate-background-color: {c['panel']};
        color: {c['text']}; gridline-color: {c['border']};
        border: 1px solid {c['border']}; border-radius: 8px; outline: 0;
    }}
    QTableWidget::item, QTableView::item {{ padding: 4px 6px; }}
    QTableWidget::item:selected, QTableView::item:selected {{
        background: {ACCENT}; color: #042830; }}
    QHeaderView::section {{
        background: {c['ribbon']}; color: {atext}; padding: 6px 8px; border: none;
        border-right: 1px solid {c['border']}; border-bottom: 1px solid {c['border']};
        font-weight: 700;
    }}
    QTableCornerButton::section {{ background: {c['ribbon']}; border: none; }}
    QLineEdit, QSpinBox, QComboBox {{
        background: {c['input']}; border: 1px solid {c['border']}; border-radius: 7px;
        padding: 6px 9px; color: {c['text']}; selection-background-color: {ACCENT};
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
    QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
        background: {c['panel']}; color: {c['dim']}; border-color: {c['border']};
    }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
        border: none; width: 22px; }}
    QComboBox::down-arrow {{ image: url({arrow}); width: 14px; height: 9px;
        margin-right: 7px; }}
    QComboBox::down-arrow:disabled {{ image: url({arrow}); }}
    QComboBox QAbstractItemView, QListView {{
        background: {c['raised']}; color: {c['text']}; border: 1px solid {c['border']};
        selection-background-color: {ACCENT}; selection-color: #042830; outline: 0;
    }}
    QComboBox QAbstractItemView::item {{ min-height: 22px; padding: 2px 6px; color: {c['text']}; }}
    QLabel {{ background: transparent; color: {c['dim']}; }}
    QPushButton {{
        background: {ACCENT}; color: #042830; border: none; border-radius: 7px;
        padding: 6px 8px; font-weight: 700;
    }}
    QPushButton:hover {{ background: {ACCENT_2}; }}
    QPushButton:disabled {{ background: {c['border']}; color: {c['dim']}; }}
    QPushButton[role="ok"] {{ background: {ACCENT_2}; }}
    QPushButton[role="danger"] {{ background: {DANGER}; color: white; }}
    QPushButton[role="ghost"] {{
        background: {c['raised']}; color: {c['text']}; border: 1px solid {c['border']};
        font-weight: 600;
    }}
    QPushButton[role="ghost"]:hover {{ border-color: {ACCENT}; background: {c['sel']}; }}
    QPushButton[role="ghost"]:pressed {{ background: {ACCENT}; color: #042830; }}
    QGroupBox {{
        background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 10px;
        margin-top: 13px; padding: 10px; font-weight: 600;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {atext}; }}
    QTabWidget::pane {{ border: 1px solid {c['border']}; background: {c['panel']}; border-radius: 6px; }}
    QTabBar::tab {{
        background: {c['tab']}; color: {c['dim']}; padding: 7px 18px;
        border: 1px solid {c['border']}; border-bottom: none;
        border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 3px;
        font-weight: 600;
    }}
    /* keep the SAME font-weight when selected — otherwise the bold text is wider
       than the tab Qt sized for the normal font, and the label gets truncated */
    QTabBar::tab:selected {{ background: {c['panel']}; color: {atext}; }}
    QTabBar::tab:hover {{ color: {c['text']}; }}
    QTabBar::tab:!selected {{ margin-top: 2px; }}
    QCheckBox {{ background: transparent; color: {c['text']}; spacing: 8px; }}
    QCheckBox::indicator, QGroupBox::indicator {{ width: 20px; height: 20px;
        border: 2px solid {c['line']}; border-radius: 5px; background: {c['input']}; }}
    QCheckBox::indicator:hover {{ border-color: {ACCENT}; background: {c['sel']}; }}
    QCheckBox::indicator:checked, QGroupBox::indicator:checked {{
        background: {ACCENT}; border-color: {ACCENT}; image: url({check}); }}
    QCheckBox::indicator:disabled {{ border-color: {c['border']}; }}
    /* checkable menu items (e.g. the ⚙ Options menu) get the same clear ✓ */
    QMenu::indicator {{ width: 18px; height: 18px; left: 6px; }}
    QMenu::indicator:checked {{ image: url({check}); }}
    QStatusBar {{ background: {c['ribbon']}; color: {c['dim']}; border-top: 1px solid {c['border']}; }}
    QProgressBar {{ border: 1px solid {c['border']}; border-radius: 6px; background: {c['input']};
        text-align: center; color: {c['text']}; height: 14px; }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
    QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 6px;
        min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
    QScrollBar::handle:vertical:pressed {{ background: {ACCENT_2}; }}
    QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 6px;
        min-width: 30px; }}
    QScrollBar::handle:horizontal:hover {{ background: {ACCENT}; }}
    QScrollBar::handle:horizontal:pressed {{ background: {ACCENT_2}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QMenu {{ background: {c['raised']}; border: 1px solid {c['border']}; }}
    QMenu::item:selected {{ background: {ACCENT}; color: #042830; }}
    QDialog {{ background: {c['win']}; }}
    """


def emoji_icon(ch: str, color: str = None):
    """Render an emoji/symbol to a QIcon. Color emojis paint themselves; plain
    symbol glyphs (⚙ ⟳ ⏻ …) would otherwise draw in black and vanish on the dark
    ribbon, so we set the pen to *color* (accent by default) — visible on both
    themes. Pass a colour to tint per-type icons (e.g. sidebar device kinds)."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap, QPainter, QFont, QIcon, QColor
    pm = QPixmap(28, 28)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setPen(QColor(color or ACCENT))
    p.setFont(QFont("Segoe UI Emoji", 14))
    p.drawText(pm.rect(), Qt.AlignCenter, ch)
    p.end()
    return QIcon(pm)
