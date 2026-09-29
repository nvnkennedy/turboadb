"""One way to stop a child process, used by the engine, scrcpy and the GUI's
capture and probe streams alike."""
import os
import subprocess
import sys

import pytest

import turboadb.core as core
import turboadb.scrcpy as scrcpy
from turboadb import ADBConfig, ADBHandler, proctree


class _Child:
    """Records what is done to it; *stubborn* ignores terminate()."""

    def __init__(self, *, stubborn=False, ended=False):
        self.calls = []
        self.stubborn = stubborn
        self.code = 0 if ended else None
        self.stdin = self.stdout = self.stderr = None

    def poll(self):
        return self.code

    def terminate(self):
        self.calls.append("terminate")
        if not self.stubborn:
            self.code = -15

    def kill(self):
        self.calls.append("kill")
        self.code = -9

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self.code is None:
            raise subprocess.TimeoutExpired("adb", timeout)
        return self.code


def test_the_engine_stops_its_children_through_the_shared_helper():
    assert core._stop_child is proctree.stop_process
    assert ADBHandler._stop_process is proctree.stop_process


def test_a_grace_of_zero_kills_at_once_and_reaps():
    child = _Child()
    assert proctree.stop_process(child, grace=0) == -9
    assert child.calls == ["kill", "wait"]


def test_a_caller_that_must_not_block_never_waits():
    child = _Child(stubborn=True)
    assert proctree.stop_process(child, grace=0, reap=False) is None
    assert child.calls == ["kill"]


def test_a_polite_stop_that_works_is_never_escalated():
    child = _Child()
    assert proctree.stop_process(child, grace=1.0) == -15
    assert child.calls == ["terminate", "wait"]


def test_only_a_real_child_that_still_runs_takes_its_tree_with_it(monkeypatch):
    walked = []
    monkeypatch.setattr(proctree, "kill_tree", lambda pid, **kw: walked.append(pid) or [])
    # a stand-in's PID could be anybody's
    proctree.stop_process(_Child(stubborn=True), grace=0, tree=True)
    assert walked == []
    real = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            creationflags=0x08000000 if os.name == "nt" else 0)
    try:
        assert proctree.stop_process(real, grace=0, tree=True) is not None
        assert walked == [real.pid]
        walked.clear()
        proctree.stop_process(real, grace=0, tree=True)  # reaped: its PID may be reused
        assert walked == []
    finally:
        if real.poll() is None:
            real.kill()
            real.wait(10)


def test_scrcpy_ends_through_the_shared_helper(monkeypatch):
    stops = []
    monkeypatch.setattr(proctree, "stop_process",
                        lambda proc, **kw: stops.append(kw) or proc.kill())
    monkeypatch.setattr(scrcpy, "_post_close_to_pid", lambda _pid: True)
    monkeypatch.setattr(scrcpy, "_send_ctrl_break", lambda _pid: True)
    monkeypatch.setattr(scrcpy, "_LIVE_SESSIONS", [])
    child = _Child(stubborn=True)
    child.pid = 4321
    scrcpy.ScrcpySession(child, "S1").stop(timeout=0.01)
    assert stops == [{"grace": 0 if os.name == "nt" else 0.01, "tree": True}]
    assert scrcpy.live_sessions() == []


def test_the_gui_probe_and_capture_helpers_share_it(monkeypatch):
    pytest.importorskip("PyQt5")
    from turboadb.gui import device_tab, logcat_view, screencap_view

    stops = []
    monkeypatch.setattr(proctree, "stop_process", lambda proc, **kw: stops.append(kw))
    device_tab._DeviceProbe._kill(_Child())
    device_tab._DeviceProbe._kill(_Child(), reap=True)
    logcat_view._LogcatThread._kill(_Child())
    screencap_view._kill(_Child(), wait=False)
    screencap_view._kill(None)  # nothing to stop
    assert stops == [
        {"grace": 0, "close_pipes": True, "reap": False},  # the probe, from the UI thread
        {"grace": 0, "close_pipes": True, "reap": True},  # the probe's own worker
        {"grace": 0, "close_pipes": True, "reap": False},  # logcat: its reader reaps it
        {"grace": 0, "reap": False},
    ]


def test_install_and_install_multiple_build_the_same_switches(fake_adb):
    dev = ADBHandler(ADBConfig(serial="S1"))
    fake_adb.add("install", stdout="Success\n")
    dev.install("app.apk", replace=True, downgrade=True, grant_perms=True, allow_test=True)
    assert fake_adb.argv_after_adb()[-6:] == ["install", "-r", "-d", "-g", "-t", "app.apk"]
    dev.install_multiple(["base.apk", "split.apk"], replace=False, grant_perms=True)
    assert fake_adb.argv_after_adb()[-4:] == ["install-multiple", "-g", "base.apk", "split.apk"]
