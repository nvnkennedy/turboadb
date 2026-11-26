"""How TurboADB runs adb clients (6.5.0), against a fake adb that really runs
as a separate process (tests/fakeadb.py: a Python script behind an adb.exe
launcher on Windows).

1. Every adb client is in a kill-on-close job: it ends with the host, even a
   crashed one; the adb server a client starts is never in it.
2. Every blocking call takes ``cancel=`` (an Event or a callable) and ends its
   client at once; a timeout ends it too; both are reported the same way
   everywhere (``cancelled`` / ``timed_out``, exit code 124, ADBCancelledError
   / ADBTimeoutError).
3. adb's output is UTF-8, and a line that is not, the OEM code page.
4. Device problems come as kinds.
5. wait_for_boot and get_state never hang.
6. ui_dump retries a UI that never settled.
"""

import os
import subprocess
import sys
import threading
import time

import pytest

from fakeadb import FakeAdb, alive, gone, kill
from turboadb import (
    ADBCancelledError, ADBCommandError, ADBConfig, ADBConnectionError, ADBError, ADBHandler,
    ADBTimeoutError, ADBTransferError, TIMEOUT_EXIT_CODE, adbproc, device_problem,
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows job objects")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    try:
        made = FakeAdb(tmp_path / "fake")
    except RuntimeError as exc:
        pytest.skip(str(exc))
    monkeypatch.setenv("TURBOADB_FAKE_ADB", made.scenario)
    return made


def _device(fake, **kwargs):
    return ADBHandler(ADBConfig(serial="FAKE1", adb_path=fake.path), **kwargs)


def _client_pid(call):
    """The process TurboADB started: on Windows the launcher, the script's parent."""
    return call["ppid"] if os.name == "nt" else call["pid"]


# --------------------------------------------------------------------------- #
# 1. the kill-on-close job
# --------------------------------------------------------------------------- #
@windows_only
def test_every_adb_client_is_in_the_job_and_what_it_starts_is_not(fake):
    fake.play([{"match": "start-server", "server": True}, {"match": "shell hold", "sleep": 60}])
    dev = _device(fake)
    dev.adb("start-server", timeout=30)
    server = fake.server_pid()
    stop = threading.Event()
    worker = threading.Thread(target=lambda: dev.shell("hold", cancel=stop, safe=True))
    worker.start()
    try:
        call = fake.wait_call("shell hold")
        assert adbproc.in_job(_client_pid(call)) is True  # the client TurboADB started
        assert adbproc.in_job(call["pid"]) is False  # what it started left the job
        assert adbproc.in_job(server) is False  # the server is never in it
    finally:
        stop.set()
        worker.join(10)
        kill(server)


_HOST = r'''
import sys
sys.path.insert(0, sys.argv[1])
from turboadb import ADBConfig, ADBHandler
dev = ADBHandler(ADBConfig(serial="FAKE1", adb_path=sys.argv[2]))
dev.adb("start-server", timeout=30)
dev.shell("hold")
'''


@windows_only
def test_adb_clients_end_with_a_host_that_crashed(fake):
    """Issue: an adb.exe started by ADBHandler outlived a crash of the host
    program."""
    fake.play([{"match": "start-server", "server": True}, {"match": "shell hold", "sleep": 60}])
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    host = subprocess.Popen([sys.executable, "-c", _HOST, root, fake.path],
                            env=dict(os.environ, TURBOADB_FAKE_ADB=fake.scenario))
    server = None
    try:
        call = fake.wait_call("shell hold", timeout=60)
        server = fake.server_pid()
        assert alive(_client_pid(call)) and alive(call["pid"])
        host.kill()  # TerminateProcess: the host never gets to clean up
        host.wait(10)
        assert gone(_client_pid(call)) and gone(call["pid"])  # the client ended with it
        assert alive(server)  # the shared server did not
    finally:
        if host.poll() is None:
            host.kill()
        if server is not None:
            kill(server)


# --------------------------------------------------------------------------- #
# 2. cancel and timeout for every blocking call
# --------------------------------------------------------------------------- #
def _hold(match):
    return {"match": match, "sleep": 60}


_BOOTED = {"match": "boot_completed", "stdout": "1\n0123456789abcdef0123\n"}

# (method, the command it hangs in, the call, what it gives when cancelled)
_CALLS = [
    ("adb", "hold", lambda d, c: d.adb("hold", cancel=c), "raises"),
    ("shell", "shell hold", lambda d, c: d.shell("hold", cancel=c), "raises"),
    ("push", "push", lambda d, c: d.push(__file__, "/sdcard/x", cancel=c), "raises"),
    ("pull", "pull", lambda d, c: d.pull("/sdcard/x", d._test_folder, cancel=c), "raises"),
    ("install", "install", lambda d, c: d.install("app.apk", cancel=c), "raises"),
    ("uninstall", "uninstall", lambda d, c: d.uninstall("com.x", cancel=c), "raises"),
    ("reboot", "reboot", lambda d, c: d.reboot(cancel=c), "raises"),
    ("root", "root", lambda d, c: d.root(cancel=c), "raises"),
    ("unroot", "unroot", lambda d, c: d.unroot(cancel=c), "raises"),
    ("remount", "remount", lambda d, c: d.remount(cancel=c), "raises"),
    ("wait_for_device", "wait-for-device", lambda d, c: d.wait_for_device(60, cancel=c), "raises"),
    ("bugreport", "bugreport", lambda d, c: d.bugreport(d._test_folder + "/r", cancel=c), "raises"),
    ("logcat", "logcat", lambda d, c: d.logcat(tail=1, cancel=c), "stream"),
    ("stream", "shell hold", lambda d, c: d.stream(["shell", "hold"], cancel=c), "stream"),
    ("wait_for_boot", "devices", lambda d, c: d.wait_for_boot(60, cancel=c), "boot"),
]


@pytest.mark.parametrize("name, hangs_in, call, gives", _CALLS, ids=[c[0] for c in _CALLS])
@pytest.mark.parametrize("kind", ["event", "callable"])
def test_every_blocking_call_can_be_cancelled(fake, tmp_path, name, hangs_in, call, gives, kind):
    fake.play([_BOOTED, _hold(hangs_in)])
    dev = _device(fake)
    dev._test_folder = str(tmp_path / "out")
    os.makedirs(dev._test_folder, exist_ok=True)
    event = threading.Event()
    cancel = event if kind == "event" else (lambda: event.is_set())
    outcome = {}

    def run():
        try:
            outcome["value"] = call(dev, cancel)
        except Exception as exc:  # (inspected below)
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    try:
        client = fake.wait_call(hangs_in, timeout=30)
        event.set()
        asked = time.monotonic()
        worker.join(10)
        assert not worker.is_alive()
        # looked at every 0.1 s, and the kill is quick (half a second here); a
        # slow CI runner gets room
        assert time.monotonic() - asked < 4.0
        assert gone(_client_pid(client)) and gone(client["pid"])  # its adb client ended
    finally:
        event.set()
        worker.join(10)
    if gives == "raises":
        error = outcome.get("error")
        assert isinstance(error, ADBCancelledError), outcome
        assert error.cancelled and not error.timed_out
        if error.result is not None:
            assert error.result.cancelled and error.result.exit_code == TIMEOUT_EXIT_CODE
    elif gives == "stream":
        result = outcome["value"]
        assert result.cancelled and result.exit_code == TIMEOUT_EXIT_CODE and not result.failed
    else:
        result = outcome["value"]
        assert result["cancelled"] and not result["ok"] and not result["timed_out"]


def test_a_cancelled_call_was_an_older_error_too(fake):
    """A cancelled connect or shell raised ADBConnectionError, and a cancelled
    push or pull ADBTransferError: code that catches those still does."""
    assert issubclass(ADBCancelledError, ADBConnectionError)
    assert issubclass(ADBCancelledError, ADBTransferError)


def test_a_call_already_cancelled_starts_no_adb(fake):
    fake.play([_hold("shell hold")])
    with pytest.raises(ADBCancelledError):
        _device(fake).shell("hold", cancel=lambda: True)
    assert fake.calls() == []


def test_a_timeout_ends_the_client_and_says_so(fake):
    fake.play([{"match": "shell hold", "stdout": "started\n", "sleep": 60}])
    dev = _device(fake)
    began = time.monotonic()
    with pytest.raises(ADBTimeoutError) as info:
        dev.shell("hold", timeout=4.0)  # (the fake takes a second to start on Windows)
    assert time.monotonic() - began < 10
    result = info.value.result
    assert result.timed_out and not result.cancelled and result.exit_code == TIMEOUT_EXIT_CODE
    assert "started" in result.stdout  # what it printed until then
    client = fake.wait_call("shell hold")
    assert gone(_client_pid(client)) and gone(client["pid"])
    # safe mode: the same, as a failed result
    failed = dev.shell("hold", timeout=0.5, safe=True)
    assert not failed and failed.timed_out and not failed.cancelled


def test_a_stream_that_runs_out_of_time_says_so(fake):
    fake.play([{"match": "logcat", "stdout": "a line\n", "sleep": 60}])
    result = _device(fake).logcat(tail=1, timeout=5.0)  # (the fake takes a second to start)
    assert result.timed_out and result.exit_code == TIMEOUT_EXIT_CODE and not result.failed
    assert result.lines == 1


def test_cancel_must_be_an_event_or_a_callable(fake):
    with pytest.raises(TypeError):
        _device(fake).shell("ls", cancel="soon")


# --------------------------------------------------------------------------- #
# 3. output on a non-English Windows
# --------------------------------------------------------------------------- #
# "Zeitüberschreitung" as cmd and Windows' messages write it on a German
# Windows (cp850; ü is 0x81 in cp437 too), then a UTF-8 line
_OEM_LINE = b"Zeit\x81berschreitung beim Warten\r\n"
_UTF8_LINE = "café ✓\n".encode("utf-8")


def test_output_is_utf8_and_otherwise_the_oem_code_page(fake):
    fake.play([{"match": "print", "stdout_hex": (_OEM_LINE + _UTF8_LINE).hex(),
                "stderr_hex": _OEM_LINE.hex(), "exit": 1}])
    res = _device(fake).adb("print")
    assert "café ✓" in res.stdout  # a UTF-8 line stays UTF-8
    if os.name == "nt":
        assert "Zeitüberschreitung beim Warten" in res.stdout and "Zeitüberschreitung" in res.stderr
    else:
        assert "Zeit�berschreitung" in res.stdout  # no OEM code page: never a failure


def test_no_output_is_an_empty_text_never_none(fake):
    fake.play([{"match": "quiet"}])
    res = _device(fake).adb("quiet")
    assert res.stdout == "" and res.stderr == "" and res.ok


def test_decoding_never_fails():
    assert adbproc.decode(b"\xff\xfe\x00 broken") != ""
    assert adbproc.decode(None) == "" and adbproc.decode(b"") == ""
    assert adbproc.decode("already text") == "already text"
    assert adbproc.decode("é".encode("cp1252"), "cp1252") == "é"  # an encoding set on purpose


# --------------------------------------------------------------------------- #
# 4. device problems as kinds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, kind", [
    ("error: no devices/emulators found", "no-device"),
    ("adb: no devices/emulators found", "no-device"),
    ("error: device 'R58M123' not found", "not-found"),
    ("adb: device 'emulator-5554' not found", "not-found"),
    ("error: device unauthorized.\nThis adb server's $ADB_VENDOR_KEYS is not set", "unauthorized"),
    ("error: device still authorizing", "unauthorized"),
    ("error: device offline", "offline"),
    ("offline", "offline"),
    ("error: more than one device/emulator", "more-than-one"),
    ("error: insufficient permissions for device: user in plugdev group", "no-permissions"),
    ("no permissions (missing udev rules?)", "no-permissions"),
    ("error: closed", ""),
    ("", ""),
    (None, ""),
])
def test_device_problems_have_kinds(text, kind):
    assert device_problem(text) == kind
    assert ADBHandler.device_problem(text) == kind


