"""Push and pull follow their progress themselves, give up only when nothing
has moved for the whole timeout, and never leave a cut-off file behind that
looks like a whole one.

adb prints its ``[ NN%]`` lines only to a terminal: into the pipe TurboADB
reads it says nothing until the copy is over. The fake adb here behaves the
same way: it copies in steps and prints only its summary at the end."""
import os
import queue
import subprocess
import threading
import time

import pytest

import turboadb.core as core
from turboadb import ADBConfig, ADBHandler
from turboadb.exceptions import ADBTimeoutError, ADBTransferError


class _Out:
    """adb's stdout as a pipe: one line at a time, ``""`` at the end."""

    def __init__(self):
        self._lines = queue.Queue()

    def readline(self):
        return self._lines.get()

    def put(self, line):
        self._lines.put(line)

    def close(self):
        self._lines.put("")


class _Adb:
    """A fake ``adb push`` / ``adb pull``, used as ``subprocess.Popen``.

    It moves *size* bytes in *steps*, *pause* seconds apart, handing the bytes
    done so far to *moved(done)*; with *hang_after* it stops moving after that
    many steps until it is ended. Ended early, it exits 1 as adb.exe does."""

    def __init__(self, size, moved, *, steps=10, pause=0.05, hang_after=None):
        self.size, self.moved, self.steps, self.pause = size, moved, steps, pause
        self.hang_after = hang_after
        self.ended = threading.Event()
        self.returncode = None
        self.stdout = _Out()
        self.kwargs = None

    def __call__(self, cmd, **kwargs):
        self.cmd, self.kwargs = list(cmd), kwargs
        self._thread = threading.Thread(target=self._copy, daemon=True)
        self._thread.start()
        return self

    def _copy(self):
        done = 0
        for step in range(self.steps):
            if self.hang_after is not None and step >= self.hang_after:
                self.ended.wait()
            if self.ended.wait(self.pause):
                self.returncode = 1
                self.stdout.close()
                return
            done = self.size if step == self.steps - 1 else done + self.size // self.steps
            self.moved(done)
        self.stdout.put(f"1 file pulled, 0 skipped. ({self.size} bytes)\n")
        self.returncode = 0
        self.stdout.close()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise subprocess.TimeoutExpired("adb", timeout)
        return self.returncode

    def terminate(self):
        self.ended.set()

    kill = terminate


@pytest.fixture
def quick(monkeypatch):
    """Measure at once and often, so a test copy of a second is followed."""
    monkeypatch.setattr(core._TransferWatch, "DELAY_S", 0.05)
    monkeypatch.setattr(core._TransferWatch, "LOCAL_EVERY_S", 0.05)
    monkeypatch.setattr(core._TransferWatch, "DEVICE_EVERY_S", 0.05)


def _device(fake_adb, monkeypatch, answer):
    """Let *answer(shell script)* reply to the device calls a transfer makes."""

    def reply(argv):
        script = argv[-1] if "shell" in argv else ""
        return 0, str(answer(script)).encode(), b""

    monkeypatch.setattr(fake_adb, "_answer", reply)


def _sizes_of(total):
    def answer(script):
        return f"{total}\n" if "stat -L -c %s" in script or "find " in script else ""
    return answer


# --------------------------------------------------------------------------- #
# progress
# --------------------------------------------------------------------------- #
def test_a_pull_reports_its_progress_while_it_runs(fake_adb, monkeypatch, tmp_path, quick):
    target = tmp_path / "video.mp4"
    size = 1_000_000
    adb = _Adb(size, lambda done: target.write_bytes(b"v" * done), steps=10, pause=0.1)
    monkeypatch.setattr(core.subprocess, "Popen", adb)
    _device(fake_adb, monkeypatch, _sizes_of(size))
    seen = []
    res = ADBHandler(ADBConfig(serial="x")).pull("/sdcard/video.mp4", str(target),
                                                 on_progress=seen.append)
    assert res.size_bytes == size
    assert seen[-1] == 100
    during = seen[:-1]
    assert len(during) >= 3 and all(0 < pct < 100 for pct in during)  # not just the final 100
    assert during == sorted(set(during))  # forward only, each value once


