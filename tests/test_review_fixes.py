"""What the 6.1.0 review found, each kept from coming back.

Tool updates never reinstall on their own; a remote deploy is judged by its
own lines and leaves a newer host alone; a self-update says when the tool
refresh did not report; the zoom bars settle; the screen follows a turned
device; Try again follows the Options; wireless discovery survives any name;
the webcam's wheel zooms by notches.  No device, adb, winget or network is used.
"""
import base64
import os
import shutil
import subprocess
import sys
import time
import types

import pytest

from turboadb import devices, remote_deploy as rd, toolsdl, update


# ---- an automatic update never takes a working tool away ------------------------
@pytest.fixture
def no_downloads(monkeypatch):
    """winget is asked (and records how); nothing is downloaded or changed."""
    asked = []

    def install_with_winget(tool, existing, *, force=False, on_progress=None):
        asked.append((tool, force))
        return f"C:/winget/{tool}.exe"

    monkeypatch.setattr(toolsdl, "_install_with_winget", install_with_winget)
    monkeypatch.setattr(toolsdl, "adb_download_supported", lambda: True)
    monkeypatch.setattr(toolsdl, "managed_adb", lambda: "C:/winget/adb.exe")
    monkeypatch.setattr(toolsdl, "managed_scrcpy", lambda: "C:/winget/scrcpy.exe")
    return asked


def test_an_update_asks_winget_without_force(no_downloads):
    """winget's --force takes the installed copy away first."""
    checks = {tool: {"upgrade": True, "installed": None, "latest": "9.9.9"}
              for tool in ("adb", "scrcpy")}
    toolsdl.upgrade_tools(checks=checks)
    assert sorted(no_downloads) == [("adb", False), ("scrcpy", False)]


def test_a_reinstall_the_user_asks_for_still_forces(no_downloads):
    toolsdl.download_platform_tools(force=True)
    toolsdl.download_scrcpy(force=True)
    assert no_downloads == [("adb", True), ("scrcpy", True)]


# ---- winget's time limit holds while a program it started keeps its output ----
def test_a_helper_holding_winget_s_output_does_not_hold_the_call():
    helper = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(25)'],"
        " stdout=sys.stdout)\n"
        "print('helper', child.pid, flush=True)\n"
    )
    started = time.monotonic()
    code, lines = toolsdl._run_winget([sys.executable, "-c", helper], timeout=20)
    took = time.monotonic() - started
    pid = int(lines[0].split()[1])
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, 9)
    except OSError:
        pass
    assert code == 0 and took < toolsdl._WINGET_DRAIN_S + 8


# ---- a remote deploy ------------------------------------------------------------
def _deployed(monkeypatch, out):
    monkeypatch.setattr(rd, "_run_ps", lambda *a, **k: (0, out, ""))
    return rd._deploy_one("lab-pc", "LAB\\admin", "pw", update=True, port=5037,
                          test_only=False, winrm_port=5985, use_ssl=False, host_timeout=60)


def test_a_pip_error_quoted_in_a_warning_is_no_failed_deploy(monkeypatch):
    ok, lines = _deployed(monkeypatch, (
        "WARNING:pip upgrade of turboadb failed (exit 1): ERROR: Could not install packages\r\n"
        "STATUS:shared adb server is listening on 0.0.0.0:5037"))
    assert ok
    assert any(line.startswith("[WARNING] lab-pc: pip upgrade") for line in lines)


def test_the_script_s_own_error_still_fails(monkeypatch):
    ok, lines = _deployed(monkeypatch, "ERROR:turboadb serve exited with code 1: no adb")
    assert not ok and lines[-1] == "[ERROR] lab-pc: turboadb serve exited with code 1: no adb"


def _script(update_flag="1"):
    return rd._DEPLOY_PS.format(upd=update_flag, port=5037, wheel=update.wheel_url("6.1.0"))


def test_a_host_with_this_version_or_newer_is_left_alone():
    script = _script()
    assert "[version]\"$have\".Trim() -ge [version]$want" in script
    assert script.index("-ge [version]$want") < script.index("pip install -U")


def test_the_deploy_script_fits_a_windows_command_line():
    """pywinrm runs it as ``powershell -encodedcommand <base64 of UTF-16>``."""
    encoded = base64.b64encode(_script().encode("utf-16-le"))
    assert len(encoded) + len("powershell -encodedcommand ") < 8000


@pytest.mark.skipif(not shutil.which("powershell"), reason="needs Windows PowerShell")
def test_the_deploy_script_is_valid_powershell(tmp_path):
    path = tmp_path / "deploy.ps1"
    path.write_text(_script(), encoding="utf-8-sig")
    check = ("$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]"
             f"::ParseFile('{path}', [ref]$t, [ref]$e); $e.Count")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", check],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "0", out.stdout + out.stderr


