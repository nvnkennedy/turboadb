"""Unit tests that exercise the REAL handler/parser code paths (arg building,
result parsing, safe-mode) against a fake adb — the layers that regressed in
1.0.14/1.0.15. Run with:  pytest tests/  (or the whole suite via CI)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from turboadb import ADBHandler, ADBConfig
from turboadb.config import ScrcpyOptions
from turboadb.devices import _parse_line, _parse_mdns_line
from turboadb import toolsdl


# --------------------------------------------------------------------------- #
# adb argv construction
# --------------------------------------------------------------------------- #
def test_serial_scopes_command(fake_adb):
    h = ADBHandler(ADBConfig(serial="emulator-5554"))
    h._run(["shell", "echo", "hi"])
    argv = fake_adb.argv_after_adb()
    assert argv[:3] == ["-s", "emulator-5554", "shell"]


def test_remote_server_flags(fake_adb):
    h = ADBHandler(ADBConfig(serial="dev1", adb_server_host="10.0.0.9", adb_server_port=5037))
    h._run(["get-state"])
    argv = fake_adb.argv_after_adb()
    assert argv[:4] == ["-H", "10.0.0.9", "-P", "5037"]
    assert "-s" in argv and "dev1" in argv


def test_mirror_launches_without_a_blocking_adb_preflight(monkeypatch):
    """Screen launch must hand off to scrcpy immediately, without extra adb IO."""
    import turboadb.scrcpy as scrcpy

    h = ADBHandler(ADBConfig(serial="ready-device"))
    h._adb = "bundled-adb"
    global_calls = []
    h._run = lambda *_args, **_kwargs: pytest.fail("mirror launch must not preflight adb")
    h._run_global = lambda args, **_kwargs: global_calls.append(args)
    marker = object()
    monkeypatch.setattr(scrcpy, "launch_scrcpy", lambda *_args, **_kwargs: marker)

    assert h.mirror(safe=False) is marker
    assert global_calls == []


def test_remote_scrcpy_uses_a_tunnel_port_range(monkeypatch):
    """Concurrent remote mirrors must not all force the same tunnel port."""
    import turboadb.scrcpy as scrcpy

    launched = []

    class Process:
        pid = 123

        @staticmethod
        def poll():
            return 0

    monkeypatch.setattr(scrcpy, "find_scrcpy", lambda _path=None: "scrcpy")
    monkeypatch.setattr(
        scrcpy.subprocess,
        "Popen",
        lambda command, **_kwargs: launched.append(command) or Process(),
    )
    scrcpy.launch_scrcpy(serial="one", adb_server_host="10.0.0.4")
    assert "--port=27184:27199" in launched[0]
    assert not any(arg.startswith("--tunnel-port=") for arg in launched[0])


def test_webcam_parser_accepts_current_ffmpeg_dshow_format():
    from turboadb.gui.ffmpeg_tools import parse_dshow_devices

    listing = '''
[in#0 @ 000001] "HD User Facing" (none)
[in#0 @ 000001]   Alternative name "@device_pnp_\\\\?\\usb#vid"
[in#0 @ 000001] "Microphone Array" (audio)
'''
    assert parse_dshow_devices(listing) == ["HD User Facing"]


def test_global_command_has_no_serial(fake_adb):
    h = ADBHandler(ADBConfig(serial="dev1"))
    h._run_global(["start-server"])
    assert "-s" not in fake_adb.argv_after_adb()


# --------------------------------------------------------------------------- #
# result parsing / exit codes
# --------------------------------------------------------------------------- #
def test_command_result_ok(fake_adb):
    fake_adb.add("shell getprop", stdout="value\n")
    h = ADBHandler(ADBConfig(serial="x"))
    res = h.shell("getprop ro.x")
    assert res.ok and res.text == "value"


def test_command_result_failure(fake_adb):
    fake_adb.add("shell boom", stdout="", returncode=1, stderr="nope")
    h = ADBHandler(ADBConfig(serial="x"))
    res = h.shell("boom")
    assert not res.ok and res.exit_code == 1 and "nope" in res.stderr


def test_package_actions_surface_adb_failures(fake_adb):
    from turboadb.exceptions import ADBCommandError

    fake_adb.add("shell pm list packages", returncode=1, stderr="device offline")
    h = ADBHandler(ADBConfig(serial="x"))
    with pytest.raises(ADBCommandError):
        h.list_packages()

    fake_adb.add("shell pm clear com.example", stdout="Failed\n")
    with pytest.raises(ADBCommandError):
        h.clear_app("com.example")


def test_get_state(fake_adb):
    fake_adb.add("get-state", stdout="device\n")
    h = ADBHandler(ADBConfig(serial="x"))
    assert h.get_state() == "device"
    assert h.is_connected is True


# --------------------------------------------------------------------------- #
# logcat argv (the -T live-only / backlog fix)
# --------------------------------------------------------------------------- #
def _capture_logcat_args(h):
    captured = {}
    h.stream = lambda args, **kw: captured.setdefault("args", list(args))
    return captured


def test_logcat_tail_adds_T(fake_adb):
    h = ADBHandler(ADBConfig(serial="x"))
    cap = _capture_logcat_args(h)
    h.logcat(tail=1)
    assert "-T" in cap["args"] and cap["args"][cap["args"].index("-T") + 1] == "1"


def test_logcat_default_no_tail(fake_adb):
    h = ADBHandler(ADBConfig(serial="x"))
    cap = _capture_logcat_args(h)
    h.logcat()
    assert "-T" not in cap["args"]


def test_logcat_dump_ignores_tail(fake_adb):
    h = ADBHandler(ADBConfig(serial="x"))
    cap = _capture_logcat_args(h)
    h.logcat(dump=True, tail=100)
    assert "-d" in cap["args"] and "-T" not in cap["args"]


def test_logcat_buffers(fake_adb):
    h = ADBHandler(ADBConfig(serial="x"))
    cap = _capture_logcat_args(h)
    h.logcat(buffers=["crash", "main"])
    assert cap["args"].count("-b") == 2


_PNG = b"\x89PNG\r\n\x1a\nrest-of-image"


# --------------------------------------------------------------------------- #
# capture_png method caching (Live View runs this many times/second)
# --------------------------------------------------------------------------- #
def test_capture_png_caches_working_method(fake_adb):
    fake_adb.add("exec-out screencap -p", stdout=_PNG)  # method 1 works
    h = ADBHandler(ADBConfig(serial="x"))
    assert h.capture_png()[:4] == b"\x89PNG"
    assert h._cap_method == 0
    n_after_first = len(fake_adb.calls)
    # second frame: only the cached method is invoked (one call), no re-probe
    assert h.capture_png()[:4] == b"\x89PNG"
    assert len(fake_adb.calls) - n_after_first == 1


def test_capture_png_falls_through_to_file_method(fake_adb):
    # methods 1 & 2 return non-PNG; method 3 (file) works
    fake_adb.add("exec-out screencap -p", stdout=b"not-a-png")
    fake_adb.add("shell screencap -p\b", stdout=b"junk")  # (won't match file form)
    fake_adb.add("exec-out cat", stdout=_PNG)
    h = ADBHandler(ADBConfig(serial="x"))
    assert h.capture_png()[:4] == b"\x89PNG"
    assert h._cap_method == 2


def test_capture_png_raises_when_all_fail(fake_adb):
    fake_adb.add("screencap", stdout=b"secure surface")
    fake_adb.add("exec-out cat", stdout=b"secure surface")
    from turboadb.exceptions import ADBError

    h = ADBHandler(ADBConfig(serial="x"))
    with pytest.raises(ADBError):
        h.capture_png()
    assert h._cap_method is None


# --------------------------------------------------------------------------- #
# device_info / health parsing
# --------------------------------------------------------------------------- #
def test_device_info_parses_props(fake_adb):
    props = (
        "[ro.product.model]: [Pixel 6]\n"
        "[ro.build.version.release]: [14]\n"
        "[ro.build.characteristics]: [automotive]\n"
    )
    fake_adb.add("shell getprop", stdout=props)
    h = ADBHandler(ADBConfig(serial="x"))
    info = h.device_info()
    assert info["model"] == "Pixel 6"
    assert info["android_version"] == "14"
    assert info["automotive"] is True


def test_health_parse_pure():
    out = ADBHandler._parse_health(
        "  level: 87\n  temperature: 351\n  status: 2\n",
        "MemTotal:  3900000 kB\nMemAvailable:  1200000 kB\n",
        "400%cpu  10%user  380%idle",
        "up 3:14",
    )
    assert out["battery_level"] == 87
    assert out["battery_temp_c"] == 35.1
    assert out["battery_status"] == "charging"
    assert out["mem_total_kb"] == 3900000
    assert out["cpu"] == "5%"


def test_health_missing_fields_are_none():
    out = ADBHandler._parse_health("", "", "", "")
    assert out["battery_level"] is None and out["cpu"] is None


def test_build_report_includes_complete_property_dump(fake_adb):
    fake_adb.add(
        "shell getprop",
        stdout=(
            "[ro.product.model]: [Pixel 6]\n"
            "[ro.build.version.release]: [14]\n"
            "[ro.extra.example]: [kept in the full dump]\n"
        ),
    )
    fake_adb.add("shell uname -a", stdout="Linux localhost 6.1.0-test")
    report = ADBHandler(ADBConfig(serial="device-123")).build_report()
    assert "TurboADB build report" in report
    assert "ADB serial: device-123" in report
    assert "[ro.extra.example]: [kept in the full dump]" in report
    assert "Linux localhost 6.1.0-test" in report


def test_health_report_includes_summary_and_raw_dumps(fake_adb):
    fake_adb.add("shell dumpsys battery", stdout="level: 87\ntemperature: 351\nstatus: 2\n")
    fake_adb.add("shell cat /proc/meminfo", stdout="MemTotal: 3900000 kB\nMemAvailable: 1200000 kB\n")
    fake_adb.add("shell top -bn1", stdout="400%cpu  10%user  380%idle\n")
    fake_adb.add("shell uptime", stdout="up 3:14\n")
    report = ADBHandler(ADBConfig(serial="device-123")).health_report()
    assert "TurboADB health report" in report
    assert "Battery: 87% (charging)" in report
    assert "Raw dumpsys battery" in report
    assert "MemAvailable: 1200000 kB" in report


# --------------------------------------------------------------------------- #
# safe mode
# --------------------------------------------------------------------------- #
def test_safe_mode_wraps_errors(fake_adb):
    fake_adb.add("shell fail", returncode=1, stderr="x")
    h = ADBHandler(ADBConfig(serial="x"), safe=True)
    # install returns OperationResult in safe mode and never raises
    fake_adb.add("install", stdout="", returncode=1, stderr="boom")
    res = h.install("/nope/app.apk")
    assert res.success is False and res.error is not None


# --------------------------------------------------------------------------- #
# connectivity toggles: modern fallbacks + honest failure (IVI/Android 12+)
# --------------------------------------------------------------------------- #
def test_set_wifi_falls_back_to_cmd_wifi(fake_adb):
    # classic `svc wifi` refused (removed in Android 12+) -> `cmd wifi` used
    fake_adb.add("svc wifi", stdout="Killed", returncode=1)
    fake_adb.add("cmd wifi set-wifi-enabled", stdout="")
    h = ADBHandler(ADBConfig(serial="x"))
    out = h.set_wifi(True)
    assert out == "ok (cmd wifi)"
    joined = [" ".join(c) for c in fake_adb.calls]
    assert any("cmd wifi set-wifi-enabled enabled" in c for c in joined)


def test_set_bluetooth_reports_blocked_honestly(fake_adb):
    # BOTH methods refused -> an explanation, not a fake "ok"
    fake_adb.add("svc bluetooth", stdout="Killed", returncode=1)
    fake_adb.add("cmd bluetooth_manager", stdout="Exception: not allowed", returncode=1)
    h = ADBHandler(ADBConfig(serial="x"))
    out = h.set_bluetooth(True)
    assert "can't be toggled" in out


def test_set_airplane_settings_fallback(fake_adb):
    fake_adb.add("cmd connectivity airplane-mode", stdout="unknown command", returncode=1)
    fake_adb.add("settings put global airplane_mode_on", stdout="")
    h = ADBHandler(ADBConfig(serial="x"))
    out = h.set_airplane(True)
    assert out == "ok (settings fallback)"
    joined = [" ".join(c) for c in fake_adb.calls]
    assert any("airplane_mode_on 1" in c for c in joined)
    assert any("am broadcast" in c for c in joined)


def test_launch_package_query_activities_fallback(fake_adb):
    """A system/IVI app with NO launcher activity must still launch via the
    query-activities MAIN fallback (step 4)."""
    pkg = "com.oem.hvac"
    # steps 1/1b: resolve-activity yields nothing usable
    fake_adb.add("resolve-activity", stdout="No activity found")
    # step 2: monkey refused (typical on automotive)
    fake_adb.add("monkey", stdout="No activities found to run", returncode=1)
    # step 3: MAIN/LAUNCHER intent fails
    fake_adb.add("android.intent.category.LAUNCHER", stdout="Error: no activities")
    # step 4: query-activities lists a MAIN activity; starting it succeeds
    fake_adb.add("query-activities", stdout=f"{pkg}/.MainActivity\n")
    fake_adb.add(f"-n {pkg}/.MainActivity", stdout="Starting: Intent {...}")
    h = ADBHandler(ADBConfig(serial="x"))
    assert h._launch_package(pkg) is True
    joined = [" ".join(c) for c in fake_adb.calls]
    assert any(f"-n {pkg}/.MainActivity" in c for c in joined)


# --------------------------------------------------------------------------- #
# device / mdns line parsing
# --------------------------------------------------------------------------- #
def test_parse_device_line():
    d = _parse_line("emulator-5554  device product:sdk model:Pixel_6 device:emu transport_id:3")
    assert d.serial == "emulator-5554" and d.is_online and d.model == "Pixel_6"


def test_parse_device_no_permissions_state():
    d = _parse_line("ZX1G22  no permissions; see [http://developer.android.com/tools/device.html]")
    assert d is not None and d.state == "no permissions" and not d.is_online


def test_parse_mdns_connect():
    r = _parse_mdns_line("adb-abc-Xy\t_adb-tls-connect._tcp.\t192.168.1.5:37021")
    assert r["service"] == "connect" and r["port"] == 37021


def test_parse_mdns_ignores_header():
    assert _parse_mdns_line("List of discovered mdns services") is None


# --------------------------------------------------------------------------- #
# toolsdl version comparison (the "never re-download / never downgrade" fix)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "installed,latest,expect",
    [
        ("37.0.0", "37.0.0", False),
        ("36.0.0", "37.0.0", True),
        (None, "37.0.0", True),
        ("37.0.0", None, None),
        ("38.0.0", "37.0.0", False),  # never "upgrade" to older
        ("35.0.2-12147458", "35.0.2", False),  # format-insensitive
        ("2.4", "3.1", True),  # scrcpy two-part
    ],
)
def test_decide(installed, latest, expect):
    assert toolsdl._decide(installed, latest) is expect


def test_vtuple():
    assert toolsdl._vtuple("35.0.2-12147458") == (35, 0, 2)
    assert toolsdl._vtuple("2.4") == (2, 4, 0)


# --------------------------------------------------------------------------- #
# device-keyboard bar mapping (the guaranteed embedded-typing path)
# --------------------------------------------------------------------------- #
def test_device_key_edit_mapping():
    """Qt key events -> ('text', str) or ('key', android_keycode). Pure map, no
    Qt widgets / adb needed — this is what forwards typing to the device
    regardless of window focus."""
    pytest.importorskip("PyQt5")
    from PyQt5.QtCore import Qt
    from turboadb.gui.mirror_panel import _DeviceKeyEdit as K

    assert K.map_event(Qt.Key_A, "a") == ("text", "a")
    assert K.map_event(Qt.Key_5, "5") == ("text", "5")
    assert K.map_event(Qt.Key_Return, "\r") == ("key", 66)  # ENTER
    assert K.map_event(Qt.Key_Backspace, "\b") == ("key", 67)  # DEL
    assert K.map_event(Qt.Key_Left, "") == ("key", 21)  # DPAD_LEFT
    assert K.map_event(Qt.Key_Tab, "\t") == ("key", 61)
    assert K.map_event(Qt.Key_Shift, "") == (None, None)  # modifier alone


class _InlineDispatcher:
    """Runs submitted device commands immediately, in submission order."""

    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        result = fn()
        if on_done:
            on_done(result)
        return True


def test_device_key_pump_batches_a_typing_burst(qapp):
    """Fast text entry should use one ADB request, not one request per key."""
    from turboadb.gui.mirror_panel import _KeyBatcher

    class Handler:
        def __init__(self):
            self.text = []

        def input_text(self, value, safe=False):
            self.text.append((value, safe))
            return True

        def keyevent(self, _code, safe=False):
            raise AssertionError("a text-only burst must not emit a key event")

    handler = Handler()
    pump = _KeyBatcher(handler, _InlineDispatcher())
    for char in "quick":
        pump.push("text", char)
    assert handler.text == []  # still inside the batching window
    pump.flush()
    pump.stop()
    assert handler.text == [("quick", True)]


def test_device_key_pump_preserves_text_before_backspace(qapp):
    from turboadb.gui.mirror_panel import _KeyBatcher

    calls = []

    class Handler:
        def input_text(self, value, safe=False):
            calls.append(("text", value))
            return True

        def keyevent(self, code, safe=False):
            calls.append(("key", code))
            return True

    pump = _KeyBatcher(Handler(), _InlineDispatcher())
    pump.push("text", "typed")
    pump.push("text", " text")
    pump.push("key", 67)  # Backspace
    pump.stop()
    assert calls == [("text", "typed text"), ("key", 67)]


def test_keyevents_sends_a_burst_in_one_input_call(fake_adb):
    """Each `input` starts a JVM on the device, so a burst is one call."""
    from turboadb.results import OperationResult

    h = ADBHandler(ADBConfig(serial="dev1"))
    assert h.keyevents([67, 67, "enter"]) is True
    inputs = [c for c in fake_adb.calls if "input" in c]
    assert len(inputs) == 1
    argv = inputs[0]
    assert argv[argv.index("shell"):] == ["shell", "input", "keyevent", "67", "67", "66"]
    assert argv[argv.index("-s") + 1] == "dev1"

    assert h.keyevents([]) is True
    assert len([c for c in fake_adb.calls if "input" in c]) == 1  # nothing to send

    fake_adb.add("input keyevent", returncode=1, stderr="boom")
    res = h.keyevents([4, 4], safe=True)
    assert isinstance(res, OperationResult) and res.success and res.value is False
    bad = h.keyevents([3], display_id="abc", safe=True)
    assert isinstance(bad, OperationResult) and not bad.success


def test_active_view_record_size_limits_encoder_load():
    pytest.importorskip("PyQt5")
    from turboadb.gui.mirror_panel import _record_size_for_active_view

    displays = [
        {"id": 0, "size": "1080x2400"},
        {"id": 1, "size": "1280x720"},
    ]
    assert _record_size_for_active_view(displays, 0) == "576x1280"
    assert _record_size_for_active_view(displays, 1) is None
    assert _record_size_for_active_view([], 0) is None


# --------------------------------------------------------------------------- #
# scrcpy option translation
# --------------------------------------------------------------------------- #
def test_scrcpy_options_render_driver_uses_equals():
    args = ScrcpyOptions(render_driver="software").to_args()
    assert "--render-driver=software" in args


def test_scrcpy_options_basic():
    args = ScrcpyOptions(max_size=1024, no_audio=True, display_id=2).to_args()
    assert "--max-size" in args and "1024" in args
    assert "--no-audio" in args
    assert "--display-id" in args and "2" in args


def test_scrcpy_audio_options():
    args = ScrcpyOptions(
        audio_source="playback",
        audio_codec="aac",
        audio_bit_rate="192K",
        audio_buffer=60,
        audio_output_buffer=15,
        audio_dup=True,
    ).to_args()
    assert "--audio-source=playback" in args
    assert "--audio-codec" in args and "aac" in args
    assert "--audio-bit-rate" in args and "192K" in args
    assert "--audio-buffer" in args and "60" in args
    assert "--audio-output-buffer" in args and "15" in args
    assert "--audio-dup" in args


# --------------------------------------------------------------------------- #
# audit remediations: binary CommandResult, IPv6, version parsing, CLI pair
# --------------------------------------------------------------------------- #
def test_command_result_binary_stdout():
    from turboadb.results import CommandResult

    res = CommandResult(
        command="cat img.png",
        exit_code=0,
        stdout=b"\x89PNG\r\n\x1a\nline1\nline2\n",
        stderr="",
        duration=0.05,
    )
    assert res.ok
    assert isinstance(res.stdout, bytes)
    assert "PNG" in res.text
    assert len(res.lines) >= 2


def test_config_ipv6_host_and_port():
    cfg1 = ADBConfig(adb_server_host="[::1]:5037")
    assert cfg1.adb_server_host == "::1"
    assert cfg1.adb_server_port == 5037

    cfg2 = ADBConfig(adb_server_host="fe80::1")
    assert cfg2.adb_server_host == "fe80::1"

    cfg3 = ADBConfig(serial="[2001:db8::1]:5555")
    assert cfg3.host == "2001:db8::1"
    assert cfg3.port == 5555
    assert cfg3.target == "[2001:db8::1]:5555"


def test_config_rejects_invalid_ports_and_formats_ipv6():
    from turboadb.config import format_host_port, validate_port

    assert format_host_port("2001:db8::1", 5555) == "[2001:db8::1]:5555"
    for invalid in (0, 65536, "not-a-port", True):
        with pytest.raises(ValueError):
            validate_port(invalid)
    with pytest.raises(ValueError):
        ADBConfig(serial="127.0.0.1:99999")


def test_connect_dialog_preserves_bare_ipv6_host():
    pytest.importorskip("PyQt5")
    from turboadb.gui.connect_dialog import _split_host_port

    assert _split_host_port("[::1]:5037", 5555) == ("::1", 5037)
    assert _split_host_port("2001:db8::1", 5555) == ("2001:db8::1", 5555)


def test_session_identity_prevents_duplicate_device_tabs():
    pytest.importorskip("PyQt5")
    from turboadb.gui.main_window import MainWindow

    live = {"type": "network", "host": "[2001:db8::7]", "port": 5555}
    saved = {"type": "network", "host": "2001:db8::7", "port": "5555"}
    assert MainWindow._session_identity(live) == MainWindow._session_identity(saved)
    assert MainWindow._session_identity({"type": "usb", "serial": "ZX1G22"}) != (
        MainWindow._session_identity({"type": "usb", "serial": "ZX1G23"})
    )


def test_connect_tcp_uses_bracketed_ipv6_target(fake_adb):
    h = ADBHandler(ADBConfig(serial="usb"))
    assert h.connect_tcp("2001:db8::7", 5555) == "[2001:db8::7]:5555"
    assert fake_adb.argv_after_adb()[-2:] == ["connect", "[2001:db8::7]:5555"]


def test_adb_path_attempts_managed_tool_fetch_when_adb_is_missing(monkeypatch, tmp_path):
    import turboadb.core as core
    import turboadb.toolsdl as toolsdl
    from turboadb.exceptions import ADBNotFoundError

    installed = tmp_path / "adb"
    installed.write_text("")
    calls = []

    def find_after_fetch(_path=None):
        calls.append(_path)
        if len(calls) == 1:
            raise ADBNotFoundError("missing")
        return str(installed)

    monkeypatch.setattr(core, "find_adb", find_after_fetch)
    monkeypatch.setattr(toolsdl, "ensure_tools", lambda **kwargs: {"errors": {}})
    h = core.ADBHandler(ADBConfig())
    assert h.adb_path == str(installed)
    assert len(calls) == 2


def test_settings_and_sessions_validate_json_shape_and_write_atomically(monkeypatch, tmp_path):
    import json
    import warnings
    from turboadb.gui import settings
    from turboadb.gui.sessions import SessionStore
    import turboadb.gui.sessions as sessions

    settings_file = tmp_path / "settings.json"
    sessions_file = tmp_path / "sessions.json"
    monkeypatch.setattr(settings, "_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "_FILE", str(settings_file))
    monkeypatch.setattr(sessions, "_DIR", str(tmp_path))
    monkeypatch.setattr(sessions, "_FILE", str(sessions_file))

    settings.save({"theme": "light"})
    assert json.loads(settings_file.read_text())["theme"] == "light"

    store = SessionStore()
    store.save({"name": "bench", "type": "network", "host": "10.0.0.7"})
    assert SessionStore().names() == ["bench"]

    sessions_file.write_text('{"not": "a list"}', encoding="utf-8")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        malformed = SessionStore()
    assert malformed.sessions == [] and malformed.load_error
    assert any("could not load saved targets" in str(w.message) for w in caught)


def test_session_import_is_validated_before_it_changes_saved_targets(monkeypatch, tmp_path):
    import json
    from turboadb.gui import sessions
    from turboadb.gui.sessions import SessionStore

    sessions_file = tmp_path / "sessions.json"
    import_file = tmp_path / "import.json"
    monkeypatch.setattr(sessions, "_DIR", str(tmp_path))
    monkeypatch.setattr(sessions, "_FILE", str(sessions_file))

    store = SessionStore()
    store.save({"name": "good", "type": "network", "host": "10.0.0.7"})
    import_file.write_text(
        json.dumps([
            {"name": "replacement", "type": "usb", "serial": "abc"},
            {"name": "bad", "type": "network", "host": "10.0.0.8", "port": "nope"},
        ]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="entry 2.*port"):
        store.import_from(str(import_file))
    assert store.names() == ["good"]


def test_session_normalizer_rejects_bad_endpoints():
    from turboadb.gui.sessions import normalize_session

    with pytest.raises(ValueError, match="adb_port"):
        normalize_session(
            {"name": "remote", "type": "remote", "adb_host": "lab-pc", "adb_port": 70000}
        )


def test_transfer_honours_a_pre_cancelled_event(fake_adb, monkeypatch, tmp_path):
    import io
    import threading
    import turboadb.core as core
    from turboadb.exceptions import ADBTransferError

    class StoppableProc:
        def __init__(self):
            self.stdout = io.StringIO("")
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    proc = StoppableProc()
    monkeypatch.setattr(core.subprocess, "Popen", lambda *args, **kwargs: proc)
    source = tmp_path / "source.txt"
    source.write_text("x")
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(ADBTransferError, match="cancelled"):
        ADBHandler(ADBConfig(serial="x")).push(str(source), "/sdcard/source.txt", cancel_event=cancelled)
    assert proc.terminated


def test_remote_deploy_rejects_invalid_ports_before_loading_winrm():
    from turboadb.remote_deploy import deploy_serve

    with pytest.raises(ValueError, match="port"):
        deploy_serve(["host"], "DOMAIN\\admin", "secret", port="5037; whoami")


def test_parse_version_lenient():
    from turboadb.tools import parse_version

    assert parse_version("35.0.2-12147458") == (35, 0, 2)
    assert parse_version("1.2") == (1, 2, 0)
    assert parse_version("2.3.4.5") == (2, 3, 4, 5)
    assert parse_version("35.0.2") > parse_version("34.0.5")


def test_cli_pair_validation_without_colon(capsys):
    from turboadb.cli import main

    # pair without colon should return exit code 1 with helpful message, not crash with ValueError
    code = main(["pair", "invalid_host_no_port", "123456"])
    assert code == 1
    captured = capsys.readouterr()
    assert "invalid host:port for pair" in (captured.err + captured.out)


def test_scrcpy_bit_rate_default():
    from turboadb.gui import settings

    assert settings.DEFAULTS["scrcpy_bit_rate"] == "16M"


def test_file_browser_dual_pane(qapp):
    from turboadb.gui.file_browser import FileBrowser, _human_size

    assert _human_size(1024) == "1.0 KB"
    assert _human_size(500) == "500 B"
    fb = FileBrowser(None, start="/sdcard")
    assert fb.cwd == "/sdcard"
    assert fb.remote_cwd == "/sdcard"
    assert os.path.exists(fb.local_cwd)
    assert hasattr(fb, "btn_push") and hasattr(fb, "btn_pull")
    assert hasattr(fb, "local_list") and hasattr(fb, "remote_list")


def test_controls_panel_compact_columns(qapp):
    from turboadb.gui.controls_panel import ControlsPanel

    cp = ControlsPanel(None, compact=True)
    assert cp._compact is True
    assert cp.minimumWidth() == 340


def test_apps_automotive_default_does_not_override_user_choice(qapp):
    from turboadb.gui.apps_panel import AppsPanel

    panel = AppsPanel(None, automotive=False)
    assert panel.third.isChecked()
    panel.set_automotive_default(True)
    assert not panel.third.isChecked()
    panel.third.setChecked(True)  # explicit user choice
    panel.set_automotive_default(True)
    assert panel.third.isChecked()


def test_windows_drives_and_ls_parser():
    pytest.importorskip("PyQt5")
    from turboadb.gui.file_browser import get_windows_drives, FileBrowser

    drives = get_windows_drives()
    assert len(drives) >= 1
    assert any(":" in d or "/" in d for d in drives)

    line = "drwxrwxr-x  4 root sdcard_rw     4096 2026-09-02 21:40 Download"
    parsed = FileBrowser._parse_ls_line(line)
    assert parsed is not None
    name, raw_size, sz_str, ftype, mtime, perms, owner, is_dir = parsed
    assert name == "Download"
    assert is_dir is True
    assert raw_size == 4096
    assert sz_str == "<DIR>"
    assert perms == "drwxrwxr-x"
    assert owner == "root:sdcard_rw"


def test_local_terminal_session():
    from turboadb.gui.local_terminal import LocalShellSession

    sess = LocalShellSession("cmd", serial="device123")
    assert sess.running
    sess.send_line("echo hello")
    sess.close()
    assert not sess.running


def test_file_table_pinning_and_drag_drop():
    import json

    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication, QTableWidgetItem
    from PyQt5.QtCore import Qt

    _ = QApplication.instance() or QApplication(["test"])
    from turboadb.gui.file_browser import _FileTableWidget

    table = _FileTableWidget(is_remote=False)
    table.base_dir = "C:\\test"
    table.insertRow(0)
    table.setItem(0, 0, QTableWidgetItem(".."))
    table.item(0, 0).setData(Qt.UserRole, ("..", True))
    table.insertRow(1)
    table.setItem(1, 0, QTableWidgetItem("z_file.txt"))
    table.item(1, 0).setData(Qt.UserRole, ("z_file.txt", False))
    table.insertRow(2)
    table.setItem(2, 0, QTableWidgetItem("a_file.txt"))
    table.item(2, 0).setData(Qt.UserRole, ("a_file.txt", False))

    # Test sorting pins '..' at row 0 in ascending and descending orders
    for col in range(2):
        for order in (Qt.AscendingOrder, Qt.DescendingOrder):
            table.sortByColumn(col, order)
            assert table.item(0, 0).text().endswith("..")

    # Test mimeData packing for drag-and-drop
    items = [table.item(1, 0), table.item(2, 0)]
    mime = table.mimeData(items)
    assert mime.hasFormat("application/x-turboadb-file")
    payload = json.loads(bytes(mime.data("application/x-turboadb-file")).decode("utf-8"))
    assert payload["is_remote"] is False
    assert any("z_file.txt" in p for p in payload["paths"])


def test_main_window_sidebar_order_and_log_dock():
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication

    _ = QApplication.instance() or QApplication(["test"])
    from turboadb.gui.main_window import MainWindow

    mw = MainWindow()
    try:
        # Check active targets (live_list) is positioned before saved targets (session_list) in layout
        # Each list sits in its own boxed card, Connected above Saved targets.
        assert mw.live_list.parentWidget() is mw._live_card
        assert mw.session_list.parentWidget() is mw._saved_card
        panel_layout = mw._live_card.parentWidget().layout()
        assert panel_layout.indexOf(mw._live_card) < panel_layout.indexOf(mw._saved_card)
        # Hiding the list shows the left-edge bar that brings it back.
        mw._toggle_devices(False)
        assert mw._sidebar_dock.isHidden() and not mw._sidebar_handle.isHidden()
        mw._sidebar_handle.clicked.emit()
        assert not mw._sidebar_dock.isHidden() and mw._sidebar_handle.isHidden()
        mw._toggle_devices()
        assert mw._sidebar_dock.isHidden()
        mw._toggle_devices()

        # Check log dock is hidden by default
        assert mw._log_dock.isHidden()
        mw._show_log_dock()
        assert not mw._log_dock.isHidden()
        # The toast button must invoke the same dock-reveal path, not merely
        # dismiss the notification.
        mw._log_dock.hide()
        from PyQt5.QtWidgets import QPushButton
        from turboadb.gui.fileutil import notification_toast
        toast = notification_toast(
            mw, "Test warning", "diagnostic", action_text="Show log",
            action=mw._show_log_dock, duration_ms=60_000,
        )
        show_log = next(button for button in toast.findChildren(QPushButton)
                        if button.text() == "Show log")
        show_log.click()
        _.processEvents()
        assert not mw._log_dock.isHidden()
    finally:
        mw.close()


def test_svc_mobile_data(fake_adb):
    fake_adb.add("svc data enable", stdout="")
    h = ADBHandler(ADBConfig(serial="x"))
    out = h.set_mobile_data(True)
    assert "ok" in out
    joined = [" ".join(c) for c in fake_adb.calls]
    assert any("svc data enable" in c for c in joined)


def test_shell_panel_subtabs():
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication

    _ = QApplication.instance() or QApplication(["test"])
    from turboadb.gui.device_tab import ShellPanel

    sp = ShellPanel(None, "device123")
    try:
        assert sp.subtabs.count() == 3
        tabs = [sp.subtabs.tabText(i) for i in range(3)]
        assert any("Android" in t for t in tabs)
        assert any("PowerShell" in t for t in tabs)
        assert any("Command Prompt" in t for t in tabs)
        # Every shell exposes visible, mouse-accessible font controls in
        # addition to Ctrl+plus/minus and Ctrl+mouse-wheel zoom.
        for shell in (sp.android_widget, sp.ps_widget, sp.cmd_widget):
            assert shell.btn_font_down.text() == "A−"
            assert shell.btn_font_up.text() == "A+"
    finally:
        sp.close_panel()


def test_reconnect_preserves_local_terminal_and_resets_nested_adb_context(qapp):
    from turboadb.gui.device_tab import ShellPanel

    panel = ShellPanel(None, "device123")
    try:
        panel.subtabs.setCurrentWidget(panel.ps_widget)
        panel.ps_widget._in_adb_shell = True
        panel.cmd_widget._in_adb_shell = True
        called = []
        panel.android_widget.reconnect = lambda **kwargs: called.append(kwargs)

        panel.pause_for_device_reboot()
        assert not panel.ps_widget._in_adb_shell
        assert not panel.cmd_widget._in_adb_shell

        panel.reconnect()
        assert called == [{"focus": False}]
        assert panel.subtabs.currentWidget() is panel.ps_widget
    finally:
        panel.close_panel()
        panel.close()


def test_local_adb_reboot_parser_scopes_the_current_device(qapp):
    from turboadb.gui.device_tab import _LocalShellWidget

    widget = _LocalShellWidget("cmd", serial="device123", handler=None)
    try:
        assert widget._local_adb_reboot_mode(["adb", "reboot"]) == ""
        assert widget._local_adb_reboot_mode(["adb", "reboot", "recovery"]) == "recovery"
        assert widget._local_adb_reboot_mode(["adb", "-s", "other", "reboot"]) is None
    finally:
        widget.close_panel()


def test_file_table_drag_flags():
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication, QTableWidget
    from PyQt5.QtCore import Qt

    _ = QApplication.instance() or QApplication(["test"])
    from turboadb.gui.file_browser import FileBrowser

    fb = FileBrowser(None)
    tbl = QTableWidget(0, 6)
    fb._add_row(tbl, "test.txt", 100, "100 B", "Text", "2026-09-04", "-rw-r--r--", "root", False)
    assert tbl.rowCount() == 1
    it = tbl.item(0, 0)
    assert bool(it.flags() & Qt.ItemIsDragEnabled)
    assert bool(it.flags() & Qt.ItemIsDropEnabled)


def test_local_terminal_non_blocking_read():
    from turboadb.gui.local_terminal import LocalShellSession

    sess = LocalShellSession("cmd", serial="device123")
    try:
        # Non-blocking read must return bytes (or b"") immediately without hanging
        data = sess.read(4096)
        assert isinstance(data, bytes)
    finally:
        sess.close()


def test_controls_panel_card_grid(qapp):
    from turboadb.gui.controls_panel import ControlsPanel

    cp = ControlsPanel(None, compact=True)
    assert hasattr(cp, "_card_grid")
    assert cp._ncols == 1 and len(cp._groups) == 6


def test_console_local_shell_enter_and_pending_echo(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent
    from turboadb.gui.console import AnsiConsole

    sent = []
    con = AnsiConsole(send_fn=lambda b: sent.append(b))
    con.set_emulate_prompt(False)
    # Type 'test'
    for ch in "test":
        con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, ord(ch), Qt.NoModifier, ch))
    assert con._line == "test"
    # Press Enter - typed command stays visible and is sent
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))
    assert con._line == ""
    assert sent == [b"test\r\n"]
    assert con.toPlainText() == "test\n"

    # Shell stdout echoes back command with prompt; feed() strips duplicate echo
    con.feed(b"test\r\nC:\\> ")
    con._drain_tick()
    assert con.toPlainText() == "test\nC:\\> "


def test_console_ignores_stale_async_completion(qapp):
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole()
    con._line = "new command"
    assert not con.apply_completion_result("old command", "old complete", [])
    assert con._line == "new command"


def test_console_cursor_navigation_and_editing(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent
    from turboadb.gui.console import AnsiConsole

    sent = []
    con = AnsiConsole(send_fn=lambda b: sent.append(b))
    con.set_emulate_prompt(True)

    # Type 'hello world'
    for ch in "hello world":
        con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, ord(ch), Qt.NoModifier, ch))
    assert con._line == "hello world"
    assert con._cpos == 11

    # Left arrow 6 times (moves cursor before 'world')
    for _ in range(6):
        con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Left, Qt.NoModifier, ""))
    assert con._cpos == 5

    # Home moves to 0
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Home, Qt.NoModifier, ""))
    assert con._cpos == 0

    # End moves to end
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_End, Qt.NoModifier, ""))
    assert con._cpos == 11

    # Word back (Ctrl+Left)
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Left, Qt.ControlModifier, ""))
    assert con._cpos == 6

    # Kill word before cursor (Ctrl+W) -> removes 'hello '
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_W, Qt.ControlModifier, ""))
    assert con._line == "world"
    assert con._cpos == 0

    # Type 'my ' at start
    for ch in "my ":
        con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, ord(ch), Qt.NoModifier, ch))
    assert con._line == "my world"
    assert con._cpos == 3

    # Delete removes 'w'
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier, ""))
    assert con._line == "my orld"

    # Escape clears line
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier, ""))
    assert con._line == ""
    assert con._cpos == 0


def test_console_standard_clipboard_shortcuts(qapp):
    """Ctrl+C/V/X/A must work before terminal control-key handling."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent, QTextCursor
    from PyQt5.QtWidgets import QApplication
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole(send_fn=lambda _data: None)
    con.set_emulate_prompt(False)
    con._set_line("hello world")

    # Ctrl+X cuts the editable input and puts it on the system clipboard.
    cursor = con.textCursor()
    end = con.document().characterCount() - 1
    cursor.setPosition(end - 5)
    cursor.setPosition(end, QTextCursor.KeepAnchor)
    con.setTextCursor(cursor)
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_X, Qt.ControlModifier, "x"))
    assert QApplication.clipboard().text() == "world"
    assert con._line == "hello "

    # Ctrl+V inserts clipboard text into the synthetic input line.
    QApplication.clipboard().setText("there")
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_V, Qt.ControlModifier, "v"))
    assert con._line == "hello there"

    # Ctrl+A then Ctrl+C copies the complete visible terminal text instead of
    # becoming an ignored control character or sending an interrupt.
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_A, Qt.ControlModifier, "a"))
    con.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_C, Qt.ControlModifier, "c"))
    assert "hello there" in QApplication.clipboard().text()
    con.close_archive()
    con.close()


