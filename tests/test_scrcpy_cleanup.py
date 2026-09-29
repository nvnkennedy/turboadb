"""A scrcpy that has to be killed takes its adb clients with it, and scrcpy's
log file is never held open once scrcpy has ended.

``ScrcpySession.stop`` fell back to TerminateProcess on scrcpy.exe alone.
scrcpy is not a job object, so its long-lived ``adb shell ... app_process``
(the scrcpy server's client) and any push or forward still running outlived
it.  The log handle was only closed by ``stop()``: sessions that ended by
themselves (and every failed launch) kept %TEMP%\\turboadb-scrcpy-*.log open.
"""
import os
import subprocess
import sys
import time

import pytest

import turboadb.scrcpy as scrcpy
from turboadb import proctree
from turboadb.exceptions import ScrcpyError
from turboadb.proctree import ProcInfo

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@pytest.fixture
def unclosable(monkeypatch):
    """A scrcpy that ignores the window close and Ctrl+Break (a hung start)."""
    monkeypatch.setattr(scrcpy, "_post_close_to_pid", lambda _pid: False)
    monkeypatch.setattr(scrcpy, "_send_ctrl_break", lambda _pid: False)
    monkeypatch.setattr(scrcpy, "_LIVE_SESSIONS", [])


def _sleeper(seconds=60.0):
    """A stand-in scrcpy that runs for *seconds*. It ignores SIGTERM, so on
    POSIX the polite first request of stop() does not end it either; returns
    once that is set up."""
    code = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"print('ready', flush=True); time.sleep({seconds})")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                            creationflags=_NO_WINDOW)
    proc.stdout.readline()
    proc.stdout.close()
    return proc


def test_a_hard_stop_ends_scrcpys_adb_clients_but_spares_the_adb_server(unclosable, monkeypatch):
    proc = _sleeper()  # "scrcpy.exe"
    table = {
        proc.pid: ProcInfo(proc.pid, 1, "scrcpy.exe", 100.0),
        9001: ProcInfo(9001, proc.pid, "adb.exe", 101.0),  # adb shell ... app_process
        9002: ProcInfo(9002, 9001, "adb.exe", 102.0),  # the adb server that client started
        9003: ProcInfo(9003, proc.pid, "adb.exe", 103.0),  # an adb push still running
        9004: ProcInfo(9004, 1, "adb.exe", 90.0),  # somebody else's adb
    }
    ended = []
    monkeypatch.setattr(proctree, "snapshot", lambda: dict(table))
    monkeypatch.setattr(proctree, "_terminate", lambda info: ended.append(info.pid) or True)
    try:
        started = time.monotonic()
        scrcpy.ScrcpySession(proc, "S1").stop(timeout=0.3)
        assert time.monotonic() - started < 10
        assert proc.poll() is not None  # scrcpy itself is gone too
        assert sorted(ended) == sorted([proc.pid, 9001, 9003])
        assert ended[-1] == proc.pid  # its children first
        assert 9002 not in ended and 9004 not in ended
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_a_graceful_exit_walks_no_process_tree(monkeypatch):
    monkeypatch.setattr(scrcpy, "_post_close_to_pid", lambda _pid: True)
    monkeypatch.setattr(scrcpy, "_send_ctrl_break", lambda _pid: True)
    monkeypatch.setattr(scrcpy, "_LIVE_SESSIONS", [])
    monkeypatch.setattr(proctree, "kill_tree", lambda *_a, **_k: pytest.fail("no tree kill"))
    proc = _sleeper(0.2)  # quits by itself, in time
    scrcpy.ScrcpySession(proc, "S1").stop(timeout=10)
    assert proc.poll() is not None


def test_a_stand_in_process_is_never_tree_killed(unclosable, monkeypatch):
    """Only a real child process is walked: a made-up PID could be anybody's."""

    class Proc:
        pid = 4
        code = None

        def poll(self):
            return self.code

        def wait(self, timeout=None):
            if self.code is None:
                raise subprocess.TimeoutExpired("scrcpy", timeout)
            return self.code

        def terminate(self):
            self.code = 1

        kill = terminate

    monkeypatch.setattr(proctree, "kill_tree", lambda *_a, **_k: pytest.fail("no tree kill"))
    proc = Proc()
    scrcpy.ScrcpySession(proc, "S1").stop(timeout=0.05)
    assert proc.code == 1


