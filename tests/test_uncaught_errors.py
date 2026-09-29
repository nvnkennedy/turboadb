"""Uncaught errors, on any thread: written to crash.log, then shown in the log
panel and the one error popup on the GUI thread. An error escaping a worker
thread used to build the popup on that worker, which crashed (or hung) the
whole app with every shell, transfer and mirror in it. Also: the files the
app writes (crash.log, the log sweep, the first-run flag) follow a changed
HOME instead of the profile the module was imported under."""

import os
import subprocess
import sys
import threading
import time
import types

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QThread  # noqa: E402

# Imported before any test moves HOME, as the app imports it at start-up.
from turboadb.gui import app as app_mod  # noqa: E402


class _Boom(QThread):
    def run(self):
        raise RuntimeError("boom in a worker")


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A fresh user profile; returns its ~/.turboadb."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path / ".turboadb"


@pytest.fixture
def hook(qapp, monkeypatch):
    """The app's exception hook, installed for one test (then put back)."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(app_mod, "_error_relay", None)
    app_mod._install_excepthook()
    return sys.excepthook


def _wait_for(qapp, condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    return condition()


def _recorders(qapp, monkeypatch):
    """Stand in for the log panel and the popup, noting which thread ran them."""
    gui = qapp.thread()
    seen = []

    def on_gui():
        return QThread.currentThread() == gui

    panel = types.SimpleNamespace(append=lambda text: seen.append(("log", on_gui(), text)))
    monkeypatch.setattr(app_mod, "_window", types.SimpleNamespace(log_panel=panel))
    monkeypatch.setattr(
        app_mod, "_show_error_popup",
        lambda exc_type, exc: seen.append(("popup", on_gui(), f"{exc_type.__name__}: {exc}")),
    )
    return seen


def test_an_error_in_a_worker_thread_is_shown_on_the_gui_thread(qapp, hook, profile, monkeypatch):
    seen = _recorders(qapp, monkeypatch)
    worker = _Boom()
    worker.start()
    assert worker.wait(10_000)
    assert _wait_for(qapp, lambda: len(seen) == 2), seen
    assert [(kind, on_gui) for kind, on_gui, _text in seen] == [("log", True), ("popup", True)]
    assert seen[0][2].startswith("[ERROR] Unexpected error:\nTraceback")
    assert "boom in a worker" in seen[0][2]
    assert seen[1][2] == "RuntimeError: boom in a worker"
    # crash.log is written on the worker itself, straight away
    assert "RuntimeError: boom in a worker" in (profile / "crash.log").read_text(encoding="utf-8")


def test_an_error_on_the_gui_thread_is_still_shown_at_once(qapp, hook, profile, monkeypatch):
    seen = _recorders(qapp, monkeypatch)
    try:
        raise ValueError("in a slot")
    except ValueError:
        hook(*sys.exc_info())
    # no event-loop turn needed: the popup is up before the hook returns
    assert [(kind, on_gui) for kind, on_gui, _text in seen] == [("log", True), ("popup", True)]
    assert seen[1][2] == "ValueError: in a slot"


def test_an_exception_that_cannot_be_shown_as_text_is_still_reported(qapp, hook, profile,
                                                                       monkeypatch):
    class Unprintable(Exception):
        def __str__(self):
            raise ValueError("no text")

    seen = _recorders(qapp, monkeypatch)
    try:
        raise Unprintable()
    except Unprintable:
        hook(*sys.exc_info())
    assert [kind for kind, _on_gui, _text in seen] == ["log", "popup"]
    assert seen[1][2].startswith("Unprintable: <")


def test_a_hook_installed_off_the_gui_thread_still_reports_there(qapp, profile, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(app_mod, "_error_relay", None)
    installer = threading.Thread(target=app_mod._install_excepthook)
    installer.start()
    installer.join(10)
    assert app_mod._error_relay.thread() == qapp.thread()
    seen = _recorders(qapp, monkeypatch)
    worker = _Boom()
    worker.start()
    assert worker.wait(10_000)
    assert _wait_for(qapp, lambda: len(seen) == 2), seen
    assert all(on_gui for _kind, on_gui, _text in seen)


def test_without_a_relay_an_error_only_reaches_crash_log(qapp, hook, profile, monkeypatch):
    seen = _recorders(qapp, monkeypatch)
    monkeypatch.setattr(app_mod, "_error_relay", None)

    def fail():
        try:
            raise KeyError("no GUI to report to")
        except KeyError:
            hook(*sys.exc_info())

    worker = threading.Thread(target=fail)
    worker.start()
    worker.join(10)
    qapp.processEvents()
    assert seen == []  # no widget was touched
    assert "no GUI to report to" in (profile / "crash.log").read_text(encoding="utf-8")


_REAL_WIDGETS = r'''
from PyQt5.QtCore import QThread, QTimer
from PyQt5.QtWidgets import QApplication, QDialog, QLabel, QMainWindow

app = QApplication([])
from turboadb.gui import app as app_mod
from turboadb.gui.log_panel import LogPanel


class Box(QDialog):
    """A real top-level window in place of the message box: Qt's offscreen
    platform on Windows cannot show a QMessageBox on any thread."""
    Warning = Ok = 0
    made_on_gui = []

    def __init__(self, _icon, title, text, _buttons, parent):
        super().__init__(parent)
        Box.made_on_gui.append(QThread.currentThread() == app.thread())
        self.setWindowTitle(title)
        QLabel(text, self)

    def setInformativeText(self, text):
        pass


window = QMainWindow()
window.log_panel = LogPanel()
window.setCentralWidget(window.log_panel)
window.show()
app_mod._window = window
app_mod.QMessageBox = Box
app_mod._install_excepthook()


class Boom(QThread):
    def run(self):
        raise RuntimeError("boom in a worker")


worker = Boom()
worker.finished.connect(lambda: QTimer.singleShot(300, app.quit))
worker.start()
QTimer.singleShot(20000, app.quit)
app.exec_()
worker.wait()
print("popup made on the GUI thread:", Box.made_on_gui, flush=True)
print("in the log panel:", "boom in a worker" in window.log_panel.view.toPlainText(), flush=True)
'''


def test_a_worker_error_with_the_real_widgets_leaves_the_app_running(tmp_path):
    """The real hook, log panel and a real popup window, in a process of their
    own: built on the worker, the popup took the whole process down."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=root, HOME=str(tmp_path),
               USERPROFILE=str(tmp_path), TURBOADB_AUTO_FETCH="0")
    try:
        done = subprocess.run([sys.executable, "-c", _REAL_WIDGETS], cwd=root, env=env,
                              capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        pytest.fail("the app hung after an error in a worker thread")
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    assert "popup made on the GUI thread: [True]" in done.stdout
    assert "in the log panel: True" in done.stdout
    crash = (tmp_path / ".turboadb" / "crash.log").read_text(encoding="utf-8")
    assert "RuntimeError: boom in a worker" in crash


def test_the_app_files_follow_a_changed_home(qapp, profile):
    app_mod._crash_log("a line for the new profile")
    assert (profile / "crash.log").read_text(encoding="utf-8") == "a line for the new profile\n"
    logs = profile / "logs"
    logs.mkdir()
    (logs / "turboadb-remnant.log").write_text("", encoding="utf-8")  # empty: swept
    (logs / "turboadb-recent.log").write_text("history", encoding="utf-8")
    app_mod._sweep_stale_logs()
    assert sorted(p.name for p in logs.iterdir()) == ["turboadb-recent.log"]
    app_mod._first_run_tasks()
    assert (profile / "first-run-done").exists()