def test_file_item_numeric_size_sorting(qapp):
    from PyQt5.QtCore import Qt
    from turboadb.gui.file_browser import _FileItem

    item_small = _FileItem("500 B")
    item_small.setData(Qt.UserRole, 500)

    item_large = _FileItem("10 MB")
    item_large.setData(Qt.UserRole, 10 * 1024 * 1024)

    item_med = _FileItem("2 MB")
    item_med.setData(Qt.UserRole, 2 * 1024 * 1024)

    items = [item_large, item_small, item_med]
    items.sort()
    assert items == [item_small, item_med, item_large]


def test_file_browser_robust_ls_regex():
    pytest.importorskip("PyQt5")
    from turboadb.gui.file_browser import FileBrowser

    # ISO date
    iso = "drwxrwx--x  42 root     sdcard_rw     4096 2026-09-02 21:40 Download"
    p1 = FileBrowser._parse_ls_line(iso)
    assert p1 is not None
    assert p1[0] == "Download"
    assert p1[1] == 4096
    assert p1[4] == "2026-09-02 21:40"
    assert p1[6] == "root:sdcard_rw"
    assert p1[7] is True  # is_dir

    # Traditional date
    trad = "-rw-r--r--  1 root     root       1234 Jan 15 12:00 my file.txt"
    p2 = FileBrowser._parse_ls_line(trad)
    assert p2 is not None
    assert p2[0] == "my file.txt"
    assert p2[1] == 1234
    assert p2[4] == "Jan 15 12:00"
    assert p2[7] is False  # is_file


