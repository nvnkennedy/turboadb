"""Deploying to a host that has Python and turboadb but no adb.

``turboadb serve`` never downloads adb (the startup task runs it as SYSTEM,
whose download would be a second adb), so on such a host it ended with "Could
not find 'adb'" and the deploy failed until someone logged in there and ran
``fetch-tools``.  The deploy now downloads adb first, as the deploying account.

The script runs here in a local PowerShell against a stand-in for the host's
``python``: no WinRM, no turboadb on the "host", no download.
"""
import base64
import logging
import os
import re
import subprocess
import sys
import textwrap
import types

import pytest

from turboadb import devices, tools, toolsdl
from turboadb import remote_deploy as rd

# What the host's `python` does, by its arguments.  It keeps its state in
# files beside itself: `adb` (the host has one) and `calls.log`.
_PYTHON = textwrap.dedent(r'''
    import os, sys

    here = os.path.dirname(os.path.abspath(__file__))
    args = sys.argv[1:]
    has_adb = os.path.exists(os.path.join(here, "adb"))
    with open(os.path.join(here, "calls.log"), "a") as log:
        log.write(" ".join(a if len(a) < 40 else "<code>" for a in args) + "\n")
    if args[:1] == ["-c"]:  # "does this host lack adb?" is exit code 3
        no_fetch = os.path.exists(os.path.join(here, "no-auto-fetch"))
        sys.exit(0 if has_adb or no_fetch else 3)
    if args[:3] == ["-m", "pip", "install"]:
        sys.exit(0)
    if args[:3] == ["-m", "turboadb", "fetch-tools"]:
        if os.path.exists(os.path.join(here, "offline")):
            sys.stderr.write("adb: <urlopen error getaddrinfo failed>\n")
            sys.exit(1)
        open(os.path.join(here, "adb"), "w").close()
        print("adb    -> C:\\Users\\admin\\.turboadb\\tools\\platform-tools\\adb.exe")
        sys.exit(0)
    if args[:3] == ["-m", "turboadb", "serve"]:
        if not has_adb:
            sys.stderr.write("ERROR: Could not find 'adb' (Android Platform-Tools).\n")
            sys.exit(3)
        print("shared adb server is listening on 0.0.0.0:5037 (0 device(s) attached here)")
        if os.path.exists(os.path.join(here, "user-python")):
            sys.stderr.write("2026-10-03 22:10:05,123 [WARNING] turboadb.devices: the startup "
                             "task runs as SYSTEM what accounts without administrator rights "
                             "can change (pythonw.exe)\n")
        said = os.path.join(here, "serve-warning")
        if os.path.exists(said):
            with open(said, encoding="utf-8") as fh:
                sys.stderr.write("2026-10-03 22:10:05,123 [WARNING] turboadb.devices: "
                                 + fh.read() + "\n")
        print("Installed startup Scheduled Task 'TurboADBSharedADB'")
        sys.exit(0)
    sys.exit(64)
''')


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A folder that is the "host": ``python`` on PATH is the stand-in above.
    Returns ``(folder, deploy)``; *deploy* runs the deploy script there and
    returns what :func:`remote_deploy._deploy_one` makes of it."""
    (tmp_path / "host_python.py").write_text(_PYTHON, encoding="utf-8")
    (tmp_path / "python.cmd").write_text(
        f'@"{sys.executable}" "%~dp0host_python.py" %*\r\n', encoding="ascii")
    system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    powershell = os.path.join(system, "WindowsPowerShell", "v1.0")
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path), system, powershell]))

    def run_ps(host, login, password, script, **kw):
        # as pywinrm's run_ps starts it on the host
        encoded = base64.b64encode(script.encode("utf_16_le")).decode("ascii")
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, timeout=120, cwd=str(tmp_path))
        return (done.returncode, done.stdout.decode("utf-8", "replace").strip(),
                done.stderr.decode("utf-8", "replace").strip())

    monkeypatch.setattr(rd, "_run_ps", run_ps)

    def deploy(update=False):
        return rd._deploy_one("lab-pc", "LAB\\admin", "pw", update=update, port=5037,
                              test_only=False, winrm_port=5985, use_ssl=False, host_timeout=60)

    return tmp_path, deploy


def _calls(folder):
    return (folder / "calls.log").read_text().splitlines()


windows_only = pytest.mark.skipif(os.name != "nt", reason="runs the script in powershell.exe")


@windows_only
def test_a_host_without_adb_gets_one_before_serve(host):
    folder, deploy = host
    ok, lines = deploy()
    assert ok, lines
    assert _calls(folder) == ["-c <code>", "-m turboadb fetch-tools --adb-only",
                              "-m turboadb serve --port 5037 --startup-task"]
    assert any(line.startswith("[INFO] lab-pc: ") and "downloaded" in line for line in lines)
    assert lines[-1].startswith("[OK] lab-pc: serve started")


@windows_only
def test_a_host_with_adb_downloads_nothing(host):
    folder, deploy = host
    (folder / "adb").write_text("")
    ok, lines = deploy(update=True)
    assert ok, lines
    assert _calls(folder) == ["-m pip install -U --quiet --disable-pip-version-check turboadb",
                              "-c <code>", "-m turboadb serve --port 5037 --startup-task"]
    assert not any("downloaded" in line for line in lines)


@windows_only
def test_a_download_that_fails_is_named_beside_the_error(host):
    folder, deploy = host
    (folder / "offline").write_text("")
    ok, lines = deploy()
    assert not ok
    warning = next(line for line in lines if line.startswith("[WARNING] lab-pc: "))
    assert "could not be downloaded" in warning and "getaddrinfo failed" in warning
    assert lines[-1].startswith("[ERROR] lab-pc: ") and "Could not find 'adb'" in lines[-1]


@windows_only
def test_a_host_that_forbids_downloads_is_left_alone(host):
    """TURBOADB_AUTO_FETCH=0 there, or a path set on purpose that names no adb."""
    folder, deploy = host
    (folder / "no-auto-fetch").write_text("")
    ok, lines = deploy()
    assert not ok and "Could not find 'adb'" in lines[-1]
    assert not any("fetch-tools" in call for call in _calls(folder))


@windows_only
def test_what_serve_warns_about_reaches_the_deploy_log(host):
    """The status line keeps 220 characters: a warning after the first lines of
    serve (a Python on the host that any of its users can change) was cut off."""
    folder, deploy = host
    (folder / "adb").write_text("")
    (folder / "user-python").write_text("")
    ok, lines = deploy()
    assert ok, lines
    assert ("[WARNING] lab-pc: the startup task runs as SYSTEM what accounts without "
            "administrator rights can change (pythonw.exe)") in lines


def test_the_script_asks_before_it_downloads_and_only_for_adb():
    script = rd._DEPLOY_PS.format(upd="0", port=5037)
    check, fetch, serve = (script.index(text) for text in (
        "tools.diagnose()", "fetch-tools --adb-only", "serve --port 5037 --startup-task"))
    assert check < fetch < serve


@pytest.mark.parametrize("found, auto_fetch, code", [
    ({"adb_path": None, "adb_error": None}, True, 3),  # no adb: download it
    ({"adb_path": None}, True, 3),  # as an older turboadb on the host reports it
    ({"adb_path": "C:/platform-tools/adb.exe", "adb_error": None}, True, 0),
    # a path set on purpose that names no adb is to be corrected, not downloaded around
    ({"adb_path": None, "adb_error": "TURBOADB_ADB=C:/gone is not adb"}, True, 0),
    ({"adb_path": None, "adb_error": None}, False, 0),  # TURBOADB_AUTO_FETCH=0 on the host
])
def test_the_question_the_script_asks_the_hosts_turboadb(monkeypatch, found, auto_fetch, code):
    question = re.search(r'-c "(import sys; [^"]+)"', rd._DEPLOY_PS).group(1)
    monkeypatch.setattr(tools, "diagnose", lambda: found)
    monkeypatch.setattr(toolsdl, "auto_fetch_enabled", lambda: auto_fetch)
    with pytest.raises(SystemExit) as answer:
        exec(question, {})
    assert answer.value.code == code


def test_what_the_script_did_about_adb_is_passed_on(monkeypatch):
    out = ("INFO:the host had no adb: downloaded platform-tools\r\n"
           "STATUS:shared adb server is listening on 0.0.0.0:5037")
    monkeypatch.setattr(rd, "_run_ps", lambda *a, **k: (0, out, ""))
    ok, lines = rd._deploy_one("lab-pc", "LAB\\admin", "pw", update=False, port=5037,
                               test_only=False, winrm_port=5985, use_ssl=False, host_timeout=60)
    assert ok
    assert "[INFO] lab-pc: the host had no adb: downloaded platform-tools" in lines


@pytest.fixture
def warning_about_python(monkeypatch, caplog):
    """What installing the startup task says of the Python python.org
    installs for one user: some 450 characters."""
    python = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312"
    code = [python + r"\pythonw.exe", python + r"\Lib\site-packages\turboadb"]
    with monkeypatch.context() as host_pc:
        host_pc.setattr(devices.os, "name", "nt")
        host_pc.setattr(devices, "find_adb", lambda p=None: r"C:\Program Files\Android\adb.exe")
        host_pc.setattr(devices, "windowless_python", lambda: code[0])
        host_pc.setattr(devices, "_task_code", lambda: code)
        host_pc.setattr(devices, "_others_can_change", lambda path: path in code)
        host_pc.setattr(devices, "_share_keys_with_system", lambda: None)
        host_pc.setattr(devices, "_change_sharing", lambda change, path=None: True)
        host_pc.setattr(devices.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stdout=b"", stderr=b""))
        with caplog.at_level(logging.WARNING, logger="turboadb.devices"):
            devices.install_serve_task(run_now=False)
    warning = caplog.records[-1].getMessage()
    assert all(path in warning for path in code) and "turboadb serve --startup-task" in warning
    return warning


def test_a_long_warning_of_serve_reaches_the_deploy_log_whole(monkeypatch, warning_about_python):
    """Kept to 240 characters, it ended inside the second path it names,
    before what follows from it and what to do about it."""
    out = (f"WARNING:{warning_about_python}\r\n"
           "STATUS:shared adb server is listening on 0.0.0.0:5037")
    monkeypatch.setattr(rd, "_run_ps", lambda *a, **k: (0, out, ""))
    ok, lines = rd._deploy_one("lab-pc", "LAB\\admin", "pw", update=False, port=5037,
                               test_only=False, winrm_port=5985, use_ssl=False, host_timeout=60)
    assert ok and f"[WARNING] lab-pc: {warning_about_python}" in lines


@windows_only
def test_a_long_warning_of_serve_comes_through_the_script_in_one_piece(host, warning_about_python):
    folder, deploy = host
    (folder / "adb").write_text("")
    (folder / "serve-warning").write_text(warning_about_python, encoding="utf-8")
    ok, lines = deploy()
    assert ok, lines
    assert f"[WARNING] lab-pc: {warning_about_python}" in lines
