"""How one adb command is run: what a timeout keeps, what an adb that cannot
run turns into, and which words never reach the log.

A command that ran out of time used to lose everything it had printed; an
adb.exe that antivirus locked raised a bare OSError past every
``except ADBError``; and the text typed with input_text (a password, a PIN)
was written to the debug log and the log file.
"""
import logging
import subprocess
import sys
import threading

import pytest

import turboadb.core as core
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBError, ADBTimeoutError


def _raising(error):
    def run(cmd, **_kw):
        raise error
    return run


# --------------------------------------------------------------------------- #
# a command that runs out of time keeps its output
# --------------------------------------------------------------------------- #
def test_a_timeout_keeps_what_the_command_printed(fake_adb, monkeypatch):
    printed = b"PING 8.8.8.8 56(84) bytes of data.\n64 bytes from 8.8.8.8: icmp_seq=1\n"
    monkeypatch.setattr(core.subprocess, "run", _raising(
        subprocess.TimeoutExpired(["adb"], 1.0, output=printed, stderr=b"note\n")))
    handler = ADBHandler(ADBConfig(serial="R58M1"))
    with pytest.raises(ADBTimeoutError, match="timed out after 1.0s") as info:
        handler.shell("ping 8.8.8.8", timeout=1.0)
    result = info.value.result
    assert result.exit_code == -1 and not result.ok
    assert result.stdout == printed.decode() and result.stderr == "note\n"
    assert result.command == "shell ping 8.8.8.8" and result.device == "R58M1"


def test_a_timeout_before_any_output_has_an_empty_result(fake_adb, monkeypatch):
    monkeypatch.setattr(core.subprocess, "run", _raising(subprocess.TimeoutExpired(["adb"], 2)))
    with pytest.raises(ADBTimeoutError) as info:
        ADBHandler(ADBConfig(serial="R58M1"))._run(["exec-out", "cat", "/x"], timeout=2,
                                                    binary=True)
    assert info.value.result.stdout == b"" and info.value.result.stderr == ""


def test_other_timeouts_have_no_result():
    assert ADBTimeoutError("adb pair timed out").result is None


_PRINT_THEN_HANG = (
    "import sys, time; sys.stdout.write('first line\\n'); sys.stdout.flush(); time.sleep(30)"
)


@pytest.mark.parametrize("cancellable", [False, True])
def test_a_real_command_keeps_its_output(monkeypatch, cancellable):
    """Both ways a command runs (the connect path's is cancellable)."""
    monkeypatch.setattr(ADBHandler, "_base",
                        lambda self, target=True: [sys.executable, "-c", _PRINT_THEN_HANG])
    handler = ADBHandler(ADBConfig(serial="R58M1"))
    cancel = threading.Event() if cancellable else None
    with pytest.raises(ADBTimeoutError) as info:
        handler._run(["shell", "ping"], timeout=2.0, cancel=cancel)
    assert info.value.result.stdout.replace("\r\n", "\n") == "first line\n"


# --------------------------------------------------------------------------- #
# an adb that cannot run is an ADBError
# --------------------------------------------------------------------------- #
_NOT_RUNNABLE = [
    PermissionError(13, "Access is denied"),
    OSError(225, "Operation did not complete successfully because the file contains a virus"),
    OSError(8, "Exec format error"),
]


@pytest.mark.parametrize("error", _NOT_RUNNABLE)
def test_an_adb_that_cannot_run_is_an_adb_error(fake_adb, monkeypatch, error):
    monkeypatch.setattr(core.subprocess, "run", _raising(error))
    handler = ADBHandler(ADBConfig(serial="R58M1"))
    with pytest.raises(ADBError, match="adb executable not runnable") as info:
        handler._run(["get-state"])
    assert info.value.__cause__ is error
    # the best-effort callers degrade instead of crashing
    assert handler.get_state() == "unknown"
    assert handler.quick_identity() == {}
    failed = handler.shell("id", safe=True)
    assert not failed.success and isinstance(failed.error, ADBError)