def test_console_osc_filtering_and_csi_clear(qapp):
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole()
    con.set_emulate_prompt(False)
    # Feed OSC window title sequence: should not appear in document
    con.feed(b"\x1b]0;Administrator: Command Prompt\x07Hello World\n")
    con._drain_tick()
    assert "Administrator" not in con.toPlainText()
    assert "Command Prompt" not in con.toPlainText()
    assert "Hello World" in con.toPlainText()

    # Feed CSI 2J to clear
    con.feed(b"\x1b[2J")
    con._drain_tick()
    assert con.toPlainText() == ""


def test_local_shell_widget_lazy_start(qapp):
    from turboadb.gui.device_tab import _LocalShellWidget

    widget = _LocalShellWidget("powershell", serial="test123")
    try:
        # Before focus or ensure_started, session is None (lazy)
        assert widget._started is False
        assert widget.session is None

        # Once ensure_started is called, session starts
        widget.ensure_started()
        assert widget._started is True
        assert widget.session is not None
        assert widget.session.running
    finally:
        widget.close_panel()


def test_local_terminal_font_buttons_change_console_size(qapp):
    from turboadb.gui.device_tab import _LocalShellWidget

    widget = _LocalShellWidget("powershell", serial="test123")
    try:
        before = widget.term.font().pointSize()
        widget.btn_font_up.clicked.emit()
        assert f"font-size: {before + 1}pt" in widget.term.styleSheet()
        assert widget.lbl_font_size.text() == f"{before + 1} pt"
        widget.btn_font_down.clicked.emit()
        assert f"font-size: {before}pt" in widget.term.styleSheet()
        assert widget.lbl_font_size.text() == f"{before} pt"
    finally:
        widget.close_panel()


