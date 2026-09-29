"""`turboadb logcat`: patterns and flags checked before anything connects, the
--match total after Ctrl+C, adb's failure as an error even under --grep, a
closed pipe ending quietly, and output that stays live through a pipe."""
import errno
import io
import os
import subprocess
import sys
import time
import types

import pytest

import turboadb.cli as cli
from turboadb.config import ADBConfig

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Dev:
    def __init__(self, logcat=None):
        self.config = ADBConfig()
        self.calls = []
        self.kwargs = None
        self._logcat = logcat or (lambda **kw: types.SimpleNamespace(matches=[]))

    def connect(self):
        self.calls.append("connect")

    def logcat(self, **kw):
        self.calls.append("logcat")
        self.kwargs = kw
        return self._logcat(**kw)


@pytest.fixture
def run(monkeypatch, capsys):
    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: True)

    def go(argv, dev):
        monkeypatch.setattr(cli, "_handler", lambda args, **kw: dev)
        rc = cli.main(argv)
        out, err = capsys.readouterr()
        return rc, out, err

    return go


@pytest.mark.parametrize("argv, why", [
    (["logcat", "--match", "("], "invalid --match pattern"),
    (["logcat", "--clear", "--match", "ANR("], "invalid --match pattern"),
    (["logcat", "--grep", "["], "invalid --grep pattern"),
    (["logcat", "--stop-on-match"], "--stop-on-match needs --match"),
    (["logcat", "--dump", "--tail", "0"], "--tail needs at least 1"),
    (["logcat", "--pid", "5", "--package", "com.example"], "give --pid or --package, not both"),
])
def test_unusable_options_are_usage_errors_before_connecting(run, argv, why):
    dev = _Dev()
    rc, _out, err = run(argv, dev)
    assert rc == 2 and f"ERROR: {why}" in err and "Traceback" not in err
    assert dev.calls == []  # no device wait, and --clear never ran


def test_ctrl_c_still_prints_the_match_total_and_exits_130(run):
    def logcat(**kw):
        for i in range(3):
            kw["on_line"](f"E Tag: ANR {i}")
            if i < 2:
                kw["on_match"](f"E Tag: ANR {i}")
        raise KeyboardInterrupt

    rc, out, err = run(["logcat", "--match", "ANR"], _Dev(logcat))
    assert rc == 130
    assert out.splitlines() == ["E Tag: ANR 0", "E Tag: ANR 1", "E Tag: ANR 2"]
    assert "[2 matched lines]" in err


def test_adb_failure_is_an_error_even_when_grep_hid_every_line(run):
    def logcat(**kw):
        return types.SimpleNamespace(matches=[], exit_code=1, stderr="error: device offline\n")

    rc, out, err = run(["logcat", "--grep", "fatal", "--package", "com.example"], _Dev(logcat))
    assert rc == 1 and out == ""
    assert "ERROR: adb logcat ended with exit 1: error: device offline" in err
    ok = _Dev(lambda **kw: types.SimpleNamespace(matches=[], exit_code=0, stderr=""))
    assert run(["logcat", "--dump", "--pid", "42"], ok)[0] == 0
    assert ok.kwargs["pid"] == 42 and ok.kwargs["package"] is None


@pytest.mark.parametrize("error", [BrokenPipeError(errno.EPIPE, "Broken pipe"),
                                   OSError(errno.EINVAL, "Invalid argument")])
def test_a_reader_that_goes_away_ends_the_stream_quietly(run, monkeypatch, error):
    class Gone(io.StringIO):
        def write(self, _text):
            raise error

    def logcat(**kw):
        kw["on_line"]("09-27 10:00:00.000  1  1 I Tag: first")
        raise AssertionError("the stream must end at the closed pipe")

    monkeypatch.setattr(sys, "stdout", Gone())
    rc, _out, err = run(["logcat", "--dump"], _Dev(logcat))
    assert rc == 0 and "ERROR" not in err


def test_output_stays_live_through_a_pipe():
    child = r'''
import sys, time, types
sys.path.insert(0, %r)
import turboadb.cli as cli
from turboadb.config import ADBConfig

class Dev:
    config = ADBConfig()
    def connect(self):
        pass
    def logcat(self, **kw):
        for i in range(3):
            kw["on_line"]("line %%d" %% i)
            time.sleep(0.4)
        return types.SimpleNamespace(matches=[], exit_code=0, stderr="")

cli.adb_available = lambda *a, **k: True
cli._handler = lambda args, **kw: Dev()
sys.exit(cli.main(["-s", "X", "logcat"]))
''' % REPO
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)
    proc = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env)
    try:
        first = proc.stdout.readline()
        first_at = time.monotonic()
        rest = proc.stdout.read()
        ended_at = time.monotonic()
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert first.strip() == b"line 0" and rest.split() == [b"line", b"1", b"line", b"2"]
    # buffered output would only arrive at exit, all at once
    assert ended_at - first_at > 0.5
