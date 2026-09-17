"""Regression tests for the engine/CLI code-review fixes (section A).

All offline: the ``fake_adb`` fixture (tests/conftest.py) or monkeypatching
stands in for adb, scrcpy, sockets and the network."""

import io
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import types

import pytest

from turboadb import ADBConfig, ADBHandler, ScrcpyOptions
from turboadb.exceptions import (
    ADBCommandError,
    ADBError,
    ADBInstallError,
    ADBTimeoutError,
)
from turboadb.results import OperationResult


def device_words(fake_adb, index=-1):
    """What the DEVICE shell receives: adb joins the words after `shell` with
    spaces and runs them through `sh -c`, which shlex.split imitates."""
    argv = fake_adb.argv_after_adb(index)
    i = argv.index("shell")
    return shlex.split(" ".join(argv[i + 1 :]))


def calls_joined(fake_adb):
    # without the adb path: it lives in a tmp dir named after the test, which
    # would otherwise satisfy substring checks such as "disconnect"
    return [" ".join(c[1:]) for c in fake_adb.calls]


# --------------------------------------------------------------------------- #
# A-H3  caller data is quoted for the device shell
# --------------------------------------------------------------------------- #
def test_start_activity_quotes_data_url_with_ampersand(fake_adb):
    h = ADBHandler(ADBConfig(serial="x"))
    url = "https://example.com/p?a=1&b=2"
    h.start_activity("com.x/.Main", data=url, extras=["--es", "k", "two words"])
    assert device_words(fake_adb) == [
        "am", "start", "-n", "com.x/.Main", "-d", url, "--es", "k", "two words",
    ]


@pytest.mark.parametrize("package", ["com.bad; reboot", "my package", "a'b"])
def test_package_commands_quote_package_names(fake_adb, package):
    fake_adb.add("pm clear", stdout="Success")
    h = ADBHandler(ADBConfig(serial="x"))
    h.stop_app(package)
    assert device_words(fake_adb) == ["am", "force-stop", package]
    h.clear_app(package)
    assert device_words(fake_adb) == ["pm", "clear", package]
    h.grant(package, "android.permission.CAMERA")
    assert device_words(fake_adb) == ["pm", "grant", package, "android.permission.CAMERA"]


def test_list_packages_getprop_media_and_mount_quote_their_arguments(fake_adb):
    h = ADBHandler(ADBConfig(serial="x"))
    h.list_packages(filter_text="a b&c")
    assert device_words(fake_adb) == ["pm", "list", "packages", "a b&c"]
    h.getprop("ro.x;id")
    assert device_words(fake_adb) == ["getprop", "ro.x;id"]
    assert h.media("play;reboot") is True
    assert device_words(fake_adb) == ["cmd", "media_session", "dispatch", "play;reboot"]
    h.mount_rw("/my dir")
    assert device_words(fake_adb) == ["mount", "-o", "remount,rw", "/my dir"]


def test_open_url_send_sms_and_input_text_reach_the_device_intact(fake_adb):
    h = ADBHandler(ADBConfig(serial="x"))
    url = "https://example.com/?q=a&x=it's"
    assert h.open_url(url) is True
    assert device_words(fake_adb) == ["am", "start", "-a", "android.intent.action.VIEW", "-d", url]
    h.send_sms("+123", "hello 'there' & bye")
    words = device_words(fake_adb)
    assert words[-3:] == ["--es", "sms_body", "hello 'there' & bye"]
    h.input_text("it's")
    assert device_words(fake_adb) == ["input", "text", "it's"]


def test_launch_package_quotes_the_grep_pattern(fake_adb):
    fake_adb.add("monkey", returncode=1, stdout="No activities found")
    fake_adb.add("android.intent.category.LAUNCHER", stdout="Error: no activities")
    h = ADBHandler(ADBConfig(serial="x"))
    assert h._launch_package("com.x;id") is False
    query = [c for c in calls_joined(fake_adb) if "query-activities" in c][0]
    assert "grep -F 'com.x;id/'" in query


# --------------------------------------------------------------------------- #
# A-H2  ForwardHandle.close is device scoped and reports failure
# --------------------------------------------------------------------------- #
def test_forward_close_is_scoped_to_the_device_and_reports_failure(fake_adb):
    from turboadb.core import ForwardHandle

    fake_adb.add("forward --remove", returncode=1, stderr="error: listener 'tcp:1' not found")
    h = ADBHandler(ADBConfig(serial="dev1"))
    fh = ForwardHandle(h, "forward", "tcp:1", "tcp:2")
    assert fh.close() is False
    argv = fake_adb.argv_after_adb()
    assert argv[:2] == ["-s", "dev1"] and argv[2:] == ["forward", "--remove", "tcp:1"]


def test_reverse_close_success(fake_adb):
    from turboadb.core import ForwardHandle

    h = ADBHandler(ADBConfig(serial="dev1"))
    fh = ForwardHandle(h, "reverse", "tcp:8000", "tcp:9000")
    assert fh.close() is True
    assert fake_adb.argv_after_adb() == ["-s", "dev1", "reverse", "--remove", "tcp:9000"]
    assert fh.close() is True  # idempotent, no second adb call
    assert len(fake_adb.calls) == 1


# --------------------------------------------------------------------------- #
# A-H4 / A-M20  CLI global flags, --adb-host everywhere, timeout default
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "argv",
    [
        ["--adb-host", "10.0.0.9", "--json", "devices"],
        ["devices", "--adb-host", "10.0.0.9", "--json"],
    ],
)
def test_cli_global_flags_are_not_clobbered_by_subcommands(argv):
    from turboadb.cli import build_parser

    args = build_parser().parse_args(argv)
    assert args.adb_host == "10.0.0.9" and args.json is True


@pytest.mark.parametrize("cmd", [["connect", "1.2.3.4:5555"], ["restart-server"], ["pair", "h:1", "2"],
                                 ["disconnect"], ["discover"], ["serve"]])
def test_cli_server_commands_honour_adb_host(cmd):
    from turboadb.cli import _config, build_parser

    args = build_parser().parse_args(["--adb-host", "lab-pc", "--adb-port", "5038"] + cmd)
    cfg = _config(args, serial=None)
    assert cfg.adb_server_host == "lab-pc" and cfg.adb_server_port == 5038


def test_cli_timeout_default_keeps_the_command_timeout():
    from turboadb.cli import _config, build_parser

    parser = build_parser()
    assert _config(parser.parse_args(["info"])).command_timeout == 60.0
    assert _config(parser.parse_args(["info", "--timeout", "5"])).command_timeout == 5.0


# --------------------------------------------------------------------------- #
# A-M1  socket.timeout is a timeout on every Python version
# --------------------------------------------------------------------------- #
def test_recv_exact_retries_socket_timeout():
    from turboadb.tools import _recv_exact

    class Flaky:
        def __init__(self):
            self.n = 0

        def recv(self, size):
            self.n += 1
            if self.n == 1:
                raise socket.timeout("slow")
            return b"OKAY"[:size]

    assert _recv_exact(Flaky(), 4, retry_timeouts=True) == b"OKAY"
    with pytest.raises((socket.timeout, TimeoutError)):
        _recv_exact(Flaky(), 4)


# --------------------------------------------------------------------------- #
# A-M2  input_text payloads decode back to the original text on Android
# --------------------------------------------------------------------------- #
def _android_input_text(payload):
    """Port of AOSP Input.sendText's escape handling: only %s -> space."""
    buff = list(payload)
    escape = False
    i = 0
    while i < len(buff):
        if escape:
            escape = False
            if buff[i] == "s":
                buff[i] = " "
                del buff[i - 1]
                i -= 1
        if buff[i] == "%":
            escape = True
        i += 1
    return "".join(buff)


@pytest.mark.parametrize(
    "text", ["hello world", "100%", "a%sb", "%s%s", "50%% s", " x ", "p%d %s!", "%"]
)
def test_input_text_chunks_round_trip(text):
    chunks = ADBHandler._input_text_chunks(text)
    assert "".join(_android_input_text(c) for c in chunks) == text
    assert all("\\" not in c for c in chunks)  # no bogus \% escape


# --------------------------------------------------------------------------- #
# A-M3 / A-L7  screenshot capture robustness
# --------------------------------------------------------------------------- #
_SIG = b"\x89PNG\r\n\x1a\n"


def test_capture_shell_keeps_binary_clean_crlf_bytes(fake_adb):
    png = _SIG + b"data\r\nmore\r\n"
    fake_adb.add("exec-out screencap", stdout=b"nope")
    fake_adb.add("shell screencap -p", stdout=png)
    assert ADBHandler(ADBConfig(serial="x")).capture_png() == png


def test_capture_shell_undoes_pty_translation(fake_adb):
    png = _SIG + b"data\nmore"
    fake_adb.add("exec-out screencap", stdout=b"nope")
    fake_adb.add("shell screencap -p", stdout=png.replace(b"\n", b"\r\n"))
    assert ADBHandler(ADBConfig(serial="x")).capture_png() == png