def test_terminal_font_buttons_resize_existing_ansi_text(qapp, monkeypatch):
    """Existing coloured output must resize, not only the empty-widget style."""
    from PyQt5.QtGui import QTextCursor, QTextFormat
    import turboadb.gui.console as console_mod
    from turboadb.gui.console import AnsiConsole

    # Zoom changes are persisted in production; never let this regression test
    # alter a developer's/user's real preference file.
    monkeypatch.setattr(console_mod.settings_mod, "set_later", lambda *_a, **_k: None)
    console = AnsiConsole()
    try:
        console.feed(b"\x1b[31mexisting coloured output\x1b[0m\n")
        console._drain_tick()
        before = console.font_size()
        console.bump_font(2)
        cursor = QTextCursor(console.document())
        cursor.setPosition(1)
        # Output formats never pin a size, so existing coloured text follows the
        # document font instead of needing a whole-document format rewrite.
        assert not cursor.charFormat().hasProperty(QTextFormat.FontPointSize)
        assert console.document().defaultFont().pointSize() == before + 2
        assert console.font_size() == before + 2
    finally:
        console.close_archive()
        console.close()


def test_file_table_subfolder_drop_targeting(qapp):
    from PyQt5.QtWidgets import QTableWidgetItem
    from PyQt5.QtCore import Qt, QPoint
    from turboadb.gui.file_browser import _FileTableWidget

    tbl = _FileTableWidget(is_remote=True)
    tbl.base_dir = "/sdcard"
    tbl.insertRow(0)
    it_sub = QTableWidgetItem("📁 Download")
    it_sub.setData(Qt.UserRole, ("Download", True))
    tbl.setItem(0, 0, it_sub)

    # itemAt row 0 returns Download subfolder
    pos = tbl.visualItemRect(it_sub).center()
    target_item = tbl.itemAt(pos)
    assert target_item is not None
    data = tbl.item(target_item.row(), 0).data(Qt.UserRole)
    assert data[0] == "Download"
    assert data[1] is True


