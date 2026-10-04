"""What a push or pull says once adb has ended: why it failed, in adb's own
words, and the files and bytes this copy really moved."""
import io

import pytest

import turboadb.core as core
from turboadb import ADBConfig, ADBHandler
from turboadb.exceptions import ADBTransferError


class _Adb:
    """A fake ``adb push`` / ``adb pull``, used as ``subprocess.Popen``: it
    does *work()* (what the copy leaves on this PC), prints *lines* and exits
    with *code*."""

    def __init__(self, lines=(), *, code=0, work=None):
        self.lines, self.code, self.work = list(lines), code, work
        self.cmd = None

    def __call__(self, cmd, **kwargs):
        self.cmd = list(cmd)
        if self.work is not None:
            self.work()
        self.stdout = io.StringIO("".join(line + "\n" for line in self.lines))
        self.returncode = self.code
        return self

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    kill = terminate


def _handler():
    return ADBHandler(ADBConfig(serial="x"))


# --------------------------------------------------------------------------- #
# why a copy failed
# --------------------------------------------------------------------------- #
def test_a_failed_push_gives_adbs_error_not_the_summary_after_it(fake_adb, monkeypatch, tmp_path):
    source = tmp_path / "app.apk"
    source.write_bytes(b"apk")
    monkeypatch.setattr(core.subprocess, "Popen", _Adb([
        "adb: error: failed to copy 'app.apk' to '/system/app/app.apk': "
        "remote couldn't create file: Read-only file system",
        "app.apk: 1 file pushed, 0 skipped. 31.2 MB/s (3 bytes in 0.001s)",
    ], code=1))
    with pytest.raises(ADBTransferError) as failed:
        _handler().push(str(source), "/system/app/app.apk")
    message = str(failed.value)
    assert message.startswith("adb push failed (exit 1): adb: error: failed to copy")
    assert "Read-only file system" in message and "1 file pushed" not in message


def test_every_error_adb_printed_is_in_the_message(fake_adb, monkeypatch, tmp_path):
    monkeypatch.setattr(core.subprocess, "Popen", _Adb([
        "adb: error: cannot create 'out/a.txt': Permission denied",
        "adb: warning: skipping special file '/data/fifo' (mode = 0o10600)",
        "error: failed to copy '/data/b.txt' to 'out/b.txt': remote Permission denied",
        "/data/: 3 files pulled, 1 skipped.",
    ], code=1))
    with pytest.raises(ADBTransferError) as failed:
        _handler().pull("/data/", str(tmp_path / "out"))
    assert str(failed.value) == (
        "adb pull failed (exit 1): adb: error: cannot create 'out/a.txt': Permission denied; "
        "error: failed to copy '/data/b.txt' to 'out/b.txt': remote Permission denied")


def test_a_failure_without_an_error_line_gives_adbs_last_words(fake_adb, monkeypatch, tmp_path):
    monkeypatch.setattr(core.subprocess, "Popen", _Adb(
        ["adb: device 'x' not found", ""], code=1))
    with pytest.raises(ADBTransferError, match=r"failed \(exit 1\): adb: device 'x' not found$"):
        _handler().pull("/sdcard/a.txt", str(tmp_path / "a.txt"))
    monkeypatch.setattr(core.subprocess, "Popen", _Adb([], code=1))
    with pytest.raises(ADBTransferError, match=r"failed \(exit 1\): \(no output\)$"):
        _handler().pull("/sdcard/a.txt", str(tmp_path / "a.txt"))


def test_a_copy_that_failed_many_times_names_the_first_five(fake_adb, monkeypatch, tmp_path):
    errors = [f"adb: error: failed to copy '/data/f{n}' to 'out/f{n}': remote Permission denied"
              for n in range(8)]
    monkeypatch.setattr(core.subprocess, "Popen",
                        _Adb(errors + ["/data/: 0 files pulled, 8 skipped."], code=1))
    with pytest.raises(ADBTransferError) as failed:
        _handler().pull("/data/", str(tmp_path / "out"))
    message = str(failed.value)
    assert message.count("failed to copy") == 5 and "/data/f4" in message
    assert "/data/f5" not in message and "skipped" not in message


