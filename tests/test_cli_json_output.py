"""With --json every command prints ONE JSON document on stdout (progress and
banners go to stderr) — serve, forward/reverse and the targets actions printed
plain text or two documents, or refused --json after the action. Without
--json, `info` prints aligned lines instead of a Python dict."""
import json
import types

import pytest

import turboadb.cli as cli
import turboadb.devices as devices

from test_cli_commands import FakeDev


@pytest.fixture
def run(monkeypatch, capsys):
    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: True)
    monkeypatch.setattr(cli, "scrcpy_available", lambda *a, **k: True)
    monkeypatch.delenv("ANDROID_ADB_SERVER_PORT", raising=False)

    def go(argv, dev=None):
        dev = dev if dev is not None else FakeDev()
        monkeypatch.setattr(cli, "_handler", lambda args, **kw: dev)
        rc = cli.main(argv)
        out, err = capsys.readouterr()
        return rc, out, err

    return go


def test_serve_prints_one_document(run, monkeypatch):
    monkeypatch.setattr(devices, "start_shared_server", lambda **kw: "shared on 0.0.0.0:5037")
    monkeypatch.setattr(devices, "open_firewall", lambda ports: "firewall: opened TCP 5037")

    def refuse(port=5037, adb_path=None):
        raise RuntimeError("access denied")

    monkeypatch.setattr(devices, "install_serve_task", refuse)
    rc, out, err = run(["serve", "--startup-task", "--json"])
    doc = json.loads(out)  # a second document, or plain text, would make this raise
    assert rc == 1 and doc["ok"] is False and doc["port"] == 5037
    assert doc["messages"] == ["shared on 0.0.0.0:5037", "firewall: opened TCP 5037"]
    assert "access denied" in doc["errors"][0] and not err

    monkeypatch.setattr(devices, "stop_shared_server", lambda **kw: "back to local-only")
    rc, out, _ = run(["--json", "serve", "--stop"])
    assert rc == 0 and json.loads(out) == {"ok": True, "port": 5037,
                                           "messages": ["back to local-only"]}

    # without --json nothing changes: the news on stdout, the failure on stderr
    rc, out, err = run(["serve", "--startup-task"])
    assert rc == 1 and "shared on 0.0.0.0:5037" in out and "access denied" in err


def test_forward_banner_goes_to_stderr_and_one_document_follows_ctrl_c(run, monkeypatch):
    handle = types.SimpleNamespace(close=lambda: True)

    def ctrl_c():
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_block", ctrl_c)
    rc, out, err = run(["forward", "tcp:9222", "tcp:9222", "--json"], FakeDev(forward=handle))
    assert rc == 0 and json.loads(out) == {"ok": True, "message": "forward tcp:9222 tcp:9222 removed."}
    assert "Ctrl+C to remove it" in err

    rc, out, _ = run(["reverse", "tcp:3000", "tcp:3000"], FakeDev(reverse=handle))
    assert rc == 0 and "Ctrl+C to remove it" in out and "reverse tcp:3000 tcp:3000 removed." in out


def test_every_targets_action_takes_json_after_it(run, monkeypatch, tmp_path):
    import turboadb.gui.sessions as sessions

    monkeypatch.setattr(sessions, "_FILE", str(tmp_path / "sessions.json"))
    exported = tmp_path / "targets.json"
    for argv in (["targets", "add", "bench", "usb", "SER1", "--json"],
                 ["targets", "export", str(exported), "--json"],
                 ["targets", "remove", "bench", "--json"],
                 ["targets", "import", str(exported), "--json"],
                 ["targets", "list", "--json"]):
        rc, out, err = run(argv)
        assert rc == 0, (argv, err)
        json.loads(out)
    assert [t["name"] for t in json.loads(out)] == ["bench"]


def test_info_is_aligned_lines_not_a_python_dict(run):
    info = {"serial": "SER1", "model": "Pixel 7", "automotive": False, "kind_label": "Phone",
            "display_size": "1080x2400", "flags": ["a", "b"], "extra": None}
    rc, out, _ = run(["info"], FakeDev(device_info=info))
    lines = out.splitlines()
    assert rc == 0 and "{" not in out and "'" not in out
    assert lines[0] == "serial:       SER1"
    assert "automotive:   no" in lines and "flags:        a, b" in lines and "extra:" in lines
    rc, out, _ = run(["info", "--json"], FakeDev(device_info=info))
    assert json.loads(out) == info
