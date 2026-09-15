"""Dialog to start ``turboadb serve`` on remote Windows hosts over WinRM
(PowerShell Remoting) — one host or a list. Includes a 'Test connection'
pre-flight so you can see WinRM/credential problems before deploying."""

from __future__ import annotations

from PyQt5.QtCore import QSize, Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QPlainTextEdit,
    QLineEdit,
    QCheckBox,
    QSpinBox,
    QPushButton,
    QLabel,
    QToolButton,
    QWidget,
)

from . import settings as settings_mod
from .icons import icon


class _TestThread(QThread):
    line = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(self, hosts, user, pw, port, use_ssl=False):
        super().__init__()
        self.hosts, self.user, self.pw, self.port = hosts, user, pw, port
        self.use_ssl = use_ssl

    def run(self):
        try:
            from ..remote_deploy import deploy_serve

            deploy_serve(
                self.hosts,
                self.user,
                self.pw,
                port=self.port,
                test_only=True,
                use_ssl=self.use_ssl,
                winrm_port=5986 if self.use_ssl else 5985,
                on_status=self.line.emit,
            )
        except Exception as exc:
            self.line.emit(f"[ERROR] test: {exc}")
        self.done.emit()


class DeployDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ADB Server — start on a remote machine")
        self.setMinimumWidth(560)
        self._test = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        root = QVBoxLayout()
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(12)
        outer.addLayout(root, 1)

        head = QHBoxLayout()
        head.setSpacing(8)
        mark = QToolButton()
        mark.setObjectName("iconButton")
        mark.setIcon(icon("server", "purple"))
        mark.setIconSize(QSize(22, 22))
        mark.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        mark.setFocusPolicy(Qt.NoFocus)
        title = QLabel("Start the adb server on another Windows PC")
        title.setObjectName("settingsPageTitle")
        head.addWidget(mark)
        head.addWidget(title, 1)
        root.addLayout(head)
        sub = QLabel(
            "It runs <b>turboadb serve</b> on each host so that PC shares "
            "its plugged-in devices over the network — you can then "
            "Connect → Remote to them from here."
        )
        sub.setWordWrap(True)
        root.addWidget(sub)

        # ---- form (aligned grid, no wall of text) ----
        grid = QGridLayout()
        grid.setVerticalSpacing(8)
        grid.setHorizontalSpacing(12)
        grid.setColumnStretch(1, 1)

        grid.addWidget(self._lbl("Host(s)"), 0, 0, Qt.AlignRight | Qt.AlignTop)
        self.hosts = QPlainTextEdit()
        self.hosts.setPlaceholderText("one per line  ·  e.g.  in-daimlerlab19")
        self.hosts.setFixedHeight(64)
        recent = settings_mod.get("recent_remote_hosts") or []
        if recent:
            self.hosts.setPlainText("\n".join(recent))
        grid.addWidget(self.hosts, 0, 1)

        grid.addWidget(self._lbl("Admin user"), 1, 0, Qt.AlignRight | Qt.AlignVCenter)
        self.user = QLineEdit(settings_mod.get("deploy_user") or "")
        self.user.setPlaceholderText(r"DOMAIN\user   (e.g.  EU\nkennedy)")
        grid.addWidget(self.user, 1, 1)

        grid.addWidget(self._lbl("Password"), 2, 0, Qt.AlignRight | Qt.AlignVCenter)
        pw_row = QHBoxLayout()
        pw_row.setContentsMargins(0, 0, 0, 0)
        pw_row.setSpacing(8)
        # pre-filled from the OS credential vault so it isn't retyped every time
        self.pw = QLineEdit(settings_mod.deploy_password())
        self.pw.setEchoMode(QLineEdit.Password)
        eye = QToolButton()
        eye.setText("Show")
        eye.setProperty("role", "ghost")
        eye.setCheckable(True)
        eye.setToolTip("Show / hide password")
        eye.toggled.connect(
            lambda on: self.pw.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        self.remember = QCheckBox("Remember")
        self.remember.setChecked(bool(settings_mod.get("deploy_remember")))
        self.remember.setToolTip(
            "Remember these credentials for next time. The user is kept in "
            "settings; the password goes in the OS credential vault (Windows "
            "Credential Manager) — never in a plain file. Untick to forget "
            "them on the next deploy."
        )
        pw_row.addWidget(self.pw, 1)
        pw_row.addWidget(eye)
        pw_row.addWidget(self.remember)
        grid.addLayout(pw_row, 2, 1)

        grid.addWidget(self._lbl("adb port"), 3, 0, Qt.AlignRight | Qt.AlignVCenter)
        port_row = QHBoxLayout()
        port_row.setContentsMargins(0, 0, 0, 0)
        port_row.setSpacing(12)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(5037)
        self.port.setFixedWidth(90)
        self.chk_update = QCheckBox("Update turboadb on each host first")
        self.chk_update.setChecked(True)
        self.https = QCheckBox("WinRM over HTTPS (5986)")
        self.https.setToolTip(
            "Use encrypted WinRM (port 5986) instead of plain "
            "HTTP (5985). The host must have an HTTPS WinRM "
            "listener configured."
        )
        port_row.addWidget(self.port)
        port_row.addStretch(1)
        grid.addLayout(port_row, 3, 1)
        opt_row = QHBoxLayout()
        opt_row.setContentsMargins(0, 0, 0, 0)
        opt_row.setSpacing(16)
        opt_row.addWidget(self.chk_update)
        opt_row.addWidget(self.https)
        opt_row.addStretch(1)
        grid.addLayout(opt_row, 4, 1)
        root.addLayout(grid)

        # ---- one-line prerequisite ----
        need = QLabel(
            "ⓘ Each host needs <b>WinRM</b> on (run "
            "<code>Enable-PSRemoting -Force</code> there once) and "
            "Python + turboadb installed."
        )
        need.setWordWrap(True)
        need.setObjectName("mutedHint")
        root.addWidget(need)

        # ---- live status (test / errors appear here) ----
        self.status = QPlainTextEdit()
        self.status.setReadOnly(True)
        self.status.setFixedHeight(96)
        self.status.setPlaceholderText("Click ‘Test connection’ to check WinRM before deploying…")
        self.status.setObjectName("logBox")  # terminal colours (theme.py)
        root.addWidget(self.status)

        # ---- footer buttons: pre-flight left, Deploy / Cancel right ----
        footer = QWidget()
        footer.setObjectName("dialogFooter")
        footer.setAttribute(Qt.WA_StyledBackground, True)
        btns = QHBoxLayout(footer)
        btns.setContentsMargins(16, 10, 16, 10)
        btns.setSpacing(8)
        self.btn_test = QPushButton("Test connection")
        self.btn_test.setProperty("role", "ghost")
        self.btn_test.setIcon(icon("plug", "blue"))
        self.btn_test.clicked.connect(self._run_test)
        btns.addWidget(self.btn_test)
        btns.addStretch(1)
        self.btn_deploy = QPushButton("Deploy")
        self.btn_deploy.setProperty("role", "ok")
        self.btn_deploy.setIcon(icon("upload", "on-accent"))
        self.btn_deploy.setDefault(True)
        self.btn_deploy.clicked.connect(self._on_deploy)
        cancel = QPushButton("Cancel")
        cancel.setProperty("role", "ghost")
        cancel.setIcon(icon("x"))
        cancel.clicked.connect(self.reject)
        btns.addWidget(self.btn_deploy)
        btns.addWidget(cancel)
        outer.addWidget(footer)

    @staticmethod
    def _lbl(text):
        # colour and size come from the application stylesheet
        return QLabel(text)

    # ---- validation + values ----
    def values(self) -> dict:
        raw = self.hosts.toPlainText().replace(",", "\n")
        hosts = [h.strip() for h in raw.splitlines() if h.strip()]
        return {
            "hosts": hosts,
            "user": self.user.text().strip(),
            "password": self.pw.text(),
            "port": self.port.value(),
            "update": self.chk_update.isChecked(),
            "use_ssl": self.https.isChecked(),
        }

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
        self.btn_test.setEnabled(False)
        self.btn_deploy.setEnabled(False)
        self._test = _TestThread(v["hosts"], v["user"], v["password"], v["port"], v["use_ssl"])
        self._test.line.connect(self._append)
        self._test.done.connect(self._test_done)
        self._test.start()

    def _test_done(self):
        self.btn_test.setEnabled(True)
        self.btn_deploy.setEnabled(True)
        self._append("— test finished —")

    def _save_credentials(self):
        """Persist (or forget) the admin login per the Remember checkbox —
        user in settings.json, password in the OS credential vault only."""
        v = self.values()
        remember = self.remember.isChecked()
        try:
            settings_mod.update(
                {"deploy_remember": remember, "deploy_user": v["user"] if remember else ""}
            )
        except (OSError, ValueError) as exc:
            self._append(f"[WARNING] could not remember the login: {exc}")
        # the password lives only in the OS vault ("" deletes it)
        settings_mod.set_deploy_password(v["password"] if remember else "")

    def done(self, result):
        # A WinRM test can outlive the dialog; keep the thread alive but never
        # let it call back into a closed dialog.
        from .qtutil import disconnect_signals, park_thread, thread_running

        if thread_running(self._test):
            disconnect_signals(self._test, ("line", "done"))
            park_thread(self._test)
        super().done(result)

    def _on_deploy(self):
        prob = self._problem()
        if prob:
            self._append(f"[WARNING] {prob}")
            return
        self._save_credentials()
        self.accept()

    def _append(self, text):
        # strip the [LEVEL] tag for the compact in-dialog view
        import re

        self.status.appendPlainText(re.sub(r"^\[(OK|ERROR|WARNING|INFO)\]\s*", "", text))
        sb = self.status.verticalScrollBar()
        sb.setValue(sb.maximum())
