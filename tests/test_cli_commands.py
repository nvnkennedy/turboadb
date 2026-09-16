"""The CLI's commands and flags, run through ``cli.main`` against a stub device
handler (no adb, no device)."""
import io
import json
import types

import pytest

import turboadb.cli as cli
from turboadb.config import ADBConfig


class FakeDev:
    """Records every method call; ``returns`` sets a method's return value (or a
    callable that computes it). Anything else returns True."""

    def __init__(self, **returns):
        self.calls = []
        self.returns = returns
        self.config = ADBConfig()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            value = self.returns.get(name, True)
            return value(*args, **kwargs) if callable(value) else value

        return method

    def called(self, name):
        return [(args, kwargs) for n, args, kwargs in self.calls if n == name]


def _result(stdout="", exit_code=0):
    return types.SimpleNamespace(
        ok=exit_code == 0, exit_code=exit_code, stdout=stdout, stderr="",
        as_dict=lambda: {"stdout": stdout, "exit_code": exit_code},
    )


@pytest.fixture
def run(monkeypatch, capsys):
    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: True)
    monkeypatch.setattr(cli, "scrcpy_available", lambda *a, **k: True)

    def go(argv, dev=None):
        dev = dev if dev is not None else FakeDev()
        monkeypatch.setattr(cli, "_handler", lambda args, **kw: dev)
        rc = cli.main(argv)
        out, err = capsys.readouterr()
        return rc, out, err

    return go


# --- shared options --------------------------------------------------------- #
def test_timeout_covers_transfers_and_scrcpy_path_is_passed():
    args = cli.build_parser().parse_args(["push", "a", "b", "--timeout", "7", "--scrcpy-path", "s.exe"])
    cfg = cli._config(args)
    assert cfg.command_timeout == 7 and cfg.transfer_timeout == 7
    assert cfg.scrcpy_path == "s.exe"


def test_saved_target_is_used_with_at_name(monkeypatch, tmp_path, run):
    import turboadb.gui.sessions as sessions

    monkeypatch.setattr(sessions, "_FILE", str(tmp_path / "sessions.json"))
    assert run(["targets", "add", "bench", "network", "192.168.1.50"])[0] == 0
    assert run(["targets", "add", "lab", "remote", "lab-pc:5038", "SER1"])[0] == 0

    cfg = cli._config(cli.build_parser().parse_args(["-s", "@bench", "info"]))
    assert (cfg.serial, cfg.host, cfg.port) == (None, "192.168.1.50", 5555)
    cfg = cli._config(cli.build_parser().parse_args(["info", "-s", "@lab"]))
    assert (cfg.serial, cfg.adb_server_host, cfg.adb_server_port) == ("SER1", "lab-pc", 5038)

    rc, out, _ = run(["--json", "targets", "list"])
    assert rc == 0 and [t["name"] for t in json.loads(out)] == ["bench", "lab"]
    assert run(["targets", "remove", "bench"])[0] == 0
    export = tmp_path / "out.json"
    assert run(["targets", "export", str(export)])[0] == 0
    assert [t["name"] for t in json.loads(export.read_text())] == ["lab"]

    with pytest.raises(ValueError, match="no saved target"):
        cli._config(cli.build_parser().parse_args(["-s", "@missing", "info"]))
    rc, _, err = run(["targets", "remove", "missing"])
    assert rc == 1 and "no saved target" in err


# --- --json everywhere ----------------------------------------------------- #
def test_json_for_actions_results_and_saved_files(run):
    rc, out, _ = run(["--json", "screen", "on"])
    assert rc == 0 and json.loads(out) == {"ok": True, "message": "screen on"}
    rc, out, _ = run(["screen", "off", "--json"], FakeDev(screen_off=False))
    assert rc == 1 and json.loads(out)["ok"] is False
    rc, out, _ = run(["wifi", "on", "--json"])
    assert json.loads(out) == {"ok": True, "result": True}
    dev = FakeDev(screenshot="shot.png")
    rc, out, _ = run(["screenshot", "shot.png", "--display", "2", "--json"], dev)
    assert json.loads(out) == {"ok": True, "path": "shot.png"}
    assert dev.called("screenshot") == [(("shot.png",), {"display_id": 2})]


def test_json_battery_build_and_health(run, tmp_path):
    rc, out, _ = run(["battery", "--json"], FakeDev(battery_status={"level": 80}))
    assert json.loads(out) == {"level": 80}
    rc, out, _ = run(["build-info", "--json"], FakeDev(build_properties={"ro.x": "1"}))
    assert json.loads(out) == {"ro.x": "1"}
    report = tmp_path / "health.txt"
    rc, out, _ = run(["health", "--full", "-o", str(report)], FakeDev(health_report="REPORT"))
    assert rc == 0 and report.read_text() == "REPORT\n"