def test_local_shell_completion_and_cwd_tracking(qapp, tmp_path):
    from turboadb.gui.device_tab import _LocalShellWidget

    widget = _LocalShellWidget("cmd", serial="test123")
    widget._shell_cwd = str(tmp_path)

    # 1. Directory creation
    d1 = tmp_path / "SubFolderA"
    d1.mkdir()
    d2 = tmp_path / "SubFolderB"
    d2.mkdir()
    f1 = tmp_path / "file1.txt"
    f1.write_text("hello")

    # cd <Tab> only returns directories
    _, opts = widget._local_complete("cd ")
    assert "SubFolderA\\" in opts
    assert "SubFolderB\\" in opts
    assert "file1.txt" not in opts

    # cd Sub<Tab> completes or offers subfolders
    _, opts = widget._local_complete("cd SubFolder")
    assert len(opts) == 2

    # Single match completion
    completed, _ = widget._local_complete("cd SubFolderA")
    assert completed == "cd SubFolderA\\"

    # Subcommand completions
    completed, _ = widget._local_complete("adb dev")
    assert completed == "adb devices "

    completed, _ = widget._local_complete("git sta")
    assert completed is None  # ambiguous (status vs stash)

    # CWD tracking on cd
    widget._send(f'cd "{d1}"'.encode("utf-8"))
    assert os.path.normpath(widget._shell_cwd) == os.path.normpath(str(d1))

    # Prompt text reflects current cwd
    prompt = widget._get_prompt()
    assert str(d1) in prompt
    widget.close_panel()
    widget.close()


