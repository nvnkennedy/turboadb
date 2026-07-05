"""On-device file browser using ``adb shell ls`` + push/pull. Browse dialogs
cover files AND folders for both upload and download. Transfers run on their own
thread with a live progress bar, so the UI stays responsive."""

from __future__ import annotations

import os
import posixpath

from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLineEdit, QListWidget, QListWidgetItem, QFileDialog,
                             QInputDialog, QMessageBox, QLabel, QProgressBar)


class _TransferThread(QThread):
    progress = pyqtSignal(int)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, handler, direction, a, b):
        super().__init__()
        self.handler, self.direction, self.a, self.b = handler, direction, a, b

    def run(self):
        try:
            if self.direction == "push":
                res = self.handler.push(self.a, self.b,
                                        on_progress=self.progress.emit, safe=False)
            else:
                res = self.handler.pull(self.a, self.b,
                                        on_progress=self.progress.emit, safe=False)
            self.done.emit(str(res))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _ShellThread(QThread):
    """One device shell command off the UI thread — listing / mkdir / rename /
    delete used to run synchronously and froze the whole window for the length
    of the adb round-trip (up to the 15-25 s timeout over a remote server)."""
    ok = pyqtSignal(object)                 # CommandResult
    fail = pyqtSignal(str)

    def __init__(self, handler, cmd):
        super().__init__()
        self.handler, self.cmd = handler, cmd

    def run(self):
        try:
            self.ok.emit(self.handler.shell(self.cmd, safe=False))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class FileBrowser(QWidget):
    log = pyqtSignal(str)

    def __init__(self, handler, start="/sdcard", parent=None):
        super().__init__(parent)
        self.handler = handler
        self.cwd = start
        self._threads = []

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.path = QLineEdit(self.cwd); self.path.returnPressed.connect(self._go)
        up = QPushButton("Up"); up.setProperty("role", "ghost"); up.clicked.connect(self._up)
        ref = QPushButton("Refresh"); ref.setProperty("role", "ghost"); ref.clicked.connect(self.refresh)
        top.addWidget(QLabel("Device:")); top.addWidget(self.path, 1)
        top.addWidget(up); top.addWidget(ref)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._open_item)

        ops = QHBoxLayout()
        for label, slot, role in (("Download file", self._dl_file, "ok"),
                                  ("Download folder", self._dl_folder, "ghost"),
                                  ("Upload file", self._up_file, None),
                                  ("Upload folder", self._up_folder, "ghost"),
                                  ("Mkdir", self._mkdir, "ghost"),
                                  ("Rename", self._rename, "ghost"),
                                  ("Delete", self._delete, "danger")):
            b = QPushButton(label)
            if role:
                b.setProperty("role", role)
            b.clicked.connect(slot)
            ops.addWidget(b)

        self.bar = QProgressBar(); self.bar.setVisible(False)

        lay.addLayout(top); lay.addWidget(self.list, 1)
        lay.addLayout(ops); lay.addWidget(self.bar)
        self.refresh()

    # --- navigation ---
    def refresh(self):
        self.path.setText(self.cwd)
        self.list.clear()
        self.list.addItem(self._mkitem("..", True))
        loading = QListWidgetItem("  loading…")
        loading.setFlags(Qt.NoItemFlags)
        self.list.addItem(loading)
        t = _ShellThread(self.handler, f"ls -1 -p {_q(self.cwd)}")
        self._ls = t                        # only the LATEST listing may land
        t.ok.connect(lambda res, t=t: self._on_ls(t, res))
        t.fail.connect(lambda m, t=t: self._on_ls_fail(t, m))
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t)
        t.start()

    def _on_ls(self, t, res):
        if t is not getattr(self, "_ls", None):
            return                          # superseded by a newer navigation
        self.list.clear()
        self.list.addItem(self._mkitem("..", True))
        if not res.ok:
            self.log.emit(f"[ERROR] ls {self.cwd}: {res.stderr.strip() or res.text}")
            return
        names = [ln for ln in res.stdout.splitlines() if ln.strip()]
        for name in sorted(names, key=lambda n: (not n.endswith("/"), n.lower())):
            is_dir = name.endswith("/")
            self.list.addItem(self._mkitem(name.rstrip("/"), is_dir))

    def _on_ls_fail(self, t, msg):
        if t is not getattr(self, "_ls", None):
            return
        self.list.clear()
        self.list.addItem(self._mkitem("..", True))
        self.log.emit(f"[ERROR] ls {self.cwd}: {msg}")

    def _mkitem(self, name, is_dir):
        it = QListWidgetItem(("📁 " if is_dir else "📄 ") + name)
        it.setData(Qt.UserRole, (name, is_dir))
        return it

    def _go(self):
        self.cwd = self.path.text().strip() or "/"
        self.refresh()

    def _up(self):
        self.cwd = posixpath.dirname(self.cwd.rstrip("/")) or "/"
        self.refresh()

    def _open_item(self, item):
        name, is_dir = item.data(Qt.UserRole)
        if name == "..":
            self._up(); return
        if is_dir:
            self.cwd = posixpath.join(self.cwd, name)
            self.refresh()

    def _selected(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else (None, None)

    # --- transfers ---
    def _run(self, direction, a, b):
        self.bar.setVisible(True); self.bar.setValue(0)
        t = _TransferThread(self.handler, direction, a, b)
        t.progress.connect(self.bar.setValue)
        t.done.connect(lambda m: (self.log.emit("[OK] " + m), self._after()))
        t.failed.connect(lambda m: (self.log.emit("[ERROR] " + m), self._after()))
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t)
        t.start()

    def _after(self):
        self.bar.setVisible(False)
        self.refresh()

    def _dl_file(self):
        name, is_dir = self._selected()
        if not name or name == "..":
            return
        if is_dir:
            QMessageBox.information(self, "Download", "That's a folder — use Download folder.")
            return
        local, _ = QFileDialog.getSaveFileName(self, "Save file as", name)
        if local:
            self._run("pull", posixpath.join(self.cwd, name), local)

    def _dl_folder(self):
        name, is_dir = self._selected()
        remote = posixpath.join(self.cwd, name) if (name and name != "..") else self.cwd
        dest = QFileDialog.getExistingDirectory(self, "Download into folder")
        if dest:
            self._run("pull", remote, dest)

    def _up_file(self):
        local, _ = QFileDialog.getOpenFileName(self, "Upload file")
        if local:
            remote = posixpath.join(self.cwd, os.path.basename(local))
            self._run("push", local, remote)

    def _up_folder(self):
        local = QFileDialog.getExistingDirectory(self, "Upload folder")
        if local:
            remote = posixpath.join(self.cwd, os.path.basename(local.rstrip("/\\")))
            self._run("push", local, remote)

    def _mkdir(self):
        name, ok = QInputDialog.getText(self, "New folder", "Name:")
        if ok and name:
            self._run_shell(f"mkdir {name}",
                            f"mkdir -p {_q(posixpath.join(self.cwd, name))}")

    def _rename(self):
        name, _ = self._selected()
        if not name or name == "..":
            return
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=name)
        if ok and new:
            src = posixpath.join(self.cwd, name)
            dst = posixpath.join(self.cwd, new)
            self._run_shell(f"rename {name}", f"mv {_q(src)} {_q(dst)}")

    def _delete(self):
        name, is_dir = self._selected()
        if not name or name == "..":
            return
        if QMessageBox.question(self, "Delete", f"Delete {name}?") != QMessageBox.Yes:
            return
        target = posixpath.join(self.cwd, name)
        self._run_shell(f"delete {name}", f"rm -rf {_q(target)}")

    def _run_shell(self, label, cmd):
        """Run a one-shot device shell command on a worker thread, log the
        outcome, then refresh the listing."""
        t = _ShellThread(self.handler, cmd)
        t.ok.connect(lambda res: (self.log.emit(
            f"[OK] {label}" if res.ok
            else f"[ERROR] {label}: " + (res.stderr.strip() or res.text)),
            self.refresh()))
        t.fail.connect(lambda m: (self.log.emit(f"[ERROR] {label}: {m}"),
                                  self.refresh()))
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t)
        t.start()

    def close_panel(self):
        from .qtutil import park_thread
        for t in list(self._threads):
            t.wait(700)
            park_thread(t)          # still running (slow remote) → keep it alive


def _q(path: str) -> str:
    """Single-quote a remote path for the device shell."""
    return "'" + path.replace("'", "'\\''") + "'"