def test_a_push_reports_its_progress_from_what_the_device_holds(fake_adb, monkeypatch, tmp_path,
                                                               quick):
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 400_000)
    on_device = [0]
    adb = _Adb(400_000, lambda done: on_device.__setitem__(0, done), steps=8, pause=0.1)
    monkeypatch.setattr(core.subprocess, "Popen", adb)
    asked = []

    def answer(script):
        asked.append(script)
        return f"{on_device[0]}\n" if "stat -L -c %s" in script else ""

    _device(fake_adb, monkeypatch, answer)
    seen = []
    ADBHandler(ADBConfig(serial="x")).push(str(source), "/data/local/tmp/", on_progress=seen.append)
    assert seen[-1] == 100 and len(seen) >= 3
    assert seen == sorted(set(seen))
    # the file adb writes into a folder: that folder plus the file's own name
    assert any('d="$d/"big.bin' in script for script in asked)


def test_a_quick_copy_is_never_measured(fake_adb, monkeypatch, tmp_path):
    source = tmp_path / "small.txt"
    source.write_bytes(b"hello")
    monkeypatch.setattr(core.subprocess, "Popen", _Adb(5, lambda done: None, steps=1, pause=0))
    ADBHandler(ADBConfig(serial="x")).push(str(source), "/sdcard/small.txt")
    assert fake_adb.calls == []  # no size questions to the device for a copy this short


def test_a_pull_nobody_watches_never_asks_the_device_for_its_size(fake_adb, monkeypatch, tmp_path,
                                                                 quick):
    target = tmp_path / "clip.mp4"
    adb = _Adb(100_000, lambda done: target.write_bytes(b"v" * done), steps=6, pause=0.1)
    monkeypatch.setattr(core.subprocess, "Popen", adb)
    ADBHandler(ADBConfig(serial="x")).pull("/sdcard/clip.mp4", str(target))
    assert fake_adb.calls == []  # its own file tells whether it still moves


def test_progress_only_moves_forward_and_says_100_only_at_the_end():
    seen = []
    last = -1
    for pct in (None, 10, 5, 10, 40, 100, 100):
        last = ADBHandler._report_progress(seen.append, pct, last)
    assert seen == [10, 40, 99]
    assert ADBHandler._report_progress(None, 50, 7) == 7  # nobody listening


def test_a_pulled_folder_counts_only_what_this_pull_wrote(tmp_path, fake_adb):
    folder = tmp_path / "DCIM"
    folder.mkdir()
    (folder / "old.jpg").write_bytes(b"o" * 700)  # was there before: not this pull's work
    watch = core._TransferWatch(ADBHandler(ADBConfig(serial="x")), "pull", str(folder),
                                "/sdcard/DCIM/.")
    (folder / "new.jpg").write_bytes(b"n" * 300)
    (folder / "sub").mkdir()
    (folder / "sub" / "clip.mp4").write_bytes(b"c" * 50)
    assert watch._moved() == 350
    (folder / "old.jpg").write_bytes(b"O" * 900)  # overwritten by the pull
    assert watch._moved() == 1250


def test_a_pushed_folder_is_measured_where_adb_puts_it(tmp_path, fake_adb, monkeypatch):
    local = tmp_path / "maps"
    local.mkdir()
    (local / "a.bin").write_bytes(b"a" * 1000)
    handler = ADBHandler(ADBConfig(serial="x"))

    def watch_for(reply, remote="/sdcard/maps"):
        _device(fake_adb, monkeypatch, lambda script: reply if "@@merge" in script else "")
        return core._TransferWatch(handler, "push", str(local), remote)

    new = watch_for("@@new\n")  # nothing there: adb makes /sdcard/maps the folder
    assert (new._target, new._merge) == ("/sdcard/maps", False)
    into = watch_for("@@into\n", "/sdcard")  # an existing folder: adb puts it inside
    assert (into._target, into._merge) == ("/sdcard/maps", False)
    merge = watch_for("@@merge\n", "/sdcard")  # into a folder that was there already
    assert (merge._target, merge._merge) == ("/sdcard/maps", True)
    lost = watch_for("")  # the device did not answer: nothing to measure
    assert lost._target is None and lost._moved() is None
    # merging: what the folder held at the first look is not this push's work
    answers = iter(["4000\n", "4000\n1500\n"])
    _device(fake_adb, monkeypatch, lambda script: next(answers))
    assert merge._moved() == 0 and merge._moved() == 1500
    assert merge._total() == 1000