def test_capture_requires_full_png_signature(fake_adb):
    fake_adb.add("screencap", stdout=b"\x89PNGjunkjunk")
    fake_adb.add("exec-out cat", stdout=b"\x89PNGjunkjunk")
    with pytest.raises(ADBError):
        ADBHandler(ADBConfig(serial="x")).capture_png()


def test_capture_method_exception_does_not_abort_fallbacks(fake_adb):
    fake_adb.add("shell screencap -p", stdout=_SIG + b"ok")
    h = ADBHandler(ADBConfig(serial="x"))

    def boom(display_id=None):
        raise ADBTimeoutError("exec-out hung")

    h._cap_exec_out = boom
    assert h.capture_png()[:8] == _SIG


def test_capture_never_silently_swaps_the_requested_display(fake_adb):
    # only a capture WITHOUT -d (i.e. the default display) yields a PNG
    fake_adb.add(lambda argv: "-d" not in argv and "screencap" in argv, stdout=_SIG + b"x")
    h = ADBHandler(ADBConfig(serial="x"))
    with pytest.raises(ADBError):
        h.capture_png(display_id=2)
    screencaps = [c for c in fake_adb.calls if "screencap" in c]
    assert screencaps and all("-d" in c for c in screencaps)
    assert h.capture_png(display_id=0)[:8] == _SIG  # "0" may retry without -d


def test_capture_file_method_uses_unique_remote_paths(fake_adb):
    h = ADBHandler(ADBConfig(serial="x"))
    h._cap_file()
    h._cap_file()
    cats = [c for c in calls_joined(fake_adb) if "exec-out cat" in c]
    assert len(cats) == 2 and cats[0] != cats[1]


# --------------------------------------------------------------------------- #
# A-M4  shortcut: refreshing existing shortcuts is success
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.name != "nt", reason="shortcuts are Windows-only")
def test_create_shortcut_succeeds_when_shortcuts_already_exist(monkeypatch, tmp_path):
    import turboadb.cli as cli

    lnk = tmp_path / "TurboADB.lnk"
    lnk.write_text("")
    monkeypatch.setattr(cli, "ensure_shortcuts", lambda **kw: {})
    monkeypatch.setattr(cli, "_shortcut_paths", lambda name="TurboADB": [("desktop", str(lnk), None)])
    assert cli.create_shortcut([]) == 0


# --------------------------------------------------------------------------- #
# A-M5 / A-L14 / A-M18  CLI exit codes, errors and tool auto-fetch gating
# --------------------------------------------------------------------------- #
class _StubDevice:
    config = ADBConfig(serial="stub")

    def connect(self):
        return self

    def open_url(self, url):
        return False

    def dial(self, number):
        return True


def test_cli_reports_failed_actions_with_a_nonzero_exit(monkeypatch, capsys):
    import turboadb.cli as cli

    monkeypatch.setattr(cli, "adb_available", lambda *a: True)
    monkeypatch.setattr(cli, "_handler", lambda args, **kw: _StubDevice())
    assert cli.main(["open", "example.com"]) == 1
    assert "could not open" in capsys.readouterr().err
    assert cli.main(["dial", "123"]) == 0


def test_cli_value_errors_are_reported_not_tracebacks(monkeypatch, capsys):
    import turboadb.cli as cli

    monkeypatch.setattr(cli, "adb_available", lambda *a: True)
    assert cli.main(["connect", "1.2.3.4:99999"]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_cli_does_not_auto_fetch_when_tools_exist(monkeypatch):
    import turboadb.cli as cli
    import turboadb.toolsdl as toolsdl

    fetched = []
    monkeypatch.setattr(cli, "adb_available", lambda *a: True)
    monkeypatch.setattr(toolsdl, "ensure_tools", lambda **kw: fetched.append(kw))
    assert cli.main(["pair", "no-port", "123"]) == 1
    assert fetched == []


def test_pair_timeout_is_an_adb_timeout(fake_adb, monkeypatch):
    import turboadb.core as core

    def hang(*a, **kw):
        raise subprocess.TimeoutExpired("adb pair", 30)

    monkeypatch.setattr(core.subprocess, "run", hang)
    with pytest.raises(ADBTimeoutError):
        ADBHandler(ADBConfig()).pair("10.0.0.2", 37000, "123456")


def test_upgrade_tools_cli_checks_once_and_fails_on_download_error(monkeypatch):
    import turboadb.cli as cli
    import turboadb.toolsdl as toolsdl

    calls = []

    def checks():
        calls.append(1)
        return {
            "adb": {"installed": "1.0.0", "latest": "2.0.0", "upgrade": True, "path": None,
                    "supported": True},
            "scrcpy": {"installed": "3.0", "latest": "3.0", "upgrade": False, "path": None,
                       "supported": True},
        }

    def broken(**kw):
        raise ADBError("download failed")

    monkeypatch.setattr(toolsdl, "check_updates", checks)
    monkeypatch.setattr(toolsdl, "download_platform_tools", broken)
    assert cli.main(["upgrade-tools"]) == 1
    assert calls == [1]


# --------------------------------------------------------------------------- #
# A-M6 / A-L13  install surfaces stderr; multi-APK keeps --downgrade
# --------------------------------------------------------------------------- #
def test_install_failure_message_includes_stderr(fake_adb, tmp_path):
    fake_adb.add(
        "install",
        stdout="Performing Streamed Install\n",
        returncode=1,
        stderr="adb: failed to install app.apk: Failure [INSTALL_FAILED_VERSION_DOWNGRADE]",
    )
    with pytest.raises(ADBInstallError, match="INSTALL_FAILED_VERSION_DOWNGRADE"):
        ADBHandler(ADBConfig(serial="x")).install(str(tmp_path / "app.apk"))


def test_install_multiple_passes_downgrade(fake_adb):
    fake_adb.add("install-multiple", stdout="Success")
    ADBHandler(ADBConfig(serial="x")).install_multiple(["a.apk", "b.apk"], downgrade=True)
    assert "-d" in fake_adb.argv_after_adb()


# --------------------------------------------------------------------------- #
# A-M7 / A-L1  pull into an existing directory; transfer decoding
# --------------------------------------------------------------------------- #
def test_pull_into_existing_directory_measures_only_the_pulled_file(fake_adb, monkeypatch, tmp_path):
    import turboadb.core as core

    (tmp_path / "unrelated.bin").write_bytes(b"x" * 1000)
    seen = {}

    class Proc:
        def __init__(self, cmd, **kw):
            seen.update(kw)
            (tmp_path / "log.txt").write_bytes(b"12345")
            self.stdout = io.StringIO("")
            self.returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(core.subprocess, "Popen", Proc)
    res = ADBHandler(ADBConfig(serial="x")).pull("/sdcard/log.txt", str(tmp_path))
    assert res.size_bytes == 5 and res.files == 1
    assert seen.get("encoding") == "utf-8" and seen.get("errors") == "replace"


# --------------------------------------------------------------------------- #
# A-M8 / A-L3  root/remount/mount/tcpip failures are failures
# --------------------------------------------------------------------------- #
def test_root_on_production_build_fails(fake_adb):
    fake_adb.add("root", stdout="adbd cannot run as root in production builds\n")
    h = ADBHandler(ADBConfig(serial="x"))
    with pytest.raises(ADBCommandError, match="cannot run as root"):
        h.root()
    res = h.root(safe=True)
    assert isinstance(res, OperationResult) and not res.success


def test_mount_rw_failure_and_tcpip_failure_raise(fake_adb):
    fake_adb.add("remount,rw", stdout="mount: '/' not in /proc/mounts")
    fake_adb.add("tcpip", returncode=1, stderr="error: no devices/emulators found")
    h = ADBHandler(ADBConfig(serial="x"))
    with pytest.raises(ADBCommandError):
        h.mount_rw()
    with pytest.raises(ADBCommandError):
        h.tcpip(5555)


def test_remount_success_message_wins(fake_adb):
    fake_adb.add("remount", stdout="Using overlayfs for /system\nremount succeeded")
    assert "remount succeeded" in ADBHandler(ADBConfig(serial="x")).remount()


# --------------------------------------------------------------------------- #
# A-M9 / A-L18  URL normalisation and the intent echo
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,expected",
    [
        ("tel:12345", "tel:12345"),
        ("geo:0,0?q=cafe", "geo:0,0?q=cafe"),
        ("market://details?id=x", "market://details?id=x"),
        ("mailto:a@b.c", "mailto:a@b.c"),
        ("vnd.youtube:abc", "vnd.youtube:abc"),
        ("example.com", "https://example.com"),
        ("localhost:8080", "https://localhost:8080"),
        ("www.x.com:8080/p", "https://www.x.com:8080/p"),
    ],
)
def test_open_url_normalisation(url, expected):
    assert ADBHandler._normalise_url(url) == expected


def test_open_url_echo_containing_error_is_not_a_failure(fake_adb):
    fake_adb.add(
        "am start",
        stdout="Starting: Intent { act=android.intent.action.VIEW dat=https://x.com/error }",
    )
    h = ADBHandler(ADBConfig(serial="x"))
    assert h.open_url("https://x.com/error") is True
    assert not any("pm list packages" in c for c in calls_joined(fake_adb))


