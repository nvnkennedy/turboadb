"""Closing a scrcpy recording gives it all the time it needs to finish its MP4.

- Without a window to close (off Windows, or one that is gone), the close
  worker called ``stop()`` with its five-second default instead of the
  recording's own timeout, so a recording that needed longer to write its
  index was killed and left without one.
- Ctrl+Break is raised on scrcpy's console by attaching this process to it,
  and a process holds one console at a time: recorders stopped together (the
  displays tab closing with several recordings) refused each other's attach,
  and all but one were ended without their index.
"""

import os
import subprocess
import sys
import threading
import time
import types

import pytest


class _Session:
    """A scrcpy session stand-in: records how it was stopped."""

    def __init__(self, exits_on_close=False):
        self.running = True
        self.stop_calls = []
        self._exits_on_close = exits_on_close

    def wait(self, timeout=None):
        if self._exits_on_close:
            self.running = False
            return 0
        raise subprocess.TimeoutExpired("scrcpy", timeout)

    def stop(self, **kwargs):
        self.stop_calls.append(kwargs)
        self.running = False


def test_a_recording_without_a_window_to_close_gets_its_whole_timeout(qapp, monkeypatch):
    import turboadb.gui.mirror_panel as mp

    monkeypatch.setattr(mp, "_post_close", lambda title, hwnd=None: False)
    session = _Session()
    worker = mp._RecWaitThread(session, window_title="turboadb-record")
    finished = []
    worker.done.connect(finished.append)
    worker.run()
    assert session.stop_calls == [{"timeout": mp._RecWaitThread.CLOSE_TIMEOUT}]
    assert mp._RecWaitThread.CLOSE_TIMEOUT >= 15
    assert finished == [True]

    viewer = _Session()  # a plain screen keeps its short close
    mp._MirrorStopThread(viewer, window_title="turboadb-embed").run()
    assert viewer.stop_calls == [{"timeout": mp._MirrorStopThread.CLOSE_TIMEOUT}]


def test_a_close_that_did_not_finish_in_time_stops_with_the_same_timeout(qapp, monkeypatch):
    import turboadb.gui.mirror_panel as mp

    monkeypatch.setattr(mp, "_post_close", lambda title, hwnd=None: True)  # WM_CLOSE sent
    session = _Session()  # ...but scrcpy is still writing when the wait ends
    mp._RecWaitThread(session, window_title="turboadb-record").run()
    assert session.stop_calls == [{"timeout": mp._RecWaitThread.CLOSE_TIMEOUT}]

    clean = _Session(exits_on_close=True)
    mp._RecWaitThread(clean, window_title="turboadb-record").run()
    assert clean.stop_calls == []  # it closed: nothing more to do


# --------------------------------------------------------------------------- #
# Ctrl+Break to several recorders at once
# --------------------------------------------------------------------------- #
class _Consoles:
    """kernel32 as far as _send_ctrl_break uses it. A process is attached to
    at most one console; AttachConsole refuses a second (ERROR_ACCESS_DENIED)
    and, as the real one does for empty standard handles, points them at the
    console until FreeConsole."""

    ACCESS_DENIED = 5

    def __init__(self):
        self.attached = None
        self.raised = []
        self.std = {n: 0 for n in (0xFFFFFFF6, 0xFFFFFFF5, 0xFFFFFFF4)}
        self._lock = threading.Lock()
        self._errors = threading.local()

    def last_error(self):
        return getattr(self._errors, "code", 0)

    def kernel32(self):
        def attach(pid):
            with self._lock:
                if self.attached is not None:
                    self._errors.code = self.ACCESS_DENIED
                    return False
                self.attached = pid
                for n in self.std:
                    self.std[n] = 7000 + (n & 0xF)  # console handles
            return True

        def free():
            with self._lock:
                self.attached = None
            return True

        def generate(_event, group):
            time.sleep(0.05)  # the event is raised while this console is held
            with self._lock:
                if self.attached != group:
                    return False
                self.raised.append(group)
            return True

        def get_std(n):
            return self.std[n]

        def set_std(n, handle):
            self.std[n] = handle
            return True

        return types.SimpleNamespace(
            AttachConsole=attach, FreeConsole=free, GenerateConsoleCtrlEvent=generate,
            GetStdHandle=get_std, SetStdHandle=set_std,
        )


@pytest.fixture
def consoles(monkeypatch):
    import ctypes

    fake = _Consoles()
    kernel32 = fake.kernel32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel32)
    monkeypatch.setattr(ctypes, "get_last_error", fake.last_error)
    # the windowed exe: there is no helper process to fall back on
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    return fake


@pytest.mark.skipif(os.name != "nt", reason="Windows consoles")
def test_ctrl_break_reaches_every_recorder_stopped_at_once(consoles):
    from turboadb import scrcpy

    pids = [101, 202, 303]
    results = {}
    start = threading.Barrier(len(pids))

    def stop(pid):
        start.wait()
        results[pid] = scrcpy._send_ctrl_break(pid)

    threads = [threading.Thread(target=stop, args=(pid,)) for pid in pids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert results == {101: True, 202: True, 303: True}
    assert sorted(consoles.raised) == pids
    assert consoles.attached is None
    assert set(consoles.std.values()) == {0}  # the empty handles are back


@pytest.mark.skipif(os.name != "nt", reason="Windows consoles")
def test_ctrl_break_waits_out_another_brief_console_attach(consoles):
    """A terminal's Stop attaches to a shell's console for a moment, outside
    the recorders' own turn-taking."""
    from turboadb import scrcpy

    consoles.attached = "terminal"
    threading.Timer(0.03, lambda: setattr(consoles, "attached", None)).start()
    assert scrcpy._send_ctrl_break(404) is True
    assert consoles.raised == [404]
