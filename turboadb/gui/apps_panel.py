"""App manager: list packages, install (APK/splits), uninstall, clear, start, stop."""

from __future__ import annotations

from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLineEdit, QListWidget, QCheckBox, QLabel,
                             QFileDialog, QMessageBox)


class _Job(QThread):
    done = pyqtSignal(str)
    failed = pyqtSignal(str)
    packages = pyqtSignal(list)

    def __init__(self, fn, emit_list=False):
        super().__init__()
        self.fn = fn
        self.emit_list = emit_list

    def run(self):
        try:
            res = self.fn()
            if self.emit_list:
                self.packages.emit(res)
            else:
                self.done.emit(str(res))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class AppsPanel(QWidget):
    log = pyqtSignal(str)

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self._jobs = []

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.filt = QLineEdit(); self.filt.setPlaceholderText("filter packages…")
        self.filt.textChanged.connect(self._apply_filter)
        self.third = QCheckBox("third-party only"); self.third.setChecked(True)
        self.third.toggled.connect(self.refresh)
        ref = QPushButton("Refresh"); ref.setProperty("role", "ghost")
        ref.clicked.connect(self.refresh)
        top.addWidget(QLabel("Apps:")); top.addWidget(self.filt, 1)
        top.addWidget(self.third); top.addWidget(ref)

        self.list = QListWidget()

        ops = QHBoxLayout()
        for label, slot, role in (("Install APK(s)…", self._install, "ok"),
                                  ("Uninstall", self._uninstall, "danger"),
                                  ("Clear data", self._clear, "ghost"),
                                  ("Start", self._start, "ghost"),
                                  ("Stop", self._stop, "ghost")):
            b = QPushButton(label)
            if role:
                b.setProperty("role", role)
            b.clicked.connect(slot)
            ops.addWidget(b)

        lay.addLayout(top); lay.addWidget(self.list, 1); lay.addLayout(ops)
        self._all = []
        self._loaded = False        # load lazily on first view (keeps connect fast)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def refresh(self):
        self.list.clear()
        self.list.addItem("loading…")
        third = self.third.isChecked()
        job = _Job(lambda: self.handler.list_packages(third_party=third, safe=False),
                   emit_list=True)
        job.packages.connect(self._on_packages)
        job.failed.connect(lambda m: self.log.emit("[ERROR] packages: " + m))
        job.finished.connect(lambda: self._jobs.remove(job) if job in self._jobs else None)
        self._jobs.append(job); job.start()

    def _on_packages(self, pkgs):
        self._all = pkgs
        self._apply_filter(self.filt.text())
        self.log.emit(f"[OK] {len(pkgs)} packages")

    def _apply_filter(self, text):
        text = (text or "").lower()
        self.list.clear()
        for p in self._all:
            if not text or text in p.lower():
                self.list.addItem(p)

    def _selected(self):
        it = self.list.currentItem()
        return it.text() if it else None

    def _install(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select APK(s)",
                                                filter="APK files (*.apk *.apks *.apkm);;All files (*)")
        if not files:
            return
        if len(files) > 1:
            fn = lambda: self.handler.install_multiple(files, grant_perms=True, safe=False)
        else:
            fn = lambda: self.handler.install(files[0], grant_perms=True, safe=False)
        self.log.emit(f"installing {len(files)} APK(s)…")
        self._do(fn, "install")

    def _uninstall(self):
        pkg = self._selected()
        if not pkg:
            return
        if QMessageBox.question(self, "Uninstall", f"Uninstall {pkg}?") == QMessageBox.Yes:
            self._do(lambda: self.handler.uninstall(pkg, safe=False), "uninstall",
                     refresh=True)

    def _clear(self):
        pkg = self._selected()
        if pkg and QMessageBox.question(self, "Clear data",
                                        f"Clear all data for {pkg}?") == QMessageBox.Yes:
            self._do(lambda: self.handler.clear_app(pkg, safe=False), "clear")

    def _start(self):
        pkg = self._selected()
        if pkg:
            self._do(lambda: self.handler.start_app(pkg, safe=False), "start")

    def _stop(self):
        pkg = self._selected()
        if pkg:
            self._do(lambda: self.handler.stop_app(pkg, safe=False), "stop")

    def _do(self, fn, label, refresh=False):
        job = _Job(fn)
        job.done.connect(lambda m: (self.log.emit(f"[OK] {label}: {m}"),
                                    self.refresh() if refresh else None))
        job.failed.connect(lambda m: self.log.emit(f"[ERROR] {label}: " + m))
        job.finished.connect(lambda: self._jobs.remove(job) if job in self._jobs else None)
        self._jobs.append(job); job.start()

    def close_panel(self):
        for j in list(self._jobs):
            j.wait(700)