# --------------------------------------------------------------------------- #
# A-M10  safe mode never raises for bad arguments
# --------------------------------------------------------------------------- #
def test_safe_mode_validation_errors_are_wrapped(fake_adb):
    h = ADBHandler(ADBConfig(serial="x"), safe=True)
    for res in (
        h.keyevent("home", display_id="abc"),
        h.input_text("hi", display_id="abc"),
        h.tap(1, 2, display_id="abc"),
        h.swipe(1, 2, 3, 4, display_id="abc"),
        h.set_brightness("bright"),
        h.display_brightness("dim"),
        h.logcat(tail="many"),
    ):
        assert isinstance(res, OperationResult) and not res.success


# --------------------------------------------------------------------------- #
# A-M11  --no-control drops options scrcpy refuses without control
# --------------------------------------------------------------------------- #
def test_no_control_skips_control_only_flags():
    args = ScrcpyOptions(no_control=True, turn_screen_off=True, show_touches=True).to_args()
    assert "--no-control" in args
    assert not {"--stay-awake", "--turn-screen-off", "--show-touches"} & set(args)
    assert "--stay-awake" in ScrcpyOptions().to_args()


# --------------------------------------------------------------------------- #
# A-M12  remote deploy script judges native commands by exit code
# --------------------------------------------------------------------------- #
def test_deploy_script_checks_native_exit_codes():
    from turboadb.remote_deploy import _DEPLOY_PS

    script = _DEPLOY_PS.format(upd="1", port=5037)
    assert "$ErrorActionPreference='Continue'" in script
    assert "$LASTEXITCODE" in script and "--port 5037" in script


# --------------------------------------------------------------------------- #
# A-M13 / A-M14 / A-D12  shared server readiness and frozen builds
# --------------------------------------------------------------------------- #
def _patch_server_start(monkeypatch, alive_seq, poll_value, lan):
    import turboadb.devices as devices
    import turboadb.tools as tools

    seq = iter(alive_seq)
    monkeypatch.setattr(devices, "find_adb", lambda path=None: "adb")
    monkeypatch.setattr(devices.subprocess, "run", lambda *a, **k: None)

    class Proc:
        def poll(self):
            return poll_value

    monkeypatch.setattr(devices.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: next(seq, True))
    monkeypatch.setattr(devices, "_lan_reachable", lambda port, timeout=2.0: lan)
    monkeypatch.setattr(devices, "_list_devices_socket", lambda *a, **k: [])
    return devices


def test_shared_server_rejects_a_localhost_only_answer(monkeypatch):
    devices = _patch_server_start(monkeypatch, [False, True], poll_value=1, lan=False)
    with pytest.raises(RuntimeError, match="NOT shared|LAN"):
        devices.start_shared_server(5037)


def test_shared_server_success(monkeypatch):
    devices = _patch_server_start(monkeypatch, [False, True], poll_value=None, lan=True)
    assert "listening on 0.0.0.0:5037 (0 device(s)" in devices.start_shared_server(5037)


def test_server_is_shared_never_runs_adb_devices(monkeypatch):
    import turboadb.devices as devices
    import turboadb.tools as tools

    monkeypatch.setattr(devices.subprocess, "run", lambda *a, **k: pytest.fail("ran adb"))
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: False)
    assert devices.server_is_shared(5037) is False


def test_frozen_build_refuses_headless_serve_launchers(monkeypatch):
    import turboadb.devices as devices

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(RuntimeError, match="standalone"):
        devices._serve_launcher(5037)


# --------------------------------------------------------------------------- #
# A-M15  only a missing PyQt5 is reported as "needs PyQt5"
# --------------------------------------------------------------------------- #
def test_launch_gui_does_not_hide_other_import_errors(monkeypatch):
    pytest.importorskip("PyQt5")
    import turboadb.cli as cli

    monkeypatch.setattr(cli, "_prewarm_gui_adb_server", lambda: None)
    monkeypatch.setitem(sys.modules, "turboadb.gui.app", types.ModuleType("turboadb.gui.app"))
    with pytest.raises(ImportError):
        cli.launch_gui([])


# --------------------------------------------------------------------------- #
# A-M16 / A-M17  adb lookup precedence, cache key, no environment mutation
# --------------------------------------------------------------------------- #
def test_find_adb_precedence_and_no_env_mutation(monkeypatch, tmp_path):
    import turboadb.tools as tools

    explicit, env, setting = (tmp_path / n for n in ("explicit-adb", "env-adb", "setting-adb"))
    for f in (explicit, env, setting):
        f.write_text("")
    before = (os.environ.get("PATH"), os.environ.get("ADB"))
    tools.clear_tools_cache()
    monkeypatch.setattr(tools, "_gui_setting", lambda name: str(setting))
    monkeypatch.setenv("TURBOADB_ADB", str(env))
    assert tools.find_adb() == str(env)  # env beats the GUI setting
    assert tools.find_adb(str(explicit)) == str(explicit)
    monkeypatch.delenv("TURBOADB_ADB")
    assert tools.find_adb() == str(setting)  # cache key follows the inputs
    assert (os.environ.get("PATH"), os.environ.get("ADB")) == before
    tools.clear_tools_cache()


# --------------------------------------------------------------------------- #
# A-H1 / A-M18 / A-L11  ensure_tools: missing tools only, stamps, thread-safe
# --------------------------------------------------------------------------- #
@pytest.fixture
def fresh_ensure(monkeypatch):
    import turboadb.toolsdl as toolsdl

    monkeypatch.setattr(toolsdl, "_ensured", False)
    monkeypatch.setenv("TURBOADB_AUTO_FETCH", "1")
    monkeypatch.setattr(toolsdl, "_sync_scrcpy_adb", lambda: None)
    monkeypatch.setattr(toolsdl, "upgrade_tools", lambda **kw: pytest.fail("ensure must not upgrade"))
    return toolsdl


def test_ensure_tools_never_upgrades_and_stamps_without_scrcpy_support(fresh_ensure, monkeypatch):
    toolsdl = fresh_ensure
    stamps = []
    monkeypatch.setattr(toolsdl, "_read_stamp", lambda: "0.0.1")  # "after an upgrade"
    monkeypatch.setattr(toolsdl, "managed_adb", lambda: "/managed/adb")
    monkeypatch.setattr(toolsdl, "managed_scrcpy", lambda: None)
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: False)
    monkeypatch.setattr(toolsdl, "fetch_tools", lambda **kw: pytest.fail("nothing is missing"))
    monkeypatch.setattr(toolsdl, "_write_stamp", stamps.append)
    res = toolsdl.ensure_tools()
    assert res["errors"] == {} and stamps


def test_ensure_tools_fetches_only_missing_tools_once_across_threads(fresh_ensure, monkeypatch):
    toolsdl = fresh_ensure
    fetches = []

    def slow_fetch(**kw):
        fetches.append(kw)
        time.sleep(0.2)
        return {"errors": {}}

    monkeypatch.setattr(toolsdl, "managed_adb", lambda: None)
    monkeypatch.setattr(toolsdl, "managed_scrcpy", lambda: None)
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: False)
    monkeypatch.setattr(toolsdl, "fetch_tools", slow_fetch)
    threads = [threading.Thread(target=toolsdl.ensure_tools) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(fetches) == 1
    assert fetches[0]["adb"] is True and fetches[0]["scrcpy"] is False


def test_tools_dir_does_not_create_directories(monkeypatch, tmp_path):
    import turboadb.toolsdl as toolsdl

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    d = toolsdl.tools_dir()
    assert d.startswith(str(tmp_path)) and not os.path.exists(d)


# --------------------------------------------------------------------------- #
# A-M19  get_state reads problem states from stderr
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "stderr,state",
    [
        ("error: device unauthorized.\nThis adb server's $ADB_VENDOR_KEYS is not set", "unauthorized"),
        ("error: device offline", "offline"),
        ("error: device 'x' not found", "unknown"),
    ],
)
def test_get_state_from_stderr(fake_adb, stderr, state):
    fake_adb.add("get-state", returncode=1, stderr=stderr)
    assert ADBHandler(ADBConfig(serial="x")).get_state() == state


# --------------------------------------------------------------------------- #
# A-M21 / A-L25  list_devices timeouts and non-default local port
# --------------------------------------------------------------------------- #
def _no_socket(monkeypatch):
    import turboadb.devices as devices
    import turboadb.tools as tools

    monkeypatch.setattr(devices, "_list_devices_socket", lambda *a, **k: None)
    monkeypatch.setattr(tools, "ensure_adb_server", lambda *a, **k: True)
    return devices


def test_list_devices_timeout_is_not_a_raw_timeoutexpired(fake_adb, monkeypatch):
    devices = _no_socket(monkeypatch)

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("adb devices", 3)

    monkeypatch.setattr(devices.subprocess, "run", hang)
    assert devices.list_devices() == []
    with pytest.raises(ConnectionError):
        devices.list_devices(strict=True)


