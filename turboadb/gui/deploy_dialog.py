"""Dialog to start ``turboadb serve`` on remote Windows hosts over WinRM
(PowerShell Remoting) — one host or a list. Includes a 'Test connection'
pre-flight so you can see WinRM/credential problems before deploying."""

from __future__ import annotations

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QPlainTextEdit, QLineEdit, QCheckBox, QSpinBox,
                             QPushButton, QLabel, QToolButton, QWidget)

from . import settings as settings_mod, theme


class _TestThread(QThread):
    line = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(self, hosts, user, pw, port):
        super().__init__()
        self.hosts, self.user, self.pw, self.port = hosts, user, pw, port

    def run(self):
        try:
            from ..remote_deploy import deploy_serve
            deploy_serve(self.hosts, self.user, self.pw, port=self.port,
                         test_only=True, on_status=self.line.emit)
        except Exception as exc:
            self.line.emit(f"[ERROR] test: {exc}")
        self.done.emit()


class DeployDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ADB Server — start on a remote machine")
        self.setMinimumWidth(560)
        self._test = None

        root = QVBoxLayout(self)
        root.setSpacing(10)

        title = QLabel("Start the adb server on another Windows PC")
        title.setStyleSheet(f"font-size:13.5pt; font-weight:800; "
                            f"color:{theme.accent_text(settings_mod.get('theme'))};")
        root.addWidget(title)
        sub = QLabel("It runs <b>turboadb serve</b> on each host so that PC shares "
                     "its plugged-in devices over the network — you can then "
                     "Connect → Remote to them from here.")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # ---- form (aligned grid, no wall of text) ----
        grid = QGridLayout()
        grid.setVerticalSpacing(8); grid.setHorizontalSpacing(10)
        grid.setColumnStretch(1, 1)

        grid.addWidget(self._lbl("Host(s)"), 0, 0, Qt.AlignTop)
        self.hosts = QPlainTextEdit()
        self.hosts.setPlaceholderText("one per line  ·  e.g.  in-daimlerlab19")
        self.hosts.setFixedHeight(64)
        recent = settings_mod.get("recent_remote_hosts") or []
        if recent:
            self.hosts.setPlainText("\n".join(recent))
        grid.addWidget(self.hosts, 0, 1)

        grid.addWidget(self._lbl("Admin user"), 1, 0)
        self.user = QLineEdit()
        self.user.setPlaceholderText(r"DOMAIN\user   (e.g.  EU\nkennedy)")
        grid.addWidget(self.user, 1, 1)

        grid.addWidget(self._lbl("Password"), 2, 0)
        pw_row = QHBoxLayout(); pw_row.setSpacing(6)
        self.pw = QLineEdit(); self.pw.setEchoMode(QLineEdit.Password)
        eye = QToolButton(); eye.setText("👁"); eye.setCheckable(True)
        eye.setToolTip("Show / hide password")
        eye.toggled.connect(lambda on: self.pw.setEchoMode(
            QLineEdit.Normal if on else QLineEdit.Password))
        pw_row.addWidget(self.pw, 1); pw_row.addWidget(eye)
        pw_w = QWidget(); pw_w.setLayout(pw_row)
        grid.addWidget(pw_w, 2, 1)

        grid.addWidget(self._lbl("adb port"), 3, 0)
        port_row = QHBoxLayout(); port_row.setSpacing(12)
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(5037)
        self.port.setFixedWidth(90)
        self.update = QCheckBox("Update turboadb on each host first")
        self.update.setChecked(True)
        port_row.addWidget(self.port); port_row.addWidget(self.update)
        port_row.addStretch(1)
        port_w = QWidget(); port_w.setLayout(port_row)
        grid.addWidget(port_w, 3, 1)
        root.addLayout(grid)

        # ---- one-line prerequisite ----
        need = QLabel("ⓘ Each host needs <b>WinRM</b> on (run "
                      "<code>Enable-PSRemoting -Force</code> there once) and "
                      "Python + turboadb installed.")
        need.setWordWrap(True)
        need.setStyleSheet("color:#8a93a0; font-size:9pt;")
        root.addWidget(need)

        # ---- live status (test / errors appear here) ----
        self.status = QPlainTextEdit(); self.status.setReadOnly(True)
        self.status.setFixedHeight(96)
        self.status.setPlaceholderText("Click ‘Test connection’ to check WinRM "
                                       "before deploying…")
        self.status.setStyleSheet("QPlainTextEdit{background:#0b0b0d;"
                                  "color:#cfe3f7;border:1px solid #2a2a2a;}")
        root.addWidget(self.status)

        # ---- buttons ----
        btns = QHBoxLayout()
        self.btn_test = QPushButton("  Test connection  ")
        self.btn_test.setProperty("role", "ghost")
        self.btn_test.setIcon(theme.emoji_icon("🔎"))
        self.btn_test.clicked.connect(self._run_test)
        btns.addWidget(self.btn_test)
        btns.addStretch(1)
        self.btn_deploy = QPushButton("  Deploy  ")
        self.btn_deploy.setProperty("role", "ok")
        self.btn_deploy.setIcon(theme.emoji_icon("📡"))
        self.btn_deploy.clicked.connect(self._on_deploy)
        cancel = QPushButton("Cancel"); cancel.setProperty("role", "ghost")
        cancel.clicked.connect(self.reject)
        btns.addWidget(self.btn_deploy); btns.addWidget(cancel)
        root.addLayout(btns)

    @staticmethod
    def _lbl(text):
        l = QLabel(text)
        l.setStyleSheet("color:#c4ccd4; font-weight:600;")
        return l

    # ---- validation + values ----
    def values(self) -> dict:
        raw = self.hosts.toPlainText().replace(",", "\n")
        hosts = [h.strip() for h in raw.splitlines() if h.strip()]
        return {"hosts": hosts, "user": self.user.text().strip(),
                "password": self.pw.text(), "port": self.port.value(),
                "update": self.update.isChecked()}

    def _problem(self):
        v = self.values()
        if not v["hosts"]:
            return "Enter at least one host."
        if not v["user"]:
            return "Enter the admin user (e.g. DOMAIN\\user)."
        if not v["password"]:
            return "Enter the password."
        return None

    # ---- test connection (pre-flight, in-dialog) ----
    def _run_test(self):
        if self._test and self._test.isRunning():
            return
        prob = self._problem()
        if prob:
            self._append(f"[WARNING] {prob}")
            return
        v = self.values()
        self.status.clear()
        self._append(f"Testing WinRM on {len(v['hosts'])} host(s)…")
        self.btn_test.setEnabled(False); self.btn_deploy.setEnabled(False)
        self._test = _TestThread(v["hosts"], v["user"], v["password"], v["port"])
        self._test.line.connect(self._append)
        self._test.done.connect(self._test_done)
        self._test.start()

    def _test_done(self):
        self.btn_test.setEnabled(True); self.btn_deploy.setEnabled(True)
        self._append("— test finished —")

    def _on_deploy(self):
        prob = self._problem()
        if prob:
            self._append(f"[WARNING] {prob}")
            return
        self.accept()

    def _append(self, text):
        # strip the [LEVEL] tag for the compact in-dialog view
        import re
        self.status.appendPlainText(re.sub(r"^\[(OK|ERROR|WARNING|INFO)\]\s*",
                                           "", text))
        sb = self.status.verticalScrollBar(); sb.setValue(sb.maximum())
