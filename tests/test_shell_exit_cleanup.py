"""A PowerShell / CMD terminal closed on the way out must not outlive TurboADB.

Closing a terminal ends the shell's process tree on a daemon thread, so the
window never waits for it.  When the app exited right after (the tabs close
in the main window's closeEvent), that thread died with the process before
its kill landed: the shell and what it ran (``ping -t``, ``adb logcat``) kept
running invisibly.  The way out now waits for those kills, briefly."""

import os
import threading
import time

import pytest


def test_join_closers_waits_for_a_closing_shell(monkeypatch):
    from turboadb.gui import local_terminal as lt

    started, release, done = threading.Event(), threading.Event(), threading.Event()

    def slow_kill(proc, wait_s=2.0):
        started.set()
        release.wait(5)
        done.set()

    monkeypatch.setattr(lt, "_kill_process_tree", slow_kill)

    class Proc:
        pid = 4242
        stdin = stdout = None

        def poll(self):
            return None

    session = lt.LocalShellSession.__new__(lt.LocalShellSession)
    session._closed = False
    session._proc = Proc()
    session._inbox = __import__("collections").deque()
    session._inbox_bytes = 0
    session._inbox_cv = threading.Condition()
    session._writer = None
    session.close()
    assert started.wait(5)
    assert lt.join_closers(0.1) == 1  # still going: reported, never waited on forever
    threading.Timer(0.2, release.set).start()
    begin = time.monotonic()
    assert lt.join_closers(5.0) == 0 and done.is_set()
    assert time.monotonic() - begin < 4.0
    assert lt.join_closers(0.0) == 0  # nothing left to wait for


def test_the_way_out_waits_for_closing_shells(monkeypatch, tmp_path):
    pytest.importorskip("PyQt5")  # gui.app is the Qt application
    from turboadb.gui import app as app_mod
    from turboadb.gui import local_terminal as lt
    from turboadb.gui import settings

    monkeypatch.setattr(settings, "_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", None)
    settings.set("stop_adb_on_exit", False)  # nothing else to stop here
    calls = []
    monkeypatch.setattr(lt, "join_closers", lambda timeout=3.0: calls.append(timeout) or 0)
    app_mod._stop_started_tools()
    assert calls and calls[0] > 0


@pytest.mark.skipif(os.name != "nt", reason="starts a real cmd.exe")
def test_real_closed_shell_and_its_command_are_gone_once_joined(tmp_path):
    from turboadb import proctree
    from turboadb.gui import local_terminal as lt

    session = lt.LocalShellSession("cmd", cwd=str(tmp_path), adb_path="__missing__")
    out = ""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and "Reply from" not in out:
        if ">" in out and "ping sent" not in out:
            session.send(b"ping -t 127.0.0.1\n")
            out += "ping sent"
        out += session.read(65536).decode("utf-8", "replace")
        time.sleep(0.02)
    assert "Reply from" in out
    shell_pid = session.proc.pid
    pings = [info.pid for info in proctree.descendants(shell_pid) if info.name == "ping.exe"]
    assert pings
    session.close()
    assert lt.join_closers(10.0) == 0
    assert session.proc.poll() is not None
    table = proctree.snapshot() or {}
    assert not any(pid in table and table[pid].name == "ping.exe" for pid in pings)
