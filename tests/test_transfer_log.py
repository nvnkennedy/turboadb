"""The Files page's transfer history model — pure Python, no Qt, no device."""

import os

import pytest

from turboadb.gui import transfer_log as tl
from turboadb.gui.transfer_log import (
    DONE, FAILED, QUEUED, RUNNING, TransferLog,
    human_bytes, human_duration, human_speed, measure_local, measure_remote,
    transfer_name,
)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, seconds):
        self.t += seconds


def jobs(n, direction="push", prefix="/pc/f"):
    return [(f"{prefix}{i}.bin", f"/sdcard/f{i}.bin", direction) for i in range(n)]


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def test_formatting_helpers():
    assert human_bytes(None) == "—"
    assert human_bytes(0) == "0 B"
    assert human_bytes(1023) == "1023 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(5 * 1024 ** 3) == "5.0 GB"
    assert human_speed(0) == "" and human_speed(None) == ""
    assert human_speed(2 * 1024 ** 2) == "2.0 MB/s"
    assert human_duration(None) == ""
    assert human_duration(7) == "0:07"
    assert human_duration(92) == "1:32"
    assert human_duration(3723) == "1:02:03"


def test_transfer_name_handles_merge_sources_and_both_separators():
    assert transfer_name("/sdcard/DCIM/.") == "DCIM"
    assert transfer_name("C:\\Users\\me\\Photos\\.") == "Photos"
    assert transfer_name("C:\\Users\\me\\a.txt") == "a.txt"
    assert transfer_name("/sdcard/x/") == "x"


# --------------------------------------------------------------------------- #
# lifecycle and batches
# --------------------------------------------------------------------------- #
def test_items_move_through_their_states():
    clock = Clock()
    log = TransferLog(clock=clock)
    [first, second] = log.add(jobs(2))
    assert [log.get(i).status for i in (first, second)] == [QUEUED, QUEUED]
    assert log.active

    log.start(first)
    assert log.get(first).status == RUNNING
    clock.tick(2)
    log.finish(first, DONE)
    assert log.get(first).status == DONE and log.get(first).percent == 100
    assert log.get(first).elapsed(clock()) == pytest.approx(2)

    log.start(second)
    log.finish(second, FAILED, error="device offline")
    assert log.get(second).error == "device offline"
    assert not log.active


def test_a_new_batch_starts_only_when_nothing_is_active():
    log = TransferLog(clock=Clock())
    a = log.add(jobs(2))
    b = log.add(jobs(1, prefix="/pc/more"))  # still active: joins the batch
    assert {log.get(i).batch for i in a + b} == {1}
    for item_id in a + b:
        log.start(item_id)
        log.finish(item_id, DONE)
    c = log.add(jobs(1, prefix="/pc/next"))  # idle again: a fresh batch
    assert log.get(c[0]).batch == 2
    assert log.stats().total == 1  # the header describes the current batch only
    assert len(log) == 4  # but the history keeps everything


def test_finishing_twice_or_an_unknown_id_is_harmless():
    log = TransferLog(clock=Clock())
    [item_id] = log.add(jobs(1))
    log.start(item_id)
    log.finish(item_id, DONE)
    log.finish(item_id, FAILED, error="late")  # a late second report
    assert log.get(item_id).status == DONE and log.get(item_id).error == ""
    log.progress(999, 50)
    log.start(999)
    log.finish(999, DONE)
    log.set_size(999, 10)
    with pytest.raises(ValueError):
        log.finish(log.add(jobs(1))[0], RUNNING)


def test_progress_is_clamped_and_ignored_when_not_running():
    log = TransferLog(clock=Clock())
    [item_id] = log.add(jobs(1))
    log.progress(item_id, 40)  # still queued
    assert log.get(item_id).percent == 0
    log.start(item_id)
    log.progress(item_id, 150)
    assert log.get(item_id).percent == 100
    log.progress(item_id, "junk")
    assert log.get(item_id).percent == 100


