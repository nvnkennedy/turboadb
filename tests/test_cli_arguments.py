"""How the CLI reads what it is given: words that end up typed on a device,
explicit tool paths, serve's ports and adb, options that need each other, the
editor command, and which adb server discovery asks."""
import json
import os
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


# --------------------------------------------------------------------------- #
# words typed on the device
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("argv, option", [
    (["text", "hello", "--display", "2"], "--display"),
    (["text", "secret", "-s", "SERIAL2"], "-s"),
    (["search", "nearest", "charger", "--json"], "--json"),
    (["send-sms", "123", "on", "my", "way", "--timeout=5"], "--timeout"),
])
def test_options_after_the_words_are_refused_not_typed(run, argv, option):
    """`text hello --display 2` typed "hello --display 2" on display 0."""
    dev = FakeDev()
    rc, _out, err = run(argv, dev)
    assert rc == 2 and f"ERROR: {option} after the" in err
    assert not dev.calls  # nothing typed, not even a connect


def test_words_after_a_double_dash_are_sent_as_written(run):
    dev = FakeDev()
    assert run(["text", "--display", "1", "--", "hello", "--display", "2"], dev)[0] == 0
    assert dev.called("input_text") == [(("hello --display 2",), {"display_id": 1})]
    dev = FakeDev()
    assert run(["search", "--", "what", "is", "-s"], dev)[0] == 0
    assert dev.called("web_search") == [(("what is -s",), {})]


# --------------------------------------------------------------------------- #
# explicit tool paths
# --------------------------------------------------------------------------- #
def test_a_mistyped_adb_path_is_reported_before_any_download(run, monkeypatch, tmp_path):
    """The lookup quietly used another adb, after downloading one first."""
    import turboadb.toolsdl as toolsdl

    fetched = []
    monkeypatch.setattr(toolsdl, "ensure_tools", lambda **kw: fetched.append(kw))
    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: False)
    missing = str(tmp_path / "platform-tools" / "adb.exe")
    rc, _out, err = run(["--adb-path", missing, "info"])
    assert rc == 3 and missing in err and not fetched
    rc, _out, err = run(["scrcpy", "--scrcpy-path", str(tmp_path / "scrcpy.exe")])
    assert rc == 3 and "scrcpy" in err and not fetched

    real = tmp_path / ("adb.exe" if os.name == "nt" else "adb")
    real.write_text("")
    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: True)
    assert run(["--adb-path", str(real), "state"], FakeDev(get_state="device"))[0] == 0
    # the folder that holds it is as good as the file
    assert run(["--adb-path", str(tmp_path), "state"], FakeDev(get_state="device"))[0] == 0


# --------------------------------------------------------------------------- #
# serve: one port, and the adb it was started with
# --------------------------------------------------------------------------- #
@pytest.fixture
def serve(monkeypatch):
    seen = {}
    monkeypatch.setattr(devices, "start_shared_server",
                        lambda port=5037, adb_path=None, **k: seen.update(start=(port, adb_path))
                        or "shared")
    monkeypatch.setattr(devices, "open_firewall",
                        lambda ports: seen.update(firewall=list(ports)) or "firewall")
    monkeypatch.setattr(devices, "install_serve_task",
                        lambda port=5037, adb_path=None, **k: seen.update(task=(port, adb_path))
                        or "TurboADBSharedADB")
    monkeypatch.setattr(devices, "install_startup",
                        lambda port=5037, adb_path=None: seen.update(login=(port, adb_path))
                        or "launcher.bat")
    monkeypatch.setattr(devices, "server_is_shared", lambda port=5037, adb_path=None: True)
    return seen


def test_the_auto_starts_pin_the_adb_the_server_was_started_with(run, serve, tmp_path):
    """Left to find their own, the task and the launcher pinned the managed adb
    while the server ran another: the task's first run then restarted it with
    a different version."""
    adb = tmp_path / "sdk" / "adb.exe"
    adb.parent.mkdir()
    adb.write_text("")
    rc, _out, err = run(["serve", "--adb-path", str(adb), "--startup-task", "--install-startup"])
    assert rc == 0, err
    assert serve["start"] == serve["task"] == serve["login"] == (5037, str(adb))


def test_serve_honours_the_shared_adb_port(run, serve):
    assert run(["--adb-port", "5040", "serve"])[0] == 0
    assert serve["start"][0] == 5040 and serve["firewall"][0] == 5040
    rc, out, _ = run(["serve", "--adb-port", "5040", "--status", "--json"])
    assert rc == 0 and json.loads(out) == {"port": 5040, "shared": True}
    assert run(["--adb-port", "5040", "serve", "--port", "5040"])[0] == 0  # the same port
    rc, _out, err = run(["--adb-port", "5040", "serve", "--port", "5041"])
    assert rc == 2 and "different ports" in err


def test_serve_status_reports_the_port_the_server_really_uses(run, serve, monkeypatch):
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5039")
    rc, out, _ = run(["serve", "--status", "--json"])
    assert rc == 0 and json.loads(out)["port"] == 5039