def test_errors_and_results_carry_the_device_problem(fake):
    fake.play([{"match": "shell ls", "exit": 1,
                "stderr": "error: device unauthorized.\nThis adb server's $ADB_VENDOR_KEYS"}])
    dev = _device(fake)
    res = dev.shell("ls")
    assert res.device_problem == "unauthorized"
    with pytest.raises(ADBCommandError) as info:
        dev.shell("ls", check=True)
    assert info.value.device_problem == "unauthorized"
    assert dev.shell("ls", check=True, safe=True).device_problem == "unauthorized"
    assert ADBError("error: more than one device/emulator").device_problem == "more-than-one"


# --------------------------------------------------------------------------- #
# 5. boot waiting and get_state
# --------------------------------------------------------------------------- #
_LISTED = "List of devices attached\nFAKE1\t{}\tproduct:x model:Fake device:x\n\n"
_NONE = "List of devices attached\n\n"


def test_wait_for_boot_needs_stable_reads_and_says_when_the_device_came(fake):
    fake.play([
        {"match": "devices", "answers": [{"stdout": _NONE}, {"stdout": _LISTED.format("offline")},
                                          {"stdout": _LISTED.format("device")}]},
        {"match": "boot_completed", "answers": [
            {"stdout": "0\nnew-boot-0123456789\n"}, {"stdout": "1\nnew-boot-0123456789\n"},
            {"stdout": "0\nnew-boot-0123456789\n"}, {"stdout": "1\nnew-boot-0123456789\n"}]},
    ])
    began = time.time()
    result = _device(fake).wait_for_boot(60, stable=2, interval=0.05)
    assert result["ok"] and result["state"] == "device" and result["boot_completed"]
    assert not result["timed_out"] and not result["cancelled"]
    assert began <= result["first_seen"] <= began + result["seconds"]
    reads = [c for c in fake.calls() if "boot_completed" in c["command"]]
    assert len(reads) == 5  # 0, 1, 0 (starts over), 1, 1