# ---- self-update ----------------------------------------------------------------
def _upgraded(monkeypatch, report):
    monkeypatch.setattr(update, "can_self_update", lambda: True)
    monkeypatch.setattr(update, "current_version", lambda: "6.0.2")
    monkeypatch.setattr(update.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(update, "_fresh_report", lambda *a, **k: report)
    monkeypatch.setattr(update, "_manual_steps",
                        lambda: {"adb": "winget upgrade adb", "scrcpy": "winget upgrade scrcpy"})
    said = []
    return update.run_upgrade(notify=said.append, expected="6.1.0"), said


def test_a_tool_refresh_that_never_reported_is_said(monkeypatch):
    res, said = _upgraded(monkeypatch, {"_failure": "it did not finish within 1800 s"})
    assert res["ok"] and res["new"] == "6.1.0" and res["unchanged"] is False
    assert "did not report back (it did not finish within 1800 s)" in res["tools_errors"]["tools"]
    assert res["tools_manual"] == {"adb": "winget upgrade adb", "scrcpy": "winget upgrade scrcpy"}
    assert "TurboADB upgraded to 6.1.0." in said
    assert any("Update adb yourself: winget upgrade adb" in line for line in said)
    assert not any("up to date" in line for line in said)


def test_a_report_that_came_back_needs_no_steps(monkeypatch):
    res, _said = _upgraded(monkeypatch, {"version": "6.1.0", "adb": "37.0.1",
                                         "scrcpy": "5.0.1", "errors": {}})
    assert res["ok"] and "tools_errors" not in res


def test_why_there_is_no_report(monkeypatch):
    def times_out(*_a, **k):
        raise subprocess.TimeoutExpired("python", k.get("timeout"))

    monkeypatch.setattr(update.subprocess, "run", times_out)
    assert update._fresh_report(timeout=5) == {"_failure": "it did not finish within 5 s"}
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=1, stdout="", stderr="Traceback\nImportError: no module named x"))
    failure = update._fresh_report()["_failure"]
    assert "exit code 1" in failure and "ImportError: no module named x" in failure


def test_the_tool_refresh_is_given_time_for_both_tools():
    worst = 2 * toolsdl._WINGET_TRIES * toolsdl._WINGET_TIMEOUT_S
    assert update._TOOLS_REPORT_TIMEOUT_S > worst


@pytest.mark.parametrize("gui, expected", [
    (True, "turboadb[gui] @ " + update.wheel_url("6.1.0")),
    (False, update.wheel_url("6.1.0")),
])
def test_the_window_s_packages_come_with_its_update(monkeypatch, gui, expected):
    monkeypatch.setattr(update, "_has_gui", lambda: gui)
    assert update._pip_upgrade_cmd("6.1.0")[-1] == expected


# ---- wireless discovery ---------------------------------------------------------
def test_wireless_discovery_reads_any_name(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen.update(kw)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(devices, "find_adb", lambda path=None: "adb")
    monkeypatch.setattr(devices.subprocess, "run", run)
    devices.mdns_devices()
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"


# ---- the screen's zoom bars -----------------------------------------------------
mp = pytest.importorskip("turboadb.gui.mirror_panel")


def _settles(full_w, full_h, aspect, zoom, thick=16):
    """The bars _pan_bars picks, asked again as the layout changes the place."""
    shown = (False, False)
    for _ in range(6):
        wanted = mp._pan_bars(full_w, full_h, (thick, thick), aspect, zoom)
        if wanted == shown:
            return wanted
        shown = wanted
    return None


def _fill(width, height, aspect):
    fit = min(float(width), height * aspect)
    return max(1.0, width / fit, height / (fit / aspect))


@pytest.mark.parametrize("width, height, aspect", [
    (1400, 800, 1080 / 2400),  # a phone held upright, in a wide place: flickered for ever
    (1400, 800, 7200 / 1178),  # a very wide head unit
    (800, 1400, 7200 / 1178),
    (1083, 800, 1080 / 2400),
])
def test_fill_the_window_settles_its_scroll_bars(width, height, aspect):
    assert _settles(width, height, aspect, _fill(width, height, aspect)) is not None


def test_bars_are_decided_from_the_whole_place():
    tall = 1080 / 2400
    assert mp._pan_bars(1400, 800, (16, 16), tall, 1.0) == (False, False)
    assert mp._pan_bars(1400, 800, (16, 16), tall, 2.0) == (False, True)
    assert mp._pan_bars(1400, 800, (16, 16), tall, 4.0) == (True, True)