# --------------------------------------------------------------------------- #
# options that need each other, help text, start-activity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cmd", ["health", "build-info"])
def test_output_needs_the_full_report(run, cmd, tmp_path):
    """`health -o FILE` without --full printed the summary and wrote nothing."""
    dev = FakeDev()
    rc, _out, err = run([cmd, "-o", str(tmp_path / "r.txt")], dev)
    assert rc == 2 and "--full" in err and not dev.calls
    assert not (tmp_path / "r.txt").exists()


def test_reboot_help_lists_only_real_modes(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["reboot", "-h"])
    out = capsys.readouterr().out
    assert "{recovery,bootloader,sideload,fastboot}" in out and "None" not in out
    assert cli.build_parser().parse_args(["reboot"]).mode is None


def test_start_activity_trusts_the_engine_not_the_word_error(run):
    """am echoes the intent it was given; a component or URI containing
    'error' made a started activity exit 1."""
    for echo in ("Starting: Intent { cmp=com.acme/.ErrorReportActivity }",
                 "Starting: Intent { dat=https://host/error cmp=com.x/.Main }"):
        rc, out, err = run(["start-activity", "com.acme/.ErrorReportActivity"],
                           FakeDev(start_activity=echo))
        assert rc == 0 and echo in out and not err


# --------------------------------------------------------------------------- #
# discover asks the adb server --connect uses
# --------------------------------------------------------------------------- #
def test_discover_asks_the_selected_adb_server(run, monkeypatch):
    asked = {}
    monkeypatch.setattr(devices, "mdns_devices", lambda adb_path=None, **kw: asked.update(kw) or [
        {"address": "10.1.1.5:40000", "service": "connect", "name": "hu"}])
    dev = FakeDev(connect_tcp="connected to 10.1.1.5:40000")
    rc, _out, _err = run(["--adb-host", "lab-pc", "--adb-port", "5038", "discover", "--connect"], dev)
    assert rc == 0 and (asked["server_host"], asked["server_port"]) == ("lab-pc", 5038)
    assert dev.called("connect_tcp") == [(("10.1.1.5", 40000), {})]


def test_mdns_devices_runs_on_the_given_server(monkeypatch):
    ran = []
    monkeypatch.setattr(devices, "find_adb", lambda explicit=None: "adb")
    monkeypatch.setattr(devices.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(stdout=""))
    devices.mdns_devices("adb", server_host="lab-pc", server_port=5038)
    devices.mdns_devices("adb")
    devices.mdns_devices("adb", server_port=5040)
    assert ran == [["adb", "-H", "lab-pc", "-P", "5038", "mdns", "services"],
                   ["adb", "mdns", "services"],
                   ["adb", "-P", "5040", "mdns", "services"]]


# --------------------------------------------------------------------------- #
# the editor command
# --------------------------------------------------------------------------- #
def test_the_editor_program_is_looked_up_like_a_console_would(monkeypatch, tmp_path):
    found = {"code": "/opt/vscode/bin/code"}
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: found.get(name))
    assert cli._editor_command("code --wait") == ["/opt/vscode/bin/code", "--wait"]
    with pytest.raises(ValueError, match="'subl' not found"):
        cli._editor_command("subl -w")
    exe = tmp_path / "my editor.exe"  # an existing path is used whole, spaces and all
    exe.write_text("")
    assert cli._editor_command(str(exe)) == [str(exe)]


def test_an_editor_that_is_not_there_stops_before_the_pull(run, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    dev = FakeDev()
    rc, _out, err = run(["edit", "/data/x.ini", "--editor", "no-such-editor"], dev)
    assert rc == 1 and "'no-such-editor' not found" in err
    assert not dev.called("edit_file")


@pytest.mark.skipif(os.name != "nt", reason="batch-file editors and PATHEXT are Windows'")
def test_a_batch_file_editor_on_path_runs_with_the_path_quoted(run, monkeypatch, tmp_path):
    """`--editor "code --wait"` failed with WinError 2: CreateProcess doesn't
    find code.cmd by its bare name. cmd.exe runs a batch file and would split
    an unquoted path at '&'."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "myedit.cmd").write_text('@echo edited %~1>> "%~2"\r\n')
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    copy = tmp_path / "a&b copy.ini"
    copy.write_text("before\n")

    def edit_file(path, opener, editor=None):
        code = opener(str(copy))
        return {"changed": "edited" in copy.read_text(), "path": path, "editor_exit": code}

    rc, out, err = run(["edit", "/data/x.ini", "--editor", "myedit --wait"],
                       FakeDev(edit_file=edit_file))
    assert rc == 0 and "Saved /data/x.ini" in out, err
    assert copy.read_text().splitlines()[-1].strip() == "edited --wait"


@pytest.mark.skipif(os.name != "nt", reason="Windows command lines keep quotes in words")
def test_a_quoted_editor_path_loses_its_quotes(monkeypatch, tmp_path):
    folder = tmp_path / "Sublime Text"
    folder.mkdir()
    subl = folder / "subl.exe"
    subl.write_text("")
    assert cli._editor_command(f'"{subl}" -w') == [str(subl), "-w"]


def test_an_executable_editor_gets_an_argument_list(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    assert cli._run_editor(["/usr/bin/vi"], "/tmp/x.ini") == 0
    assert calls == [["/usr/bin/vi", "/tmp/x.ini"]]
