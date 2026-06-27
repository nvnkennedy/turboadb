"""Settings dialog — categorised (sidebar + pages), like a proper preferences
window: Appearance, Tools, scrcpy, Logcat, Startup. The theme applies live the
moment you pick it. Persisted to ~/.turboadb/settings.json."""

from __future__ import annotations

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                             QComboBox, QSpinBox, QFontComboBox, QCheckBox,
                             QDialogButtonBox, QLabel, QLineEdit, QPushButton,
                             QFileDialog, QWidget, QListWidget, QListWidgetItem,
                             QStackedWidget, QApplication)
from PyQt5.QtGui import QFont

from . import settings as settings_mod
from . import theme as theme_mod


def _hint(text: str) -> QLabel:
    lab = QLabel(text); lab.setWordWrap(True)
    lab.setStyleSheet("color:#8a8a8a;")
    return lab


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TurboADB — Settings")
        self.resize(640, 460)
        self.cfg = settings_mod.load()
        self._orig_theme = self.cfg.get("theme", "dark")

        outer = QVBoxLayout(self)
        body = QHBoxLayout(); outer.addLayout(body, 1)
        self.nav = QListWidget()
        self.nav.setFixedWidth(150)
        body.addWidget(self.nav)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)

        self._add_page("Appearance", self._page_appearance())
        self._add_page("Tools", self._page_tools())
        self._add_page("scrcpy", self._page_scrcpy())
        self._add_page("Logcat", self._page_logcat())
        self._add_page("Startup", self._page_startup())
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _add_page(self, name, content):
        self.nav.addItem(QListWidgetItem(name))
        page = QWidget(); v = QVBoxLayout(page)
        v.setContentsMargins(18, 6, 8, 8)
        title = QLabel(name)
        title.setStyleSheet(f"color:{theme_mod.accent_text(self._orig_theme)};"
                            f"font-size:14pt; font-weight:700;")
        v.addWidget(title)
        v.addWidget(content)
        v.addStretch(1)
        self.pages.addWidget(page)

    # ---- pages ----
    def _page_appearance(self):
        w = QWidget(); f = QFormLayout(w)
        self.theme = QComboBox(); self.theme.addItems(["dark", "light"])
        self.theme.setCurrentText(self._orig_theme)
        # live preview: changing the theme applies it to the whole app immediately
        self.theme.currentTextChanged.connect(
            lambda name: QApplication.instance().setStyleSheet(
                theme_mod.stylesheet(name)))
        self.font = QFontComboBox()
        self.font.setCurrentFont(QFont(self.cfg.get("term_font", "Consolas")))
        self.font_size = QSpinBox(); self.font_size.setRange(7, 28)
        self.font_size.setValue(self.cfg.get("term_font_size", 10))
        f.addRow("Theme", self.theme)
        f.addRow("Terminal font", self.font)
        f.addRow("Terminal font size", self.font_size)
        f.addRow("", _hint("Theme & font apply immediately. Open tabs keep their "
                           "font until reopened."))
        return w

    def _page_tools(self):
        w = QWidget(); f = QFormLayout(w)
        self.adb_path = QLineEdit(self.cfg.get("adb_path", ""))
        self.scrcpy_path = QLineEdit(self.cfg.get("scrcpy_path", ""))
        self.ffmpeg_path = QLineEdit(self.cfg.get("ffmpeg_path", ""))
        self.adb_path.setPlaceholderText("blank = auto-detect / bundled")
        self.scrcpy_path.setPlaceholderText("blank = auto-detect / bundled")
        self.ffmpeg_path.setPlaceholderText("blank = download once / use PATH (Webcam tab)")
        f.addRow("adb path", _browse_row(self.adb_path, self))
        f.addRow("scrcpy path", _browse_row(self.scrcpy_path, self))
        f.addRow("ffmpeg path", _browse_row(self.ffmpeg_path, self))
        f.addRow("", _hint("Leave blank to let TurboADB find or download each tool. "
                           "ffmpeg powers the Webcam tab."))
        return w

    def _page_scrcpy(self):
        w = QWidget(); f = QFormLayout(w)
        self.max_size = QSpinBox(); self.max_size.setRange(0, 8192)
        self.max_size.setValue(int(self.cfg.get("scrcpy_max_size", 0)))
        self.max_size.setSpecialValueText("native")
        self.bit_rate = QLineEdit(self.cfg.get("scrcpy_bit_rate", "8M"))
        self.codec = QComboBox()
        self.codec.addItems(["auto", "h264", "h265", "av1"])
        self.codec.setCurrentText(self.cfg.get("scrcpy_video_codec") or "auto")
        self.stay = QCheckBox("Keep device awake while mirroring")
        self.stay.setChecked(self.cfg.get("scrcpy_stay_awake", True))
        self.tso = QCheckBox("Turn device screen off while mirroring")
        self.tso.setChecked(self.cfg.get("scrcpy_turn_screen_off", False))
        f.addRow("Max size (px)", self.max_size)
        f.addRow("Bit rate", self.bit_rate)
        f.addRow("Video codec", self.codec)
        f.addRow(self.stay)
        f.addRow(self.tso)
        f.addRow("", _hint("h264 is the most compatible on automotive / IVI encoders."))
        return w

    def _page_logcat(self):
        w = QWidget(); f = QFormLayout(w)
        self.logfmt = QComboBox()
        self.logfmt.addItems(["threadtime", "brief", "time", "long", "tag"])
        self.logfmt.setCurrentText(self.cfg.get("logcat_format", "threadtime"))
        f.addRow("Logcat format", self.logfmt)
        f.addRow("", _hint("'threadtime' shows the pid/tid + timestamp most logcat "
                           "filters expect."))
        return w

    def _page_startup(self):
        w = QWidget(); v = QVBoxLayout(w)
        self.docs = QCheckBox("Open docs on first run")
        self.docs.setChecked(self.cfg.get("open_docs_first_run", True))
        self.shortcut = QCheckBox("Create a desktop shortcut on first run")
        self.shortcut.setChecked(self.cfg.get("make_shortcut_first_run", True))
        self.autoupd = QCheckBox("Check PyPI for a newer TurboADB at launch")
        self.autoupd.setChecked(self.cfg.get("auto_update", True))
        v.addWidget(self.docs); v.addWidget(self.shortcut); v.addWidget(self.autoupd)
        return w

    def reject(self):
        # revert the live theme preview if the user cancels
        QApplication.instance().setStyleSheet(theme_mod.stylesheet(self._orig_theme))
        super().reject()

    def result_settings(self) -> dict:
        return {
            "theme": self.theme.currentText(),
            "term_font": self.font.currentFont().family(),
            "term_font_size": self.font_size.value(),
            "adb_path": self.adb_path.text().strip(),
            "scrcpy_path": self.scrcpy_path.text().strip(),
            "ffmpeg_path": self.ffmpeg_path.text().strip(),
            "scrcpy_max_size": self.max_size.value(),
            "scrcpy_bit_rate": self.bit_rate.text().strip(),
            "scrcpy_video_codec": ("" if self.codec.currentText() == "auto"
                                   else self.codec.currentText()),
            "scrcpy_stay_awake": self.stay.isChecked(),
            "scrcpy_turn_screen_off": self.tso.isChecked(),
            "logcat_format": self.logfmt.currentText(),
            "open_docs_first_run": self.docs.isChecked(),
            "make_shortcut_first_run": self.shortcut.isChecked(),
            "auto_update": self.autoupd.isChecked(),
        }


def _browse_row(line_edit: QLineEdit, parent) -> QWidget:
    row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
    btn = QPushButton("Browse…"); btn.setProperty("role", "ghost")

    def pick():
        path, _ = QFileDialog.getOpenFileName(parent, "Select executable")
        if path:
            line_edit.setText(path)
    btn.clicked.connect(pick)
    row.addWidget(line_edit, 1); row.addWidget(btn)
    w = QWidget(); w.setLayout(row)
    return w