def test_merge_sources_keep_their_dot(tmp_path, fake_adb):
    folder = tmp_path / "DCIM"
    folder.mkdir()
    watch = core._TransferWatch(ADBHandler(ADBConfig(serial="x")), "push",
                                os.path.join(str(folder), "."), "/sdcard/DCIM")
    assert watch._name() == "."  # adb copies the contents into /sdcard/DCIM itself


# --------------------------------------------------------------------------- #
# a stall limit, not a deadline
# --------------------------------------------------------------------------- #
def test_a_slow_copy_that_keeps_moving_outlasts_the_timeout(fake_adb, monkeypatch, tmp_path,
                                                          quick):
    target = tmp_path / "drive.mp4"
    # a step every 0.25 s against a 1.5 s stall limit: a busy CI runner that
    # pauses a thread for a moment (0.45 s failed 0.15 s steps against 0.6 s)
    # must not look like a stall
    adb = _Adb(100_000, lambda done: target.write_bytes(b"v" * done), steps=13, pause=0.25)
    monkeypatch.setattr(core.subprocess, "Popen", adb)
    _device(fake_adb, monkeypatch, _sizes_of(100_000))
    started = time.monotonic()
    res = ADBHandler(ADBConfig(serial="x")).pull("/sdcard/drive.mp4", str(target), timeout=1.5)
    assert time.monotonic() - started > 3.0  # twice the timeout, and still fine
    assert res.size_bytes == 100_000


def test_a_copy_that_stops_moving_times_out_and_removes_its_unfinished_file(
        fake_adb, monkeypatch, tmp_path, quick):
    target = tmp_path / "drive.mp4"
    adb = _Adb(100_000, lambda done: target.write_bytes(b"v" * done), steps=10, pause=0.05,
               hang_after=3)
    monkeypatch.setattr(core.subprocess, "Popen", adb)
    _device(fake_adb, monkeypatch, _sizes_of(100_000))
    with pytest.raises(ADBTimeoutError, match=r"nothing was copied for 0\.5 s.*unfinished file "
                                              r"was removed"):
        ADBHandler(ADBConfig(serial="x")).pull("/sdcard/drive.mp4", str(target), timeout=0.5)
    assert adb.ended.is_set() and not target.exists()


# --------------------------------------------------------------------------- #
# what an unfinished copy leaves behind
# --------------------------------------------------------------------------- #
def _cancel_after_first_step(target, *, size=100_000):
    cancel = threading.Event()

    def moved(done):
        with open(target, "ab") as fh:
            fh.write(b"v" * 1000)
        cancel.set()

    return _Adb(size, moved, steps=10, pause=0.05, hang_after=1), cancel


def test_a_cancelled_pull_removes_the_file_it_created(fake_adb, monkeypatch, tmp_path):
    target = tmp_path / "clip.mp4"
    adb, cancel = _cancel_after_first_step(str(target))
    monkeypatch.setattr(core.subprocess, "Popen", adb)
    with pytest.raises(ADBTransferError, match="cancelled; the unfinished file was removed"):
        ADBHandler(ADBConfig(serial="x")).pull("/sdcard/clip.mp4", str(target),
                                               cancel_event=cancel)
    assert not target.exists()


def test_a_file_that_was_there_before_is_never_removed(fake_adb, monkeypatch, tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"the user's own file")
    adb, cancel = _cancel_after_first_step(str(target))
    monkeypatch.setattr(core.subprocess, "Popen", adb)
    with pytest.raises(ADBTransferError, match="a partial copy may remain at .*clip.mp4"):
        ADBHandler(ADBConfig(serial="x")).pull("/sdcard/clip.mp4", str(target),
                                               cancel_event=cancel)
    assert target.exists()


