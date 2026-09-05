"""TurboADB themes — a clean BLACK dark theme (default, neutral grays, no blue
tint) and a soft light theme. Applied at the QApplication level so every window
and dialog is styled. Accent is Android green with an automotive amber redline."""

from __future__ import annotations

import os
import tempfile

# modern refined brand accents (clean sky/cyan + rose redline)
ACCENT = "#38bdf8"  # modern sky/cyan
ACCENT_2 = "#0ea5e9"  # vivid sky blue
ACCENT_DARK = "#0369a1"  # high contrast on light
DANGER = "#f43f5e"  # rose/red
WARN = "#f59e0b"  # amber
TERM_BG = "#0b0f19"  # rich dark slate terminal


def accent_text(name: str = "dark") -> str:
    """Accent colour to use for TEXT (group titles, section labels…)."""
    return ACCENT if name != "light" else ACCENT_DARK


LOG_COLORS = {
    "ERROR": "#f43f5e",
    "WARNING": "#f59e0b",
    "stderr": "#fb923c",
    "OK": "#34d399",
    "INFO": "#93c5fd",
    # logcat level tags
    " E ": "#f43f5e",
    " W ": "#f59e0b",
    " F ": "#ec4899",
    " I ": "#34d399",
    " D ": "#38bdf8",
    " V ": "#94a3b8",
}

# Modern dark slate/zinc palette — compact, elegant, readable
_DARK = {
    "win": "#0f1117",
    "panel": "#161922",
    "raised": "#1e222d",
    "border": "#282d3b",
    "line": "#475569",
    "text": "#f1f5f9",
    "dim": "#94a3b8",
    "sel": "#1e293b",
    "input": "#13161f",
    "tab": "#151821",
    "ribbon": "#161922",
}
# light = soft, high contrast, clean
_LIGHT = {
    "win": "#f1f5f9",
    "panel": "#e2e8f0",
    "raised": "#ffffff",
    "border": "#cbd5e1",
    "line": "#94a3b8",
    "text": "#0f172a",
    "dim": "#475569",
    "sel": "#e0f2fe",
    "input": "#ffffff",
    "tab": "#e2e8f0",
    "ribbon": "#e2e8f0",
}
THEMES = {"dark": _DARK, "light": _LIGHT}