# --------------------------------------------------------------------------- #
# byte-weighted progress: the whole point of measuring sizes
# --------------------------------------------------------------------------- #
def test_one_huge_file_among_many_small_ones_is_weighted_by_bytes():
    """Counting items said 99 % once the photos were done, with the 4 GB film
    still to go. By bytes it is honestly about 2 %."""
    log = TransferLog(clock=Clock())
    ids = log.add(jobs(100))
    for item_id in ids[:99]:
        log.set_size(item_id, 1024 ** 2)  # 99 photos of 1 MB
    log.set_size(ids[99], 4 * 1024 ** 3)  # one 4 GB film
    for item_id in ids[:99]:
        log.start(item_id)
        log.finish(item_id, DONE)
    stats = log.stats()
    assert stats.by_size
    assert stats.done == 99 and stats.total == 100
    assert stats.fraction == pytest.approx(99 / (99 + 4096), rel=1e-6)
    assert stats.fraction < 0.03


def test_progress_counts_items_until_every_size_is_known():
    log = TransferLog(clock=Clock())
    a, b = log.add(jobs(2))
    log.set_size(a, 1000)
    log.start(a)
    log.progress(a, 50)
    stats = log.stats()
    assert not stats.by_size and stats.measuring == 1 and stats.bytes_total is None
    assert stats.fraction == pytest.approx(0.25)  # half of one item out of two
    log.set_size(b, 3000)
    stats = log.stats()
    assert stats.by_size and stats.bytes_total == 4000
    assert stats.fraction == pytest.approx(500 / 4000)


def test_failed_and_cancelled_items_finish_the_batch_but_are_not_transferred():
    log = TransferLog(clock=Clock())
    a, b, c = log.add(jobs(3))
    for item_id in (a, b, c):
        log.set_size(item_id, 100)
    log.start(a)
    log.finish(a, DONE)
    log.start(b)
    log.progress(b, 60)
    log.finish(b, FAILED, error="denied")
    log.cancel_queued()
    stats = log.stats()
    assert (stats.done, stats.failed, stats.cancelled) == (1, 1, 1)
    assert stats.fraction == pytest.approx(1.0)  # nothing left to do
    assert stats.bytes_done == 100  # only the one that arrived intact
    assert not stats.active


def test_a_cancelled_item_needs_no_size():
    log = TransferLog(clock=Clock())
    a, b = log.add(jobs(2))
    log.set_size(a, 100)
    log.start(a)
    log.cancel_queued()  # b never started, never measured
    log.finish(a, DONE)
    stats = log.stats()
    assert stats.measuring == 0 and stats.bytes_total == 100 and stats.by_size


def test_an_estimate_never_replaces_an_exact_size():
    log = TransferLog(clock=Clock())
    [item_id] = log.add(jobs(1, direction="pull"))
    log.set_size(item_id, 4096, exact=False)  # du -sk
    assert log.get(item_id).size_exact is False
    log.set_size(item_id, 3001)  # the real figure
    log.set_size(item_id, 8192, exact=False)  # a late estimate
    assert log.get(item_id).size == 3001 and log.get(item_id).size_exact
    log.set_size(item_id, None)  # unknown: ignored
    assert log.get(item_id).size == 3001


def test_the_finished_result_size_is_authoritative():
    log = TransferLog(clock=Clock())
    [item_id] = log.add(jobs(1, direction="pull"))
    log.set_size(item_id, 4096, exact=False)
    log.start(item_id)
    log.finish(item_id, DONE, size=3001, duration=1.5)
    item = log.get(item_id)
    assert (item.size, item.size_exact, item.transferred) == (3001, True, 3001)
    assert item.elapsed(log.now()) == pytest.approx(1.5)


