"""A per-device tab: connects in the background, then exposes an interactive
Shell, a live Logcat viewer, a file browser, and an app manager — plus quick
Mirror (scrcpy), Screenshot, and Reboot actions in a header bar."""

from __future__ import annotations

import shlex

from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QTabWidget, QFileDialog, QMenu, QToolButton,
                             QMessageBox)

from ..config import ADBConfig
from ..core import ADBHandler
from ..results import OperationResult
from . import settings as settings_mod
from . import theme
from .terminal import ReaderThread
from .console import AnsiConsole
from .logcat_view import LogcatPanel
from .file_browser import FileBrowser
from .apps_panel import AppsPanel
from .controls_panel import ControlsPanel
from .phone_panel import PhonePanel
from .mirror_panel import MirrorPanel


def config_from_session(s: dict) -> ADBConfig:
    st = settings_mod.load()
    adb_path = st.get("adb_path") or None
    scrcpy_path = st.get("scrcpy_path") or None
    if s.get("type") == "network":
        return ADBConfig(host=s.get("host", ""), port=int(s.get("port", 5555)),
                         adb_path=adb_path, scrcpy_path=scrcpy_path)
    if s.get("type") == "remote":
        return ADBConfig(serial=s.get("serial") or None,
                         adb_server_host=s.get("adb_host", ""),
                         adb_server_port=int(s.get("adb_port", 5037)),
                         adb_path=adb_path, scrcpy_path=scrcpy_path)
    return ADBConfig(serial=s.get("serial") or None,
                     adb_path=adb_path, scrcpy_path=scrcpy_path)


class _ConnectThread(QThread):
    ok = pyqtSignal(object)
    fail = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        try:
            # wire the handler's rich logging (real adb commands, durations,
            # exit codes, full error text) straight into the GUI log from the
            # very first connect step
            h = ADBHandler(self.cfg, safe=True,
                           log_callback=lambda m: self.log.emit(m))
            res = h.connect()
            if isinstance(res, OperationResult) and not res.success:
                self.fail.emit(str(res.error)); return
            self.ok.emit(h)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _ActionThread(QThread):
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


class _ReconnectThread(QThread):
    """Wait for the device to come back to the 'device' state after a reboot."""
    done = pyqtSignal(bool)

    def __init__(self, handler, timeout=180):
        super().__init__()
        self.handler = handler
        self.timeout = timeout

    def run(self):
        import time
        time.sleep(3)                       # let it actually go down first
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                if self.handler.get_state() == "device":
                    time.sleep(1.0)         # settle
                    self.done.emit(True)
                    return
            except Exception:
                pass
            time.sleep(2.0)
        self.done.emit(False)


class _PromptThread(QThread):
    """Fetch the real device name + root state to build an authentic shell
    prompt (device:/ $ or device:/ #), without blocking the UI."""
    ready = pyqtSignal(str, bool)

    def __init__(self, handler, device_name):
        super().__init__()
        self.handler = handler
        self.device_name = device_name

    def run(self):
        host = self.device_name
        root = False
        try:
            r = self.handler.shell("getprop ro.product.device", safe=False)
            if r.ok and r.text.strip():
                host = r.text.strip()
        except Exception:
            pass
        try:
            r = self.handler.shell("id -u", safe=False)
            root = (r.ok and r.text.strip() == "0")
        except Exception:
            pass
        self.ready.emit(host or "android", root)