def _checkmark_png(color: str = "#ffffff") -> str:
    """Generate (once) a small ✓ image and return its path, so a ticked checkbox
    / menu item shows a clear checkmark instead of just a filled square."""
    cached = _CHECK_CACHE.get(color)
    if cached and os.path.exists(cached):
        return cached
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap, QPainter, QPen, QColor

    pm = QPixmap(18, 18)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(2.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawLine(4, 9, 8, 13)
    p.drawLine(8, 13, 14, 5)
    p.end()
    path = os.path.join(tempfile.gettempdir(), f"turboadb-check-{color.lstrip('#')}.png")
    pm.save(path)
    _CHECK_CACHE[color] = path
    return path


_CHECK_CACHE = {}
_ARROW_CACHE = {}
_DOT_CACHE = {}


def _dot_png(color: str = "#ffffff") -> str:
    """Paint (once) a small filled dot and return its path — the inner marker of
    a CHECKED radio button (Qt's QSS engine can't draw one natively)."""
    cached = _DOT_CACHE.get(color)
    if cached and os.path.exists(cached):
        return cached
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap, QPainter, QColor

    pm = QPixmap(20, 20)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(6, 6, 8, 8)
    p.end()
    path = os.path.join(tempfile.gettempdir(), f"turboadb-dot-{color.lstrip('#')}.png")
    pm.save(path)
    _DOT_CACHE[color] = path
    return path


def _down_arrow_png(color: str = "#aeb4bb") -> str:
    """Paint (once) a small down-chevron PNG and return its path. Qt's stylesheet
    engine can't draw a CSS border-triangle for a subcontrol — it renders as a tiny
    square/'dot' — so QComboBox::down-arrow points at a real image instead."""
    cached = _ARROW_CACHE.get(color)
    if cached and os.path.exists(cached):
        return cached
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap, QPainter, QPen, QColor

    pm = QPixmap(14, 9)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(1.8)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawLine(3, 3, 7, 7)  # a clean "v" chevron
    p.drawLine(7, 7, 11, 3)
    p.end()
    path = os.path.join(tempfile.gettempdir(), f"turboadb-arrow-{color.lstrip('#')}.png")
    pm.save(path)
    _ARROW_CACHE[color] = path
    return path


def stylesheet(name: str = "dark") -> str:
    c = THEMES.get(name, _DARK)
    atext = accent_text(name)  # accent that's readable as text on this bg
    try:
        check = _checkmark_png("#ffffff").replace("\\", "/")
    except Exception:
        check = ""
    try:
        arrow = _down_arrow_png(c["dim"]).replace("\\", "/")
    except Exception:
        arrow = ""
    try:
        dot = _dot_png("#042830").replace("\\", "/")
    except Exception:
        dot = ""
    return f"""
    QWidget {{
        background: {c["win"]}; color: {c["text"]};
        font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
        font-size: 9pt;
    }}
    QMainWindow::separator {{ background: {c["border"]}; width: 1px; height: 1px; }}
    QToolBar {{
        background: {c["ribbon"]}; border: none;
        border-bottom: 1px solid {c["border"]}; spacing: 2px; padding: 2px 4px;
    }}
    QToolButton {{
        background: transparent; border: 1px solid transparent; border-radius: 5px;
        padding: 2px 6px; color: {c["text"]}; font-size: 9pt;
    }}
    QToolButton:hover {{ background: {c["raised"]}; border: 1px solid {c["border"]}; }}
    QToolBar#ribbon QToolButton {{ padding: 2px 4px; }}
    QToolBar#ribbon {{ spacing: 1px; }}
    QToolButton:pressed {{ background: {ACCENT}; color: #0f172a; }}
    QToolButton[role="ok"] {{ background: {ACCENT}; color: #0f172a; border-radius: 5px;
        padding: 4px 8px; font-weight: 600; }}
    QToolButton[role="ok"]:hover {{ background: {ACCENT_2}; border: 1px solid {ACCENT_2}; }}
    QToolButton[role="ghost"] {{ background: {c["raised"]}; color: {c["text"]};
        border: 1px solid {c["border"]}; border-radius: 5px; padding: 4px 8px; font-weight: 500; }}
    QToolButton[role="ghost"]:hover {{ border: 1px solid {ACCENT}; }}
    QToolButton[role="danger"] {{ background: {DANGER}; color: white; border-radius: 5px;
        padding: 4px 8px; font-weight: 600; }}
    QToolButton[role="ok"]:disabled, QToolButton[role="ghost"]:disabled {{
        background: {c["border"]}; color: {c["dim"]}; border-color: {c["border"]}; }}
    QToolButton::menu-indicator {{ subcontrol-position: right center;
        subcontrol-origin: padding; right: 4px; }}
    QDockWidget {{ color: {c["dim"]}; }}
    QDockWidget::title {{
        background: {c["ribbon"]}; padding: 4px 8px;
        border-bottom: 1px solid {c["border"]}; font-weight: 600; font-size: 9pt;
    }}
    QListWidget, QTreeWidget {{
        background: {c["raised"]}; border: 1px solid {c["border"]}; border-radius: 6px;
        outline: 0; font-size: 9pt;
    }}
    QListWidget::item {{ padding: 3px 6px; border-radius: 4px; }}
    QListWidget::item:selected {{ background: {c["sel"]}; color: {ACCENT}; }}
    QListWidget::indicator, QTreeWidget::indicator, QTreeView::indicator,
    QListView::indicator {{
        width: 14px; height: 14px; border: 1px solid {c["line"]};
        border-radius: 3px; background: {c["input"]}; margin-right: 4px;
    }}
    QListWidget::indicator:hover, QTreeWidget::indicator:hover,
    QListView::indicator:hover {{ border-color: {ACCENT}; }}
    QListWidget::indicator:checked, QTreeWidget::indicator:checked,
    QListView::indicator:checked {{
        background: {ACCENT}; border-color: {ACCENT}; image: url({check}); }}
    QTableWidget, QTableView {{
        background: {c["raised"]}; alternate-background-color: {c["panel"]};
        color: {c["text"]}; gridline-color: {c["border"]};
        border: 1px solid {c["border"]}; border-radius: 6px; outline: 0;
        font-size: 9pt;
    }}
    QTableWidget::item, QTableView::item {{ padding: 3px 5px; }}
    QTableWidget::item:selected, QTableView::item:selected {{
        background: {c["sel"]}; color: {ACCENT}; }}
    QHeaderView::section {{
        background: {c["ribbon"]}; color: {atext}; padding: 4px 6px; border: none;
        border-right: 1px solid {c["border"]}; border-bottom: 1px solid {c["border"]};
        font-weight: 600; font-size: 8.5pt;
    }}
    QTableCornerButton::section {{ background: {c["ribbon"]}; border: none; }}
    QLineEdit, QSpinBox, QComboBox {{
        background: {c["input"]}; border: 1px solid {c["border"]}; border-radius: 5px;
        padding: 3px 7px; color: {c["text"]}; selection-background-color: {ACCENT};
        font-size: 9pt;
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
    QPlainTextEdit {{
        font-family: 'Consolas', 'Cascadia Code', monospace;
    }}
    QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
        background: {c["panel"]}; color: {c["dim"]}; border-color: {c["border"]};
    }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
        border: none; width: 20px; }}
    QComboBox::down-arrow {{ image: url({arrow}); width: 12px; height: 8px;
        margin-right: 5px; }}
    QComboBox::down-arrow:disabled {{ image: url({arrow}); }}
    QComboBox QAbstractItemView, QListView {{
        background: {c["raised"]}; color: {c["text"]}; border: 1px solid {c["border"]};
        selection-background-color: {c["sel"]}; selection-color: {ACCENT}; outline: 0;
        font-size: 9pt;
    }}
    QComboBox QAbstractItemView::item {{ min-height: 20px; padding: 2px 5px; color: {c["text"]}; }}
    QLabel {{ background: transparent; color: {c["dim"]}; font-size: 9pt; }}
    QPushButton, QToolButton {{
        background: {c["raised"]}; color: {c["text"]}; border: 1px solid {c["border"]};
        border-radius: 5px; padding: 4px 8px; font-weight: 600; font-size: 9pt;
    }}
    QPushButton:hover, QToolButton:hover {{ border-color: {ACCENT}; background: {c["sel"]}; }}
    QPushButton:pressed, QToolButton:pressed {{ background: {ACCENT}; color: #0f172a; }}
    QPushButton:disabled, QToolButton:disabled {{ background: {c["border"]}; color: {c["dim"]}; }}
    QPushButton[role="ok"], QToolButton[role="ok"] {{ background: {ACCENT}; color: #0f172a; border-color: {ACCENT}; font-weight: 700; }}
    QPushButton[role="ok"]:hover, QToolButton[role="ok"]:hover {{ background: {ACCENT_2}; border-color: {ACCENT_2}; }}
    QPushButton[role="danger"], QToolButton[role="danger"] {{ background: {DANGER}; color: white; border-color: {DANGER}; }}
    QPushButton[role="ghost"], QToolButton[role="ghost"] {{
        background: {c["raised"]}; color: {c["text"]}; border: 1px solid {c["border"]};
        font-weight: 500;
    }}
    QPushButton[role="ghost"]:hover, QToolButton[role="ghost"]:hover {{ border-color: {ACCENT}; background: {c["sel"]}; }}
    QPushButton[role="ghost"]:pressed, QToolButton[role="ghost"]:pressed {{ background: {ACCENT}; color: #0f172a; }}
    QToolButton::menu-button {{ border: none; width: 0px; }}
    QToolButton::menu-arrow, QToolButton::menu-indicator {{ image: none; width: 0px; height: 0px; }}
    #ribbon QToolButton {{
        background: transparent; border: 1px solid transparent; border-radius: 4px;
        padding: 3px 6px; font-size: 8.5pt; font-weight: 500; color: {c["text"]};
    }}
    #ribbon QToolButton:hover {{
        background: {c["sel"]}; border-color: {c["border"]}; color: {ACCENT};
    }}
    #ribbon QToolButton:pressed {{
        background: {c["border"]}; color: {ACCENT};
    }}
    QGroupBox {{
        background: {c["panel"]}; border: 1px solid {c["border"]}; border-radius: 6px;
        margin-top: 10px; padding: 6px; font-weight: 600; font-size: 9pt;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {atext}; }}
    QTabWidget::pane {{ border: 1px solid {c["border"]}; background: {c["panel"]}; border-radius: 5px; }}
    QTabBar::tab {{
        background: {c["tab"]}; color: {c["dim"]}; padding: 5px 12px;
        border: 1px solid {c["border"]}; border-bottom: none;
        border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px;
        font-weight: 500; font-size: 9pt;
    }}
    QTabBar::tab:selected {{ background: {c["panel"]}; color: {atext}; font-weight: 600; }}
    QTabBar::tab:hover {{ color: {c["text"]}; }}
    QTabBar::tab:!selected {{ margin-top: 2px; }}
    QCheckBox {{ background: transparent; color: {c["text"]}; spacing: 6px; font-size: 9pt; }}
    QCheckBox::indicator, QGroupBox::indicator {{ width: 15px; height: 15px;
        border: 1px solid {c["line"]}; border-radius: 3px; background: {c["input"]}; }}
    QCheckBox::indicator:hover {{ border-color: {ACCENT}; background: {c["sel"]}; }}
    QCheckBox::indicator:checked, QGroupBox::indicator:checked {{
        background: {ACCENT}; border-color: {ACCENT}; image: url({check}); }}
    QCheckBox::indicator:disabled {{ border-color: {c["border"]}; }}
    QRadioButton {{ background: transparent; color: {c["text"]}; spacing: 6px; font-size: 9pt; }}
    QRadioButton::indicator {{ width: 15px; height: 15px;
        border: 1px solid {c["line"]}; border-radius: 8px; background: {c["input"]}; }}
    QRadioButton::indicator:hover {{ border-color: {ACCENT}; background: {c["sel"]}; }}
    QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT};
        image: url({dot}); }}
    QRadioButton::indicator:disabled {{ border-color: {c["border"]}; }}
    QMenu::indicator {{ width: 14px; height: 14px; left: 4px; }}
    QMenu::indicator:checked {{ image: url({check}); }}
    QSplitter::handle:horizontal {{ background: {c["border"]}; width: 5px;
        margin: 4px 1px; border-radius: 2px; }}
    QSplitter::handle:vertical {{ background: {c["border"]}; height: 5px;
        margin: 1px 4px; border-radius: 2px; }}
    QSplitter::handle:hover {{ background: {ACCENT}; }}
    QStatusBar {{ background: {c["ribbon"]}; color: {c["dim"]}; border-top: 1px solid {c["border"]}; font-size: 8.5pt; }}
    QProgressBar {{ border: 1px solid {c["border"]}; border-radius: 4px; background: {c["input"]};
        text-align: center; color: {c["text"]}; height: 12px; font-size: 8pt; }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 1px; }}
    QScrollBar::handle:vertical {{ background: {c["border"]}; border-radius: 5px;
        min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
    QScrollBar::handle:vertical:pressed {{ background: {ACCENT_2}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 1px; }}
    QScrollBar::handle:horizontal {{ background: {c["border"]}; border-radius: 5px;
        min-width: 24px; }}
    QScrollBar::handle:horizontal:hover {{ background: {ACCENT}; }}
    QScrollBar::handle:horizontal:pressed {{ background: {ACCENT_2}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QMenu {{ background: {c["raised"]}; border: 1px solid {c["border"]}; border-radius: 5px; font-size: 9pt; }}
    QMenu::item:selected {{ background: {c["sel"]}; color: {ACCENT}; }}
    QDialog {{ background: {c["win"]}; }}
    #welcomeCard {{ background: {c["panel"]}; border: 1px solid {c["border"]};
        border-radius: 10px; }}
    #welcomeLogo {{ color: {ACCENT}; font-size: 20pt; font-weight: 800;
        letter-spacing: 0.5px; }}
    #welcomeTag {{ color: {c["dim"]}; font-size: 9.5pt; }}
    #welcomeSection {{ color: {ACCENT}; font-size: 8.5pt; font-weight: 800;
        letter-spacing: 1px; padding-top: 4px; }}
    #welcomeHint {{ color: {c["text"]}; font-size: 9pt; }}
    #welcomeFoot {{ color: {c["dim"]}; font-size: 8.5pt; }}
    #welcomeTileSub {{ color: {c["dim"]}; }}
    QPushButton#welcomeTile, QToolButton#welcomeTile {{ background: {c["raised"]}; color: {c["text"]};
        border: 1px solid {c["border"]}; border-radius: 8px;
        text-align: left; }}
    QPushButton#welcomeTile:hover, QToolButton#welcomeTile:hover {{ border: 1px solid {ACCENT};
        background: {c["sel"]}; }}
    QPushButton#welcomeTile:pressed, QToolButton#welcomeTile:pressed {{ background: {c["panel"]}; }}
    """


def emoji_icon(ch: str, color: str = None):
    """Render an emoji/symbol to a QIcon. Color emojis paint themselves; plain
    symbol glyphs (⚙ ⟳ ⏻ …) would otherwise draw in black and vanish on the dark
    ribbon, so we set the pen to *color* (accent by default) — visible on both
    themes. Pass a colour to tint per-type icons (e.g. sidebar device kinds)."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap, QPainter, QFont, QIcon, QColor

    pm = QPixmap(20, 20)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setPen(QColor(color or ACCENT))
    p.setFont(QFont("Segoe UI Emoji", 11))
    p.drawText(pm.rect(), Qt.AlignCenter, ch)
    p.end()
    return QIcon(pm)
