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
        # multi-select: Ctrl/Shift-click ranges, Ctrl+A selects all; Delete key
        # removes the selection (files AND folders)
        self.list.setSelectionMode(QListWidget.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._update_ops)
        self.list.itemChanged.connect(lambda *_: self._update_ops())  # checkbox
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        from PyQt5.QtWidgets import QShortcut
        from PyQt5.QtGui import QKeySequence
        QShortcut(QKeySequence.Delete, self.list, activated=self._delete)
        QShortcut(QKeySequence("F2"), self.list, activated=self._rename)

        ops = QHBoxLayout()
        self._ops_btns = {}
        for key, label, slot, role in (
                ("dl_file", "⬇ Download", self._dl_file, "ok"),
                ("upload", "⬆ Upload…", self._upload, None),
                ("mkdir", "New folder", self._mkdir, "ghost"),
                ("newfile", "New file", self._new_file, "ghost"),
                ("rename", "Rename", self._rename, "ghost"),
                ("selall", "Select all", lambda: self._check_all(True), "ghost"),
                ("delete", "🗑 Delete", self._delete, "danger")):
            b = QPushButton(label)
            if role:
                b.setProperty("role", role)
            b.clicked.connect(slot)
            ops.addWidget(b)
            self._ops_btns[key] = b

        self.bar = QProgressBar(); self.bar.setVisible(False)

        lay.addLayout(top); lay.addWidget(self.list, 1)
        lay.addLayout(ops); lay.addWidget(self.bar)
        # drag files/folders from the OS file manager straight onto the list to
        # upload them to the current device directory (additive — the buttons
        # still work exactly as before)
        self.setAcceptDrops(True)
        self.list.setAcceptDrops(True)
        self.list.setDragDropMode(QListWidget.DropOnly)
        self.list.viewport().installEventFilter(self)
        self._hint = QLabel("Tip: tick the checkboxes to select multiple · "
                            "right-click for all actions · Del deletes · "
                            "drag files here to upload.")
        self._hint.setStyleSheet("color:#8a93a0; font-size:9pt;")
        lay.addWidget(self._hint)
        self._update_ops()
        self.refresh()

    # ---- drag-and-drop upload ----
    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent
        if obj is self.list.viewport():
            if event.type() == QEvent.DragEnter or event.type() == QEvent.DragMove:
                if event.mimeData().hasUrls():
                    event.acceptProposedAction()
                    return True
            elif event.type() == QEvent.Drop:
                if event.mimeData().hasUrls():
                    self._drop_upload(event.mimeData().urls())
                    event.acceptProposedAction()
                    return True
        return super().eventFilter(obj, event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            self._drop_upload(event.mimeData().urls())
            event.acceptProposedAction()

    def _drop_upload(self, urls):
        locals_ = [u.toLocalFile() for u in urls if u.isLocalFile()]
        locals_ = [p for p in locals_ if p and os.path.exists(p)]
        if not locals_:
            return
        # upload sequentially so the single progress bar stays meaningful
        self._drop_queue = list(locals_)
        self.log.emit(f"uploading {len(locals_)} item(s) to {self.cwd}…")
        self._next_drop()

    def _next_drop(self):
        if not getattr(self, "_drop_queue", None):
            self.bar.setVisible(False)       # queue drained — finish up
            self.refresh()
            return
        local = self._drop_queue.pop(0)
        remote = posixpath.join(self.cwd, os.path.basename(local.rstrip("/\\")))
        self._run("push", local, remote, then=self._next_drop)

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
        # a checkbox on every real entry, so multi-select is obvious and works
        # with a single click (no Ctrl needed). '..' is made non-checkable
        # (QListWidgetItem is checkable by default, so strip the flag there).
        if name == "..":
            it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable)
        else:
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
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

    def _selected_many(self):
        """[(name, is_dir), …] to act on: CHECKED entries if any are ticked,
        otherwise the highlighted (selected) entries. '..' is never included."""
        checked = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.flags() & Qt.ItemIsUserCheckable and it.checkState() == Qt.Checked:
                name, is_dir = it.data(Qt.UserRole)
                if name and name != "..":
                    checked.append((name, is_dir))
        if checked:
            return checked
        out = []
        for it in self.list.selectedItems():
            name, is_dir = it.data(Qt.UserRole)
            if name and name != "..":
                out.append((name, is_dir))
        return out

    def _check_all(self, state):
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.flags() & Qt.ItemIsUserCheckable:
                it.setCheckState(Qt.Checked if state else Qt.Unchecked)

    def _context_menu(self, pos):
        from PyQt5.QtWidgets import QMenu
        from . import theme
        ico = theme.emoji_icon
        sel = self._selected_many()
        n = len(sel)
        m = QMenu(self)
        # open a folder if exactly one folder is under the cursor
        if n == 1 and sel[0][1]:
            m.addAction(ico("📂"), f"Open  “{sel[0][0]}”",
                        lambda: self._open_named(sel[0][0]))
            m.addSeparator()
        act_dl = m.addAction(ico("⬇"), "Copy to my PC…"
                             + (f"  ({n})" if n > 1 else ""))
        act_dl.setEnabled(n >= 1)
        act_dl.triggered.connect(self._dl_file)
        act_rn = m.addAction(ico("✏"), "Rename…")
        act_rn.setEnabled(n == 1)
        act_rn.triggered.connect(self._rename)
        m.addSeparator()
        act_del = m.addAction(ico("🗑", theme.DANGER),
                              "Delete" + (f"  ({n})" if n > 1 else ""))
        act_del.setEnabled(n >= 1)
        act_del.triggered.connect(self._delete)
        act_delall = m.addAction(ico("🧨", theme.DANGER),
                                 "Empty this folder (delete contents, keep folder)…")
        act_delall.triggered.connect(self._delete_all)
        m.addSeparator()
        m.addAction("☑ Select all", lambda: self._check_all(True))
        m.addAction("☐ Clear selection",
                    lambda: (self._check_all(False), self.list.clearSelection()))
        m.addSeparator()
        m.addAction(ico("⬆"), "Upload here…", self._upload)
        m.addAction(ico("📁"), "New folder…", self._mkdir)
        m.addAction(ico("📄"), "New file…", self._new_file)
        m.addAction("Refresh", self.refresh)
        m.exec_(self.list.viewport().mapToGlobal(pos))

    def _open_named(self, name):
        self.cwd = posixpath.join(self.cwd, name)
        self.refresh()

    def _delete_all(self):
        """Empty the current directory — delete everything INSIDE it, but keep
        the folder itself so you can immediately add files again."""
        if QMessageBox.question(
                self, "Empty this folder",
                f"Delete everything INSIDE\n{self.cwd}\n\n"
                f"(the folder itself is kept). This cannot be undone.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        base = _q(self.cwd.rstrip("/") or "/")
        # absolute-path globs for normal entries + dotfiles, deliberately using
        # .[!.]* and ..?* so '.' and '..' are NEVER matched (that would reach up
        # into the parent). No `cd`, no chance of removing the folder itself.
        cmd = (f"rm -rf {base}/* {base}/.[!.]* {base}/..?* 2>/dev/null; true")
        self._run_shell("empty folder", cmd)

    def _update_ops(self):
        """Enable/label the action buttons for the current selection."""
        sel = self._selected_many()
        n = len(sel)
        b = self._ops_btns
        b["dl_file"].setEnabled(n >= 1)
        b["dl_file"].setText("⬇ Download" + (f" ({n})" if n > 1 else ""))
        b["rename"].setEnabled(n == 1)          # rename is single-target
        b["delete"].setEnabled(n >= 1)
        b["delete"].setText("🗑 Delete" + (f" ({n})" if n > 1 else ""))

    # --- transfers ---
    def _run(self, direction, a, b, then=None):
        self.bar.setVisible(True); self.bar.setValue(0)
        t = _TransferThread(self.handler, direction, a, b)
        t.progress.connect(self.bar.setValue)
        t.done.connect(lambda m: (self.log.emit("[OK] " + m), self._after(then)))
        t.failed.connect(lambda m: (self.log.emit("[ERROR] " + m),
                                    self._after(then)))
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t)
        t.start()

    def _after(self, then=None):
        # only hide the bar + refresh when nothing else is queued (a multi-file
        # drop chains through *then*)
        if then is not None:
            then()
            return
        self.bar.setVisible(False)
        self.refresh()

    def _dl_file(self):
        """Download the selection. One file → Save As; one folder or several
        entries → pick a destination folder and pull each into it."""
        sel = self._selected_many()
        if not sel:
            return
        from .fileutil import download_path, download_dir
        if len(sel) == 1 and not sel[0][1]:
            name = sel[0][0]
            local, _ = QFileDialog.getSaveFileName(self, "Save file as",
                                                   download_path(name))
            if local:
                self._run("pull", posixpath.join(self.cwd, name), local)
            return
        dest = QFileDialog.getExistingDirectory(
            self, f"Download {len(sel)} item(s) into folder", download_dir())
        if not dest:
            return
        self._dl_queue = [posixpath.join(self.cwd, n) for n, _ in sel]
        self._dl_dest = dest
        self.log.emit(f"downloading {len(sel)} item(s) → {dest}…")
        self._next_dl()

    def _next_dl(self):
        if not getattr(self, "_dl_queue", None):
            self.bar.setVisible(False)
            self.refresh()
            return
        remote = self._dl_queue.pop(0)
        self._run("pull", remote, self._dl_dest, then=self._next_dl)

    def _upload(self):
        """Upload files and/or a folder — one chooser for files, plus a folder
        option, into the current directory."""
        box = QMessageBox(self)
        box.setWindowTitle("Upload")
        box.setText("Upload files or a whole folder to\n" + self.cwd + " ?")
        b_files = box.addButton("Choose files…", QMessageBox.AcceptRole)
        b_folder = box.addButton("Choose a folder…", QMessageBox.ActionRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is b_files:
            files, _ = QFileDialog.getOpenFileNames(self, "Upload files")
            locals_ = [f for f in files if f]
        elif clicked is b_folder:
            d = QFileDialog.getExistingDirectory(self, "Upload folder")
            locals_ = [d] if d else []
        else:
            return
        if locals_:
            self._drop_queue = list(locals_)
            self.log.emit(f"uploading {len(locals_)} item(s) to {self.cwd}…")
            self._next_drop()

    def _mkdir(self):
        name, ok = QInputDialog.getText(self, "New folder", "Name:")
        if ok and name.strip():
            self._run_shell(f"mkdir {name}",
                            f"mkdir -p {_q(posixpath.join(self.cwd, name.strip()))}")

    def _new_file(self):
        name, ok = QInputDialog.getText(self, "New file",
                                        "Name of the empty file to create:")
        if ok and name.strip():
            target = _q(posixpath.join(self.cwd, name.strip()))
            # create an empty file (touch, with a redirect fallback for shells
            # that lack touch)
            self._run_shell(f"create {name.strip()}",
                            f"touch {target} 2>/dev/null || : > {target}")

    def _rename(self):
        sel = self._selected_many()
        if len(sel) != 1:
            return
        name = sel[0][0]
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=name)
        if ok and new:
            src = posixpath.join(self.cwd, name)
            dst = posixpath.join(self.cwd, new)
            self._run_shell(f"rename {name}", f"mv {_q(src)} {_q(dst)}")

    def _delete(self):
        sel = self._selected_many()
        if not sel:
            return
        n = len(sel)
        folders = sum(1 for _, is_dir in sel if is_dir)
        if n == 1:
            what = ("folder '%s' and everything in it" % sel[0][0]) if folders \
                else "'%s'" % sel[0][0]
            msg = f"Delete {what}?"
        else:
            fbit = f" ({folders} folder(s), recursively)" if folders else ""
            msg = f"Delete these {n} items{fbit}?\n\n" + \
                  ", ".join(name for name, _ in sel[:12]) + \
                  (" …" if n > 12 else "")
        if QMessageBox.question(self, "Delete", msg,
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        targets = " ".join(_q(posixpath.join(self.cwd, name)) for name, _ in sel)
        self._run_shell(f"delete {n} item(s)", f"rm -rf {targets}")

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