_OLD_BOOT = "11111111-2222-3333-4444-555555555555"
_NEW_BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _booted(boot_id):
    """What the boot read prints: sys.boot_completed 1, then the boot id."""
    return "\n".join(("1", boot_id, ""))


def test_wait_for_boot_after_reboot_waits_for_the_next_boot(fake):
    """The device is often still up for a moment after adb reboot: reads of
    the boot before it must not count, or this wait would end before the
    device even went away."""
    fake.play([
        {"match": "boot_completed", "answers": [
            {"stdout": _booted(_OLD_BOOT)},  # (reboot() notes this one)
            {"stdout": _booted(_OLD_BOOT)}, {"stdout": _booted(_OLD_BOOT)},
            {"stdout": _booted(_NEW_BOOT)}]},
        {"match": "reboot"},
        {"match": "devices", "answers": [
            {"stdout": _LISTED.format("device")}, {"stdout": _LISTED.format("device")},
            {"stdout": _NONE}, {"stdout": _LISTED.format("device")}]},
    ])
    dev = _device(fake)
    assert dev.reboot()
    assert dev._booted_before == {"FAKE1": _OLD_BOOT}
    began = time.time()
    result = dev.wait_for_boot(60, stable=2, interval=0.05)
    assert result["ok"] and began <= result["first_seen"]
    listings = [c for c in fake.calls() if c["command"].startswith("devices")]
    assert len(listings) == 5  # device, device (the old boot), gone, back, back
    assert dev._booted_before == {}  # (that boot is over)