@pytest.mark.skipif(os.name != "nt", reason="the Toolhelp32 walk on real processes")
def test_a_hard_stop_really_ends_a_child_process(unclosable):
    code = ("import subprocess,sys,time; "
            "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "print(c.pid, flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                            creationflags=_NO_WINDOW)
    child = None
    try:
        child = int(proc.stdout.readline())
        assert any(p.pid == child for p in proctree.descendants(proc.pid))
        scrcpy.ScrcpySession(proc, "S1").stop(timeout=0.3)
        assert proc.poll() is not None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _alive(child):
            time.sleep(0.1)
        assert not _alive(child)  # before: orphaned, sleeping on
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        if child is not None and _alive(child):
            subprocess.run(["taskkill", "/PID", str(child), "/F"], capture_output=True)


def _alive(pid):
    snap = proctree.snapshot() or {}
    return pid in snap


# --------------------------------------------------------------------------- #
# the log handle
# --------------------------------------------------------------------------- #
class _Ended:
    pid = 5

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.code = 0
        return 0

    def terminate(self):
        self.code = 1

    kill = terminate


def test_a_session_that_ended_by_itself_closes_its_log(tmp_path, monkeypatch):
    monkeypatch.setattr(scrcpy, "_LIVE_SESSIONS", [])
    log = open(tmp_path / "scrcpy.log", "w")
    proc = _Ended()
    session = scrcpy.ScrcpySession(proc, "S1", log_path=str(tmp_path / "scrcpy.log"), logfh=log)
    assert session.running and not log.closed
    proc.code = 0  # the user closed the mirror window
    assert scrcpy.live_sessions() == []  # the registry drops it...
    assert log.closed  # ...and its log is closed, so it can be deleted
    os.remove(tmp_path / "scrcpy.log")


def test_waiting_for_a_session_closes_its_log(tmp_path, monkeypatch):
    monkeypatch.setattr(scrcpy, "_LIVE_SESSIONS", [])
    log = open(tmp_path / "scrcpy.log", "w")
    session = scrcpy.ScrcpySession(_Ended(), "S1", logfh=log)
    session.wait(timeout=1)  # the WM_CLOSE path waits instead of calling stop()
    assert log.closed


def _recording_open(opened):
    def fake_open(path, *args, **kwargs):
        fh = open(path, *args, **kwargs)
        opened.append(fh)
        return fh

    return fake_open


def test_a_launch_that_fails_leaves_no_log_handle_open(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(scrcpy, "open", _recording_open(opened), raising=False)
    monkeypatch.setattr(scrcpy, "find_scrcpy", lambda _path=None: "scrcpy")

    def refuse(*_args, **_kwargs):
        raise OSError("not a valid Win32 application")

    monkeypatch.setattr(scrcpy.subprocess, "Popen", refuse)
    log_path = tmp_path / "scrcpy.log"
    with pytest.raises(ScrcpyError):
        scrcpy.launch_scrcpy("S1", log_path=str(log_path))
    assert len(opened) == 1 and opened[0].closed
    os.remove(log_path)  # on Windows an open handle made this fail


def test_a_launched_scrcpy_writes_its_log_through_its_own_handle(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(scrcpy, "open", _recording_open(opened), raising=False)
    monkeypatch.setattr(scrcpy, "find_scrcpy", lambda _path=None: sys.executable)
    monkeypatch.setattr(scrcpy, "_LIVE_SESSIONS", [])
    monkeypatch.setattr(scrcpy.ScrcpyOptions, "to_args",
                        lambda self: ["-c", "print('scrcpy 9.9')"], raising=False)
    log_path = tmp_path / "scrcpy.log"
    session = scrcpy.launch_scrcpy(None, log_path=str(log_path))
    try:
        assert opened and opened[0].closed  # TurboADB's copy closed at once
        session.wait(timeout=30)
        assert "scrcpy 9.9" in session.read_log()  # the child's output still arrived
        os.remove(log_path)
    finally:
        session.stop(timeout=1)
