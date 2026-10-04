"""Saving a file back to the device (``replace_file``): the step that writes
the pushed copy over the file.

``cat copy > file`` empties the file before it writes, so a write that could
not finish (the partition full, the cable pulled) left a file with part of the
new content, and the copy next to it was removed all the same. The device is
now asked for room first, and a copy the file may depend on is kept."""
import pytest

from turboadb import ADBConfig, ADBHandler
from turboadb.exceptions import ADBError

_FILE = "/data/app.ini"
_BEFORE = "2026-10-03 10:00:00.000000000 +0000"


class _Device:
    """The device's side of a save: what its shell answers.

    *free_blocks* (of 4096 bytes) and *size* are what ``stat`` says about the
    file and its file system (None: this ``stat`` can't say). *write* is how
    the write-over ends: ``"ok"``, ``"refused"`` (the file can't be opened),
    ``"cut"`` (it was emptied, then the write failed) or ``"gone"`` (the
    device went away during it)."""

    def __init__(self, *, free_blocks=1000, size=1000, write="ok"):
        self.free_blocks, self.size, self.write = free_blocks, size, write
        self.stamp = _BEFORE
        self.cuts = 0
        self.gone = False
        self.commands = []

    def answer(self, argv):
        command = argv[-1] if "shell" in argv else ""
        self.commands.append(command)
        if self.gone:
            return 1, b"", b"adb: device offline\n"
        if command.startswith("stat -c %a"):
            return 0, b"644\n", b""
        if "@@room" in command:
            room = "" if self.free_blocks is None else f"{self.free_blocks} 4096"
            stamp = "" if self.size is None else f"{self.size} {self.stamp}"
            return 0, f"@@room {room}\n@@file {stamp}\n".encode(), b""
        if "TURBOADB_WRITTEN" in command:
            if self.write == "ok":
                return 0, b"TURBOADB_WRITTEN\n", b""
            if self.write == "refused":
                return 1, b"", f"/system/bin/sh: can't create {_FILE}: Permission denied\n".encode()
            if self.write == "cut":
                self.cuts += 1
                self.size = 4096
                self.stamp = f"2026-10-03 10:07:{30 + self.cuts}.250000000 +0000"
                return 1, b"", b"cat: write error: No space left on device\n"
            self.gone = True
            return 1, b"", b"error: closed\n"
        return 0, b"", b""

    def ran(self, start):
        return [command for command in self.commands if command.startswith(start)]


@pytest.fixture
def save(fake_adb, monkeypatch, tmp_path):
    """``save(device, kilobytes)`` saves a local file of that size over the
    device file through a handler whose push always works; ``save.handler``
    and ``save.pushed`` (the temporary copies) are there to look at."""
    handler = ADBHandler(ADBConfig(serial="x"))
    pushed = []
    monkeypatch.setattr(handler, "_transfer",
                        lambda direction, a, b, *rest: pushed.append(b) or "ok")

    def run(device, kilobytes=1):
        monkeypatch.setattr(fake_adb, "_answer", device.answer)
        local = tmp_path / "app.ini"
        local.write_bytes(b"k" * 1024 * kilobytes)
        return handler.replace_file(str(local), _FILE)

    run.handler, run.pushed = handler, pushed
    return run


# --------------------------------------------------------------------------- #
# room on the device, asked before the file is touched
# --------------------------------------------------------------------------- #
def test_a_save_the_device_has_no_room_for_leaves_the_file_as_it_was(save):
    device = _Device(free_blocks=10, size=1000)  # 40 KB free
    with pytest.raises(ADBError, match=r"was not saved: .*40\.0 KB.*free") as refused:
        save(device, kilobytes=100)
    assert "as it was" in str(refused.value)
    assert not device.ran("cat ")  # never written over: it would have been emptied first
    assert device.ran("rm -f -- ") == [f"rm -f -- {save.pushed[0]}"]  # the copy goes


def test_the_room_the_file_takes_now_counts(save):
    device = _Device(free_blocks=0, size=200 * 1024)  # full, but the file shrinks
    assert save(device, kilobytes=100) == "644"
    assert len(device.ran("cat ")) == 1


