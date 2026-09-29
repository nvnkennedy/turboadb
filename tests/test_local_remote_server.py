"""The PowerShell / CMD terminals of a device reached through a remote adb
server (or a local one on another port) send typed adb commands there too, and
say so; without Windows they are not offered at all."""
import types

import pytest

pytest.importorskip("PyQt5")


class RecordingSession:
    made = []

    def __init__(self, shell_type="cmd", serial=None, cwd=None, adb_path=None, **kwargs):
        self.kwargs = dict(kwargs, serial=serial)
        self.sent = []
        self.running = True
        self.env = {}
        RecordingSession.made.append(self)

    def send(self, data):
        self.sent.append(data)
        return True

    def read(self, _size=4096):
        return b""

    def interrupt(self, on_done=None):
        self.running = False

    def close(self):
        self.running = False


def _handler(host, port, serial="R58M"):
    config = types.SimpleNamespace(adb_server_host=host, adb_server_port=port)
    return types.SimpleNamespace(config=config, serial=serial)


@pytest.fixture
def make(qapp, monkeypatch):
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", RecordingSession)
    monkeypatch.setattr(RecordingSession, "made", [])
    made = []

    def make(handler, shell_type="powershell"):
        widget = _LocalShellWidget(shell_type, serial=getattr(handler, "serial", None),
                                   handler=handler)
        widget.ensure_started()
        made.append(widget)
        return widget

    yield make
    for widget in made:
        widget.close_panel()
        widget.deleteLater()


def _banner(widget):
    while widget.term._inq:
        widget.term._drain_tick()
    return widget.term.toPlainText()


def test_a_remote_servers_terminal_uses_that_server(make):
    widget = make(_handler("192.168.1.50", 5038))
    kwargs = RecordingSession.made[-1].kwargs
    assert kwargs["adb_server_host"] == "192.168.1.50" and kwargs["adb_server_port"] == 5038
    assert "adb server 192.168.1.50:5038" in _banner(widget)


def test_a_local_server_on_another_port(make):
    widget = make(_handler(None, 5099), "cmd")
    kwargs = RecordingSession.made[-1].kwargs
    assert kwargs["adb_server_host"] is None and kwargs["adb_server_port"] == 5099
    assert "adb server port 5099" in _banner(widget)


def test_the_default_server_and_stand_in_handlers(make):
    from unittest import mock

    widget = make(_handler(None, 5037))
    assert "adb server" not in _banner(widget)
    make(mock.MagicMock(serial="X"))  # not a real config: nothing is passed on
    kwargs = RecordingSession.made[-1].kwargs
    assert kwargs["adb_server_host"] is None and kwargs["adb_server_port"] is None
    make(None)
    assert RecordingSession.made[-1].kwargs["adb_server_host"] is None


def test_the_terminal_width_reaches_powershell(make):
    widget = make(None)
    columns = RecordingSession.made[-1].kwargs["columns"]
    assert isinstance(columns, int) and columns == widget._console_columns()


def test_windows_offers_android_powershell_and_cmd(qapp, monkeypatch):
    from turboadb.gui import device_tab

    monkeypatch.setattr(device_tab, "_LOCAL_SHELLS", True)
    panel = device_tab.ShellPanel(None, "device123")
    try:
        assert [len(group) for group in panel._switch_groups] == [3, 3, 3]
    finally:
        panel.close_panel()


def test_no_powershell_or_cmd_switch_without_windows(qapp, monkeypatch):
    from turboadb.gui import device_tab

    monkeypatch.setattr(device_tab, "_LOCAL_SHELLS", False)
    panel = device_tab.ShellPanel(None, "device123")
    try:
        assert panel._switch_groups == []
        assert panel.subtabs.currentWidget() is panel.android_widget
        assert not panel.ps_widget._started and not panel.cmd_widget._started
    finally:
        panel.close_panel()
