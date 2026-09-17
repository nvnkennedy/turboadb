"""App manager: list packages, install (APK/splits), uninstall, clear, start, stop."""

from __future__ import annotations

from PyQt5.QtCore import pyqtSignal, QSignalBlocker, QSize, Qt
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QCheckBox,
    QLabel,
    QFrame,
    QFileDialog,
    QMessageBox,
    QToolButton,
)

from .icons import icon
from .qtutil import close_jobs, page_toolbar, run_job, unwrap


def _glyph(name, tone, size=16):
    """A non-interactive, theme-following icon (a flat #iconButton)."""
    b = QToolButton()
    b.setObjectName("iconButton")
    b.setIcon(icon(name, tone))
    b.setIconSize(QSize(size, size))
    b.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    b.setFocusPolicy(Qt.NoFocus)
    return b


def _unwrap_packages(res) -> list:
    res = unwrap(res)
    if not isinstance(res, list):
        raise RuntimeError("ADB did not return a package list")
    return res


class AppsPanel(QWidget):
    log = pyqtSignal(str)

    def __init__(self, handler, automotive=False, parent=None, *, adb_gate=None):
        """*adb_gate* (optional, from the device tab) has ``wrap(fn)``: device
        jobs then wait for one of the tab's adb slots before starting adb."""
        super().__init__(parent)
        self.handler = handler
        self._adb_gate = adb_gate
        self._jobs = []
        self._closed = False
        # Each refresh gets a generation; an older (slower) listing that
        # finishes after a newer one must not overwrite the newer result.
        self._list_generation = 0
        # One `pm list packages` at a time: refreshes requested while one runs
        # (Third-party toggled twice, Refresh clicked repeatedly) collapse into
        # a single follow-up listing instead of one adb process per click.
        self._listing = False
        self._refresh_pending = False
        self._setting_automotive_default = False
        self._third_user_changed = False
        self._all = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # One page toolbar: Install left, the package filter in the middle,
        # then the actions that apply to the selected package on the right.
        # It wraps instead of widening the window when the tab is narrow.
        toolbar, top = page_toolbar()
        self.btn_install = self._button("Install APK(s)…", self._install, "ok",
                                        "Install one APK, or several split APKs together",
                                        "download", "on-accent")
        self.filt = QLineEdit()
        self.filt.setPlaceholderText("Filter packages…")
        self.filt.setMinimumWidth(140)
        self.filt.addAction(icon("search"), QLineEdit.LeadingPosition)
        self.filt.textChanged.connect(self._apply_filter)
        # On automotive / IVI builds nearly EVERYTHING is a preinstalled system
        # app — a third-party-only default showed a near-empty, useless list
        # there, so show all apps on those devices.
        self.third = QCheckBox("Third-party only")
        self.third.setChecked(not automotive)
        self.third.setToolTip(
            "Untick to include system / preinstalled apps — "
            "on IVI head units almost every app is one."
        )
        self.third.toggled.connect(self._on_third_toggled)
        self.btn_refresh = self._button("Refresh", self.refresh, "ghost",
                                        "Reload the package list from the device",
                                        "refresh", "green")
        self.btn_start = self._button("Start", self._start, "ghost", "Launch the selected app",
                                      "play", "green")
        self.btn_stop = self._button("Stop", self._stop, "ghost", "Force-stop the selected app",
                                     "stop", "amber")
        self.btn_clear = self._button("Clear data", self._clear, "ghost",
                                      "Clear all data of the selected app", "eraser", "red")
        self.btn_uninstall = self._button("Uninstall", self._uninstall, "danger",
                                          "Uninstall the selected app", "trash", "on-danger")
        top.addWidget(self.btn_install)
        top.addSpacing(8)
        top.addWidget(self.filt, 1)
        top.addWidget(self.third)
        top.addWidget(self.btn_refresh)
        top.addSpacing(8)
        for b in (self.btn_start, self.btn_stop, self.btn_clear, self.btn_uninstall):
            top.addWidget(b)
        lay.addWidget(toolbar)

        # The package list sits in a card with a header row and a live count.
        body = QVBoxLayout()
        body.setContentsMargins(12, 12, 12, 12)
        card = QFrame()
        card.setObjectName("card")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)
        header = QWidget()
        header.setObjectName("cardHeader")
        header.setAttribute(Qt.WA_StyledBackground, True)
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(12, 8, 12, 8)
        header_lay.setSpacing(6)
        title = QLabel("Installed packages")
        title.setObjectName("cardTitle")
        self.count = QLabel("")
        self.count.setObjectName("mutedHint")
        header_lay.addWidget(_glyph("apps", "orange"))
        header_lay.addWidget(title)
        header_lay.addStretch(1)
        header_lay.addWidget(self.count)
        card_lay.addWidget(header)

        # Every package row shares one (theme-following) icon instance.
        self._pkg_icon = icon("apps", "orange")
        self.list = QListWidget()
        self.list.setContentsMargins(4, 4, 4, 4)
        self.list.setIconSize(QSize(18, 18))
        self.list.currentItemChanged.connect(lambda *_: self._sync_actions())
        list_box = QVBoxLayout()
        list_box.setContentsMargins(6, 6, 6, 6)
        list_box.addWidget(self.list)
        list_box.addWidget(self._build_empty_state(), 1)
        card_lay.addLayout(list_box, 1)
        body.addWidget(card)
        lay.addLayout(body, 1)

        self._listed = False  # a package listing has arrived at least once
        self._loaded = False  # load lazily on first view (keeps connect fast)
        self._sync_actions()

    def _build_empty_state(self):
        """Shown instead of the list when a listing (or the filter) has no rows."""
        self.empty = QWidget()
        v = QVBoxLayout(self.empty)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(6)
        v.addStretch(1)
        v.addWidget(_glyph("search", "orange", 40), 0, Qt.AlignHCenter)
        self.empty_title = QLabel("")
        self.empty_title.setObjectName("cardTitle")
        self.empty_title.setAlignment(Qt.AlignCenter)
        self.empty_hint = QLabel("")
        self.empty_hint.setObjectName("mutedHint")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        v.addWidget(self.empty_title)
        v.addWidget(self.empty_hint)
        v.addStretch(2)
        self.empty.setVisible(False)
        return self.empty

    def _show_list(self):
        self.empty.setVisible(False)
        self.list.setVisible(True)

    def _show_empty(self, title, hint):
        self.empty_title.setText(title)
        self.empty_hint.setText(hint)
        self.list.setVisible(False)
        self.empty.setVisible(True)

    def _status_row(self, text, name, tone):
        """A non-package row ("Loading…", errors); never selectable as a package."""
        self._show_list()
        self.list.addItem(QListWidgetItem(icon(name, tone), text))

    @staticmethod
    def _button(label, slot, role, tip, icon_name=None, tone=None):
        b = QPushButton(label)
        b.setProperty("role", role)
        b.setToolTip(tip)
        if icon_name:
            b.setIcon(icon(icon_name, tone))
        b.clicked.connect(lambda _=False: slot())
        return b

    def _sync_actions(self):
        """Package actions are only enabled while a real package is selected."""
        has_pkg = self._selected() is not None
        for b in (self.btn_start, self.btn_stop, self.btn_clear, self.btn_uninstall):
            b.setEnabled(has_pkg)

    def _update_count(self):
        shown, total = self.list.count(), len(self._all)
        if not total:
            self.count.setText("")
        elif shown == total:
            self.count.setText(f"{total} packages")
        else:
            self.count.setText(f"{shown} of {total} packages")

    def showEvent(self, event):
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def _device_job(self, fn):
        return self._adb_gate.wrap(fn) if self._adb_gate is not None else fn

    def refresh(self):
        if self._closed:
            return
        self.list.clear()
        self._status_row("Loading…", "refresh", "dim")
        self._sync_actions()
        self._list_generation += 1
        if self._listing:
            self._refresh_pending = True  # listed when the running one ends
            return
        self._start_listing()

    def _start_listing(self):
        self._listing = True
        self._refresh_pending = False
        third = self.third.isChecked()
        generation = self._list_generation
        handler = self.handler
        run_job(
            self._jobs,
            self._device_job(
                lambda: _unwrap_packages(handler.list_packages(third_party=third, safe=True))
            ),
            lambda pkgs, g=generation: self._listing_done(g, pkgs, None),
            lambda message, g=generation: self._listing_done(g, None, message),
        )

    def _listing_done(self, generation, pkgs, message):
        self._listing = False
        if self._closed:
            return
        if self._refresh_pending:
            self._start_listing()  # newer settings: this result is stale anyway
            return
        if message is None:
            self._on_packages(generation, pkgs)
        else:
            self._on_packages_failed(generation, message)

    def _on_packages(self, generation, pkgs):
        if self._closed or generation != self._list_generation:
            return
        self._all = pkgs
        self._listed = True
        self._apply_filter(self.filt.text())
        self.log.emit(f"[OK] {len(pkgs)} packages")

    def _on_packages_failed(self, generation, message):
        if self._closed or generation != self._list_generation:
            return
        self.list.clear()
        self._status_row("Could not list packages — see the log", "alert", "red")
        self._update_count()
        self._sync_actions()
        self.log.emit("[ERROR] packages: " + message)

    def _on_third_toggled(self, _checked):
        if not self._setting_automotive_default:
            self._third_user_changed = True
        if self._loaded:
            self.refresh()

    def set_automotive_default(self, automotive: bool) -> None:
        """Apply delayed device identity without overriding a user choice."""
        if self._third_user_changed:
            return
        wanted = not bool(automotive)
        if self.third.isChecked() == wanted:
            return
        self._setting_automotive_default = True
        try:
            blocker = QSignalBlocker(self.third)
            self.third.setChecked(wanted)
            del blocker
        finally:
            self._setting_automotive_default = False
        if self._loaded:
            self.refresh()

    def _apply_filter(self, text):
        raw = (text or "").strip()
        text = (text or "").lower()
        self.list.clear()
        for p in self._all:
            if not text or text in p.lower():
                self.list.addItem(QListWidgetItem(self._pkg_icon, p))
        self._update_count()
        self._sync_actions()
        if not self._listed or self.list.count():
            self._show_list()
            return
        system_tip = ("Untick Third-party only to include system apps."
                      if self.third.isChecked() else "")
        if self._all:
            self._show_empty(f"No packages match “{raw}”",
                             " ".join(filter(None, ("Try a shorter filter.", system_tip))))
        else:
            self._show_empty("No packages found", system_tip or "The device reported no packages.")

    def _selected(self):
        """The selected package name; None for no selection or a status row."""
        it = self.list.currentItem()
        if it is None or it.text() not in self._all:
            return None
        return it.text()

    def _install(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select APK(s)", filter="APK files (*.apk *.apks *.apkm);;All files (*)"
        )
        if not files:
            return
        handler = self.handler
        if len(files) > 1:

            def fn():
                return handler.install_multiple(files, grant_perms=True, safe=True)
        else:

            def fn():
                return handler.install(files[0], grant_perms=True, safe=True)

        self.log.emit(f"[INFO] Installing {len(files)} APK(s)…")
        # An install can push hundreds of MB for minutes: like a file transfer
        # it takes no adb slot, so listings, Reboot and Health never queue
        # behind it.
        self._do(fn, "install", refresh=True, gated=False)

    def _uninstall(self):
        pkg = self._selected()
        if not pkg:
            return
        if QMessageBox.question(self, "Uninstall", f"Uninstall {pkg}?") == QMessageBox.Yes:
            self._do(lambda h=self.handler: h.uninstall(pkg, safe=True), "uninstall", refresh=True)

    def _clear(self):
        pkg = self._selected()
        if (
            pkg
            and QMessageBox.question(self, "Clear data", f"Clear all data for {pkg}?")
            == QMessageBox.Yes
        ):
            self._do(lambda h=self.handler: h.clear_app(pkg, safe=True), "clear")

    def _start(self):
        pkg = self._selected()
        if pkg:
            self._do(lambda h=self.handler: h.start_app(pkg, safe=True), "start")

    def _stop(self):
        pkg = self._selected()
        if pkg:
            self._do(lambda h=self.handler: h.stop_app(pkg, safe=True), "stop")

    def _do(self, fn, label, refresh=False, *, gated=True):
        """Run a package action; *gated* False: it takes no adb slot."""
        if self._closed:
            return

        def done(message):
            self.log.emit(f"[OK] {label}: {message}")
            if refresh:
                self.refresh()

        def job():
            return str(unwrap(fn()))

        run_job(
            self._jobs,
            self._device_job(job) if gated else job,
            done,
            lambda message: self.log.emit(f"[ERROR] {label}: " + message),
        )

    def close_panel(self):
        """Detach running jobs (no waiting on the UI thread) and drop the list."""
        self._closed = True
        self._refresh_pending = False
        close_jobs(self._jobs)
        self._all = []
        self.list.clear()
