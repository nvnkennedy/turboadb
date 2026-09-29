"""Engine logcat: one argv builder, validation before side effects, adb's exit
status and error text, never inheriting stdin, and one child-shutdown path."""
import re
import subprocess
import sys
import threading

import pytest

import turboadb.core as core
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBCommandError, ADBError

# A stand-in for adb: the handler's `_base()` is pointed at it, so the real
# iter_lines / stream / logcat code runs against a real child process.
_STANDIN = r'''
import os, sys, time
mode = os.environ.get("STANDIN_MODE", "lines")
out = sys.stdout.buffer
def line(i, level="E", text="FATAL EXCEPTION"):
    out.write(("09-27 10:00:0%d.000  1234  5678 %s AndroidRuntime: %s #%d\n" % (i % 10, level, text, i)).encode())
if mode == "fail":
    sys.stderr.write("error: device offline\n")
    sys.exit(1)
if mode == "fail-late":
    line(0, "I", "hello")
    out.flush()
    sys.stderr.write("error: device offline\n")
    sys.exit(1)
if mode == "lines":
    for i in range(3):
        line(i)
    out.flush()
if mode == "slow":
    for i in range(100):
        line(i)
        out.flush()
        time.sleep(0.05)
'''


@pytest.fixture
def standin(tmp_path, monkeypatch):
    script = tmp_path / "adb_standin.py"
    script.write_text(_STANDIN)
    h = ADBHandler(ADBConfig(serial="FAKE1"))
    h._base = lambda target=True: [sys.executable, str(script)]

    def mode(name):
        monkeypatch.setenv("STANDIN_MODE", name)
        return h

    return mode


# --------------------------------------------------------------------------- #
# the one argv builder
# --------------------------------------------------------------------------- #
def test_several_tags_become_one_spec_each_and_silence_the_rest():
    args = ADBHandler.logcat_args(tag="ActivityManager, CarService", priority="e")
    assert args == ["logcat", "-v", "threadtime", "ActivityManager:E", "CarService:E", "*:S"]
    assert ADBHandler.logcat_args(tag="A B")[-3:] == ["A:V", "B:V", "*:S"]
    assert ADBHandler.logcat_args(tag=["A", "B:W"])[-3:] == ["A:V", "B:W", "*:S"]
    assert ADBHandler.logcat_args(priority="W")[-1] == "*:W"


def test_filterspecs_pass_through_as_logcat_takes_them():
    args = ADBHandler.logcat_args(filterspecs=["ActivityManager:I", "CarService:D"], tag="X")
    assert args[-2:] == ["ActivityManager:I", "CarService:D"] and "*:S" not in args


def test_history_flags_follow_the_mode():
    assert ADBHandler.logcat_args(tail=1)[1:3] == ["-T", "1"]
    assert ADBHandler.logcat_args(dump=True, tail=500)[1:4] == ["-d", "-t", "500"]
    assert "-T" not in ADBHandler.logcat_args() and "-t" not in ADBHandler.logcat_args(dump=True)
    # a restart resumes at a logcat time instead of a count
    assert ADBHandler.logcat_args(tail="09-27 10:00:00.123")[1:3] == ["-T", "09-27 10:00:00.123"]
    assert ADBHandler.logcat_args(pid=42, crashes=True) == [
        "logcat", "--pid=42", "-v", "threadtime", "-b", "crash", "-b", "main", "-b", "system", "*:E",
    ]


@pytest.mark.parametrize("bad", [
    {"tail": 0}, {"tail": -5}, {"tail": "many"}, {"priority": "X"}, {"priority": "warn"},
    {"pid": "abc"}, {"pid": 0},
])
def test_options_logcat_cannot_take_are_refused(bad):
    with pytest.raises(ValueError):
        ADBHandler.logcat_args(**bad)


def test_tail_zero_is_not_the_whole_buffer(fake_adb):
    h = ADBHandler(ADBConfig(serial="x"))
    with pytest.raises(ValueError, match="at least 1"):
        h.logcat(tail=0, dump=True)
    assert not h.logcat(tail=0, safe=True).success
    assert fake_adb.calls == []  # nothing ran


def test_a_bad_pattern_never_clears_the_device_log(fake_adb):
    h = ADBHandler(ADBConfig(serial="x"))
    with pytest.raises(re.error):
        h.logcat(clear_first=True, match="(")
    with pytest.raises(re.error):
        h.logcat(clear_first=True, grep="[")
    assert not any("-c" in call for call in fake_adb.calls)


def test_logcat_uses_the_builder_and_keeps_stderr_apart(fake_adb, monkeypatch):
    h = ADBHandler(ADBConfig(serial="x"))
    seen = {}
    monkeypatch.setattr(h, "stream", lambda args, **kw: seen.update(args=list(args), **kw))
    h.logcat(tag="A,B", priority="I", dump=True, tail=10)
    assert seen["args"] == ADBHandler.logcat_args(tag="A,B", priority="I", dump=True, tail=10)
    assert seen["stderr"] == "separate"