def test_local_completion_removes_unix_style_leading_slash(qapp, tmp_path):
    """A local PowerShell tab must not turn `/target` into `C:\\target`."""
    if os.name != "nt":
        pytest.skip("Windows path semantics")
    from turboadb.gui.device_tab import _LocalShellWidget

    (tmp_path / "target").mkdir()
    widget = _LocalShellWidget("powershell")
    widget._shell_cwd = str(tmp_path)
    completed, options = widget._local_complete("cd /tar")
    assert completed == "cd target\\"
    assert options == []
    widget.close()


def test_local_terminal_tab_normalizes_slash_path_and_adb_completion(qapp, tmp_path):
    """Tab must preserve a valid local path after showing multiple matches."""
    if os.name != "nt":
        pytest.skip("Windows path semantics")
    from turboadb.gui.device_tab import _LocalShellWidget

    (tmp_path / "target-alpha").mkdir()
    (tmp_path / "target-beta").mkdir()
    widget = _LocalShellWidget("cmd")
    try:
        widget._shell_cwd = str(tmp_path)
        widget.term._set_line("cd /t")
        widget.term._do_complete()
        # The multiple-choice branch used to keep `/t` in the editor.  The
        # following Tab then inserted the candidate after the slash, producing
        # an invalid Windows path instead of cycling valid folders.
        assert widget.term._line == "cd target-"

        # In the UI, typing clears the candidate cycle before starting a new
        # command; mirror that keypress boundary in this direct console test.
        widget.term._clear_tab_cycle()
        widget.term._set_line("adb dev")
        widget.term._do_complete()
        assert widget.term._line == "adb devices "
    finally:
        widget.close_panel()