def test_a_save_with_room_is_written_over_and_its_copy_removed(save):
    device = _Device(free_blocks=1000)
    assert save(device, kilobytes=100) == "644"
    temp = save.pushed[0]
    assert temp.startswith("/data/.turboadb-save-")
    assert device.ran("cat ") == [f"cat {temp} > {_FILE} && echo TURBOADB_WRITTEN"]
    assert device.commands[-1] == f"rm -f -- {temp}"


def test_a_device_whose_stat_cannot_say_is_saved_to_as_before(save):
    device = _Device(free_blocks=None, size=None)
    assert save(device, kilobytes=100) == "644"
    assert device.commands[-1] == f"rm -f -- {save.pushed[0]}"


# --------------------------------------------------------------------------- #
# a write-over that fails
# --------------------------------------------------------------------------- #
def test_a_file_cut_short_keeps_the_whole_copy_next_to_it(save):
    device = _Device(write="cut")
    with pytest.raises(ADBError, match="No space left on device") as failed:
        save(device)
    temp = save.pushed[0]
    message = str(failed.value)
    assert message.startswith(f"{_FILE} was not saved: ")
    assert "part of the new content" in message and message.rstrip(".").endswith(temp)
    assert not device.ran("rm -f -- ")  # the only whole copy on the device


def test_a_device_that_goes_away_during_the_write_keeps_the_copy_too(save):
    device = _Device(write="gone")
    with pytest.raises(ADBError, match="was not saved") as failed:
        save(device)
    assert save.pushed[0] in str(failed.value)
    assert not device.ran("rm -f -- ")


def test_a_file_that_could_not_be_opened_is_as_it_was_and_the_copy_goes(save):
    device = _Device(write="refused")
    with pytest.raises(ADBError, match="was not saved: .*Permission denied") as failed:
        save(device)
    assert "part of the new content" not in str(failed.value)
    assert device.commands[-1] == f"rm -f -- {save.pushed[0]}"


def test_a_refusal_on_a_device_whose_stat_cannot_say_removes_the_copy_as_before(save):
    device = _Device(free_blocks=None, size=None, write="refused")
    with pytest.raises(ADBError, match="Permission denied"):
        save(device)
    assert device.commands[-1] == f"rm -f -- {save.pushed[0]}"


def test_a_second_save_that_is_cut_short_keeps_only_its_own_copy(save):
    device = _Device(write="cut")
    with pytest.raises(ADBError):
        save(device)
    with pytest.raises(ADBError) as failed:
        save(device)
    older, newer = save.pushed
    assert newer in str(failed.value)
    assert device.ran("rm -f -- ") == [f"rm -f -- {older}"]


def test_the_next_save_that_works_removes_the_copy_that_was_kept(save):
    device = _Device(write="cut")
    with pytest.raises(ADBError):
        save(device)
    device.write = "ok"  # room was made
    assert save(device) == "644"
    kept, temp = save.pushed
    assert device.ran("rm -f -- ") == [f"rm -f -- {temp}", f"rm -f -- {kept}"]


def test_a_refusal_that_names_the_mark_is_not_taken_for_written(fake_adb, monkeypatch, tmp_path):
    """Before Android 7 the shell's complaint comes with the output, and it
    names the file: one called TURBOADB_WRITTEN.txt was reported as saved."""
    target = "/sdcard/TURBOADB_WRITTEN.txt"
    fake_adb.exit_status = False
    fake_adb.add("stat -c %a", stdout="644\n")
    fake_adb.add("cat ", returncode=1,
                 stderr=f"/system/bin/sh: can't create {target}: Permission denied\n")
    handler = ADBHandler(ADBConfig(serial="x"))
    monkeypatch.setattr(handler, "_transfer", lambda *args: "ok")
    local = tmp_path / "new.txt"
    local.write_bytes(b"new")
    with pytest.raises(ADBError, match="was not saved: .*Permission denied"):
        handler.replace_file(str(local), target)
