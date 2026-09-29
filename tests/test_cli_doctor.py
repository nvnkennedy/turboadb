"""`turboadb doctor` also reports the running adb server — which adb, which
protocol, and whether TurboADB's adb would keep restarting it (a check only
the GUI made) — and which TurboADB runs next to the one this Python has
installed, the copy shortcuts and self-update use."""
import json

import pytest

import turboadb.cli as cli
import turboadb.tools as tools


@pytest.fixture
def doctor(monkeypatch, capsys):
    monkeypatch.setattr(tools, "diagnose", lambda: {
        "adb": "Android Debug Bridge version 1.0.41", "adb_path": "C:/tools/adb.exe",
        "scrcpy": "found", "scrcpy_path": "C:/tools/scrcpy.exe"})
    monkeypatch.setattr(tools, "local_adb_port", lambda port=None: 5037)
    monkeypatch.setattr(tools, "adb_server_env_note", lambda: None)
    monkeypatch.setattr(cli, "_install_report", lambda: {
        "version": "3.0.0", "path": "E:/work/turboadb", "installed": "3.0.0",
        "installed_path": "E:/work/turboadb", "same": True, "note": None})

    def go(argv):
        rc = cli.main(argv)
        out, err = capsys.readouterr()
        return rc, out, err

    return go


def test_a_server_of_another_adb_is_described(doctor, monkeypatch):
    monkeypatch.setattr(tools, "adb_server_version", lambda port=5037, timeout=0.25: 40)
    monkeypatch.setattr(tools, "adb_server_status", lambda port=5037, timeout=0.5: {
        "version": "34.0.5", "executable": "C:/sdk/platform-tools/adb.exe"})
    monkeypatch.setattr(tools, "describe_adb_server", lambda adb, port=5037: (
        "WARNING", "The adb server on port 5037 runs C:/sdk/platform-tools/adb.exe 34.0.5 "
                   "(protocol 40), but TurboADB's adb speaks protocol 41."))
    rc, out, _ = doctor(["doctor"])
    assert rc == 0
    assert "server   : port 5037, adb 34.0.5, protocol 40" in out
    assert "C:/sdk/platform-tools/adb.exe" in out
    assert "[WARNING] The adb server on port 5037 runs" in out
    assert "turboadb : 3.0.0" in out

    rc, out, _ = doctor(["doctor", "--json"])
    server = json.loads(out)["adb_server"]
    assert (server["protocol"], server["version"]) == (40, "34.0.5")
    assert server["notes"] == [{"level": "WARNING", "text": server["notes"][0]["text"]}]
    assert "protocol 41" in server["notes"][0]["text"]


def test_no_server_is_said_without_starting_one(doctor, monkeypatch):
    monkeypatch.setattr(tools, "adb_server_version", lambda port=5037, timeout=0.25: None)
    monkeypatch.setattr(tools, "adb_server_status", lambda *a, **k: pytest.fail("asked a dead port"))
    monkeypatch.setattr(tools, "describe_adb_server", lambda adb, port=5037: None)
    monkeypatch.setattr(tools, "ensure_adb_server", lambda *a, **k: pytest.fail("started a server"))
    rc, out, _ = doctor(["doctor"])
    assert rc == 0 and "server   : not running on port 5037" in out
    rc, out, _ = doctor(["--json", "doctor"])
    assert json.loads(out)["adb_server"] == {"port": 5037, "protocol": None, "version": None,
                                             "executable": None, "notes": []}


def test_a_checkout_next_to_an_older_install_is_pointed_out(doctor, monkeypatch):
    monkeypatch.setattr(tools, "adb_server_version", lambda port=5037, timeout=0.25: None)
    monkeypatch.setattr(tools, "describe_adb_server", lambda adb, port=5037: None)
    note = "TurboADB 3.0.0 runs from E:/work/turboadb, but this Python has TurboADB 2.0.0 installed"
    monkeypatch.setattr(cli, "_install_report", lambda: {
        "version": "3.0.0", "path": "E:/work/turboadb", "installed": "2.0.0",
        "installed_path": "C:/Python/Lib/site-packages/turboadb", "same": False, "note": note})
    rc, out, _ = doctor(["doctor"])
    assert rc == 0 and f"[WARNING] {note}." in out
    assert "shortcuts and self-update work on the TurboADB installed in this Python" in out
    rc, out, _ = doctor(["doctor", "--json"])
    assert json.loads(out)["turboadb"]["installed"] == "2.0.0"


def test_the_environment_note_is_reported(doctor, monkeypatch):
    monkeypatch.setattr(tools, "adb_server_version", lambda port=5037, timeout=0.25: None)
    monkeypatch.setattr(tools, "describe_adb_server", lambda adb, port=5037: None)
    monkeypatch.setattr(tools, "adb_server_env_note", lambda: (
        "INFO", "Using the local adb server on port 5038 (ANDROID_ADB_SERVER_PORT)."))
    rc, out, _ = doctor(["doctor"])
    assert "[INFO] Using the local adb server on port 5038" in out