def test_list_devices_uses_non_default_local_port(fake_adb, monkeypatch):
    devices = _no_socket(monkeypatch)
    devices.list_devices(server_port=5038)
    argv = fake_adb.argv_after_adb()
    assert "-H" not in argv and argv[:2] == ["-P", "5038"]


def test_handler_base_uses_local_non_default_port():
    h = ADBHandler(ADBConfig(serial="x", adb_server_port=5038))
    h._adb = "adb"
    assert h._base() == ["adb", "-P", "5038", "-s", "x"]


def test_config_strips_server_host():
    assert ADBConfig(adb_server_host="  lab-pc  ").adb_server_host == "lab-pc"
    assert ADBConfig(adb_server_host="   ").adb_server_host is None


# --------------------------------------------------------------------------- #
# A-M22  a "remote" server that is this PC uses the local server for scrcpy
# --------------------------------------------------------------------------- #
def test_scrcpy_local_host_gets_no_remote_env(monkeypatch):
    import turboadb.scrcpy as scrcpy

    captured = {}

    class Out:
        stdout, stderr, returncode = "", "--display-id=0 (1920x1080)", 0

    def fake_run(cmd, **kw):
        captured["cmd"], captured["env"] = cmd, kw.get("env")
        return Out()

    monkeypatch.setattr(scrcpy, "find_scrcpy", lambda path=None: "scrcpy")
    monkeypatch.setattr(scrcpy, "is_local_host", lambda host: True)
    monkeypatch.setattr(scrcpy.subprocess, "run", fake_run)
    assert scrcpy.list_displays("s", adb_server_host="192.168.1.5") == [{"id": 0, "size": "1920x1080"}]
    assert not any(a.startswith("--tunnel-host") for a in captured["cmd"])
    assert not (captured["env"] or {}).get("ANDROID_ADB_SERVER_ADDRESS")


# --------------------------------------------------------------------------- #
# A-M23 / A-M24  telephony URIs and content-provider rows
# --------------------------------------------------------------------------- #
def test_dial_percent_encodes_hash(fake_adb):
    ADBHandler(ADBConfig(serial="x")).dial("*#06#")
    assert device_words(fake_adb)[-1] == "tel:*%2306%23"


def test_sms_rows_are_records_not_lines(fake_adb):
    fake_adb.add(
        "content query",
        stdout=(
            "Row: 0 type=1, date=3, address=+1, body=hello\nworld, again\n"
            "Row: 1 type=2, date=2, address=+2, body=hi\n"
            "Row: 2 type=1, date=1, address=+3, body=x\n"
        ),
    )
    rows = ADBHandler(ADBConfig(serial="x")).sms_list(limit=2)
    assert len(rows) == 2 and rows[0]["body"] == "hello\nworld, again"
    assert "head" not in " ".join(fake_adb.argv_after_adb())


def test_content_provider_security_exception_is_raised(fake_adb):
    fake_adb.add(
        "content query",
        stderr="Error while accessing provider:call_log\njava.lang.SecurityException: Permission Denial",
    )
    with pytest.raises(ADBCommandError):
        ADBHandler(ADBConfig(serial="x")).call_log()


# --------------------------------------------------------------------------- #
# A-L2 / A-L20  screen size and device IP selection
# --------------------------------------------------------------------------- #
def test_screen_size_uses_current_rotated_size(fake_adb):
    fake_adb.add(
        "dumpsys window displays",
        stdout="Display: mDisplayId=0\n  init=1080x1920 440dpi cur=1920x1080 app=1920x1000\n",
    )
    assert ADBHandler(ADBConfig(serial="x"))._screen_size() == (1920, 1080)


def test_screen_size_prefers_override_size(fake_adb):
    fake_adb.add("wm size", stdout="Physical size: 1080x1920\nOverride size: 720x1280\n")
    assert ADBHandler(ADBConfig(serial="x"))._screen_size() == (720, 1280)


def test_device_ip_prefers_wifi_over_cellular(fake_adb):
    fake_adb.add(
        "ip route",
        stdout=(
            "10.0.0.0/8 dev rmnet_data0 proto kernel scope link src 10.1.2.3\n"
            "192.168.1.0/24 dev wlan0 proto kernel scope link src 192.168.1.7\n"
        ),
    )
    assert ADBHandler(ADBConfig(serial="x")).device_ip() == "192.168.1.7"


# --------------------------------------------------------------------------- #
# A-L4  airplane fallback is honest when the broadcast is refused
# --------------------------------------------------------------------------- #
def test_airplane_fallback_reports_refused_broadcast(fake_adb):
    fake_adb.add("cmd connectivity airplane-mode", stdout="unknown command", returncode=1)
    fake_adb.add("am broadcast", stdout="Security exception: Permission Denial: not allowed")
    out = ADBHandler(ADBConfig(serial="x")).set_airplane(True)
    assert out != "ok (settings fallback)" and "refused" in out


# --------------------------------------------------------------------------- #
# A-L5  mirror never mutates the caller's options; kwargs apply
# --------------------------------------------------------------------------- #
def test_mirror_copies_options(monkeypatch):
    import turboadb.scrcpy as scrcpy

    got = {}
    monkeypatch.setattr(scrcpy, "launch_scrcpy", lambda serial, opts, **kw: got.setdefault("o", opts))
    h = ADBHandler(ADBConfig(serial="x"))
    h._adb = "adb"
    mine = ScrcpyOptions(extra_args=["--x"])
    h.mirror(mine, compat=True, max_size=800)
    assert got["o"].max_size == 800 and got["o"].video_codec == "h264" and got["o"].no_audio
    assert mine.max_size is None and mine.video_codec is None and not mine.no_audio
    assert got["o"].extra_args is not mine.extra_args


# --------------------------------------------------------------------------- #
# A-L6  forwards are scoped to this device
# --------------------------------------------------------------------------- #
def test_remove_all_forwards_only_touches_this_device(fake_adb):
    fake_adb.add("forward --list", stdout="dev1 tcp:1 tcp:2\ndev2 tcp:3 tcp:4\n")
    h = ADBHandler(ADBConfig(serial="dev1"))
    assert h.list_forwards() == ["dev1 tcp:1 tcp:2"]
    assert h.remove_all_forwards() is True
    removals = [c for c in calls_joined(fake_adb) if "--remove" in c]
    assert len(removals) == 1 and removals[0].endswith("forward --remove tcp:1")


# --------------------------------------------------------------------------- #
# A-L8 / A-D2  context manager only disconnects what it connected
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reply,expect_disconnect", [
    ("already connected to 1.2.3.4:5555", False),
    ("connected to 1.2.3.4:5555", True),
])
def test_exit_disconnects_only_owned_connections(fake_adb, monkeypatch, reply, expect_disconnect):
    import turboadb.core as core

    monkeypatch.setattr(core, "is_adb_server_alive", lambda **kw: True)
    fake_adb.add(lambda argv: "connect" in argv, stdout=reply)
    fake_adb.add("get-state", stdout="device")
    with ADBHandler(ADBConfig(host="1.2.3.4")):
        pass
    assert any("disconnect" in c for c in calls_joined(fake_adb)) is expect_disconnect