def test_nested_adb_shell_never_uses_local_path_completion(qapp, tmp_path):
    from turboadb.gui.device_tab import _LocalShellWidget

    class FakeSession:
        running = True

        def __init__(self):
            self.sent = []

        def send(self, data):
            self.sent.append(data)

        def close(self):
            self.running = False

    local_child = tmp_path / "AndroidDirectory"
    local_child.mkdir()
    widget = _LocalShellWidget("powershell")
    widget._started = True
    widget.session = FakeSession()
    widget._shell_cwd = str(tmp_path)

    widget._send(b"adb shell\n")
    assert widget._in_adb_shell
    assert widget.term._completion_fn == widget._adb_shell_complete
    assert widget.session.sent[-1] == b"adb shell -t -t\r\n"

    widget._send(b"cd AndroidDirectory\n")
    assert widget._shell_cwd == str(tmp_path)

    widget._send(b"exit\n")
    assert not widget._in_adb_shell and widget.term._completion_fn is not None
    widget.close_panel()


def test_nested_adb_shell_completes_android_paths(qapp):
    from turboadb.gui.device_tab import _LocalShellWidget
    from turboadb.results import CommandResult, OperationResult

    class FakeHandler:
        def shell(self, command, **_kwargs):
            assert "cd '/'" in command
            assert "'/t'*" in command
            result = CommandResult(command, 0, "tmp/\n", "", 0.0)
            return OperationResult(True, "shell", value=result)

    widget = _LocalShellWidget("cmd", serial="V2318", handler=FakeHandler())
    try:
        widget._in_adb_shell = True
        # The UI-facing provider returns immediately; the worker owns the
        # potentially slow ADB query. Validate its pure query separately.
        completed, options = widget._query_adb_shell_complete("cd /t")
        assert completed == "cd tmp/"
        assert options == []
    finally:
        widget.close_panel()


