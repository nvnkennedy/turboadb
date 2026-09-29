"""turboadb.proctree: tree walks that never take down an adb server."""
import os
import subprocess
import sys
import time

import pytest

from turboadb import proctree
from turboadb.proctree import ProcInfo


def _table(*rows):
    return {pid: ProcInfo(pid, ppid, name, created) for pid, ppid, name, created in rows}


def test_descendants_are_listed_parents_first():
    procs = _table(
        (10, 1, "cmd.exe", 100.0),
        (11, 10, "ping.exe", 101.0),
        (12, 11, "conhost.exe", 102.0),
        (13, 10, "adb.exe", 103.0),
        (99, 1, "other.exe", 50.0),
    )
    kids = proctree.descendants(10, procs)
    assert [p.pid for p in kids][0] in (11, 13)
    order = [p.pid for p in kids]
    assert set(order) == {11, 12, 13}
    assert order.index(11) < order.index(12)


def test_an_orphan_older_than_its_reused_parent_pid_is_not_a_descendant():
    procs = _table(
        (10, 1, "cmd.exe", 200.0),
        (11, 10, "ping.exe", 150.0),   # started before this cmd existed
    )
    assert proctree.descendants(10, procs) == []


def test_kill_tree_spares_an_adb_server_and_its_subtree():
    procs = _table(
        (10, 1, "cmd.exe", 100.0),
        (11, 10, "adb.exe", 101.0),     # `adb logcat` typed in the terminal
        (12, 11, "adb.exe", 102.0),     # the server it forked
        (13, 12, "adb.exe", 103.0),     # anything under the server
        (14, 10, "ping.exe", 104.0),
    )
    ended = []
    killed = proctree.kill_tree(10, procs=procs, terminate=lambda p: ended.append(p.pid) or True)
    assert sorted(killed) == [10, 11, 14]
    assert 12 not in ended and 13 not in ended
    assert ended[-1] == 10   # the root goes last


def test_kill_tree_can_keep_the_root_and_skip_names():
    procs = _table(
        (10, 1, "powershell.exe", 100.0),
        (11, 10, "conhost.exe", 101.0),
        (12, 10, "ping.exe", 102.0),
    )
    ended = []
    proctree.kill_tree(10, include_root=False, skip_names=("conhost.exe",), procs=procs,
                       terminate=lambda p: ended.append(p.pid) or True)
    assert ended == [12]


def test_is_adb_server_needs_an_adb_parent():
    procs = _table((1, 0, "explorer.exe", 1.0), (2, 1, "adb.exe", 2.0), (3, 2, "adb.exe", 3.0))
    assert not proctree.is_adb_server(procs[2], procs)
    assert proctree.is_adb_server(procs[3], procs)


def test_an_unreadable_process_table_returns_none(monkeypatch):
    monkeypatch.setattr(proctree, "_snapshot_raw", lambda: None)
    assert proctree.snapshot() is None
    assert proctree.kill_tree(12345) is None


@pytest.mark.skipif(os.name != "nt", reason="exercises the Toolhelp32 walk")
def test_kill_tree_ends_a_real_grandchild_and_keeps_the_root():
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"
    root = subprocess.Popen([sys.executable, "-c", code], creationflags=0x08000000)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(proctree.descendants(root.pid)) < 1:
            time.sleep(0.1)
        kids = proctree.descendants(root.pid)
        assert kids, "the child never appeared"
        killed = proctree.kill_tree(root.pid, include_root=False)
        assert set(killed) >= {k.pid for k in kids}
        assert root.poll() is None          # the root was kept
        assert proctree.kill_tree(root.pid) == [root.pid]
        root.wait(timeout=10)
    finally:
        if root.poll() is None:
            root.kill()