# --------------------------------------------------------------------------- #
# A-L9 / A-L10  ANSI OSC stripping; started_at is the start time
# --------------------------------------------------------------------------- #
def test_strip_ansi_removes_osc_sequences():
    from turboadb.results import strip_ansi

    assert strip_ansi("\x1b]0;my title\x07hello") == "hello"
    assert strip_ansi("\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\") == "link"
    assert strip_ansi("\x1b[1;32mok\x1b[0m") == "ok"


def test_command_result_started_at_is_the_start(fake_adb, monkeypatch):
    real_run = fake_adb.run

    def slow_run(cmd, **kw):
        time.sleep(0.05)
        return real_run(cmd, **kw)

    import turboadb.core as core

    monkeypatch.setattr(core.subprocess, "run", slow_run)
    h = ADBHandler(ADBConfig(serial="x"))
    h._adb = "adb"  # no tool lookup between t0 and the command start
    t0 = time.time()
    res = h._run(["get-state"])
    assert t0 <= res.started_at <= t0 + 0.04
    assert res.started_at + res.duration >= t0 + 0.05


# --------------------------------------------------------------------------- #
# A-L15 / A-D10  relaunch uses the running interpreter
# --------------------------------------------------------------------------- #
def test_relaunch_uses_current_environment():
    from turboadb import update
    from turboadb.tools import windowless_python

    assert update._relaunch_cmd() == [windowless_python(), "-m", "turboadb", "gui"]


# --------------------------------------------------------------------------- #
# A-L16  the library attaches no handlers; quiet suppresses INFO
# --------------------------------------------------------------------------- #
def test_handler_logger_has_no_handlers_and_quiet_is_respected(caplog):
    h = ADBHandler(ADBConfig(serial="log-test-serial"), quiet=True)
    assert h.log.handlers == []
    with caplog.at_level("INFO", logger="turboadb"):
        h._emit(20, "info-line")
        h._emit(30, "warning-line")
    text = caplog.text
    assert "warning-line" in text and "info-line" not in text


# --------------------------------------------------------------------------- #
# A-L17  go_wireless retries the connect instead of a fixed sleep
# --------------------------------------------------------------------------- #
def test_go_wireless_retries_connect(fake_adb, monkeypatch):
    import turboadb.core as core

    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    state = {"n": 0}

    def first_connect(argv):
        if "connect" not in argv:
            return False
        state["n"] += 1
        return state["n"] == 1

    fake_adb.add("ip route", stdout="192.168.1.0/24 dev wlan0 scope link src 192.168.1.7\n")
    fake_adb.add("tcpip", stdout="restarting in TCP mode port: 5555")
    fake_adb.add(first_connect, stdout="failed to connect to 192.168.1.7:5555: Connection refused")
    fake_adb.add("connect 192.168.1.7", stdout="connected to 192.168.1.7:5555")
    assert ADBHandler(ADBConfig(serial="usb")).go_wireless() == "192.168.1.7:5555"
    assert sum(1 for c in fake_adb.calls if "connect" in c) == 2


# --------------------------------------------------------------------------- #
# A-L19  get_brightness has no made-up default
# --------------------------------------------------------------------------- #
def test_get_brightness_raises_when_unreadable(fake_adb):
    fake_adb.add("screen_brightness", stdout="null")
    h = ADBHandler(ADBConfig(serial="x"))
    with pytest.raises(ADBError):
        h.get_brightness()
    assert h.get_brightness(safe=True).success is False


# --------------------------------------------------------------------------- #
# A-L21  the name-hint fallback tries only a few packages
# --------------------------------------------------------------------------- #
def test_open_app_hint_fallback_is_bounded(fake_adb, monkeypatch):
    h = ADBHandler(ADBConfig(serial="x"))
    fake_adb.add("am start", stdout="Error: Activity not started, unable to resolve Intent")
    pkgs = [f"com.vendor{i}.camera" for i in range(10)]
    monkeypatch.setattr(h, "list_packages", lambda **kw: pkgs)
    tried = []
    monkeypatch.setattr(h, "_launch_package", lambda p: tried.append(p) or False)
    assert h.open_camera() is False
    assert len(tried) == 3


# --------------------------------------------------------------------------- #
# A-L22  screen_record clamps the limit and cleans up an interrupted recording
# --------------------------------------------------------------------------- #
def _record_harness(monkeypatch, tmp_path, done):
    import turboadb.core as core
    from turboadb.results import CommandResult

    h = ADBHandler(ADBConfig(serial="dev"))
    h._adb = "adb"
    commands, popen_cmds = [], []

    class Proc:
        stdout = None

        def __init__(self):
            self.done = done

        def poll(self):
            return 0 if self.done else None

        def terminate(self):
            self.done = True

        def wait(self, timeout=None):
            self.done = True
            return 0

        def kill(self):
            self.done = True

    h._run = lambda args, **kw: commands.append(args) or CommandResult(" ".join(args), 0, "", "", 0.0)
    out = tmp_path / "rec.mp4"
    h._transfer = lambda *a: out.write_bytes(b"video")
    monkeypatch.setattr(core.subprocess, "Popen", lambda cmd, **kw: popen_cmds.append(cmd) or Proc())
    return h, commands, popen_cmds, out


def test_screen_record_clamps_time_limit(monkeypatch, tmp_path):
    h, _commands, popen_cmds, out = _record_harness(monkeypatch, tmp_path, done=True)
    assert h.screen_record(str(out), time_limit=500) == str(out)
    assert "--time-limit 180" in popen_cmds[0][-1]


def test_interrupted_screen_record_removes_the_partial_device_file(monkeypatch, tmp_path):
    import turboadb.core as core

    h, commands, _popen, out = _record_harness(monkeypatch, tmp_path, done=False)

    def interrupt(_s):
        raise KeyboardInterrupt

    monkeypatch.setattr(core.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        h.screen_record(str(out), time_limit=10)
    last = " ".join(commands[-1])
    assert "rm" in last and "turboadb_rec_" in last and "turboadb_screenrecord_" in last


# --------------------------------------------------------------------------- #
# A-L23  ShellSession.send reports a dead pipe
# --------------------------------------------------------------------------- #
def test_shell_session_send_reports_broken_pipe():
    from turboadb.core import ShellSession

    class Stdin:
        def write(self, data):
            raise BrokenPipeError

        def flush(self):
            pass

    proc = types.SimpleNamespace(stdin=Stdin(), stdout=None, poll=lambda: 0)
    session = ShellSession(proc)
    assert session.send("ls\n") is False and session.at_eof is True


# --------------------------------------------------------------------------- #
# A-L26  release: a failed build restores the version strings
# --------------------------------------------------------------------------- #
def test_release_restores_version_when_build_fails(monkeypatch, tmp_path):
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "release.py")
    spec = importlib.util.spec_from_file_location("turboadb_release_under_test", path)
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)

    pyproject = tmp_path / "pyproject.toml"
    init = tmp_path / "__init__.py"
    pyproject.write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    init.write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "PYPROJECT", pyproject)
    monkeypatch.setattr(release, "INIT", init)

    def failing_run(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(release, "run", failing_run)
    with pytest.raises(subprocess.CalledProcessError):
        release.main(["patch", "--skip-tests"])
    assert '"1.2.3"' in pyproject.read_text(encoding="utf-8")
    assert '"1.2.3"' in init.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# A-L27  `scrcpy -s ip:port` connects first, without poking private attrs
# --------------------------------------------------------------------------- #
def test_cli_scrcpy_connects_network_target(fake_adb, monkeypatch):
    import turboadb.cli as cli

    monkeypatch.setattr(cli, "adb_available", lambda *a: True)
    monkeypatch.setattr(cli, "scrcpy_available", lambda *a: True)
    fake_adb.add(lambda argv: "connect" in argv, stdout="connected to 10.0.0.5:5555")
    monkeypatch.setattr(ADBHandler, "mirror", lambda self, opts: types.SimpleNamespace(pid=42))
    assert cli.main(["-s", "10.0.0.5:5555", "scrcpy"]) == 0
    assert any(c[-2:] == ["connect", "10.0.0.5:5555"] for c in fake_adb.calls)


# --------------------------------------------------------------------------- #
# A-D3  one host:port parser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [
        ("10.0.0.1:5555", ("10.0.0.1", 5555)),
        ("[::1]:5037", ("::1", 5037)),
        ("[fe80::1]", ("fe80::1", None)),
        ("fe80::1", ("fe80::1", None)),
        ("lab-pc", ("lab-pc", None)),
        ("host:abc", ("host:abc", None)),
        ("  h:1  ", ("h", 1)),
    ],
)
def test_parse_host_port(value, expected):
    from turboadb.config import parse_host_port

    assert parse_host_port(value) == expected


def test_device_is_network_and_mdns_use_the_shared_parser():
    from turboadb.devices import Device, _parse_mdns_line

    assert Device("[fe80::1]:5555", "device").is_network
    assert not Device("emulator-5554", "device").is_network
    assert _parse_mdns_line("adb-x\t_adb-tls-connect._tcp\t[fe80::1]:37000")["host"] == "fe80::1"


# --------------------------------------------------------------------------- #
# A-D11  restart_server + tunnel firewall range constant
# --------------------------------------------------------------------------- #
def test_restart_server_kills_then_starts_globally(fake_adb, monkeypatch):
    import turboadb.core as core

    monkeypatch.setattr(core, "is_adb_server_alive", lambda **kw: False)
    h = ADBHandler(ADBConfig(serial="dev1"))
    res = h.restart_server()
    assert res.ok
    assert [c[1:] for c in fake_adb.calls] == [["kill-server"], ["start-server"]]


def test_restart_server_failure(fake_adb, monkeypatch):
    import turboadb.core as core

    monkeypatch.setattr(core, "is_adb_server_alive", lambda **kw: False)
    fake_adb.add("start-server", returncode=1, stderr="could not bind to 5037")
    h = ADBHandler(ADBConfig())
    with pytest.raises(ADBCommandError, match="could not bind"):
        h.restart_server()
    assert h.restart_server(safe=True).success is False


def test_tunnel_firewall_range_constant():
    import inspect

    from turboadb import devices
    from turboadb.scrcpy import TUNNEL_PORT_FIREWALL_RANGE, TUNNEL_PORT_RANGE

    assert TUNNEL_PORT_FIREWALL_RANGE == "27184-27199" and TUNNEL_PORT_RANGE == "27184:27199"
    assert inspect.signature(devices.open_firewall).parameters["ports"].default == (
        5037,
        TUNNEL_PORT_FIREWALL_RANGE,
    )


# --------------------------------------------------------------------------- #
# Redundant: shell_many / get_serialno follow the safe-mode convention
# --------------------------------------------------------------------------- #
def test_shell_many_honours_kwargs_and_safe_mode(fake_adb):
    fake_adb.add("-c false", returncode=1)
    h = ADBHandler(ADBConfig(serial="x"))
    results = h.shell_many(["echo a", "false", "echo b"], su=True)
    assert len(results) == 2 and "su -c" in " ".join(fake_adb.calls[0])
    with pytest.raises(TypeError):
        h.shell_many(["echo a"], bogus=1)
    wrapped = h.shell_many(["echo a"], bogus=1, safe=True)
    assert isinstance(wrapped, OperationResult) and not wrapped.success
    assert h.get_serialno(safe=True).success is True