# --- devices and shell ------------------------------------------------------ #
def test_state_wait_and_serialno_do_not_connect(run):
    dev = FakeDev(get_state="offline", get_serialno="SER")
    assert run(["state"], dev)[1].strip() == "offline"
    assert run(["serialno"], dev)[1].strip() == "SER"
    assert run(["wait", "5"], dev)[0] == 0
    assert dev.called("wait_for_device") == [((5.0,), {})]
    assert not dev.called("connect")


def test_adb_passthrough(run):
    dev = FakeDev(adb=_result("device\n", exit_code=0))
    rc, out, _ = run(["adb", "--", "get-state"], dev)
    assert rc == 0 and out == "device\n"
    assert dev.called("adb") == [(("get-state",), {})]
    assert run(["adb"])[0] == 2


def test_shell_interactive_needs_a_terminal(run, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    assert run(["shell"])[0] == 2
    dev = FakeDev(interactive_shell=0)
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: True))
    assert run(["shell"], dev)[0] == 0 and dev.called("interactive_shell")


def test_shell_batch(run, tmp_path):
    batch = tmp_path / "cmds.txt"
    batch.write_text("echo 1\n# comment\n\necho 2\n")
    dev = FakeDev(shell_many=[_result("1\n"), _result("2\n")])
    rc, out, _ = run(["shell", "--batch", str(batch), "--keep-going"], dev)
    assert rc == 0 and "$ echo 2" in out
    assert dev.called("shell_many") == [((["echo 1", "echo 2"],), {"stop_on_error": False, "su": False})]


def test_shell_all_runs_on_every_online_device(run, monkeypatch, capsys):
    online = types.SimpleNamespace
    monkeypatch.setattr(cli, "list_devices", lambda *a, **k: [
        online(serial="A", state="device"), online(serial="B", state="offline"),
        online(serial="C", state="device")])
    devs = {s: FakeDev(shell=_result(f"{s}\n")) for s in "ABC"}
    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_handler", lambda args, **kw: devs[kw["serial"]])
    rc = cli.main(["shell", "--all", "--", "getprop", "ro.product.model"])
    out = capsys.readouterr().out
    assert rc == 0 and "--- A" in out and "--- C" in out and "--- B" not in out
    assert devs["A"].called("shell") == [(("getprop ro.product.model",), {"su": False})]


def test_discover_connect_only_ready_devices(run, monkeypatch):
    import turboadb.devices as devices

    monkeypatch.setattr(devices, "mdns_devices", lambda *a, **k: [
        {"address": "192.168.1.5:40000", "service": "connect", "name": "x"},
        {"address": "192.168.1.5:37000", "service": "pairing", "name": "x"}])
    dev = FakeDev(connect_tcp="connected")
    assert run(["discover", "--connect"], dev)[0] == 0
    assert dev.called("connect_tcp") == [(("192.168.1.5", 40000), {})]


# --- logs ------------------------------------------------------------------- #
def test_logcat_crashes_filter_and_grep(run):
    def logcat(**kw):
        for line in ("E AndroidRuntime: FATAL crash", "I fine"):
            kw["on_line"](line)
        return types.SimpleNamespace(matches=[])

    dev = FakeDev(logcat=logcat)
    rc, out, _ = run(["logcat", "--crashes", "--dump", "--grep", "fatal",
                      "--filter", "AndroidRuntime:E"], dev)
    assert rc == 0 and "FATAL crash" in out and "fine" not in out
    kwargs = dev.called("logcat")[0][1]
    assert kwargs["buffers"] == ["crash", "main", "system"] and kwargs["priority"] == "E"
    assert kwargs["filterspecs"] == ["AndroidRuntime:E"] and kwargs["dump"] is True
    assert run(["logcat", "--grep", "("])[0] == 2


def test_bugreport_uses_the_timeout(run):
    dev = FakeDev(bugreport="br.zip")
    assert run(["bugreport", "br.zip", "--timeout", "99"], dev)[0] == 0
    assert dev.called("bugreport") == [(("br.zip",), {"timeout": 99.0})]