# --------------------------------------------------------------------------- #
# speed and time left
# --------------------------------------------------------------------------- #
def test_speed_follows_recent_progress_and_gives_a_time_left():
    clock = Clock()
    log = TransferLog(clock=clock)
    [item_id] = log.add(jobs(1))
    log.set_size(item_id, 100 * 1024 ** 2)  # 100 MB
    log.start(item_id)
    for percent in range(10, 60, 10):  # 10 MB every second
        clock.tick(1)
        log.progress(item_id, percent)
    stats = log.stats()
    assert stats.speed == pytest.approx(10 * 1024 ** 2, rel=0.05)
    assert stats.eta == pytest.approx(5, rel=0.1)  # 50 MB left at 10 MB/s


def test_a_size_that_arrives_mid_transfer_is_not_counted_as_speed():
    clock = Clock()
    log = TransferLog(clock=clock)
    [item_id] = log.add(jobs(1, direction="pull"))
    log.start(item_id)
    clock.tick(1)
    log.progress(item_id, 50)  # size unknown: nothing counted yet
    clock.tick(0.1)
    log.set_size(item_id, 10 * 1024 ** 3)  # 5 GB suddenly "done"
    clock.tick(0.1)
    stats = log.stats()
    assert stats.speed < 10 * 1024 ** 3  # not a 50 GB/s spike


def test_the_finished_batch_reports_its_average_speed():
    clock = Clock()
    log = TransferLog(clock=clock)
    a, b = log.add(jobs(2))
    log.set_size(a, 1000)
    log.set_size(b, 3000)
    log.start(a)
    clock.tick(1)
    log.finish(a, DONE)
    log.start(b)
    clock.tick(1)
    log.finish(b, DONE)
    clock.tick(30)  # time passing afterwards must not dilute it
    stats = log.stats()
    assert not stats.active and stats.eta is None
    assert stats.elapsed == pytest.approx(2)
    assert stats.speed == pytest.approx(2000)


def test_verbs_follow_the_directions_in_the_batch():
    log = TransferLog(clock=Clock())
    log.add(jobs(1, direction="push"))
    assert (log.stats().verb, log.stats().past) == ("Pushing", "Pushed")
    log.add(jobs(1, direction="pull", prefix="/sd/g"))
    assert (log.stats().verb, log.stats().past) == ("Transferring", "Transferred")


# --------------------------------------------------------------------------- #
# history management
# --------------------------------------------------------------------------- #
def test_clearing_keeps_what_is_still_active():
    log = TransferLog(clock=Clock())
    a, b, c = log.add(jobs(3))
    log.start(a)
    log.finish(a, DONE)
    log.start(b)  # running
    before = log.structure
    assert log.clear_finished() == 1
    assert [item.id for item in log] == [b, c]
    assert log.structure > before  # a view must re-sync its rows
    assert log.remove([b, c]) == 0  # active items are never dropped


def test_retryable_lists_failed_and_cancelled_items():
    log = TransferLog(clock=Clock())
    a, b, c = log.add(jobs(3))
    log.start(a)
    log.finish(a, DONE)
    log.start(b)
    log.finish(b, FAILED, error="x")
    log.cancel_queued()
    assert log.retryable() == [b, c]
    assert log.retryable([a, b]) == [b]
    assert log.get(b).job == ("/pc/f1.bin", "/sdcard/f1.bin", "push")


def test_old_history_is_trimmed_but_never_the_current_batch(monkeypatch):
    monkeypatch.setattr(TransferLog, "MAX_ITEMS", 5)
    log = TransferLog(clock=Clock())
    old = log.add(jobs(4, prefix="/pc/old"))
    for item_id in old:
        log.start(item_id)
        log.finish(item_id, DONE)
    current = log.add(jobs(4, prefix="/pc/new"))  # 8 items > 5
    ids = [item.id for item in log]
    assert all(item_id in ids for item_id in current)
    assert len(ids) == 5


def test_revision_moves_on_every_change():
    log = TransferLog(clock=Clock())
    [item_id] = log.add(jobs(1))
    seen = log.revision
    log.start(item_id)
    assert log.revision > seen and log.get(item_id).rev == log.revision