# --------------------------------------------------------------------------- #
# B-1 / B-2  the SYSTEM serve task pins ITS adb and proves the server came up
# --------------------------------------------------------------------------- #
def _fake_schtasks(monkeypatch, devices, *, query_out=b""):
    """Record every schtasks/adb argv the installer runs; everything succeeds."""
    calls = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        out = query_out if "/query" in cmd else b""
        return types.SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    monkeypatch.setattr(devices.subprocess, "run", run)
    return calls


def test_serve_task_pins_the_adb_the_installing_user_resolved(monkeypatch):
    import turboadb.devices as devices
    import turboadb.tools as tools

    monkeypatch.setattr(devices.os, "name", "nt")
    monkeypatch.setattr(devices, "find_adb", lambda p=None: r"C:\Users\me\.turboadb\tools\adb.exe")
    monkeypatch.setattr(devices, "windowless_python", lambda: r"C:\Py\pythonw.exe")
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: True)
    calls = _fake_schtasks(monkeypatch, devices)

    assert devices.install_serve_task(port=5037) == devices._SERVE_TASK
    create = next(c for c in calls if "/create" in c)
    command = create[create.index("/tr") + 1]
    # SYSTEM has no ~/.turboadb/tools and no GUI settings: without this the task
    # resolves a DIFFERENT adb and binds 5037 with it.
    assert r'--adb-path "C:\Users\me\.turboadb\tools\adb.exe"' in command
    assert "serve --port 5037" in command


def test_serve_task_fails_when_the_shared_server_never_binds(monkeypatch):
    import turboadb.devices as devices
    import turboadb.tools as tools

    monkeypatch.setattr(devices.os, "name", "nt")
    monkeypatch.setattr(devices, "find_adb", lambda p=None: "adb")
    monkeypatch.setattr(devices, "windowless_python", lambda: "pythonw")
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: False)
    _fake_schtasks(monkeypatch, devices, query_out=b"Last Result: 0x1\r\n")

    # `schtasks /run` returning 0 only means the task LAUNCHED.
    with pytest.raises(RuntimeError, match="no adb server is listening"):
        devices.install_serve_task(port=5037, ready_timeout=0.05)


def test_serve_task_reports_the_schedulers_last_result(monkeypatch):
    import turboadb.devices as devices
    import turboadb.tools as tools

    monkeypatch.setattr(devices.os, "name", "nt")
    monkeypatch.setattr(devices, "find_adb", lambda p=None: "adb")
    monkeypatch.setattr(devices, "windowless_python", lambda: "pythonw")
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: False)
    _fake_schtasks(monkeypatch, devices, query_out=b"Last Result:  267011\r\n")

    with pytest.raises(RuntimeError, match="267011"):
        devices.install_serve_task(ready_timeout=0.05)


# --------------------------------------------------------------------------- #
# B-3  "Stop sharing" works when the shared server is already stopped
# --------------------------------------------------------------------------- #
def test_stop_shared_server_tolerates_a_kill_server_that_had_nothing_to_kill(monkeypatch):
    import turboadb.devices as devices
    import turboadb.tools as tools

    monkeypatch.setattr(devices, "find_adb", lambda p=None: "adb")
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: True)

    def run(cmd, **kw):
        if "kill-server" in cmd:  # several adb builds do this when idle
            return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"server not running")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(devices.subprocess, "run", run)
    assert "not running" in devices.stop_shared_server()


def test_stop_shared_server_still_fails_when_the_local_server_does_not_return(monkeypatch):
    import turboadb.devices as devices
    import turboadb.tools as tools

    monkeypatch.setattr(devices, "find_adb", lambda p=None: "adb")
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: False)
    monkeypatch.setattr(
        devices.subprocess,
        "run",
        lambda cmd, **kw: types.SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"cannot bind 5037"
        ),
    )
    with pytest.raises(RuntimeError, match="cannot bind"):
        devices.stop_shared_server()


# --------------------------------------------------------------------------- #
# B-4  the login .bat is written the way cmd.exe reads it; no Startup, no lie
# --------------------------------------------------------------------------- #
def test_startup_dir_fails_loudly_without_appdata(monkeypatch):
    import turboadb.devices as devices

    monkeypatch.delenv("APPDATA", raising=False)
    with pytest.raises(RuntimeError, match="APPDATA"):
        devices._startup_dir()
    monkeypatch.setattr(devices.os, "name", "nt")
    assert devices.uninstall_startup() is False  # never reports a phantom removal


@pytest.mark.skipif(os.name != "nt", reason="the login launcher is Windows-only")
def test_login_launcher_is_encoded_for_cmd_not_utf8(monkeypatch, tmp_path):
    import turboadb.devices as devices

    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(devices, "find_adb", lambda p=None: None)
    monkeypatch.setattr(devices, "windowless_python", lambda: "C:\\Pyth\u00f6n\\pythonw.exe")
    bat = devices.install_startup(port=5037)
    with open(bat, "rb") as fh:
        raw = fh.read()
    # cmd.exe never reads a .bat as UTF-8, so the bytes must decode in the
    # console codepage — a profile with an accent used to break the launcher.
    text = raw.decode(devices._batch_encoding())
    assert "serve --port 5037" in text
    assert b"\xc3\xb6" not in raw  # the UTF-8 encoding of the accent
    assert devices.uninstall_startup() is True


# --------------------------------------------------------------------------- #
# B-5  platform-tools is verified against Google's published checksum
# --------------------------------------------------------------------------- #
_PT_XML = """<sdk:sdk-repository xmlns:sdk="http://schemas.android.com/repository">
 <remotePackage path="platform-tools">
  <revision><major>35</major><minor>0</minor><micro>2</micro></revision>
  <archives>
   <archive><complete><size>11</size><checksum type="sha1">aabb</checksum>
    <url>platform-tools_r35.0.2-windows.zip</url></complete>
    <host-os>windows</host-os></archive>
   <archive><complete><size>22</size><checksum type="sha1">ccdd</checksum>
    <url>platform-tools_r35.0.2-linux.zip</url></complete>
    <host-os>linux</host-os></archive>
  </archives>
 </remotePackage>
</sdk:sdk-repository>"""


def test_manifest_gives_the_version_and_the_matching_archive():
    import turboadb.toolsdl as toolsdl

    win = toolsdl._parse_platform_tools_manifest(_PT_XML, "windows")
    assert win["version"] == "35.0.2" and win["size"] == 11 and win["checksum"] == "aabb"
    assert win["url"] == toolsdl.PLATFORM_TOOLS_BASE + "platform-tools_r35.0.2-windows.zip"
    assert toolsdl._parse_platform_tools_manifest(_PT_XML, "linux")["checksum"] == "ccdd"
    # an unknown schema must still yield the version the update check needs
    loose = '<x path="platform-tools"><revision><major>34</major><minor>1</minor>'
    loose += "<micro>0</micro></revision>"
    assert toolsdl._parse_platform_tools_manifest(loose, "windows") == {"version": "34.1.0"}


def test_platform_tools_download_refuses_a_mismatching_checksum(monkeypatch, tmp_path):
    import hashlib
    import zipfile

    import turboadb.toolsdl as toolsdl

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    src = tmp_path / "pt.zip"
    with zipfile.ZipFile(src, "w") as archive_file:
        archive_file.writestr("platform-tools/" + toolsdl._exe("adb"), "not really adb")
    payload = src.read_bytes()

    def fake_download(url, dest, on_progress=None):
        with open(dest, "wb") as fh:
            fh.write(payload)

    def archive(checksum):
        return {
            "version": "35.0.2",
            "url": "https://dl.example/platform-tools_r35.0.2.zip",
            "size": len(payload),
            "checksum": checksum,
            "checksum_type": "sha1",
        }

    monkeypatch.setattr(toolsdl, "_download", fake_download)
    monkeypatch.setattr(toolsdl, "_sync_scrcpy_adb", lambda: None)
    monkeypatch.setattr(toolsdl, "latest_adb_archive", lambda: archive("0" * 40))
    with pytest.raises(ADBError, match="SHA1 mismatch"):
        toolsdl.download_platform_tools(force=True)
    assert toolsdl.managed_adb() is None  # nothing was swapped in

    monkeypatch.setattr(
        toolsdl, "latest_adb_archive", lambda: archive(hashlib.sha1(payload).hexdigest())
    )
    assert toolsdl.download_platform_tools(force=True) == toolsdl.managed_adb()


def test_a_published_size_alone_is_checked_and_the_gap_is_logged(tmp_path, caplog):
    import logging

    import turboadb.toolsdl as toolsdl

    blob = tmp_path / "pt.zip"
    blob.write_bytes(b"0123456789")
    with pytest.raises(ADBError, match="manifest publishes"):
        toolsdl._verify_platform_tools(str(blob), {"size": 99})
    with caplog.at_level(logging.WARNING, logger="turboadb.toolsdl"):
        toolsdl._verify_platform_tools(str(blob), {"size": 10, "version": "35.0.2"})
    assert "no checksum" in caplog.text