def test_a_cancelled_folder_pull_keeps_what_it_copied(fake_adb, monkeypatch, tmp_path):
    folder = tmp_path / "DCIM"
    cancel = threading.Event()

    def moved(done):
        folder.mkdir(exist_ok=True)
        (folder / "whole.jpg").write_bytes(b"j" * 100)  # finished before the cancel
        cancel.set()

    monkeypatch.setattr(core.subprocess, "Popen", _Adb(1000, moved, hang_after=1))
    with pytest.raises(ADBTransferError, match="a partial copy may remain at .*DCIM"):
        ADBHandler(ADBConfig(serial="x")).pull("/sdcard/DCIM", str(folder), cancel_event=cancel)
    assert (folder / "whole.jpg").exists()


def test_a_cancelled_push_says_a_partial_copy_may_remain(fake_adb, monkeypatch, tmp_path):
    source = tmp_path / "a.bin"
    source.write_bytes(b"x" * 1000)
    cancel = threading.Event()
    monkeypatch.setattr(core.subprocess, "Popen",
                        _Adb(1000, lambda done: cancel.set(), hang_after=1))
    with pytest.raises(ADBTransferError, match="a partial copy may remain on the device"):
        ADBHandler(ADBConfig(serial="x")).push(str(source), "/sdcard/a.bin", cancel_event=cancel)
    assert source.exists()


def test_ctrl_c_during_a_pull_ends_adb_and_removes_the_unfinished_file(
        fake_adb, monkeypatch, tmp_path, quick):
    target = tmp_path / "clip.mp4"
    adb = _Adb(100_000, lambda done: target.write_bytes(b"v" * done), steps=20, pause=0.05)
    monkeypatch.setattr(core.subprocess, "Popen", adb)
    _device(fake_adb, monkeypatch, _sizes_of(100_000))

    def interrupt(_pct):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ADBHandler(ADBConfig(serial="x")).pull("/sdcard/clip.mp4", str(target),
                                               on_progress=interrupt)
    assert adb.ended.is_set() and not target.exists()
    assert adb not in core._TRACKED


# --------------------------------------------------------------------------- #
# Ctrl+C in a terminal, and children left at exit
# --------------------------------------------------------------------------- #
def test_transfers_run_in_a_session_of_their_own_off_windows(fake_adb, monkeypatch, tmp_path):
    assert core._OWN_SESSION is (os.name != "nt")
    source = tmp_path / "a.txt"
    source.write_bytes(b"a")
    for own in (True, False):
        monkeypatch.setattr(core, "_OWN_SESSION", own)
        adb = _Adb(1, lambda done: None, steps=1, pause=0)
        monkeypatch.setattr(core.subprocess, "Popen", adb)
        ADBHandler(ADBConfig(serial="x")).push(str(source), "/sdcard/a.txt")
        assert adb.kwargs["start_new_session"] is own


def test_a_transfer_is_tracked_while_it_runs(fake_adb, monkeypatch, tmp_path):
    source = tmp_path / "a.txt"
    source.write_bytes(b"a")
    tracked = []

    def moved(done):
        tracked.append(adb in core._TRACKED)

    adb = _Adb(1, moved, steps=1, pause=0.05)
    monkeypatch.setattr(core.subprocess, "Popen", adb)
    ADBHandler(ADBConfig(serial="x")).push(str(source), "/sdcard/a.txt")
    assert tracked == [True] and adb not in core._TRACKED


def test_children_still_running_at_exit_are_ended_and_cleaned_up(monkeypatch):
    class Child:
        stdin = stdout = stderr = None

        def __init__(self):
            self.code = None

        def poll(self):
            return self.code

        def terminate(self):
            self.code = -15

        kill = terminate

        def wait(self, timeout=None):
            return self.code

    monkeypatch.setattr(core, "_TRACKED", {})
    cleaned = []
    first, second = Child(), Child()
    core._track_child(first, lambda: cleaned.append("first"))
    core._track_child(second)
    core._track_child(Child(), lambda: 1 / 0)  # a failing clean-up stops nothing else
    core._end_tracked_children()
    assert first.code == -15 and second.code == -15 and cleaned == ["first"]
    assert core._TRACKED == {}
    core._end_tracked_children()  # nothing left: nothing happens