def test_the_cancellable_path_converts_it_too(fake_adb, monkeypatch):
    monkeypatch.setattr(core.subprocess, "Popen", _raising(PermissionError(13, "Access is denied")))
    handler = ADBHandler(ADBConfig(serial="R58M1"))
    with pytest.raises(ADBError, match="adb executable not runnable"):
        handler._run(["wait-for-device"], cancel=threading.Event())


def test_pairing_with_an_adb_that_cannot_run_is_an_adb_error(fake_adb, monkeypatch):
    monkeypatch.setattr(core.subprocess, "run", _raising(PermissionError(13, "Access is denied")))
    with pytest.raises(ADBError, match="adb executable not runnable"):
        ADBHandler(ADBConfig()).pair("192.168.1.5", 37000, "123456")


# --------------------------------------------------------------------------- #
# typed text never reaches the log
# --------------------------------------------------------------------------- #
SECRET = "hunter2 pin"


@pytest.fixture
def logged(caplog):
    """Every line the handler logs: its log_callback and the turboadb logger
    (what the GUI's log panel and its log file receive)."""
    lines = []
    caplog.set_level(logging.DEBUG, logger="turboadb")

    def collected():
        return "\n".join(lines + [record.getMessage() for record in caplog.records])

    collected.callback = lines.append
    return collected


def test_typed_text_is_sent_but_never_logged(fake_adb, logged):
    handler = ADBHandler(ADBConfig(serial="R58M1"), log_callback=logged.callback)
    assert handler.input_text(SECRET) is True
    assert fake_adb.calls[0][-1] == "hunter2%spin"  # the device still gets it
    text = logged()
    assert "hunter2" not in text
    assert "$ adb shell input text <11 characters>" in text
    assert "type 11 characters: ok" in text


def test_a_refused_text_is_reported_without_it(fake_adb, logged):
    fake_adb.add("input", returncode=1, stderr="java.lang.SecurityException: Injecting denied")
    handler = ADBHandler(ADBConfig(serial="R58M1"), log_callback=logged.callback)
    assert handler.input_text(SECRET, display_id=2) is False
    text = logged()
    assert "hunter2" not in text
    assert "shell input -d 2 text <11 characters>" in text
    assert "type 11 characters: exit 1" in text and "SecurityException" in text


def test_a_text_that_times_out_is_reported_without_it(fake_adb, monkeypatch, logged):
    """The timeout's message names the command, and safe mode logs it as an
    error; the TimeoutExpired behind it (which spells out the argv) is not
    chained on."""
    monkeypatch.setattr(core.subprocess, "run", _raising(
        subprocess.TimeoutExpired(["adb", "shell", "input", "text", "hunter2"], 15)))
    handler = ADBHandler(ADBConfig(serial="R58M1"), log_callback=logged.callback)
    result = handler.input_text("hunter2", safe=True)
    assert not result.success
    assert "hunter2" not in str(result.error) and result.error.__cause__ is None
    assert "hunter2" not in result.error.result.command
    assert "hunter2" not in logged() and "input_text failed" in logged()


def test_each_part_counts_the_characters_it_types(fake_adb, logged):
    handler = ADBHandler(ADBConfig(serial="R58M1"), log_callback=logged.callback)
    handler.input_text("a%sb c")  # sent as "a%", then "sb%sc" (a literal %s)
    handler.input_text("x")
    text = logged()
    assert "text <2 characters>" in text and "text <4 characters>" in text
    assert "type 6 characters" in text
    assert "text <1 character>" in text and "type 1 character:" in text


def test_other_commands_still_log_their_command_line(fake_adb, logged):
    handler = ADBHandler(ADBConfig(serial="R58M1"), log_callback=logged.callback)
    handler.keyevent("home")
    assert "$ adb shell input keyevent 3" in logged()
