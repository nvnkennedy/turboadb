"""Leaving TurboADB: whose adb server it is, and how long the exit waits for it.

The server lock is held across blocking adb calls (a kill waits up to 20 s, a
shared-server start 10 s more, a closing tab's disconnect about 10 s), and the
exit took the same lock on the GUI thread, so a closed app lingered that long.

And a server another program started while TurboADB was starting its own
(both found none running) answered TurboADB's launcher, counted as TurboADB's
and was stopped on exit.  No test here runs a real adb.
"""
import os
import threading
import time
import types

import pytest

from turboadb import tools

_STARTED = b"* daemon not running; starting now at tcp:5037\n* daemon started successfully\n"
# what adb 37 prints when another server took the port first (seen on a real one)
_LOST_RACE = (b"* daemon not running; starting now at tcp:5037\ncould not read ok from ADB "
              b"Server\n* failed to start daemon\nerror: cannot connect to daemon\n")


class _Launcher:
    """A fake ``adb start-server`` launcher that prints and exits on cue."""

    def __init__(self, made, kw, world):
        self.code = None
        self.killed = False
        self.out = kw.get("stdout")
        made.append(self)
        if world.prints:
            self.say(world.prints)

    def say(self, text):
        self.out.write(text)
        self.out.flush()

    def poll(self):
        return self.code

    def kill(self):
        self.killed = True
        self.code = -9


@pytest.fixture
def world(monkeypatch, tmp_path):
    adb = tmp_path / ("adb.exe" if os.name == "nt" else "adb")
    adb.write_text("")
    state = types.SimpleNamespace(made=[], alive=False, adb=str(adb), prints=b"")
    monkeypatch.setattr(tools.subprocess, "Popen",
                        lambda cmd, **kw: _Launcher(state.made, kw, state))
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: state.alive)
    monkeypatch.setattr(tools, "find_adb", lambda path=None: state.adb)
    monkeypatch.setattr(tools, "adb_server_status", lambda *a, **k: None)
    return state


# --------------------------------------------------------------------------- #
# whose server is it?
# --------------------------------------------------------------------------- #
def test_a_server_another_program_started_first_is_not_ours(world):
    assert tools.ensure_adb_server(world.adb, timeout=0.02) is False
    launcher = world.made[0]
    world.alive = True  # the other program's server took the port
    assert tools.ensure_adb_server(world.adb, timeout=1.0) is True  # usable all the same
    launcher.say(_LOST_RACE)
    launcher.code = 1
    assert tools.owned_adb_server() is None


def test_ours_once_our_launcher_says_it_started_it(world):
    world.prints = _STARTED
    assert tools.ensure_adb_server(world.adb, timeout=0.02) is False
    world.alive = True
    assert tools.ensure_adb_server(world.adb, timeout=1.0) is True
    world.made[0].code = 0
    assert tools.owned_adb_server() == world.adb


def test_ours_while_our_launcher_is_still_reporting(world):
    """A launcher whose daemon answers but has not said so yet (the exit can
    come that early) still counts: that is nearly always its own daemon."""
    assert tools.ensure_adb_server(world.adb, timeout=0.02) is False
    world.alive = True
    assert tools.owned_adb_server() == world.adb
    world.made[0].say(_STARTED)
    world.made[0].code = 0
    assert tools.owned_adb_server() == world.adb
    assert not tools._UNCONFIRMED  # settled, nothing left to watch


def test_a_launcher_whose_daemon_answers_is_left_to_end_by_itself(world):
    """Ending it before its daemon reported to it takes the daemon down (seen
    on a real adb), so the way out only forgets it."""
    assert tools.ensure_adb_server(world.adb, timeout=0.02) is False
    world.alive = True
    tools.owned_adb_server()
    tools.end_server_launchers()
    assert not world.made[0].killed and not tools._UNCONFIRMED


def test_a_kill_forgets_a_launcher_it_was_still_watching(world, monkeypatch):
    assert tools.ensure_adb_server(world.adb, timeout=0.02) is False
    world.alive = True
    assert tools.owned_adb_server() == world.adb
    monkeypatch.setattr(tools.subprocess, "run", lambda cmd, **kw: None)
    world.alive = False
    tools.kill_adb_server(world.adb, wait=0)
    assert not tools._UNCONFIRMED and tools.owned_adb_server() is None


# --------------------------------------------------------------------------- #
# the exit's patience
# --------------------------------------------------------------------------- #
@pytest.fixture
def busy_lock():
    """Another thread holding the server lock (a kill against a hung adb)."""
    taken, release = threading.Event(), threading.Event()

    def hold():
        with tools.adb_server_lock():
            taken.set()
            release.wait(20)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert taken.wait(5)
    yield release
    release.set()
    holder.join(5)


def test_the_exit_gives_up_on_a_busy_server_lock(busy_lock):
    started = time.monotonic()
    with tools.server_lock_patience(0.3):
        with pytest.raises(tools.ServerLockBusy):
            tools.owned_adb_server()
        with pytest.raises(tools.ServerLockBusy):
            tools.end_server_launchers()
    assert time.monotonic() - started < 2.0


def test_other_threads_still_wait_for_the_lock(busy_lock):
    """Only the thread that asked for patience gives up: a start must never
    overlap a stop again."""
    done = threading.Event()

    def ask():
        tools.owned_adb_server()
        done.set()

    with tools.server_lock_patience(0.1):  # this thread's, not the asker's
        threading.Thread(target=ask, daemon=True).start()
        assert not done.wait(0.5)
    busy_lock.set()
    assert done.wait(5)


def test_the_exits_own_kill_is_bounded_by_the_patience(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen["timeout"] = kw["timeout"]
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(tools.subprocess, "run", run)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: True)  # it never lets go
    started = time.monotonic()
    with tools.server_lock_patience(0.5):
        tools.kill_adb_server("adb")
    assert seen["timeout"] <= 1.0
    assert time.monotonic() - started < 2.0  # not the 5 s port wait


def test_closing_the_app_is_not_held_by_a_long_server_task(busy_lock, monkeypatch, tmp_path):
    pytest.importorskip("PyQt5")
    from turboadb import scrcpy
    from turboadb.gui import app as app_mod
    from turboadb.gui import local_terminal, settings

    monkeypatch.setattr(settings, "_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(local_terminal, "join_closers", lambda timeout: 0)
    monkeypatch.setattr(scrcpy, "stop_all", lambda *a, **k: 0)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: True)
    monkeypatch.setattr(app_mod, "_EXIT_SERVER_WAIT_S", 0.3)
    warned = []
    monkeypatch.setattr(app_mod.logging.getLogger("turboadb.gui"), "warning",
                        lambda msg, *args: warned.append(msg % args))
    started = time.monotonic()
    app_mod._stop_started_tools()
    assert time.monotonic() - started < 3.0  # was as long as the other task took
    assert any("could not stop the adb server" in line for line in warned)