class ShellPanel(QWidget):
    """A native interactive ``adb shell``: type straight into the terminal —
    prompt, echo and line editing work, with real text selection + copy/paste."""

    log = pyqtSignal(str)
    disconnected = pyqtSignal()             # the shell died (reboot / unplug)

    def __init__(self, handler, device_name="", parent=None):
        super().__init__(parent)
        self.handler = handler
        self.device_name = device_name or (handler.serial or "android")
        self.session = None
        self.reader = None
        self._pt = None
        self._closing = False
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)

        # a slim action row — input goes INTO the terminal
        row = QHBoxLayout()
        stop = QPushButton(" Stop"); stop.setProperty("role", "danger")
        stop.setIcon(theme.emoji_icon("⏹"))
        stop.setToolTip("Stop a running command (e.g. logcat). Works even over RDP "
                        "/ a remote adb server, where Ctrl+C can't reach the device.")
        stop.clicked.connect(self.interrupt)
        clr = QPushButton(" Clear"); clr.setProperty("role", "ghost")
        clr.setIcon(theme.emoji_icon("🧹"))
        paste = QPushButton(" Paste"); paste.setProperty("role", "ghost")
        paste.setIcon(theme.emoji_icon("📥"))
        paste.clicked.connect(lambda: self.term.paste_clipboard())
        copy = QPushButton(" Copy"); copy.setProperty("role", "ghost")
        copy.setIcon(theme.emoji_icon("📋"))
        copy.clicked.connect(lambda: self.term.copy())
        save = QPushButton(" Save…"); save.setProperty("role", "ghost")
        save.setIcon(theme.emoji_icon("💾"))
        save.clicked.connect(lambda: self.term._save_output())
        row.addWidget(stop); row.addWidget(copy); row.addWidget(paste)
        row.addWidget(save); row.addWidget(clr); row.addStretch(1)
        row.addWidget(QLabel("type here • drag to select • Stop halts logcat etc. "
                             "• right-click for Copy/Paste/Save"))
        lay.addLayout(row)

        self.term = AnsiConsole(send_fn=self._send)
        self.term.set_completion_fn(self._complete)   # Tab path-completion
        self.term.set_interrupt_fn(self.interrupt)    # reliable Ctrl+C / Stop
        clr.clicked.connect(self.term.clear)
        lay.addWidget(self.term, 1)
        self._open()

    _BIN_DIRS = ("/system/bin", "/system/xbin", "/vendor/bin",
                 "/apex/com.android.runtime/bin", "/apex/com.android.art/bin")

    def _complete(self, line):
        """Tab-complete the last token: a COMMAND (search PATH bins) when it's
        the first word, else a device PATH (ls-based)."""
        import re
        import os
        m = re.search(r"(\S*)$", line)
        token = m.group(1)
        if not token or not self.handler:
            return None, []
        dq = lambda s: "'" + s.replace("'", "'\\''") + "'"
        head = line[:len(line) - len(token)]
        first_word = " " not in line.strip()

        try:
            if first_word:                        # complete a command name
                globs = " ".join(f"{d}/{dq(token)}*" for d in self._BIN_DIRS)
                res = self.handler.shell(f"ls -d {globs} 2>/dev/null", safe=False)
                names = sorted({os.path.basename(p.rstrip("\r"))
                                for p in res.text.split() if p.strip()})
                if not names:
                    return None, []
                if len(names) == 1:
                    return head + names[0] + " ", []
                prefix = os.path.commonprefix(names)
                return (head + prefix if len(prefix) > len(token) else None), names
            # complete a path, relative to the shell's cwd
            cwd = getattr(self.term, "_cwd", "/")
            res = self.handler.shell(
                f"cd {dq(cwd)} 2>/dev/null; ls -dp {dq(token)}* 2>/dev/null",
                safe=False)
            entries = [e.rstrip("\r") for e in res.text.split("\n") if e.strip()]
            if not entries:
                return None, []
            if len(entries) == 1:
                return head + entries[0], []
            prefix = os.path.commonprefix(entries)
            if len(prefix) > len(token):
                return head + prefix, entries
            return None, entries
        except Exception:
            return None, []

    def _open(self):
        # cooked mode: no PTY (reliable input on Windows); we echo locally and
        # send a whole line per Enter — so one Enter runs the command.
        res = self.handler.open_shell(tty=False)
        self.session = res.value if isinstance(res, OperationResult) else res
        if self.session is None:
            self.term.feed(b"\n[could not open adb shell]\n")
            return

        sess = self.session              # bind THIS reader to THIS session

        def read_fn():
            if not sess.running:
                data = sess.read(65536)
                return data or None
            return sess.read(65536)

        self.reader = ReaderThread(read_fn, decode=False)   # AnsiConsole eats bytes
        rd = self.reader
        # route through a guard so leftover lines from a shell we just tore down
        # (after Stop) are dropped instead of trickling into the fresh shell
        self.reader.data.connect(lambda d: self._feed_from(rd, d))
        self.reader.closed.connect(self._on_reader_closed)
        self.reader.start()
        self.term.setFocus()

        # Show a prompt with the name we ALREADY know, immediately — the exact
        # device name + root state are refined below, but that adb round-trip is
        # slow over RDP, so we never leave a blank "$" waiting for it. (When
        # reopening after a disconnect, set_alive() draws the prompt instead.)
        self.term.set_prompt(self.device_name, root=False)
        if self.term._alive:
            self.term.show_prompt()
        self._pt = _PromptThread(self.handler, self.device_name)
        self._pt.ready.connect(self._on_prompt)
        self._pt.start()

    def _feed_from(self, reader, data):
        # only the CURRENT reader may write to the terminal — stragglers from a
        # torn-down shell (after Stop) are silently dropped
        if reader is self.reader:
            self.term.feed(data)

    def _on_reader_closed(self):
        # the adb shell pipe closed (device rebooted or was unplugged)
        if self._closing:
            return
        self.term.set_alive(False)
        self.disconnected.emit()

    def reconnect(self):
        """Reopen the shell after the device comes back (e.g. post-reboot)."""
        if self.reader:
            try:                            # don't let the old reader's close
                self.reader.closed.disconnect(self._on_reader_closed)
            except Exception:
                pass
            self.reader.stop(); self.reader.wait(800)
        if self.session:
            self.session.close()
        self.session = None
        self.term.feed(b"\n")
        self._open()
        self.term.set_alive(True)

    def _on_prompt(self, host, is_root):
        # refine the prompt (exact device name + root #/$) for subsequent prompts;
        # the immediate one shown in _open already has the name, so no duplicate
        self.term.set_prompt(host, is_root)

    def _send(self, data: bytes):
        if self.session and self.session.running:
            self.session.send(data)

    def interrupt(self):
        """Reliably stop a runaway command (e.g. `logcat`) even with no PTY and
        over a remote adb server: tear the shell down — which kills the device-side
        shell and its children — then reopen a fresh one, preserving the directory.
        This is what makes Ctrl+C / the Stop button actually work over RDP."""
        if self._closing or not self.handler:
            return
        cwd = getattr(self.term, "_cwd", "/")
        # stop rendering whatever flood is already queued on screen
        try:
            self.term._inq.clear(); self.term._inq_len = 0; self.term._drain.stop()
        except Exception:
            pass
        # Drop the current shell -> the device-side process group (logcat) dies.
        # Order matters: detach the close handler FIRST (so the resulting EOF isn't
        # treated as a device disconnect), then close the session to UNBLOCK the
        # reader's blocking read, then join it — otherwise the join waits 800ms.
        if self.reader:
            try:
                self.reader.closed.disconnect(self._on_reader_closed)
            except Exception:
                pass
        if self.session:
            try:
                self.session.close()       # terminates adb.exe -> read unblocks
            except Exception:
                pass
        if self.reader:
            self.reader.stop(); self.reader.wait(800); self.reader = None
        self.session = None
        self.term._echo("\n^C  — stopped\n", "#ff7a6e")
        self.term._last_feed = 0.0           # shell is idle again after the stop
        self.term._cwd = cwd                 # new prompt shows the right path
        self._open()                         # reopen + show a fresh prompt
        self.term.set_alive(True)
        if cwd and cwd not in ("", "/") and self.session:
            try:                             # put the new shell back in that dir
                self.session.send(("cd " + shlex.quote(cwd) + "\n").encode("utf-8"))
            except Exception:
                pass

    def close_panel(self):
        self._closing = True
        if self._pt:
            self._pt.wait(700)
        if self.reader:
            self.reader.stop(); self.reader.wait(700)
        if self.session:
            self.session.close()
        try:
            self.term.close_archive()           # drop the temp scrollback file
        except Exception:
            pass