def test_report_lists_every_item_and_the_error():
    log = TransferLog(clock=Clock())
    a, b = log.add(jobs(2))
    log.set_size(a, 2048)
    log.start(a)
    log.finish(a, DONE)
    log.start(b)
    log.finish(b, FAILED, error="Permission denied")
    text = log.report()
    assert "TurboADB transfer report" in text
    assert "[done]" in text and "[failed]" in text
    assert "/pc/f0.bin -> /sdcard/f0.bin" in text
    assert "error: Permission denied" in text
    assert "1 done, 1 failed" in text


# --------------------------------------------------------------------------- #
# measuring
# --------------------------------------------------------------------------- #
def test_measure_local_sizes_files_and_folders(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 300)
    folder = tmp_path / "album"
    (folder / "sub").mkdir(parents=True)
    (folder / "1.jpg").write_bytes(b"y" * 1000)
    (folder / "sub" / "2.jpg").write_bytes(b"z" * 24)
    merge = os.path.join(str(folder), ".")  # how a merge into an existing folder is queued
    result = measure_local([str(tmp_path / "a.bin"), str(folder), merge,
                            str(tmp_path / "missing")])
    assert result[str(tmp_path / "a.bin")] == (300, 1)
    assert result[str(folder)] == (1024, 2)
    assert result[merge] == (1024, 2)
    assert str(tmp_path / "missing") not in result


class FakeShell:
    def __init__(self, reply):
        self.reply = reply
        self.commands = []

    def shell(self, command, timeout=None, safe=None):
        self.commands.append(command)
        return type("R", (), {"stdout": self.reply(command) if callable(self.reply)
                              else self.reply})()


def test_measure_remote_reads_files_exactly_and_folders_as_estimates():
    handler = FakeShell("@@0 F 1234\n@@1 D 8\n@@2 N\nnoise line\n@@9 F 1\n")
    result = measure_remote(handler, ["/sdcard/a.mp4", "/sdcard/DCIM/.", "/sdcard/gone"])
    assert result == {"/sdcard/a.mp4": (1234, True), "/sdcard/DCIM/.": (8192, False)}
    command = handler.commands[0]
    assert "/sdcard/DCIM" in command and "/sdcard/DCIM/." not in command  # merge stripped
    assert "stat -c %s" in command and "du -sk" in command


def test_measure_remote_quotes_names_with_spaces():
    handler = FakeShell("")
    measure_remote(handler, ["/sdcard/My Files/it's here.txt"])
    assert "'/sdcard/My Files/it'\"'\"'s here.txt'" in handler.commands[0]


def test_measure_remote_chunks_long_lists(monkeypatch):
    monkeypatch.setattr(tl, "_MEASURE_CHUNK", 2)

    def reply(command):
        count = command.count("@@") // 3  # three echoes per path
        return "".join(f"@@{i} F {i + 1}\n" for i in range(count))

    handler = FakeShell(reply)
    paths = [f"/sdcard/{i}" for i in range(5)]
    result = measure_remote(handler, paths)
    assert len(handler.commands) == 3
    assert result == {"/sdcard/0": (1, True), "/sdcard/1": (2, True), "/sdcard/2": (1, True),
                      "/sdcard/3": (2, True), "/sdcard/4": (1, True)}


def test_measure_remote_without_a_device_or_with_a_failing_shell():
    assert measure_remote(None, ["/sdcard/a"]) == {}

    class Broken:
        def shell(self, *a, **k):
            raise RuntimeError("offline")

    assert measure_remote(Broken(), ["/sdcard/a"]) == {}


def test_batch_size_is_kept_without_scanning():
    log = TransferLog(clock=Clock())
    a = log.add(jobs(3))
    assert log.batch_size() == 3
    log.add(jobs(2, prefix="/pc/x"))  # joins the active batch
    assert log.batch_size() == 5
    for item_id in [item.id for item in log]:
        log.start(item_id)
        log.finish(item_id, DONE)
    log.remove(a[:1])
    assert log.batch_size() == 4 == log.stats().total
    log.add(jobs(1, prefix="/pc/new"))  # a new batch
    assert log.batch_size() == 1
