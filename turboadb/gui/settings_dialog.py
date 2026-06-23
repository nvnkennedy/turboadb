"""Settings dialog: theme, terminal font, tool paths, scrcpy defaults, logcat
format, first-run behaviour. Persisted to ~/.turboadb/settings.json."""

from __future__ import annotations

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QComboBox,
                             QSpinBox, QFontComboBox, QCheckBox, QDialogButtonBox,
                             QGroupBox, QLabel, QLineEdit, QHBoxLayout, QPushButton,
                             QFileDialog, QWidget, QApplication)
from PyQt5.QtGui import QFont

from . import settings as settings_mod
from . import theme as theme_mod


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TurboADB — Settings")
        self.resize(480, 520)
        self.cfg = settings_mod.load()
        lay = QVBoxLayout(self)

        appear = QGroupBox("Appearance")
        af = QFormLayout(appear)
        self._orig_theme = self.cfg.get("theme", "dark")
        self.theme = QComboBox(); self.theme.addItems(["dark", "light"])
        self.theme.setCurrentText(self._orig_theme)
        # live preview: changing the theme applies it immediately
        self.theme.currentTextChanged.connect(
            lambda name: QApplication.instance().setStyleSheet(
                theme_mod.stylesheet(name)))
        self.font = QFontComboBox()
        self.font.setCurrentFont(QFont(self.cfg.get("term_font", "Consolas")))
        self.font_size = QSpinBox(); self.font_size.setRange(7, 28)
        self.font_size.setValue(self.cfg.get("term_font_size", 10))
        af.addRow("Theme", self.theme)
        af.addRow("Terminal font", self.font)
        af.addRow("Terminal font size", self.font_size)
        lay.addWidget(appear)

        tools = QGroupBox("Tool paths (blank = auto-detect)")
        tf = QFormLayout(tools)
        self.adb_path = QLineEdit(self.cfg.get("adb_path", ""))
        self.scrcpy_path = QLineEdit(self.cfg.get("scrcpy_path", ""))
        tf.addRow("adb path", _browse_row(self.adb_path, self))
        tf.addRow("scrcpy path", _browse_row(self.scrcpy_path, self))
        lay.addWidget(tools)

        scr = QGroupBox("scrcpy defaults")
        sf = QFormLayout(scr)
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
        sf.addRow("Max size (px)", self.max_size)
        sf.addRow("Bit rate", self.bit_rate)
        sf.addRow("Video codec", self.codec)
        sf.addRow(self.stay)
        sf.addRow(self.tso)
        lay.addWidget(scr)

        misc = QGroupBox("Logcat & startup")
        mf = QFormLayout(misc)
        self.logfmt = QComboBox()
        self.logfmt.addItems(["threadtime", "brief", "time", "long", "tag"])
        self.logfmt.setCurrentText(self.cfg.get("logcat_format", "threadtime"))
        mf.addRow("Logcat format", self.logfmt)
        self.docs = QCheckBox("Open docs on first run")
        self.docs.setChecked(self.cfg.get("open_docs_first_run", True))
        self.shortcut = QCheckBox("Create a desktop shortcut on first run")
        self.shortcut.setChecked(self.cfg.get("make_shortcut_first_run", True))
        mf.addRow(self.docs)
        mf.addRow(self.shortcut)
        lay.addWidget(misc)

        lay.addWidget(QLabel("Theme & font apply immediately. Open tabs keep their "
                             "font until reopened."))

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def reject(self):
        # revert the live preview if the user cancels
        QApplication.instance().setStyleSheet(theme_mod.stylesheet(self._orig_theme))
        super().reject()

    def result_settings(self) -> dict:
        return {
            "theme": self.theme.currentText(),
            "term_font": self.font.currentFont().family(),
            "term_font_size": self.font_size.value(),
            "adb_path": self.adb_path.text().strip(),
            "scrcpy_path": self.scrcpy_path.text().strip(),
            "scrcpy_max_size": self.max_size.value(),
            "scrcpy_bit_rate": self.bit_rate.text().strip(),
            "scrcpy_video_codec": ("" if self.codec.currentText() == "auto"
                                   else self.codec.currentText()),
            "scrcpy_stay_awake": self.stay.isChecked(),
            "scrcpy_turn_screen_off": self.tso.isChecked(),
            "logcat_format": self.logfmt.currentText(),
            "open_docs_first_run": self.docs.isChecked(),
            "make_shortcut_first_run": self.shortcut.isChecked(),
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
