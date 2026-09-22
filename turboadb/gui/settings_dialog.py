"""Categorised application settings with live, reversible theme preview."""

from __future__ import annotations

from PyQt5.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QComboBox,
    QSpinBox,
    QFontComboBox,
    QCheckBox,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QFrame,
    QScrollArea,
    QWidget,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QApplication,
    QToolButton,
    QSizePolicy,
)
from PyQt5.QtCore import QPointF, QRectF, QSize, Qt
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

from . import settings as settings_mod
from . import theme as theme_mod
from .icons import icon


def _theme_swatch(c: dict, size: int = 30) -> QIcon:
    """A small preview of palette *c*: its window, card and accent colours.

    Chips always preview their own palette, so this is a fixed pixmap."""
    ratio = 2.0
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QColor(c["border"]))
    p.setBrush(QColor(c["win"]))
    p.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), 7, 7)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(c["panel"]))
    p.drawRoundedRect(QRectF(5, 5, size - 10, size * 0.42), 3, 3)
    p.setBrush(QColor(c["accent_text"]))  # the visible accent; "accent" is a deep fill in some themes
    p.drawEllipse(QPointF(size * 0.34, size * 0.72), size * 0.14, size * 0.14)
    p.setBrush(QColor(c["text"]))
    p.drawRoundedRect(QRectF(size * 0.55, size * 0.66, size * 0.3, size * 0.12), 1.5, 1.5)
    p.end()
    return QIcon(pm)