# --- files ------------------------------------------------------------------ #
def test_file_commands(run):
    entries = [
        {"name": "Download", "is_dir": True, "size_text": "", "permissions": "drwxrwx---",
         "owner": "root sdcard_rw", "modified": "2026-01-01 10:00"},
        {"name": "a.txt", "is_dir": False, "size_text": "12 B", "permissions": "-rw-rw----",
         "owner": "root sdcard_rw", "modified": "2026-01-01 10:01"},
    ]
    rc, out, _ = run(["ls", "/sdcard"], FakeDev(list_dir=entries))
    assert rc == 0 and "Download/" in out and "a.txt" in out

    dev = FakeDev(remove=["/sdcard/x"], move="/sdcard/b", copy="/sdcard/c", make_dir="/sdcard/d")
    assert run(["rm", "-r", "/sdcard/x"], dev)[0] == 0
    assert dev.called("remove") == [((["/sdcard/x"],), {"recursive": True})]
    assert run(["mv", "/sdcard/a", "/sdcard/b", "-f"], dev)[0] == 0
    assert dev.called("move") == [(("/sdcard/a", "/sdcard/b"), {"overwrite": True})]
    assert run(["cp", "/sdcard/a", "/sdcard/c"], dev)[0] == 0
    assert run(["mkdir", "/sdcard/d", "/sdcard/e"], dev)[0] == 0
    assert len(dev.called("make_dir")) == 2

    info = {"path": "/p", "real_path": "/p", "type": "regular file", "size": 3, "mode": "644"}
    rc, out, _ = run(["stat", "/p", "--json"], FakeDev(stat_path=info))
    assert json.loads(out) == info


def _edit_dev(tmp_file_type="regular file"):
    from pathlib import Path

    def pull(remote, local, **kw):
        Path(local).write_text("a=1\n")

    info = {"path": "/data/x.ini", "real_path": "/data/x.ini", "type": tmp_file_type,
            "size": 4, "mode": "600"}
    return FakeDev(stat_path=info, pull=pull)


def test_edit_pushes_back_only_changes(run, monkeypatch):
    from pathlib import Path

    def editor(cmd):
        Path(cmd[-1]).write_text("a=2\n")
        return 0

    monkeypatch.setattr(cli.subprocess, "call", editor)
    dev = _edit_dev()
    rc, out, _ = run(["edit", "/data/x.ini", "--editor", "myeditor"], dev)
    assert rc == 0 and "Saved /data/x.ini" in out
    (local, remote), _ = dev.called("push")[0]
    assert remote == "/data/x.ini" and not Path(local).exists()  # temp file cleaned up
    assert dev.called("chmod") == [(("/data/x.ini", "600"), {})]

    monkeypatch.setattr(cli.subprocess, "call", lambda cmd: 0)
    dev = _edit_dev()
    rc, out, _ = run(["edit", "/data/x.ini", "--editor", "myeditor"], dev)
    assert rc == 0 and "No changes" in out and not dev.called("push")

    assert run(["edit", "/data"], _edit_dev("directory"))[0] == 1


# --- apps ------------------------------------------------------------------- #
def test_app_flags(run):
    dev = FakeDev(list_packages=["com.a"])
    run(["packages", "--disabled", "--path"], dev)
    kwargs = dev.called("list_packages")[0][1]
    assert kwargs["disabled"] is True and kwargs["include_path"] is True
    run(["install", "a.apk", "--test"], dev)
    assert dev.called("install")[0][1]["allow_test"] is True

    dev = FakeDev(start_activity="Starting: Intent { cmp=com.x/.Main }")
    assert run(["start-activity", "com.x/.Main", "--es", "mode", "demo", "--ez", "debug", "true"], dev)[0] == 0
    assert dev.called("start_activity")[0][1]["extras"] == ["--es", "mode", "demo", "--ez", "debug", "true"]
    dev = FakeDev(start_activity="Error: Activity class {com.x/.Nope} does not exist.")
    assert run(["start-activity", "com.x/.Nope"], dev)[0] == 1
    dev = FakeDev()
    assert run(["grant", "com.x", "android.permission.CAMERA"], dev)[0] == 0
    assert dev.called("grant") == [(("com.x", "android.permission.CAMERA"), {})]


# --- screen and input ------------------------------------------------------- #
def test_record_continuous_saves_every_part(run):
    dev = FakeDev()

    def screen_record(path, **kw):
        assert kw["time_limit"] == 180 and kw["display_id"] == 1
        if len(dev.called("screen_record")) == 2:
            kw["stop_event"].set()
        return path

    dev.returns["screen_record"] = screen_record
    rc, out, _ = run(["record", "clip.mp4", "--continuous", "--display", "1"], dev)
    assert rc == 0 and "Saved clip.mp4" in out and "Saved clip-part02.mp4" in out


