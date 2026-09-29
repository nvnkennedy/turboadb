"""Everything about the local adb server names the port it really uses.

``ANDROID_ADB_SERVER_PORT`` (adb's own variable) moves the default 5037, and
``tools.local_adb_port`` now makes TurboADB's probes, its server start and
``start_shared_server`` follow it, as does the main window's Share.  Two
callers were left on 5037: ``turboadb serve`` and the Connect dialog's share
opened the firewall (and ``serve --status`` reported) for 5037, so other
machines could not reach the moved server; and a device tab whose server did
not come up said "port 5037" while it had waited on the moved one.
"""
import json

import pytest

import turboadb.cli as cli
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBConnectionError


@pytest.fixture
def shared(monkeypatch):
    import turboadb.devices as devices

    seen = {}
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5038")
    monkeypatch.setattr(devices, "start_shared_server",
                        lambda port=5037, adb_path=None, **_k: seen.setdefault("port", port) and
                        "adb server shared")
    monkeypatch.setattr(devices, "open_firewall",
                        lambda ports: seen.setdefault("firewall", list(ports)) and "rules added")
    monkeypatch.setattr(devices, "server_is_shared", lambda port=5037, adb_path=None: True)
    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: True)
    monkeypatch.setattr(cli, "scrcpy_available", lambda *a, **k: True)
    return seen


def test_serve_opens_the_firewall_for_the_moved_port(shared, capsys):
    assert cli.main(["serve"]) == 0
    assert shared["firewall"][0] == 5038


def test_serve_status_reports_the_moved_port(shared, capsys):
    assert cli.main(["serve", "--status", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"port": 5038, "shared": True}


def test_an_explicit_other_port_is_kept(shared, capsys):
    assert cli.main(["serve", "--port", "5050"]) == 0
    assert shared["firewall"][0] == 5050


def test_the_connect_dialogs_share_opens_the_moved_port(shared, qapp):
    from turboadb.gui.connect_dialog import _ServeThread

    worker = _ServeThread(port=5037)
    done = []
    worker.done.connect(done.append)
    worker.fail.connect(done.append)
    worker.run()  # the worker's body, on this thread
    assert shared["firewall"][0] == 5038, done


def test_a_server_that_does_not_start_is_reported_on_the_moved_port(monkeypatch, tmp_path):
    import turboadb.core as core
    import turboadb.tools as tools

    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5038")
    monkeypatch.setattr(core, "is_adb_server_alive", lambda **_k: False)
    waited = []
    monkeypatch.setattr(tools, "ensure_adb_server",
                        lambda adb, timeout=15.0, port=5037: waited.append(port) or False)
    # connect resolves adb before it starts the server, so it must exist
    adb = tmp_path / "adb.exe"
    adb.write_text("")
    handler = ADBHandler(ADBConfig(serial="FAKE123", adb_path=str(adb)))
    with pytest.raises(ADBConnectionError, match="port 5038"):
        handler.connect(safe=False)
    assert waited == [5037]  # (ensure_adb_server maps it to 5038 itself)