class DeviceTab(QWidget):
    log = pyqtSignal(str)
    title_changed = pyqtSignal(str)          # device name for the tab header

    def __init__(self, session: dict, parent=None):
        super().__init__(parent)
        self.session = session
        self.handler = None
        self._threads = []
        self._scrcpy = []
        self._automotive = False
        self._reconnecting = False

        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.status = QLabel("Connecting…")

        self.btn_mirror = QToolButton()
        self.btn_mirror.setText(" Mirror"); self.btn_mirror.setIcon(theme.emoji_icon("📱"))
        self.btn_mirror.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_mirror.setProperty("role", "ok")
        self.btn_mirror.setPopupMode(QToolButton.InstantPopup)   # whole btn = menu
        mmenu = QMenu(self.btn_mirror)
        mmenu.addAction("Mirror (separate window)", lambda: self.mirror())
        mmenu.addAction("Embed in this tab (experimental)",
                        lambda: self.mirror(embed=True))
        mmenu.addAction("Mirror a specific display…", self.mirror_choose_display)
        mmenu.addAction("Mirror (compatibility mode — for IVI/automotive)",
                        lambda: self.mirror(compat=True))
        self.btn_mirror.setMenu(mmenu)

        self.btn_shot = QToolButton()
        self.btn_shot.setText(" Screenshot"); self.btn_shot.setIcon(theme.emoji_icon("📸"))
        self.btn_shot.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_shot.setProperty("role", "ghost")
        self.btn_shot.clicked.connect(self.screenshot)

        # Root / Mount: the common adb maintenance operations as one-click items
        self.btn_adv = QToolButton()
        self.btn_adv.setText(" Root / Mount"); self.btn_adv.setIcon(theme.emoji_icon("🔧"))
        self.btn_adv.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_adv.setProperty("role", "ghost")
        self.btn_adv.setPopupMode(QToolButton.InstantPopup)
        amenu = QMenu(self.btn_adv)
        amenu.addAction("adb root", lambda: self._op("root", lambda h: h.root(safe=False)))
        amenu.addAction("adb unroot", lambda: self._op("unroot", lambda h: h.unroot(safe=False)))
        amenu.addSeparator()
        amenu.addAction("adb remount (rw)", lambda: self._op("remount", lambda h: h.remount(safe=False)))
        amenu.addAction("mount -o remount,rw /", lambda: self._op("mount rw", lambda h: h.mount_rw(safe=False)))
        amenu.addSeparator()
        amenu.addAction("adb disable-verity  (sync + reboot)", lambda: self._verity(False))
        amenu.addAction("adb enable-verity  (sync + reboot)", lambda: self._verity(True))
        self.btn_adv.setMenu(amenu)

        self.btn_reboot = QToolButton()
        self.btn_reboot.setText(" Reboot"); self.btn_reboot.setIcon(theme.emoji_icon("🔁"))
        self.btn_reboot.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_reboot.setProperty("role", "ghost")
        self.btn_reboot.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.btn_reboot)
        for label, mode in (("System", None), ("Recovery", "recovery"),
                            ("Bootloader", "bootloader"), ("Sideload", "sideload")):
            menu.addAction(label, lambda m=mode: self.reboot(m))
        self.btn_reboot.setMenu(menu)

        for w in (self.status, self.btn_mirror, self.btn_shot, self.btn_adv,
                  self.btn_reboot):
            bar.addWidget(w)
        bar.setStretch(0, 1)
        lay.addLayout(bar)

        self.inner = QTabWidget()
        # show full tab labels (Qt elides them by default, which truncated the
        # Shell/Logcat/… text); scroll instead of cram when there are many
        self.inner.setElideMode(Qt.ElideNone)
        self.inner.setUsesScrollButtons(True)
        self.inner.tabBar().setExpanding(False)
        lay.addWidget(self.inner, 1)
        self._enable_actions(False)

        cfg = config_from_session(session)
        self._ct = _ConnectThread(cfg)
        self._ct.ok.connect(self._on_connected)
        self._ct.fail.connect(self._on_fail)
        self._ct.log.connect(self.log)
        self._ct.start()

    def _enable_actions(self, on):
        for w in (self.btn_mirror, self.btn_shot, self.btn_adv, self.btn_reboot):
            w.setEnabled(on)

    # ---- reconnect after a reboot / disconnect ----
    def _on_shell_lost(self):
        self._wait_and_reconnect()

    def _wait_and_reconnect(self):
        if self._reconnecting or not self.handler:
            return
        self._reconnecting = True
        self.status.setText("Reconnecting… waiting for the device to come back")
        self.log.emit("[WARNING] device went away (reboot/unplug) — waiting for it "
                      "to come back…")
        self._enable_actions(False)
        self._rc = _ReconnectThread(self.handler)
        self._rc.done.connect(self._on_reconnected)
        self._rc.start()

    def _on_reconnected(self, ok):
        self._reconnecting = False
        if ok:
            name = self.handler.serial or self.session.get("name")
            self.status.setText(f"Connected — {name}")
            self._enable_actions(True)
            self.log.emit("[OK] device back online — reconnected")
            try:
                self.shell.reconnect()
            except Exception as exc:
                self.log.emit(f"[ERROR] shell reconnect: {exc}")
        else:
            self.status.setText("Device didn't come back (timed out)")
            self.log.emit("[ERROR] device did not return to 'device' state "
                          "(timed out). If it's in recovery/bootloader this is "
                          "expected; otherwise replug and use Connect.")

    def _op(self, label, fn):
        """Run an adb maintenance op (root/remount/verity…) on a worker thread."""
        if not self.handler:
            return
        self.log.emit(f"{label}…")
        t = _ActionThread(lambda: fn(self.handler))
        t.done.connect(lambda r: self.log.emit(f"[OK] {label}: {r}"))
        t.fail.connect(lambda m: self.log.emit(f"[ERROR] {label}: {m}"))
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t); t.start()

    def _verity(self, enable):
        """disable/enable dm-verity, then sync — and offer the required reboot."""
        if not self.handler:
            return
        label = "enable-verity" if enable else "disable-verity"

        def work(h):
            out = (h.enable_verity if enable else h.disable_verity)(safe=False)
            try:
                h.shell("sync", safe=False)        # flush before the reboot
            except Exception:
                pass
            return out

        self.log.emit(f"{label}…")
        t = _ActionThread(lambda: work(self.handler))
        t.done.connect(lambda r: self._after_verity(label, r))
        t.fail.connect(lambda m: self.log.emit(f"[ERROR] {label}: {m}"))
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t); t.start()

    def _after_verity(self, label, result):
        self.log.emit(f"[OK] {label} (+ sync): {result}")
        if QMessageBox.question(
                self, "Reboot required",
                f"{label} done and filesystem synced.\n\n"
                f"A reboot is required for it to take effect. Reboot now?"
                ) == QMessageBox.Yes:
            self._op("reboot", lambda h: h.reboot(safe=False) or "rebooting")

    def _on_connected(self, handler):
        self.handler = handler
        self.status.setText(f"Connected — {handler.serial or self.session.get('name')}")
        self.log.emit(f"[OK] {self.session.get('name')}: connected")
        self._enable_actions(True)

        # device summary first, so we know whether it's automotive for the mirror
        info = handler.device_info(safe=True)
        if isinstance(info, OperationResult) and info.success:
            d = info.value
            self._automotive = bool(d.get("automotive"))
            auto = " · AUTOMOTIVE" if self._automotive else ""
            self.log.emit(f"[OK] {d.get('manufacturer')} {d.get('model')} · "
                          f"Android {d.get('android_version')} (SDK {d.get('sdk')}) "
                          f"· {d.get('abi')}{auto}")
            if self._automotive:
                self.btn_mirror.setText("📱 Mirror (IVI) ▾")
            # tab header shows the friendly device name, not the raw serial
            name = (d.get("model") or d.get("device")
                    or d.get("name") or handler.serial or "")
            if name:
                self.title_changed.emit(name)

        dev_name = ""
        if isinstance(info, OperationResult) and info.success:
            dev_name = info.value.get("device") or info.value.get("model") or ""
        self.shell = ShellPanel(handler, device_name=dev_name)
        self.shell.log.connect(self.log)
        self.shell.disconnected.connect(self._on_shell_lost)
        self.logcat = LogcatPanel(handler); self.logcat.log.connect(self.log)
        self.files = FileBrowser(handler, start="/sdcard"); self.files.log.connect(self.log)
        self.apps = AppsPanel(handler); self.apps.log.connect(self.log)
        self.controls = ControlsPanel(handler); self.controls.log.connect(self.log)
        self.phone = PhonePanel(handler); self.phone.log.connect(self.log)
        self.mirror_tab = MirrorPanel(handler, self.session,
                                      automotive=self._automotive)
        self.mirror_tab.log.connect(self.log)
        # the emoji goes on the tab as a real ICON (with plain text), so Qt sizes
        # the tab to the text correctly — inline emoji in the label throws the
        # width calc off and truncated the labels
        self._add_subtab(self.shell, "🖥", "Shell")
        self._add_subtab(self.logcat, "📜", "Logcat")
        self._add_subtab(self.files, "📁", "Files")
        self._add_subtab(self.apps, "📦", "Apps")
        self._add_subtab(self.controls, "🎛", "Controls")
        self._add_subtab(self.phone, "📞", "Phone")
        self._add_subtab(self.mirror_tab, "📱", "Mirror")
        # a combined "easy control" view: the screen + the controls side by side
        self.combo_view = self._build_control_view(handler)
        self._add_subtab(self.combo_view, "🎮", "Control + Mirror")

    def _add_subtab(self, widget, emoji, label):
        idx = self.inner.addTab(widget, label)
        self.inner.setTabIcon(idx, theme.emoji_icon(emoji))
        return idx

    def _build_control_view(self, handler):
        """A side-by-side view: the device screen (mirror / live view) on the left,
        the controls panel on the right — so you can watch and tap/press without
        switching tabs. Each is its own instance bound to the same device."""
        from PyQt5.QtWidgets import QSplitter
        split = QSplitter(Qt.Horizontal)
        self.cv_mirror = MirrorPanel(handler, self.session,
                                     automotive=self._automotive)
        self.cv_mirror.log.connect(self.log)
        self.cv_controls = ControlsPanel(handler)
        self.cv_controls.log.connect(self.log)
        split.addWidget(self.cv_mirror)
        split.addWidget(self.cv_controls)
        split.setStretchFactor(0, 3)         # the screen gets the larger share
        split.setStretchFactor(1, 2)
        split.setSizes([640, 380])
        split.setChildrenCollapsible(False)
        return split

    def _on_fail(self, msg):
        self.status.setText("Connect failed")
        self.log.emit(f"[ERROR] {self.session.get('name')}: {msg}")
        QMessageBox.warning(self, "Connect failed", msg)

    def save_active_output(self):
        """Save the COMPLETE output of whichever sub-tab is showing (Shell or
        Logcat) — archived history included, not just what's on screen."""
        if not self.handler:
            QMessageBox.information(self, "Save output", "Connect a device first.")
            return
        w = self.inner.currentWidget()
        if isinstance(w, ShellPanel):
            w.term._save_output()
        elif isinstance(w, LogcatPanel):
            w._save()
        else:
            QMessageBox.information(
                self, "Save output",
                "Switch to the Shell or Logcat tab, then Save to write its full "
                "output to a file.")

    def show_subtab(self, name: str):
        names = {"shell": 0, "logcat": 1, "files": 2, "apps": 3,
                 "controls": 4, "phone": 5, "mirror": 6}
        if self.handler and name in names:
            self.inner.setCurrentIndex(names[name])

    # --- actions: the Mirror tab hosts scrcpy (separate window by default,
    #     or embedded inside the tab when you opt in) ---
    def mirror(self, display_id=None, compat=False, embed=None):
        if not self.handler:
            return
        self.inner.setCurrentWidget(self.mirror_tab)
        self.mirror_tab.start(display_id=display_id, compat=compat, embed=embed)

    def mirror_choose_display(self):
        if not self.handler:
            return
        self.inner.setCurrentWidget(self.mirror_tab)
        self.mirror_tab.refresh_displays()

    def screenshot(self):
        if not self.handler:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save screenshot",
                                              "screenshot.png", "PNG (*.png)")
        if not path:
            return
        t = _ActionThread(lambda: self.handler.screenshot(path, safe=False))
        t.done.connect(lambda p: self.log.emit(f"[OK] screenshot saved: {p}"))
        t.fail.connect(lambda m: self.log.emit("[ERROR] screenshot: " + m))
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t); t.start()

    def reboot(self, mode):
        if not self.handler:
            return
        label = mode or "system"
        # bootloader/sideload are risky on head units (often no screen UI or
        # buttons to navigate back) — warn hard, especially on automotive.
        if mode in ("bootloader", "sideload"):
            warn = (f"Reboot to {label.upper()}?\n\n"
                    f"On Android Automotive / IVI head units this is risky: many "
                    f"have no on-screen {label} UI and no hardware buttons, so the "
                    f"unit can get STUCK with no easy way back. Only continue if you "
                    f"know this device exposes {label} and how to recover it.")
            if self._automotive:
                warn = "⚠ AUTOMOTIVE DEVICE\n\n" + warn
            if QMessageBox.warning(self, f"Reboot to {label} — risky",
                                   warn, QMessageBox.Yes | QMessageBox.No,
                                   QMessageBox.No) != QMessageBox.Yes:
                return
        elif QMessageBox.question(self, "Reboot",
                                  f"Reboot device to {label}?") != QMessageBox.Yes:
            return
        t = _ActionThread(lambda: self.handler.reboot(mode, safe=False))
        t.done.connect(lambda _: self.log.emit(f"[OK] rebooting to {label}…"))
        t.fail.connect(lambda m: self.log.emit("[ERROR] reboot: " + m))
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t); t.start()

    def close_session(self):
        for attr in ("shell", "logcat", "files", "apps", "controls", "phone",
                     "mirror_tab", "cv_mirror", "cv_controls"):
            p = getattr(self, attr, None)
            if p is not None:
                try:
                    p.close_panel()
                except Exception:
                    pass
        for s in self._scrcpy:
            try:
                s.stop()
            except Exception:
                pass
        if self.handler:
            try:
                self.handler.disconnect()
            except Exception:
                pass