def test_a_summary_that_holds_the_word_error_is_not_the_reason(fake_adb, monkeypatch, tmp_path):
    # the summary starts with the path, and a folder can be called "my error: notes"
    monkeypatch.setattr(core.subprocess, "Popen", _Adb([
        "adb: error: failed to copy 'a.txt' to 'out/a.txt': remote No space left on device",
        "/sdcard/my error: notes/: 3 files pulled, 1 skipped.",
    ], code=1))
    with pytest.raises(ADBTransferError) as failed:
        _handler().pull("/sdcard/my error: notes/", str(tmp_path / "out"))
    assert str(failed.value) == ("adb pull failed (exit 1): adb: error: failed to copy 'a.txt' "
                                 "to 'out/a.txt': remote No space left on device")


# --------------------------------------------------------------------------- #
# what a copy moved
# --------------------------------------------------------------------------- #
def _downloads(tmp_path):
    """A Downloads folder that already holds files, as on a user's PC."""
    folder = tmp_path / "Download"
    (folder / "films").mkdir(parents=True)
    (folder / "films" / "holiday.mp4").write_bytes(b"m" * 40_000)
    (folder / "notes.txt").write_bytes(b"n" * 500)
    return folder


def test_a_pull_into_a_folder_with_files_counts_only_what_it_copied(fake_adb, monkeypatch,
                                                                   tmp_path):
    folder = _downloads(tmp_path)
    adb = _Adb(["/sdcard/Download/./: 1 file pulled, 0 skipped."],
               work=lambda: (folder / "ticket.pdf").write_bytes(b"t" * 2048))
    monkeypatch.setattr(core.subprocess, "Popen", adb)
    res = _handler().pull("/sdcard/Download/.", str(folder))  # the Files tab's merge form
    assert (res.files, res.size_bytes) == (1, 2048)  # not the film that was there


def test_a_pull_under_its_own_name_counts_only_what_it_copied(fake_adb, monkeypatch, tmp_path):
    folder = _downloads(tmp_path)

    def copy():
        (folder / "notes.txt").write_bytes(b"N" * 700)  # pulled again: replaced
        (folder / "films" / "clip.mp4").write_bytes(b"c" * 300)

    monkeypatch.setattr(core.subprocess, "Popen",
                        _Adb(["/sdcard/Download/: 2 files pulled, 0 skipped."], work=copy))
    res = _handler().pull("/sdcard/Download", str(tmp_path))  # lands in the Download there
    assert (res.files, res.size_bytes) == (2, 1000)


def test_a_pull_into_a_new_folder_counts_all_of_it(fake_adb, monkeypatch, tmp_path):
    folder = tmp_path / "DCIM"

    def copy():
        (folder / "Camera").mkdir(parents=True)
        (folder / "Camera" / "a.jpg").write_bytes(b"a" * 100)
        (folder / "b.jpg").write_bytes(b"b" * 50)

    monkeypatch.setattr(core.subprocess, "Popen", _Adb([], work=copy))
    res = _handler().pull("/sdcard/DCIM", str(folder))
    assert (res.files, res.size_bytes) == (2, 150)


def test_a_pushed_folder_counts_every_file_in_it(fake_adb, monkeypatch, tmp_path):
    folder = _downloads(tmp_path)
    monkeypatch.setattr(core.subprocess, "Popen", _Adb([]))
    res = _handler().push(str(folder), "/sdcard/Download")
    assert (res.files, res.size_bytes) == (2, 40_500)


def test_a_pushed_folder_counts_a_file_that_is_still_open_at_its_size_now(fake_adb, monkeypatch,
                                                                         tmp_path):
    # a Windows folder lists a file another program still writes with the
    # size it had when it was last closed
    folder = _downloads(tmp_path)
    monkeypatch.setattr(core.subprocess, "Popen", _Adb([]))
    with open(folder / "live.log", "wb", buffering=0) as held:
        held.write(b"l" * 4096)
        res = _handler().push(str(folder), "/sdcard/Download")
    assert (res.files, res.size_bytes) == (3, 44_596)