def test_wait_for_boot_that_runs_out_of_time_returns_a_result(fake):
    fake.play([{"match": "devices", "stdout": _NONE}])
    result = _device(fake).wait_for_boot(1.0, interval=0.05)
    assert not result["ok"] and result["timed_out"] and result["first_seen"] is None
    assert result["state"] == "unknown" and result["seconds"] < 5


def test_get_state_never_hangs(fake):
    """Issue: adb's own get-state can hang on a device that goes away."""
    fake.play([{"match": "devices", "sleep": 60}])
    dev = _device(fake)
    began = time.monotonic()
    assert dev.get_state(timeout=0.5) == "unknown"
    assert time.monotonic() - began < 5
    with pytest.raises(ADBCancelledError):
        dev.get_state(cancel=lambda: True)


def test_get_state_reads_the_device_list(fake):
    fake.play([{"match": "devices", "stdout": _LISTED.format("unauthorized")}])
    assert _device(fake).get_state() == "unauthorized"
    fake.play([{"match": "devices", "stdout": _NONE}])
    assert _device(fake).get_state() == "unknown"


# --------------------------------------------------------------------------- #
# 6. the UI tree
# --------------------------------------------------------------------------- #
_XML = "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\"></hierarchy>"
_WINDOWS_XML = (
    "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><displays>"
    "<display id=\"0\"><window index=\"0\"><hierarchy rotation=\"0\"/></window></display>"
    "<display id=\"2\"><window index=\"0\"><hierarchy rotation=\"1\"/></window></display>"
    "</displays>"
)


def test_ui_dump_retries_a_screen_that_never_settled(fake):
    fake.play([{"match": "uiautomator dump", "answers": [
        {"stdout": "ERROR: could not get idle state.\n"},
        {"stdout": "ERROR: null root node returned by UiTestAutomationBridge.\n"},
        {"stdout": "UI hierchary dumped to: /data/local/tmp/x.xml\n" + _XML}]}])
    assert _device(fake).ui_dump(tries=3) == _XML
    assert len([c for c in fake.calls() if "uiautomator" in c["command"]]) == 3


def test_ui_dump_gives_up_after_its_tries(fake):
    fake.play([{"match": "uiautomator dump", "stdout": "ERROR: could not get idle state.\n"}])
    with pytest.raises(ADBError, match="could not get idle state"):
        _device(fake).ui_dump(tries=2)
    assert len([c for c in fake.calls() if "uiautomator" in c["command"]]) == 2


def test_ui_dump_of_one_display(fake):
    fake.play([{"match": "uiautomator dump --windows", "stdout": _WINDOWS_XML}])
    dev = _device(fake)
    one = dev.ui_dump(display_id=2)
    assert '<display id="2">' in one and '<display id="0">' not in one
    with pytest.raises(ADBError, match="display 5 is not in the UI dump"):
        dev.ui_dump(display_id=5)