# --------------------------------------------------------------------------- #
# B-6  a stale cached release body is not an answer to "what is the latest?"
# --------------------------------------------------------------------------- #
def test_only_a_200_or_304_settles_the_latest_scrcpy_version(monkeypatch, tmp_path):
    import json as json_mod
    import urllib.error

    import turboadb.toolsdl as toolsdl

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    toolsdl._ensure_tools_dir()
    with open(toolsdl._release_cache_path(), "w", encoding="utf-8") as fh:
        json_mod.dump({"etag": 'W/"x"', "data": {"tag_name": "v3.0"}}, fh)

    def offline(req, timeout=0):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(toolsdl.urllib.request, "urlopen", offline)
    data, authoritative = toolsdl._github_release()
    assert data["tag_name"] == "v3.0" and authoritative is False
    assert toolsdl.latest_scrcpy_version() is None  # "unknown", not "up to date"

    def not_modified(req, timeout=0):
        raise urllib.error.HTTPError("u", 304, "Not Modified", {}, None)

    monkeypatch.setattr(toolsdl.urllib.request, "urlopen", not_modified)
    assert toolsdl._github_release()[1] is True
    assert toolsdl.latest_scrcpy_version() == "3.0"


# --------------------------------------------------------------------------- #
# B-7  a missing MANAGED copy is "install", not "up to date"
# --------------------------------------------------------------------------- #
def test_check_updates_reports_a_missing_managed_copy_and_the_path_one_apart(monkeypatch):
    import turboadb.toolsdl as toolsdl

    monkeypatch.setattr(toolsdl, "managed_adb", lambda: None)
    monkeypatch.setattr(toolsdl, "managed_scrcpy", lambda: None)
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: True)
    monkeypatch.setattr(toolsdl, "latest_adb_version", lambda: "35.0.2")
    monkeypatch.setattr(toolsdl, "latest_scrcpy_version", lambda: "3.1")
    monkeypatch.setattr(toolsdl, "installed_adb_version", lambda p=None: "34.0.0")
    monkeypatch.setattr(toolsdl, "installed_scrcpy_version", lambda p=None: "2.7")

    checks = toolsdl.check_updates()
    # the PATH adb used to be reported as "installed", so the cache stayed empty
    # and gui_adb_path() kept returning None
    assert checks["adb"]["installed"] is None and checks["adb"]["upgrade"] is True
    assert checks["adb"]["system"] == "34.0.0" and checks["adb"]["path"] is None
    assert checks["scrcpy"]["installed"] is None and checks["scrcpy"]["system"] == "2.7"


# --------------------------------------------------------------------------- #
# B-8  the stamp actually gates the auto-fetch it was written for
# --------------------------------------------------------------------------- #
def test_a_matching_stamp_skips_the_whole_tool_check(fresh_ensure, monkeypatch):
    toolsdl = fresh_ensure
    monkeypatch.setattr(toolsdl, "_pkg_version", lambda: "9.9.9")
    monkeypatch.setattr(toolsdl, "_read_stamp", lambda: "9.9.9")
    monkeypatch.setattr(toolsdl, "managed_adb", lambda: "/managed/adb")
    monkeypatch.setattr(toolsdl, "managed_scrcpy", lambda: "/managed/scrcpy")
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: True)
    monkeypatch.setattr(toolsdl, "fetch_tools", lambda **kw: pytest.fail("the stamp must skip this"))
    monkeypatch.setattr(toolsdl, "_write_stamp", lambda v: pytest.fail("nothing to re-stamp"))
    assert toolsdl.ensure_tools()["note"] == "stamped"


def test_a_stamp_does_not_cover_a_tool_that_was_deleted(fresh_ensure, monkeypatch):
    toolsdl = fresh_ensure
    fetched = []
    monkeypatch.setattr(toolsdl, "_pkg_version", lambda: "9.9.9")
    monkeypatch.setattr(toolsdl, "_read_stamp", lambda: "9.9.9")
    monkeypatch.setattr(toolsdl, "managed_adb", lambda: "/managed/adb")
    monkeypatch.setattr(toolsdl, "managed_scrcpy", lambda: None)
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: True)
    monkeypatch.setattr(toolsdl, "_write_stamp", lambda v: None)
    monkeypatch.setattr(toolsdl, "fetch_tools", lambda **kw: fetched.append(kw) or {"errors": {}})
    toolsdl.ensure_tools()
    assert fetched and fetched[0]["scrcpy"] is True


# --------------------------------------------------------------------------- #
# B-9  the upgrade's tool refresh and version report run in the NEW code
# --------------------------------------------------------------------------- #
def test_run_upgrade_never_drives_the_new_downloader_from_the_old_package(monkeypatch):
    import turboadb.toolsdl as toolsdl
    import turboadb.update as update

    monkeypatch.setattr(update, "can_self_update", lambda: True)
    monkeypatch.setattr(
        update.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        toolsdl, "upgrade_tools", lambda **kw: pytest.fail("the OLD package must not upgrade tools")
    )
    monkeypatch.setattr(
        update,
        "_fresh_report",
        lambda *a, **k: {
            "version": "9.9.9",
            "adb": "35.0.2",
            "scrcpy": "3.1",
            "errors": {"scrcpy": "boom"},
        },
    )
    res = update.run_upgrade(notify=lambda m: None)
    assert res["ok"] and res["new"] == "9.9.9"
    assert res["adb"] == "35.0.2" and res["scrcpy"] == "3.1"
    assert res["tools_errors"] == {"scrcpy": "boom"}


def test_the_post_upgrade_report_is_valid_code_run_in_another_interpreter(monkeypatch):
    import turboadb.update as update

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        return types.SimpleNamespace(
            returncode=0,
            stdout='chatter\n' + update._REPORT_MARK + '{"version": "2.0.0"}\n',
            stderr="",
        )

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    assert update._fresh_report()["version"] == "2.0.0"
    assert seen["cmd"][0] == sys.executable and seen["cmd"][1] == "-c"
    compile(seen["cmd"][2], "<post-upgrade>", "exec")  # the generated source is valid


def test_relaunch_flushes_settings_and_only_then_starts_the_replacement(monkeypatch):
    import turboadb.update as update

    order = []
    monkeypatch.setattr(update, "_flush_user_state", lambda: order.append("flush"))
    monkeypatch.setattr(update, "_spawn", lambda: order.append("spawn") or True)
    assert update.relaunch() is True and order == ["flush", "spawn"]


def test_relaunch_on_exit_starts_nothing_while_this_instance_still_runs(monkeypatch):
    import turboadb.update as update

    spawned = []
    monkeypatch.setattr(update, "_flush_user_state", lambda: None)
    monkeypatch.setattr(update, "_spawn", lambda: spawned.append(1) or True)
    monkeypatch.setattr(update, "_relaunch_scheduled", False)
    try:
        # the safe contract: no second process exists while dialogs are up
        assert update.relaunch_on_exit() is True and spawned == []
        update._relaunch_at_exit()  # what interpreter shutdown will run
        assert spawned == [1]
    finally:
        update.cancel_relaunch()


# --------------------------------------------------------------------------- #
# B-10  ONE ~/.turboadb resolver, consulted on every call
# --------------------------------------------------------------------------- #
def test_every_user_state_path_follows_the_same_resolver(monkeypatch, tmp_path):
    from turboadb import config, tools, update
    from turboadb.gui import sessions, settings

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = config.user_dir()
    assert root.startswith(str(tmp_path))
    # frozen-at-import paths made one process read tools and settings from two
    # different home directories
    for path in (
        tools.managed_tools_dir(),
        settings.settings_file(),
        sessions.sessions_file(),
        update.check_cache_path(),
    ):
        assert path.startswith(root), path