def _form(widget: QWidget) -> QFormLayout:
    """A settings form with one aligned label column and consistent spacing."""
    form = QFormLayout(widget)
    form.setContentsMargins(0, 0, 0, 0)
    form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    form.setHorizontalSpacing(12)
    form.setVerticalSpacing(8)
    return form


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setObjectName("settingsHint")
    return lab


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TurboADB — Settings")
        # 840 wide: theme chips show a swatch + the full description. The height
        # is set once the pages exist (see _open_height).
        self.resize(840, 560)
        self.cfg = settings_mod.load()
        # A retired theme name stands for the default theme of its kind.
        self._orig_theme = theme_mod.resolve_name(self.cfg.get("theme"))
        self._selected_theme = self._orig_theme

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(16, 16, 16, 12)
        body.setSpacing(16)
        outer.addLayout(body, 1)
        self.nav = QListWidget()
        self.nav.setFixedWidth(160)
        self.nav.setIconSize(QSize(18, 18))
        body.addWidget(self.nav)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)

        theme_glyph = "sun" if theme_mod.is_light(self._orig_theme) else "moon"
        self._add_page("Appearance", self._page_appearance(), "terminal", "teal")
        self._add_page("Tools", self._page_tools(), "folder", "blue")
        self._add_page("scrcpy", self._page_scrcpy(), "monitor", "purple")
        self._add_page("Logcat", self._page_logcat(), "logcat", "amber")
        self._add_page("Themes", self._page_themes(), theme_glyph, "pink")
        self._add_page("Startup", self._page_startup(), "power", "green")
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

        footer = QWidget()
        footer.setObjectName("dialogFooter")
        footer.setAttribute(Qt.WA_StyledBackground, True)
        footer_lay = QHBoxLayout(footer)
        footer_lay.setContentsMargins(16, 10, 16, 10)
        footer_lay.setSpacing(8)
        footer_lay.addStretch(1)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setProperty("role", "ok")
        btns.button(QDialogButtonBox.Ok).setIcon(icon("check", "on-accent"))
        btns.button(QDialogButtonBox.Cancel).setProperty("role", "ghost")
        btns.button(QDialogButtonBox.Cancel).setIcon(icon("x"))
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        footer_lay.addWidget(btns)
        outer.addWidget(footer)
        self.resize(840, self._open_height())
        # What every control showed when the dialog opened: OK persists only
        # the keys the user actually changed, never a stale full snapshot.
        self._initial_values = self._dialog_values()

    def _add_page(self, name, content, glyph, tone):
        self.nav.addItem(QListWidgetItem(icon(glyph, tone), name))
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(8)
        mark = QToolButton()
        mark.setObjectName("iconButton")
        mark.setIcon(icon(glyph, tone))
        mark.setIconSize(QSize(22, 22))
        mark.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        mark.setFocusPolicy(Qt.NoFocus)
        title = QLabel(name)
        title.setObjectName("settingsPageTitle")
        head.addWidget(mark)
        head.addWidget(title)
        head.addStretch(1)
        v.addLayout(head)
        # The page body scrolls under its fixed title: the tallest pages (scrcpy,
        # Themes) otherwise set the dialog's minimum height, which pushed OK and
        # Cancel off a ~680 px work area. The footer sits outside the pages.
        body = QWidget()
        body.setObjectName("settingsPageBody")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 6, 0)  # room beside a vertical scroll bar
        body_lay.setSpacing(0)
        body_lay.addWidget(content)
        body_lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("settingsPageScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)
        self.pages.addWidget(page)

    def _open_height(self) -> int:
        """Tall enough to show the tallest page without scrolling, but never
        taller than the screen's work area (minus the title bar): there the
        page scrolls and OK/Cancel stay visible."""
        chrome = self.minimumSizeHint() - self.pages.minimumSizeHint()
        # Word-wrapped hints grow taller at the page's real width.
        width = max(1, self.width() - chrome.width())
        tallest = 0
        for index in range(self.pages.count()):
            page = self.pages.widget(index)
            lay = page.layout()
            scroll = page.findChild(QScrollArea, "settingsPageScroll")
            body = scroll.widget()
            needed = max(body.heightForWidth(width), body.minimumSizeHint().height())
            min_width = body.minimumSizeHint().width()
            if min_width > width:
                # Too wide for the page (long option names, large fonts): the
                # scroll area lays it out at its own width and adds a sideways
                # scroll bar, which takes height. Linux fonts left the scrcpy
                # page exactly that bar short.
                laid_out = max(body.heightForWidth(min_width), body.minimumSizeHint().height())
                bar = scroll.horizontalScrollBar().sizeHint().height()
                needed = max(needed, laid_out + bar)
            tallest = max(tallest, lay.itemAt(0).sizeHint().height() + lay.spacing() + needed)
        height = max(560, chrome.height() + tallest)
        parent = self.parentWidget()
        handle = parent.window().windowHandle() if parent is not None else None
        screen = handle.screen() if handle is not None else QApplication.primaryScreen()
        if screen is not None:
            height = min(height, screen.availableGeometry().height() - 60)
        return max(height, self.minimumSizeHint().height())

    # ---- pages ----
    def _page_themes(self):
        """Keep the application palette intentionally simple and predictable."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        v.addWidget(_hint("Choose a palette. The preview applies immediately; Cancel restores the previous one."))
        # Dark palettes on the left, light palettes on the right, each titled.
        columns = QHBoxLayout()
        columns.setSpacing(16)
        column_layouts = {}
        for light in (False, True):
            column = QVBoxLayout()
            column.setSpacing(8)
            heading = QLabel("Light themes" if light else "Dark themes")
            heading.setObjectName("cardTitle")
            column.addWidget(heading)
            column_layouts[light] = column
            columns.addLayout(column, 1)
        self._theme_buttons = {}
        for name in theme_mod.theme_names():
            c = theme_mod.THEMES[name]
            button = QToolButton()
            button.setText(f"{theme_mod.theme_label(name)}\n{theme_mod.theme_description(name)}")
            button.setIcon(_theme_swatch(c, 26))
            button.setIconSize(QSize(26, 26))
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setCheckable(True)
            button.setChecked(name == self._selected_theme)
            button.setMinimumSize(230, 60)
            # Every card fills its column: sized to their own text, a short
            # description (Black) left a narrower card than its neighbours.
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setToolTip(f"{theme_mod.theme_label(name)}: {theme_mod.theme_description(name)}")
            # Each chip previews its own palette; only the selected one gets an
            # accent outline (rows are dark/light pairs). The outline is a line,
            # so it uses accent_text: the fill-only accent of the deep themes
            # (Black, Slate, Night) barely showed against their own panels.
            button.setStyleSheet(
                "QToolButton {"
                f"background:{c['panel']}; color:{c['text']}; border:2px solid {c['panel']}; "
                "border-radius:8px; text-align:left; padding:8px; font-weight:600; }"
                "QToolButton:hover {"
                f"background:{c['sel']}; border-color:{c['accent_text']}; }}"
                "QToolButton:checked {"
                f"border-color:{c['accent_text']}; }}"
            )
            button.clicked.connect(lambda _checked=False, key=name: self._select_theme(key))
            self._theme_buttons[name] = button
            column_layouts[theme_mod.is_light(name)].addWidget(button)
        for column in column_layouts.values():
            column.addStretch(1)
        v.addLayout(columns)
        v.addStretch(1)
        return w

    def _select_theme(self, name: str) -> None:
        self._selected_theme = name
        for key, button in self._theme_buttons.items():
            button.setChecked(key == name)
        app = QApplication.instance()
        if app is not None:
            theme_mod.apply_to_app(app, name)

    def _page_appearance(self):
        w = QWidget()
        f = _form(w)
        self.font_combo = QFontComboBox()
        # A picker, not a text field: an editable combo puts a caret in the box
        # and lets a half-typed font name stand as the setting.
        self.font_combo.setEditable(False)
        self.font_combo.setCurrentFont(QFont(self.cfg.get("term_font", "Consolas")))
        self.font_size = QSpinBox()
        # the same range Ctrl+wheel / A+ A− zoom uses
        self.font_size.setRange(settings_mod.FONT_SIZE_MIN, settings_mod.FONT_SIZE_MAX)
        default_size = settings_mod.DEFAULTS["term_font_size"]
        self.font_size.setValue(int(self.cfg.get("term_font_size", default_size)))
        f.addRow("Terminal font", self.font_combo)
        f.addRow("Terminal font size", self.font_size)
        f.addRow(
            "", _hint("Choose colours in Themes. Terminal font applies to newly opened terminal tabs.")
        )
        return w

    def _page_tools(self):
        w = QWidget()
        f = _form(w)
        self.adb_path = QLineEdit(self.cfg.get("adb_path", ""))
        self.scrcpy_path = QLineEdit(self.cfg.get("scrcpy_path", ""))
        self.ffmpeg_path = QLineEdit(self.cfg.get("ffmpeg_path", ""))
        self.adb_path.setPlaceholderText("blank = auto-detect / bundled")
        self.scrcpy_path.setPlaceholderText("blank = auto-detect / bundled")
        self.ffmpeg_path.setPlaceholderText("blank = download once / use PATH (Webcam tab)")
        f.addRow("adb path", _browse_row(self.adb_path, self))
        f.addRow("scrcpy path", _browse_row(self.scrcpy_path, self))
        f.addRow("ffmpeg path", _browse_row(self.ffmpeg_path, self))
        f.addRow(
            "",
            _hint(
                "Leave blank to let TurboADB find or download each tool. "
                "ffmpeg powers the Webcam tab."
            ),
        )
        return w

    def _page_scrcpy(self):
        w = QWidget()
        f = _form(w)
        self.screen_backend = QComboBox()
        self.screen_backend.addItem("scrcpy (fast, recommended)", "scrcpy")
        self.screen_backend.addItem(
            "ADB screencap (slower fallback for builds where scrcpy doesn't work)", "screencap"
        )
        # An unknown stored value shows the default rather than an empty combo.
        backend = self.cfg.get("screen_backend", settings_mod.SCREEN_BACKENDS[0])
        self.screen_backend.setCurrentIndex(max(0, self.screen_backend.findData(backend)))
        f.addRow("Screen renderer", self.screen_backend)
        f.addRow(
            "",
            _hint(
                "Screencap mode picks the display through dumpsys display, captures it with "
                "adb screencap and supports touch, but cannot record."
            ),
        )
        self.max_size = QSpinBox()
        self.max_size.setRange(0, 8192)
        self.max_size.setValue(int(self.cfg.get("scrcpy_max_size", 0)))
        self.max_size.setSpecialValueText("native")
        self.bit_rate = QComboBox()
        for label, value in (
            ("8M — low bandwidth / Remote Desktop", "8M"),
            ("16M — balanced (recommended)", "16M"),
            ("32M — high quality USB", "32M"),
            ("50M — maximum quality USB", "50M"),
        ):
            self.bit_rate.addItem(label, value)
        self._set_combo_value(self.bit_rate, self.cfg.get("scrcpy_bit_rate", "16M"))
        self.codec = QComboBox()
        self.codec.addItems(["auto", "h264", "h265", "av1"])
        self.codec.setCurrentText(self.cfg.get("scrcpy_video_codec") or "auto")
        self.stay = QCheckBox("Keep device awake while mirroring")
        self.stay.setChecked(self.cfg.get("scrcpy_stay_awake", True))
        self.tso = QCheckBox("Turn device screen off while mirroring")
        self.tso.setChecked(self.cfg.get("scrcpy_turn_screen_off", False))
        self.audio_enabled = QCheckBox("Forward device audio to this PC")
        self.audio_enabled.setChecked(self.cfg.get("scrcpy_audio", True))
        self.audio_source = QComboBox()
        self.audio_source.addItem("Whole device output (device audio mutes)", "output")
        self.audio_source.addItem("App playback (device keeps playing)", "playback")
        self.audio_source.addItem("Microphone", "mic")
        self.audio_source.addItem("Voice-call downlink", "voice-call-downlink")
        self.audio_source.addItem("Voice performance / karaoke", "voice-performance")
        self._set_combo_value(self.audio_source, self.cfg.get("scrcpy_audio_source", "output"))
        self.audio_codec = QComboBox()
        self.audio_codec.addItems(["opus", "aac", "flac", "raw"])
        self.audio_codec.setCurrentText(self.cfg.get("scrcpy_audio_codec", "opus"))
        self.audio_bit_rate = QComboBox()
        for value in ("64K", "128K", "192K", "256K", "320K"):
            self.audio_bit_rate.addItem(value, value)
        self._set_combo_value(self.audio_bit_rate, self.cfg.get("scrcpy_audio_bit_rate", "128K"))
        self.audio_buffer = QSpinBox()
        self.audio_buffer.setRange(20, 500)
        self.audio_buffer.setSuffix(" ms")
        self.audio_buffer.setValue(int(self.cfg.get("scrcpy_audio_buffer", 50)))
        self.audio_output_buffer = QSpinBox()
        self.audio_output_buffer.setRange(5, 200)
        self.audio_output_buffer.setSuffix(" ms")
        self.audio_output_buffer.setValue(int(self.cfg.get("scrcpy_audio_output_buffer", 10)))
        self.audio_dup = QCheckBox("Keep sound playing on the device")
        self.audio_dup.setChecked(self.cfg.get("scrcpy_audio_dup", False))
        f.addRow("Max size (px)", self.max_size)
        f.addRow("Video quality", self.bit_rate)
        f.addRow("Video codec", self.codec)
        f.addRow("", self.stay)
        f.addRow("", self.tso)
        f.addRow("", _hint("16M is the normal USB default; 8M is only for constrained links or Remote Desktop."))
        f.addRow("Audio", self.audio_enabled)
        f.addRow("Audio source", self.audio_source)
        f.addRow("Audio codec", self.audio_codec)
        f.addRow("Audio bit rate", self.audio_bit_rate)
        f.addRow("Audio latency", self.audio_buffer)
        f.addRow("PC playback buffer", self.audio_output_buffer)
        f.addRow("", self.audio_dup)
        f.addRow("", _hint("Audio needs Android 11+ and a compatible capture policy. Playback mode is required to keep sound on the device."))
        return w

    @staticmethod
    def _set_combo_value(combo: QComboBox, value) -> None:
        """Select the entry whose data is *value*.

        A value the list doesn't offer (an older setting, or one written into
        settings.json by hand) is added as its own entry, so it is still shown
        and saved unchanged now that these dropdowns are pickers only."""
        text = str(value or "")
        if not text:
            return
        index = combo.findData(text)
        if index < 0:
            combo.addItem(text, text)
            index = combo.findData(text)
        combo.setCurrentIndex(index)

    @staticmethod
    def _combo_value(combo: QComboBox) -> str:
        value = combo.currentData()
        return str(value if value is not None else combo.currentText()).strip()

    def _page_logcat(self):
        w = QWidget()
        f = _form(w)
        self.logfmt = QComboBox()
        self.logfmt.addItems(["threadtime", "brief", "time", "long", "tag"])
        self.logfmt.setCurrentText(self.cfg.get("logcat_format", "threadtime"))
        f.addRow("Logcat format", self.logfmt)
        f.addRow(
            "", _hint("'threadtime' shows the pid/tid + timestamp most logcat filters expect.")
        )
        return w

    def _page_startup(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        self.shortcut = QCheckBox(
            "Keep the Desktop + Start-menu shortcuts (recreated at every launch, self-healing)"
        )
        self.shortcut.setChecked(self.cfg.get("make_shortcut_first_run", True))
        self.autoupd = QCheckBox(
            "Check PyPI for a newer TurboADB at launch "
            "(notify in the log only — never auto-installs)"
        )
        self.autoupd.setChecked(self.cfg.get("auto_update", True))
        self.duplicate_device_action = QComboBox()
        self.duplicate_device_action.addItem("Ask every time", "ask")
        self.duplicate_device_action.addItem("Open an additional terminal session", "terminal")
        self.duplicate_device_action.addItem("Focus the existing device tab", "focus")
        current = self.cfg.get("duplicate_device_action", "ask")
        index = self.duplicate_device_action.findData(current)
        self.duplicate_device_action.setCurrentIndex(max(0, index))
        self.auto_save_targets = QCheckBox(
            "Add connected devices to Saved targets automatically"
        )
        self.auto_save_targets.setToolTip(
            "When a device tab connects, save it (USB by serial, network by host:port, "
            "remote by server and serial) unless a saved target already points at it."
        )
        self.auto_save_targets.setChecked(bool(self.cfg.get("auto_save_targets", True)))
        self.stop_adb_on_exit = QCheckBox(
            "Close ADB and scrcpy when TurboADB closes"
        )
        self.stop_adb_on_exit.setToolTip(
            "On exit, close any scrcpy window TurboADB opened and stop the adb server, so "
            "no adb daemon keeps running. A server shared with other machines "
            "(turboadb serve) is left alone."
        )
        self.stop_adb_on_exit.setChecked(bool(self.cfg.get("stop_adb_on_exit", True)))
        v.addWidget(self.shortcut)
        v.addWidget(self.autoupd)
        v.addWidget(self.auto_save_targets)
        v.addWidget(self.stop_adb_on_exit)
        v.addSpacing(8)
        v.addWidget(QLabel("When the same device is opened again"))
        v.addWidget(self.duplicate_device_action)
        v.addWidget(
            _hint(
                "An additional terminal session contains Android Shell, PowerShell and "
                "Command Prompt only; it does not duplicate files, apps or mirroring."
            )
        )
        return w

    def reject(self):
        # Revert the live theme preview through apply_to_app, not just the
        # stylesheet: the active theme name drives icons, status dots and every
        # status_colors() lookup, so restoring the sheet alone left the whole
        # app on the cancelled palette until the next restart.
        app = QApplication.instance()
        if app is not None:
            theme_mod.apply_to_app(app, self._orig_theme)
        super().reject()

    def changed_settings(self) -> dict:
        """Only the settings whose control differs from when the dialog opened."""
        initial = self._initial_values
        changes = {
            key: value
            for key, value in self._dialog_values().items()
            if initial.get(key) != value
        }
        if "theme" in changes:
            # Remember the chosen (and the replaced) theme per kind, as the
            # Themes menu does, so the dark/light toggle returns to them.
            changes.update(theme_mod.theme_choice(self._selected_theme, self._orig_theme))
        return changes

    def accept(self):
        """Persist just the changed keys, merged into the CURRENT settings file
        under the settings lock — a zoom level or recent host written while the
        dialog was open is no longer overwritten by the dialog's old snapshot."""
        changes = self.changed_settings()
        if changes:
            try:
                settings_mod.update(changes)
            except (OSError, ValueError) as exc:
                from PyQt5.QtWidgets import QMessageBox

                QMessageBox.warning(self, "Settings", f"Could not save settings:\n{exc}")
                return
        super().accept()

    def done(self, result):
        super().done(result)
        if self.parent() is not None:
            # exec_() callers read result_settings() synchronously right after
            # exec_ returns; the deferred delete runs later in the outer loop.
            self.deleteLater()

    def result_settings(self) -> dict:
        """The full settings as they are now: the current file plus this
        dialog's changes (keys it doesn't show — recent hosts, ribbon density,
        remembered logins — are preserved, and unchanged controls never
        overwrite newer values)."""
        out = settings_mod.load()
        out.update(self.changed_settings())
        return out

    def _dialog_values(self) -> dict:
        return dict(
            {
                "theme": self._selected_theme,
                "term_font": self.font_combo.currentFont().family(),
                "term_font_size": self.font_size.value(),
                "adb_path": self.adb_path.text().strip(),
                "scrcpy_path": self.scrcpy_path.text().strip(),
                "ffmpeg_path": self.ffmpeg_path.text().strip(),
                "screen_backend": self.screen_backend.currentData(),
                "scrcpy_max_size": self.max_size.value(),
                "scrcpy_bit_rate": self._combo_value(self.bit_rate),
                "scrcpy_video_codec": (
                    "" if self.codec.currentText() == "auto" else self.codec.currentText()
                ),
                "scrcpy_stay_awake": self.stay.isChecked(),
                "scrcpy_turn_screen_off": self.tso.isChecked(),
                "scrcpy_audio": self.audio_enabled.isChecked(),
                "scrcpy_audio_source": self._combo_value(self.audio_source),
                "scrcpy_audio_codec": self.audio_codec.currentText(),
                "scrcpy_audio_bit_rate": self._combo_value(self.audio_bit_rate),
                "scrcpy_audio_buffer": self.audio_buffer.value(),
                "scrcpy_audio_output_buffer": self.audio_output_buffer.value(),
                "scrcpy_audio_dup": self.audio_dup.isChecked(),
                "logcat_format": self.logfmt.currentText(),
                "make_shortcut_first_run": self.shortcut.isChecked(),
                "auto_update": self.autoupd.isChecked(),
                "duplicate_device_action": self.duplicate_device_action.currentData(),
                "auto_save_targets": self.auto_save_targets.isChecked(),
                "stop_adb_on_exit": self.stop_adb_on_exit.isChecked(),
            }
        )


def _browse_row(line_edit: QLineEdit, parent) -> QWidget:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    btn = QPushButton("Browse…")
    btn.setProperty("role", "ghost")
    btn.setIcon(icon("folder", "blue"))

    def pick():
        path, _ = QFileDialog.getOpenFileName(parent, "Select executable")
        if path:
            line_edit.setText(path)

    btn.clicked.connect(pick)
    row.addWidget(line_edit, 1)
    row.addWidget(btn)
    w = QWidget()
    w.setLayout(row)
    return w
