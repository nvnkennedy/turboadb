"""Connect goes ahead when the recent-hosts list can't be saved.

The Connect dialog remembers the host it connects to in settings.json.  That
write can fail (a settings file another program keeps locked, or one that
can't be read and can't be copied aside), and the error escaped the Connect
button: the dialog stayed open and the device could not be opened at all."""
import pytest

pytest.importorskip("PyQt5")

from turboadb.gui import connect_dialog  # noqa: E402


@pytest.fixture
def no_scans(monkeypatch):
    monkeypatch.setattr(connect_dialog.ConnectDialog, "_scan_usb", lambda self: None)
    monkeypatch.setattr(connect_dialog.ConnectDialog, "_scan_remote", lambda self: None)


@pytest.mark.parametrize("error", [OSError("settings.json is locked"),
                                   ValueError("settings.json could not be copied aside")])
def test_connect_goes_ahead_when_the_recent_host_is_not_saved(qapp, no_scans, monkeypatch, error):
    def refuse(key, value, cap=10):
        raise error

    monkeypatch.setattr(connect_dialog.settings_mod, "add_recent", refuse)
    dlg = connect_dialog.ConnectDialog()
    try:
        dlg.mode.setCurrentIndex(1)
        dlg.net_host.setCurrentText("192.168.1.7")
        dlg._accept()
        assert dlg.result() == dlg.Accepted
        session = dlg.session()
        assert session["type"] == "network" and session["host"] == "192.168.1.7"
    finally:
        dlg.deleteLater()


def test_connect_still_remembers_the_host(qapp, no_scans, monkeypatch):
    remembered = []
    monkeypatch.setattr(connect_dialog.settings_mod, "add_recent",
                        lambda key, value, cap=10: remembered.append((key, value)))
    dlg = connect_dialog.ConnectDialog()
    try:
        dlg.mode.setCurrentIndex(1)
        dlg.net_host.setCurrentText("192.168.1.8")
        dlg._accept()
        assert remembered == [("recent_network_hosts", "192.168.1.8")]
        assert dlg.result() == dlg.Accepted
    finally:
        dlg.deleteLater()