def test_displays_and_scrcpy_options(run):
    rc, out, _ = run(["displays"], FakeDev(list_displays=[{"id": 2, "size": "1920x720", "name": "Cluster"}]))
    assert rc == 0 and "1920x720" in out and "Cluster" in out

    dev = FakeDev(mirror=types.SimpleNamespace(pid=7, wait=lambda: 0))
    rc, _, _ = run(["scrcpy", "--compat", "--display-id", "2", "--log", "s.log",
                    "--video-codec", "h264", "--keyboard", "uhid", "--no-stay-awake"], dev)
    (opts,), kwargs = dev.called("mirror")[0]
    assert rc == 0 and kwargs == {"compat": True, "log_path": "s.log"}
    assert (opts.display_id, opts.video_codec, opts.keyboard_mode, opts.stay_awake) == (2, "h264", "uhid", False)


def test_input_commands_with_display(run):
    dev = FakeDev()
    run(["key", "down", "down", "enter", "--display", "1"], dev)
    assert dev.called("keyevents") == [((["down", "down", "enter"],), {"display_id": 1})]
    run(["key", "power", "--longpress"], dev)
    assert dev.called("keyevent") == [(("power",), {"longpress": True, "display_id": None})]
    assert run(["key", "a", "b", "--longpress"], dev)[0] == 2
    run(["tap", "10", "20", "--display", "2"], dev)
    assert dev.called("tap") == [((10, 20), {"display_id": 2})]
    assert run(["tap", "5"], dev)[0] == 2
    run(["swipe", "1", "2", "3", "4", "--ms", "300"], dev)
    assert dev.called("swipe") == [((1, 2, 3, 4, 300), {"display_id": None})]
    run(["text", "--display", "2", "hello", "world"], dev)
    assert dev.called("input_text") == [(("hello world",), {"display_id": 2})]


def test_brightness_modes(run):
    dev = FakeDev(adjust_brightness=100, get_brightness=120)
    assert run(["brightness"], dev)[0] == 2
    run(["brightness", "--step", "-20"], dev)
    assert dev.called("adjust_brightness") == [((-20,), {})]
    assert run(["brightness", "--get"], dev)[1].strip() == "120"


# --- system and forwarding -------------------------------------------------- #
def test_reboot_wait_and_verity_reboot(run):
    dev = FakeDev()
    assert run(["reboot", "--wait"], dev)[0] == 0
    assert dev.called("wait_for_boot") == [((180,), {})]
    dev = FakeDev()
    assert run(["disable-verity", "--reboot"], dev)[0] == 0
    assert dev.called("shell") == [(("sync",), {})] and dev.called("reboot")


def test_forward_list_remove_and_no_wait(run):
    rc, out, _ = run(["forward", "--list", "--json"], FakeDev(list_forwards=["S tcp:1 tcp:2"]))
    assert json.loads(out) == ["S tcp:1 tcp:2"]
    assert run(["forward"])[0] == 2
    dev = FakeDev()
    assert run(["reverse", "--remove", "tcp:3000"], dev)[0] == 0
    assert dev.called("remove_reverse") == [(("tcp:3000",), {})]
    dev = FakeDev(forward="tcp:1 -> tcp:2")
    assert run(["forward", "tcp:1", "tcp:2", "--no-wait"], dev)[0] == 0
    assert dev.called("forward") == [(("tcp:1", "tcp:2"), {})]


# --- sharing and updates ---------------------------------------------------- #
def test_serve_status_and_deploy_ssl(run, monkeypatch):
    import turboadb.devices as devices
    import turboadb.remote_deploy as remote_deploy

    monkeypatch.setattr(devices, "server_is_shared", lambda port=5037, adb_path=None: True)
    rc, out, _ = run(["serve", "--status", "--json"])
    assert rc == 0 and json.loads(out) == {"port": 5037, "shared": True}

    seen = {}
    monkeypatch.setattr(remote_deploy, "deploy_serve", lambda *a, **k: seen.update(k) or 0)
    assert run(["deploy-serve", "pc1", "-u", "D\\u", "-p", "pw", "--ssl"])[0] == 0
    assert seen["use_ssl"] is True and seen["winrm_port"] == 5986


def test_self_update_check(run, monkeypatch):
    import turboadb.update as update

    monkeypatch.setattr(update, "current_version", lambda: "2.0.0")
    monkeypatch.setattr(update, "pypi_latest", lambda *a, **k: "2.1.0")
    rc, out, _ = run(["self-update", "--check"])
    assert rc == 0 and "2.1.0 is available" in out
    rc, out, _ = run(["--json", "self-update", "--check"])
    assert json.loads(out)["update_available"] is True
