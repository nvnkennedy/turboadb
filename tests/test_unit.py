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
    h = ADBHandler(ADBConfig(serial="dev1", adb_server_host="10.0.0.9",
                             adb_server_port=5037))
    h._run(["get-state"])
    argv = fake_adb.argv_after_adb()
    assert argv[:4] == ["-H", "10.0.0.9", "-P", "5037"]
    assert "-s" in argv and "dev1" in argv


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


# --------------------------------------------------------------------------- #
# device_info / health parsing
# --------------------------------------------------------------------------- #
def test_device_info_parses_props(fake_adb):
    props = ("[ro.product.model]: [Pixel 6]\n"
             "[ro.build.version.release]: [14]\n"
             "[ro.build.characteristics]: [automotive]\n")
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
        "up 3:14")
    assert out["battery_level"] == 87
    assert out["battery_temp_c"] == 35.1
    assert out["battery_status"] == "charging"
    assert out["mem_total_kb"] == 3900000
    assert out["cpu"] == "5%"


def test_health_missing_fields_are_none():
    out = ADBHandler._parse_health("", "", "", "")
    assert out["battery_level"] is None and out["cpu"] is None


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
    fake_adb.add("cmd bluetooth_manager", stdout="Exception: not allowed",
                 returncode=1)
    h = ADBHandler(ADBConfig(serial="x"))
    out = h.set_bluetooth(True)
    assert "can't be toggled" in out


def test_set_airplane_settings_fallback(fake_adb):
    fake_adb.add("cmd connectivity airplane-mode", stdout="unknown command",
                 returncode=1)
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
    d = _parse_line("emulator-5554  device product:sdk model:Pixel_6 "
                    "device:emu transport_id:3")
    assert d.serial == "emulator-5554" and d.is_online and d.model == "Pixel_6"


def test_parse_mdns_connect():
    r = _parse_mdns_line("adb-abc-Xy\t_adb-tls-connect._tcp.\t192.168.1.5:37021")
    assert r["service"] == "connect" and r["port"] == 37021


def test_parse_mdns_ignores_header():
    assert _parse_mdns_line("List of discovered mdns services") is None


# --------------------------------------------------------------------------- #
# toolsdl version comparison (the "never re-download / never downgrade" fix)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("installed,latest,expect", [
    ("37.0.0", "37.0.0", False),
    ("36.0.0", "37.0.0", True),
    (None, "37.0.0", True),
    ("37.0.0", None, None),
    ("38.0.0", "37.0.0", False),               # never "upgrade" to older
    ("35.0.2-12147458", "35.0.2", False),      # format-insensitive
    ("2.4", "3.1", True),                      # scrcpy two-part
])
def test_decide(installed, latest, expect):
    assert toolsdl._decide(installed, latest) is expect


def test_vtuple():
    assert toolsdl._vtuple("35.0.2-12147458") == (35, 0, 2)
    assert toolsdl._vtuple("2.4") == (2, 4, 0)


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
