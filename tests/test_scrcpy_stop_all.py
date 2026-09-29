"""Closing TurboADB stops its scrcpy sessions all at once.

``scrcpy.stop_all`` gave each session its whole time to quit (5 s) one after
another, on the GUI thread after the window had closed: with four mirrors a
scrcpy that ignored the request kept the app alive for 20 s.  They now share
one budget.
"""
import subprocess
import threading
import time

import pytest

from turboadb import scrcpy


class _Stubborn:
    """A scrcpy that ignores the request to quit and ends only when killed."""

    def __init__(self, pid):
        self.pid = pid
        self._gone = threading.Event()

    def poll(self):
        return 0 if self._gone.is_set() else None

    def terminate(self):
        pass  # SIGTERM ignored, as a stuck scrcpy may

    def kill(self):
        self._gone.set()

    def wait(self, timeout=None):
        if not self._gone.wait(timeout):
            raise subprocess.TimeoutExpired("scrcpy", timeout)
        return 0


@pytest.fixture
def sessions(monkeypatch):
    monkeypatch.setattr(scrcpy, "_post_close_to_pid", lambda pid: False)
    monkeypatch.setattr(scrcpy, "_send_ctrl_break", lambda pid: False)
    monkeypatch.setattr(scrcpy, "_LIVE_SESSIONS", [])
    return [scrcpy.ScrcpySession(_Stubborn(pid), f"S{pid}") for pid in (11, 12, 13, 14)]


def test_every_session_gets_the_same_time_to_quit_at_once(sessions):
    started = time.monotonic()
    assert scrcpy.stop_all(timeout=0.5) == 4
    took = time.monotonic() - started
    assert took < 1.5, took  # one after another it took four times 0.5 s
    assert not any(session.running for session in sessions)
    assert scrcpy.live_sessions() == []


def test_nothing_to_stop_returns_at_once(monkeypatch):
    monkeypatch.setattr(scrcpy, "_LIVE_SESSIONS", [])
    started = time.monotonic()
    assert scrcpy.stop_all(timeout=5) == 0
    assert time.monotonic() - started < 0.5