def test_console_columnize_and_prompt_provider(qapp):
    from turboadb.gui.console import AnsiConsole
    from turboadb.results import strip_ansi

    items = ["devices", "shell", "push", "pull", "install"]
    cols = AnsiConsole._format_columns(items, width=40)
    assert "devices" in cols
    assert "shell" in cols

    console = AnsiConsole()
    console.set_emulate_prompt(False)
    console.set_prompt_provider_fn(lambda: "TEST_PROMPT> ")
    assert console._prompt_provider_fn() == "TEST_PROMPT> "
    assert "📅" not in console._prompt_text()
    assert "adb@" in console._prompt_text()
    assert strip_ansi(AnsiConsole._style_local_prompts("PS C:\\Users\\n> ")) == "PS C:\\Users\\n> "


def test_tools_cache_and_clear():
    from turboadb.tools import find_adb, clear_tools_cache, _CACHE

    clear_tools_cache()
    assert len(_CACHE) == 0
    adb = find_adb()
    assert adb is not None
    assert len(_CACHE) > 0
    clear_tools_cache()
    assert len(_CACHE) == 0


def test_screen_record_display_id_parameter():
    import inspect
    from turboadb.core import ADBHandler

    sig = inspect.signature(ADBHandler.screen_record)
    assert "display_id" in sig.parameters


def test_screen_record_interrupts_only_its_android_process_before_pulling(
    monkeypatch, tmp_path
):
    """Stopping from Windows must signal device-side screenrecord cleanly."""
    import turboadb.core as core
    from turboadb.results import CommandResult

    handler = ADBHandler(ADBConfig(serial="test-device"))
    handler._adb = "adb.exe"
    commands, transfers = [], []

    class Process:
        done = False
        stdout = None

        def poll(self):
            return 0 if self.done else None

        def terminate(self):
            self.done = True

        def wait(self, timeout=None):
            self.done = True
            return 0

        def kill(self):
            self.done = True

    proc = Process()

    def device_run(args, **_kwargs):
        commands.append(args)
        if "kill -INT" in " ".join(args):
            proc.done = True
        return CommandResult(" ".join(args), 0, "", "", 0.0)

    class StopNow:
        @staticmethod
        def is_set():
            return True

    handler._run = device_run
    output = tmp_path / "record.mp4"

    def transfer(*args):
        transfers.append(args)
        output.write_bytes(b"valid video")

    handler._transfer = transfer
    monkeypatch.setattr(core.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(core.time, "sleep", lambda *_: None)

    assert handler.screen_record(str(output), stop_event=StopNow(), safe=False) == str(output)
    joined = [" ".join(command) for command in commands]
    assert any("kill -INT" in command for command in joined)
    assert not any("pidof screenrecord" in command for command in joined)
    assert len(transfers) == 1
    assert transfers[0][0] == "pull"
    assert transfers[0][1].startswith("/sdcard/turboadb_rec_")
    assert transfers[0][2] == str(output)


def test_console_apply_cd_with_flags(qapp):
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole()
    con._cwd = "/sdcard"
    con._apply_cd("cd -P /data/local/tmp")
    assert con._cwd == "/data/local/tmp"

    con._apply_cd("cd -L /system")
    assert con._cwd == "/system"

    con._apply_cd("cd ..")
    assert con._cwd == "/"

    con._apply_cd("cd ~")
    assert con._cwd == "/"


def test_console_paste_clipboard_method(qapp):
    from turboadb.gui.console import AnsiConsole

    con = AnsiConsole()
    assert hasattr(con, "paste_clipboard")
    assert callable(con.paste_clipboard)


def test_is_adb_server_alive_and_socket_devices():
    from turboadb.tools import is_adb_server_alive
    from turboadb.devices import _list_devices_socket, list_devices

    # Check that is_adb_server_alive runs cleanly without crashing
    alive = is_adb_server_alive()
    assert isinstance(alive, bool)

    # Check that socket device query works or falls back cleanly
    sock_devs = _list_devices_socket()
    if alive:
        assert sock_devs is not None
        assert isinstance(sock_devs, list)
    devs = list_devices()
    assert isinstance(devs, list)


def test_adb_socket_reads_partial_protocol_fields():
    """TCP is a byte stream, so ADB's short headers can arrive in fragments."""
    from turboadb.tools import _recv_exact

    class FragmentedSocket:
        def __init__(self, data):
            self.data = bytearray(data)

        def recv(self, size):
            if not self.data:
                return b""
            n = min(1, size, len(self.data))
            out, self.data = bytes(self.data[:n]), self.data[n:]
            return out

    assert _recv_exact(FragmentedSocket(b"OKAY"), 4) == b"OKAY"
    assert _recv_exact(FragmentedSocket(b"OK"), 4) is None


def test_local_device_scan_retries_socket_once_after_startup(monkeypatch):
    """A daemon startup should not turn one device scan into two slow retries."""
    import turboadb.devices as devices
    import turboadb.tools as tools

    observed_timeouts = []
    device = devices.Device(serial="test-serial", state="device")

    def socket_query(host, port, timeout):
        observed_timeouts.append(timeout)
        return None if len(observed_timeouts) == 1 else [device]

    monkeypatch.setattr(devices, "_list_devices_socket", socket_query)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda: False)
    monkeypatch.setattr(tools, "ensure_adb_server", lambda *args, **kwargs: True)

    assert devices.list_devices(timeout=4.0) == [device]
    assert observed_timeouts == [0.25, 0.5]


def test_adb_server_launcher_exit_gets_a_socket_grace_period(monkeypatch):
    """The launcher can return before its detached daemon binds port 5037."""
    import turboadb.tools as tools

    checks = iter((False, False, False, True))

    class ExitedLauncher:
        def poll(self):
            return 0

    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **_: next(checks))
    monkeypatch.setattr(tools, "find_adb", lambda _: "adb.exe")
    monkeypatch.setattr(tools.os.path, "exists", lambda _: True)
    monkeypatch.setattr(tools.subprocess, "Popen", lambda *_, **__: ExitedLauncher())

    assert tools.ensure_adb_server("adb.exe", timeout=1.0) is True


def test_mirror_panel_session_dict_none(qapp):
    from turboadb.gui.mirror_panel import MirrorPanel, _post_close

    class DummyHandler:
        serial = "dummy123"
        config = None

    mp = MirrorPanel(DummyHandler(), session=None)
    assert mp.session_dict is None
    # Verify _post_close is callable and safe
    assert isinstance(_post_close("nonexistent_title_test_xyz"), bool)
