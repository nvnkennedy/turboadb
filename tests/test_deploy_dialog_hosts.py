"""The remote-deploy dialog: which hosts it starts with, and the credential
vault kept off the UI thread.

Its Host(s) box started with every remote adb server ever opened from Connect
→ Remote (up to ten), so one click on Deploy, its default button, sent the
admin password to all of them and installed a SYSTEM task on each.  And the
vault read (a locked Linux keyring waits on D-Bus) froze the window.
"""
import threading
import time

import pytest

pytest.importorskip("PyQt5")

from turboadb.gui import deploy_dialog  # noqa: E402
from turboadb.gui import settings as settings_mod  # noqa: E402


@pytest.fixture
def cfg(qapp, monkeypatch, tmp_path):
    """A settings file of this test's own, and a vault that is never the real one."""
    monkeypatch.setattr(settings_mod, "_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings_mod, "_cache", None)
    vault = {"password": "", "writes": []}
    monkeypatch.setattr(settings_mod, "deploy_password", lambda: vault["password"])
    monkeypatch.setattr(
        settings_mod, "set_deploy_password",
        lambda value: vault["writes"].append((value, threading.current_thread().name)))
    return vault


def _dialog():
    dlg = deploy_dialog.DeployDialog()
    if dlg._pw_load is not None:
        dlg._pw_load.wait(5000)
    return dlg


def _answer(monkeypatch, choice):
    """Answer the several-hosts question with the button whose text starts
    with *choice*; returns what it was asked."""
    asked = []

    def exec_(box):
        asked.append(box.text())
        next(b for b in box.buttons() if b.text().startswith(choice)).click()
        return 0

    monkeypatch.setattr(deploy_dialog.QMessageBox, "exec_", exec_)
    return asked


def test_the_host_box_starts_with_the_last_deploy_only(cfg):
    settings_mod.update({"recent_remote_hosts": ["lab-a", "lab-b", "lab-c"]})
    dlg = _dialog()
    try:
        assert dlg.values()["hosts"] == []  # Connect's history is not a deploy list
    finally:
        dlg.deleteLater()
    settings_mod.update({"recent_deploy_hosts": ["lab-g"]})
    dlg = _dialog()
    try:
        assert dlg.values()["hosts"] == ["lab-g"]
        dlg._fill_recent()
        offered = [a.text() for a in dlg._recent_menu.actions()]
        assert offered == ["lab-g", "lab-a", "lab-b", "lab-c"]
        dlg._recent_menu.actions()[1].trigger()
        assert dlg.values()["hosts"] == ["lab-g", "lab-a"]
    finally:
        dlg.deleteLater()


def test_several_hosts_are_confirmed_by_name(cfg, monkeypatch):
    dlg = _dialog()
    try:
        dlg.hosts.setPlainText("lab-a\nlab-b")
        dlg.user.setText("EU\\me")
        dlg.pw.setText("pw")
        asked = _answer(monkeypatch, "Cancel")
        dlg._on_deploy()
        assert "lab-a" in asked[0] and "lab-b" in asked[0]
        assert dlg.result() != dlg.Accepted
        assert settings_mod.get("recent_deploy_hosts") is None  # nothing was deployed
        _answer(monkeypatch, "Deploy to 2 hosts")
        dlg._on_deploy()
        assert dlg.result() == dlg.Accepted
        assert settings_mod.get("recent_deploy_hosts") == ["lab-a", "lab-b"]
    finally:
        dlg.deleteLater()


def test_one_host_is_not_asked_about(cfg, monkeypatch):
    dlg = _dialog()
    try:
        dlg.hosts.setPlainText("lab-a")
        dlg.user.setText("EU\\me")
        dlg.pw.setText("pw")
        asked = _answer(monkeypatch, "Cancel")
        dlg._on_deploy()
        assert asked == [] and dlg.result() == dlg.Accepted
    finally:
        dlg.deleteLater()


def test_the_vault_is_read_off_the_ui_thread(cfg, monkeypatch, qapp):
    release = threading.Event()

    def locked_keyring():
        release.wait(10)
        return "from-the-vault"

    monkeypatch.setattr(settings_mod, "deploy_password", locked_keyring)
    started = time.monotonic()
    dlg = deploy_dialog.DeployDialog()
    try:
        assert time.monotonic() - started < 2.0 and dlg.pw.text() == ""
        release.set()
        dlg._pw_load.wait(5000)
        qapp.processEvents()
        assert dlg.pw.text() == "from-the-vault"
    finally:
        release.set()
        dlg.deleteLater()


def test_a_password_typed_meanwhile_is_kept(cfg, monkeypatch, qapp):
    release = threading.Event()
    monkeypatch.setattr(settings_mod, "deploy_password", lambda: release.wait(10) and "late")
    dlg = deploy_dialog.DeployDialog()
    try:
        dlg.pw.setFocus()
        from PyQt5.QtTest import QTest

        QTest.keyClicks(dlg.pw, "typed")
        release.set()
        dlg._pw_load.wait(5000)
        qapp.processEvents()
        assert dlg.pw.text() == "typed"
    finally:
        release.set()
        dlg.deleteLater()


def test_the_password_is_stored_off_the_ui_thread(cfg):
    dlg = _dialog()
    try:
        dlg.hosts.setPlainText("lab-a")
        dlg.user.setText("EU\\me")
        dlg.pw.setText("secret")
        dlg.remember.setChecked(True)
        dlg._save_credentials()
        deadline = time.monotonic() + 5
        while not cfg["writes"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cfg["writes"] == [("secret", "turboadb-keyring")]
    finally:
        dlg.deleteLater()


def test_nothing_is_read_from_the_vault_when_not_remembering(cfg, monkeypatch):
    settings_mod.update({"deploy_remember": False})
    monkeypatch.setattr(settings_mod, "deploy_password", lambda: pytest.fail("read the vault"))
    dlg = deploy_dialog.DeployDialog()
    try:
        assert dlg._pw_load is None
    finally:
        dlg.deleteLater()