def test_an_explicit_settings_path_override_still_wins(monkeypatch, tmp_path):
    from turboadb.gui import sessions, settings

    monkeypatch.setattr(settings, "_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(sessions, "_FILE", str(tmp_path / "t.json"))
    assert settings.settings_file() == str(tmp_path / "s.json")
    assert sessions.sessions_file() == str(tmp_path / "t.json")


# --------------------------------------------------------------------------- #
# B-11  a long-lived store must not delete targets added behind its back
# --------------------------------------------------------------------------- #
def test_saving_merges_targets_another_process_added(monkeypatch, tmp_path):
    from turboadb.gui import sessions

    monkeypatch.setattr(sessions, "_FILE", str(tmp_path / "sessions.json"))
    gui = sessions.SessionStore()  # the long-running window
    gui.save({"name": "bench", "type": "usb", "serial": "a"})

    cli = sessions.SessionStore()  # the CLI / a second window
    cli.save({"name": "lab", "type": "network", "host": "10.0.0.8"})

    gui.save({"name": "bench", "type": "usb", "serial": "c"})  # from a stale snapshot
    assert sorted(sessions.SessionStore().names()) == ["bench", "lab"]
    assert sessions.SessionStore().get("bench")["serial"] == "c"

    gui.delete("bench")  # an explicit delete still sticks
    assert sessions.SessionStore().names() == ["lab"]


def test_renaming_does_not_resurrect_the_old_name(monkeypatch, tmp_path):
    from turboadb.gui import sessions

    monkeypatch.setattr(sessions, "_FILE", str(tmp_path / "sessions.json"))
    store = sessions.SessionStore()
    store.save({"name": "bench", "type": "usb", "serial": "a"})
    store.save({"name": "lab", "type": "usb", "serial": "a", "previous_name": "bench"})
    assert sessions.SessionStore().names() == ["lab"]


# --------------------------------------------------------------------------- #
# B-12  settings are serialised across PROCESSES, not just threads
# --------------------------------------------------------------------------- #
def test_settings_lock_is_exclusive_across_processes_and_reentrant_within_one(tmp_path):
    from turboadb.gui import settings

    path = str(tmp_path / "settings.json")
    lock = os.path.abspath(path) + ".lock"
    with settings._file_lock(path):
        with settings._file_lock(path):  # update() -> save() takes it twice
            # a second handle stands in for the second TurboADB process
            assert settings._lock_acquire(lock, 0.05) is None
    handle = settings._lock_acquire(lock, 1.0)
    assert handle is not None  # released again afterwards
    settings._lock_release(handle)


def test_settings_writes_still_work_when_the_lock_cannot_be_taken(monkeypatch, tmp_path):
    from turboadb.gui import settings

    monkeypatch.setattr(settings, "_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_lock_acquire", lambda path, timeout: None)
    settings.update({"theme": "light"})
    assert settings.get("theme") == "light"


# --------------------------------------------------------------------------- #
# B-13 / B-14  a bounded, parallel deploy, and a password that stays out of argv
# --------------------------------------------------------------------------- #
def _deploy_env(monkeypatch, worker):
    import turboadb.remote_deploy as rd

    monkeypatch.setattr(rd, "_ensure_winrm", lambda say=None: True)
    monkeypatch.setattr(rd, "_deploy_one", worker)
    return rd


def test_unreachable_hosts_cannot_outlast_the_total_budget(monkeypatch):
    def slow(host, *a, **kw):
        time.sleep(1.0)
        return True, [f"[OK] {host}"]

    rd = _deploy_env(monkeypatch, slow)
    said = []
    rc = rd.deploy_serve(
        ["a", "b", "c"], "D\\u", "pw", on_status=said.append, total_timeout=0.2, max_workers=1
    )
    assert rc == 1
    skipped = [line for line in said if "Skipped" in line]
    assert skipped and "a, b, c" in skipped[0]


def test_independent_hosts_are_contacted_in_parallel_and_reported_in_order(monkeypatch):
    def slow(host, *a, **kw):
        time.sleep(0.3)
        return True, [f"[OK] {host}"]

    rd = _deploy_env(monkeypatch, slow)
    said = []
    started = time.monotonic()
    rc = rd.deploy_serve(
        ["a", "b", "c", "d"], "D\\u", "pw", on_status=said.append, max_workers=4
    )
    elapsed = time.monotonic() - started
    assert rc == 0 and elapsed < 1.0  # serial would need >= 1.2 s
    assert [line for line in said if line.startswith("[OK]")] == [
        "[OK] a", "[OK] b", "[OK] c", "[OK] d",
    ]


def test_one_failing_host_does_not_abort_the_others(monkeypatch):
    def worker(host, *a, **kw):
        if host == "b":
            return False, [f"[ERROR] {host}: nope"]
        return True, [f"[OK] {host}"]

    rd = _deploy_env(monkeypatch, worker)
    said = []
    assert rd.deploy_serve(["a", "b", "c"], "D\\u", "pw", on_status=said.append) == 1
    assert "[OK] c" in said


def test_the_winrm_password_can_avoid_argv_entirely(monkeypatch):
    import turboadb.remote_deploy as rd

    monkeypatch.setenv(rd.PASSWORD_ENV, "from-env")
    assert rd.resolve_password() == "from-env"
    assert rd.resolve_password("explicit") == "explicit"
    monkeypatch.setattr(rd.sys, "stdin", io.StringIO("from-stdin\n"))
    assert rd.resolve_password(from_stdin=True) == "from-stdin"
    monkeypatch.delenv(rd.PASSWORD_ENV)
    with pytest.raises(ValueError, match=rd.PASSWORD_ENV):
        rd.resolve_password(prompt=False)


# --------------------------------------------------------------------------- #
# B-15  packaging metadata matches what the code actually supports
# --------------------------------------------------------------------------- #
def _repo_file(*parts):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_packaging_declares_every_supported_os_and_extra():
    text = _repo_file("pyproject.toml")
    for classifier in (
        "Operating System :: Microsoft :: Windows",
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS :: MacOS X",
    ):
        assert classifier in text, classifier
    extras = text.split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
    all_line = next(line for line in extras.splitlines() if line.startswith("all"))
    for dep in ("PyQt5", "pywinrm", "keyring", "argcomplete"):
        assert dep in all_line, dep


def test_the_sdist_ships_the_reference_docs():
    manifest = _repo_file("MANIFEST.in")
    for doc in ("CLI.md", "CHANGELOG.md", "ARCHITECTURE.md", "LICENSE"):
        assert f"include {doc}" in manifest, doc


# --------------------------------------------------------------------------- #
# B-16 / B-17 / B-19  CI really runs the GUI suite; releases are gated
# --------------------------------------------------------------------------- #
def test_ci_has_a_job_that_installs_qt_and_runs_the_gui_tests():
    ci = _repo_file(".github", "workflows", "ci.yml")
    assert "QT_QPA_PLATFORM: offscreen" in ci
    assert '".[all,test]"' in ci and "libxkbcommon-x11-0" in ci and "libxcb-cursor0" in ci
    assert 'python-version: ["3.8", "3.11", "3.13"]' in ci  # the plain job stays


def test_the_release_workflow_gates_on_version_tests_and_artifacts():
    rel = _repo_file(".github", "workflows", "release.yml")
    assert "turboadb.__version__" in rel  # the tag must match
    assert "python -m pytest tests/ -q" in rel
    assert "twine check" in rel and "wheel_has_exe" in rel
    assert "TURBOADB_SELFTEST: winrm" in rel
    assert "fail_on_unmatched_files: true" in rel


def test_conftest_fixes_the_qt_platform_for_the_whole_process():
    conftest = _repo_file("tests", "conftest.py")
    assert 'os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")' in conftest
    assert os.environ.get("QT_QPA_PLATFORM") == "offscreen"


# --------------------------------------------------------------------------- #
# B-18  the release script rebuilds a stale exe and never leaves a phantom bump
# --------------------------------------------------------------------------- #
def _release_under_test():
    import importlib.util

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "release.py"
    )
    spec = importlib.util.spec_from_file_location("turboadb_release_staleness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_exe_older_than_its_sources_is_rebuilt(tmp_path, monkeypatch):
    release = _release_under_test()
    monkeypatch.setattr(release, "ROOT", tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "turboadb").mkdir()
    exe = tmp_path / "dist" / "TurboADB-9.9.9-win64.exe"
    exe.write_bytes(b"MZ-old")
    source = tmp_path / "turboadb" / "core.py"
    source.write_text("# edited after the build\n", encoding="utf-8")
    os.utime(exe, (1_000_000, 1_000_000))
    os.utime(source, (2_000_000, 2_000_000))
    assert release.exe_is_stale(exe) is True

    built = []

    def fake_run(cmd, **kw):
        built.append(cmd)
        exe.write_bytes(b"MZ-new")

    monkeypatch.setattr(release, "run", fake_run)
    target = release.bundle_exe("9.9.9")
    assert built and target.read_bytes() == b"MZ-new"

    os.utime(exe, (3_000_000, 3_000_000))  # newer than every source again
    assert release.exe_is_stale(exe) is False


def test_a_failed_upload_rolls_the_version_back(monkeypatch, tmp_path):
    release = _release_under_test()
    pyproject = tmp_path / "pyproject.toml"
    init = tmp_path / "__init__.py"
    pyproject.write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    init.write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    (tmp_path / "dist").mkdir()
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "PYPROJECT", pyproject)
    monkeypatch.setattr(release, "INIT", init)
    monkeypatch.setattr(release, "bundle_exe", lambda version, rebuild=False: None)
    monkeypatch.setattr(release, "wheel_has_exe", lambda wheel: True)
    monkeypatch.setenv("TWINE_PASSWORD", "pypi-token")

    def fake_run(cmd, **kw):
        if "upload" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        if "build" in cmd:
            (tmp_path / "dist" / "turboadb-1.2.4-py3-none-any.whl").write_bytes(b"")

    monkeypatch.setattr(release, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        release.main(["patch", "--skip-tests"])
    # a bump with nothing published makes the next `patch` skip a number
    assert '"1.2.3"' in pyproject.read_text(encoding="utf-8")
    assert '"1.2.3"' in init.read_text(encoding="utf-8")
