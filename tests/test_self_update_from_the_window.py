"""The main window's self-update and shortcut refresh.

- The update pip-installs exactly the version the user was offered
  (``run_upgrade(expected=…)``), as ``turboadb update`` does: a package mirror
  that lags behind PyPI then fails the update instead of the window reporting
  the old version as new and offering it again.
- A shortcut refresh that this process may not do (it runs another TurboADB
  than a shortcut starts) says why, as ``turboadb shortcut`` does, instead of
  only that both shortcuts failed.
"""
import types

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QThread, QTimer, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QWidget  # noqa: E402

import turboadb.cli as cli  # noqa: E402
import turboadb.gui.main_window as mw  # noqa: E402
from turboadb import update  # noqa: E402


def test_the_upgrade_worker_installs_the_offered_version(qapp, monkeypatch):
    asked = []
    monkeypatch.setattr(update, "run_upgrade",
                        lambda notify=None, **kw: asked.append(kw) or {"ok": True})
    worker = mw._AppUpgradeThread(expected="2.6.0")
    results = []
    worker.done.connect(results.append)
    worker.run()  # the worker's body, on this thread
    assert asked == [{"expected": "2.6.0"}] and results == [{"ok": True}]


class _Upgrade(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(dict)
    made = []

    def __init__(self, expected=None):
        super().__init__()
        self.expected = expected
        _Upgrade.made.append(self)

    def run(self):
        pass


class _Window(QWidget):
    _do_self_update = mw.MainWindow._do_self_update

    def __init__(self):
        super().__init__()
        self._version = "2.5.0"
        self._timer = QTimer(self)
        self.logged = []
        self.log_panel = types.SimpleNamespace(append=lambda _text: None)

    def _log(self, text):
        self.logged.append(text)

    def _on_self_update_done(self, res):
        pass

    def _server_task_running(self):
        return False

    def _run_tools_task(self, thread, what):
        thread.start()  # no device tabs to pause here


def test_the_window_asks_for_the_version_it_offered(qapp, monkeypatch):
    _Upgrade.made = []
    monkeypatch.setattr(mw, "_AppUpgradeThread", _Upgrade)
    monkeypatch.setattr(mw.QMessageBox, "question", staticmethod(lambda *a, **k: mw.QMessageBox.Ok))
    window = _Window()
    try:
        window._do_self_update("2.6.0")
        assert [worker.expected for worker in _Upgrade.made] == ["2.6.0"]
        assert _Upgrade.made[0].wait(5000)
    finally:
        mw._discard_dialog(getattr(window, "_upd_dlg", None))
        window.deleteLater()


# --------------------------------------------------------------------------- #
# the shortcut refresh
# --------------------------------------------------------------------------- #
_BLOCKED = ("This process runs TurboADB 3.0.0 from C:\\src\\turboadb, while this Python "
            "starts TurboADB 2.5.0, so a shortcut would start that copy. To use this one, "
            "install this copy with pip first")


def _refresh(monkeypatch, results, blocker):
    monkeypatch.setattr(cli, "ensure_shortcuts", lambda name="TurboADB", force=False: dict(results))
    monkeypatch.setattr(cli, "_shortcut_blocker", lambda: blocker)
    worker = mw._shortcut_worker(force=True)
    done = []
    worker.done.connect(done.append)
    worker.run()
    logged = []
    fake = types.SimpleNamespace(_log=logged.append)
    mw.MainWindow._on_shortcuts_refreshed(fake, done[0])
    return logged


def test_a_refused_refresh_says_why(qapp, monkeypatch):
    logged = _refresh(monkeypatch, {"Desktop": False, "Start menu": False}, _BLOCKED)
    assert logged == [f"[WARNING] The shortcuts were not changed: {_BLOCKED}."]


def test_a_failed_refresh_without_a_reason_names_what_failed(qapp, monkeypatch):
    logged = _refresh(monkeypatch, {"Start menu": False}, None)
    assert logged == ["[WARNING] Could not create: Start menu."]


def test_a_refresh_that_worked_says_so(qapp, monkeypatch):
    logged = _refresh(monkeypatch, {}, _BLOCKED)  # nothing failed: no reason is asked for
    assert logged == ["[OK] Desktop + Start-menu shortcuts refreshed."]