def test_package_resolves_to_its_process(fake_adb, monkeypatch):
    fake_adb.add("pidof", stdout="4321\n")
    h = ADBHandler(ADBConfig(serial="x"))
    assert h.pid_of("com.example.app") == 4321
    assert "pidof -s com.example.app" in " ".join(fake_adb.argv_after_adb())
    seen = {}
    monkeypatch.setattr(h, "stream", lambda args, **kw: seen.setdefault("args", list(args)))
    h.logcat(package="com.example.app", tail=1)
    assert seen["args"][:2] == ["logcat", "--pid=4321"]

    other = ADBHandler(ADBConfig(serial="y"))
    fake_adb.rules.insert(0, (lambda argv: "pidof" in argv, (1, b"", b"")))
    assert other.pid_of("com.not.running") is None
    with pytest.raises(ADBError, match="not running"):
        other.logcat(package="com.not.running")
    with pytest.raises(ValueError):
        other.logcat(package="com.example.app", pid=5)


# --------------------------------------------------------------------------- #
# exit status and adb's own error text
# --------------------------------------------------------------------------- #
def test_a_failing_logcat_raises_with_adbs_words_even_with_grep(standin):
    h = standin("fail")
    seen = []
    with pytest.raises(ADBCommandError) as info:
        h.logcat(dump=True, grep="fatal", on_line=seen.append)
    assert "device offline" in str(info.value) and info.value.result.exit_code == 1
    assert seen == []  # the error was never a "line", grep or not
    res = h.logcat(dump=True, grep="fatal", safe=True)
    assert not res.success and "device offline" in str(res.error)


def test_a_logcat_that_fails_mid_stream_returns_what_it_streamed(standin):
    h = standin("fail-late")
    res = h.logcat(dump=True)
    assert res.lines == 1 and res.exit_code == 1 and res.failed
    assert "device offline" in res.stderr
    hidden = h.logcat(dump=True, grep="nothing matches this")
    assert hidden.lines == 0 and hidden.exit_code == 1 and "device offline" in hidden.stderr


def test_a_clean_run_reports_exit_zero_and_a_stopped_one_none(standin):
    h = standin("lines")
    res = h.logcat(dump=True)
    assert res.lines == 3 and res.exit_code == 0 and not res.failed and res.stderr == ""
    stopped = standin("slow").logcat(match="#2", stop_on_match=True)
    assert stopped.matches and stopped.exit_code is None  # we ended it, not adb
    stop = threading.Event()
    res = h.logcat(stop_event=stop, on_line=lambda _line: stop.set())
    assert res.exit_code is None and res.lines >= 1


def test_iter_lines_status_tells_a_stop_from_an_exit(standin):
    h = standin("fail-late")
    status = {}
    assert list(h.iter_lines(["logcat"], stderr="separate", status=status)) == [
        "09-27 10:00:00.000  1234  5678 I AndroidRuntime: hello #0"
    ]
    assert status["returncode"] == 1 and not status["stopped"]
    assert status["stderr"].strip() == "error: device offline"
    merged = list(h.iter_lines(["logcat"]))  # the default still folds stderr in
    assert "error: device offline" in merged
    status = {}
    lines = standin("slow").iter_lines(["logcat"], status=status)
    next(lines)
    lines.close()  # the consumer broke off
    assert status["stopped"] is True
    with pytest.raises(ValueError):
        next(h.iter_lines(["logcat"], stderr="bogus"))


# --------------------------------------------------------------------------- #
# streaming children never read the caller's stdin
# --------------------------------------------------------------------------- #
def test_streaming_children_get_no_stdin(standin, monkeypatch):
    h = standin("lines")
    kwargs = []
    real = subprocess.Popen

    def recording(*args, **kw):
        kwargs.append(kw)
        return real(*args, **kw)

    monkeypatch.setattr(core.subprocess, "Popen", recording)
    list(h.iter_lines(["logcat"]))
    proc = h.popen(["logcat"], stderr="pipe")
    proc.communicate(timeout=10)
    assert [kw["stdin"] for kw in kwargs] == [subprocess.DEVNULL, subprocess.DEVNULL]
    assert kwargs[1]["stderr"] == subprocess.PIPE
    with pytest.raises(ValueError):
        h.popen(["logcat"], stderr="separate")


# --------------------------------------------------------------------------- #
# one child-shutdown helper
# --------------------------------------------------------------------------- #
class _Stubborn:
    """Ignores terminate(); only kill() ends it."""

    def __init__(self):
        self.calls = []
        self.stdin = self.stdout = self.stderr = None
        self.pipes = [_Pipe(self), _Pipe(self), _Pipe(self)]
        self.stdin, self.stdout, self.stderr = self.pipes
        self.alive = True

    def poll(self):
        return None if self.alive else -9

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")
        self.alive = False

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self.alive:
            raise subprocess.TimeoutExpired("adb", timeout)
        return -9


class _Pipe:
    def __init__(self, proc):
        self.proc = proc
        self.closed = False

    def close(self):
        self.closed = True
        self.proc.calls.append("close")


def test_the_shared_stop_escalates_reaps_and_closes_pipes():
    proc = _Stubborn()
    assert core._stop_child(proc, close_pipes=True, grace=0.01) == -9
    assert proc.calls == ["terminate", "close", "close", "close", "wait", "kill", "wait"]
    assert all(p.closed for p in proc.pipes)
    # the old name stays, and a child that already ended is only reaped
    done = _Stubborn()
    done.alive = False
    assert ADBHandler._stop_process(done) == -9 and done.calls == ["wait"]
    assert not any(p.closed for p in done.pipes)


def test_shell_session_close_uses_the_shared_stop():
    proc = _Stubborn()
    core.ShellSession(proc).close()
    assert "kill" in proc.calls and all(p.closed for p in proc.pipes)
