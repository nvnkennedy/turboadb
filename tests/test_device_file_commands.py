"""The engine's device file commands (``make_dir``, ``touch``, ``copy``,
``move``, ``remove``, ``chmod``) tell a change that worked from one the device
refused, on every device.

A device before Android 7 has no shell protocol: ``adb shell`` exits 0
whatever the command did, and its complaint comes on the same stream as its
output. Judged by adb's exit status alone, a refused ``mkdir`` there was
reported as done. Here the commands run in a real ``sh`` that stands for the
device, once as a device that gives its exit status and once as one that does
not."""
import os
import shutil
import subprocess

import pytest

import turboadb.core as core
from turboadb import ADBConfig, ADBHandler, remotefs
from turboadb.exceptions import ADBCommandError, ADBError

_SH = shutil.which("sh") or shutil.which("bash")
pytestmark = pytest.mark.skipif(_SH is None, reason="needs a POSIX sh")

_real_run = subprocess.run  # the engine's own is replaced by the device below

# The device's rm when it refuses: it says why and removes nothing.
_REFUSING_RM = """#!/bin/sh
echo "rm: $2: Permission denied" >&2
exit 1
"""


class _Completed:
    def __init__(self, args, returncode=0, stdout=b"", stderr=b""):
        self.args, self.returncode, self.stdout, self.stderr = args, returncode, stdout, stderr


class _Device:
    """``adb shell COMMAND`` as a real sh runs it, in a folder that is the
    device's ``/device``. With *exit_status* False it is a device before
    Android 7: adb exits 0 whatever happened, and errors arrive with the
    output."""

    def __init__(self, home, exit_status):
        self.home, self.exit_status = home, exit_status
        self.root = home / "device"
        self.root.mkdir()
        self.commands, self.tools = [], []
        self.handler = ADBHandler(ADBConfig(serial="x"))

    def stand_in(self, tool, script):
        """Put *script* in the device's PATH as its *tool*."""
        bindir = self.home / "standins"
        bindir.mkdir(exist_ok=True)
        (bindir / tool).write_bytes(script.encode())  # no CRLF on Windows
        (bindir / tool).chmod(0o755)
        self.tools.append(tool)

    def refuse_rm(self):
        self.stand_in("rm", _REFUSING_RM)

    def run(self, cmd, **_kwargs):
        argv = list(cmd)
        if "shell" not in argv:
            return _Completed(argv)
        command = argv[-1]
        self.commands.append(command)
        script = f'cd "{self.home.as_posix()}" || exit 96; '
        if self.tools:
            # $PWD, not the path as Python writes it: "C:/…" would split PATH
            # at the drive's colon under a Windows sh
            script += 'PATH="$PWD/standins:$PATH"; '
            script += "".join(f"command -v {tool} | grep -q standins || exit 97; "
                              for tool in self.tools)
        script += command.replace("/device/", "./device/")
        if self.exit_status:
            res = _real_run([_SH, "-c", script], capture_output=True)
        else:  # one stream, in the order things were said
            res = _real_run([_SH, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert res.returncode not in (96, 97), "the sh was not set up for the test"
        if self.exit_status:
            return _Completed(argv, res.returncode, res.stdout, res.stderr)
        return _Completed(argv, 0, res.stdout, b"")

    def names(self, folder=""):
        return sorted(os.listdir(self.root / folder))


@pytest.fixture(params=[True, False], ids=["exit-status", "no-exit-status"])
def device(request, fake_adb, monkeypatch, tmp_path):
    dev = _Device(tmp_path, request.param)
    monkeypatch.setattr(core.subprocess, "run", dev.run)
    return dev


def test_changes_that_work_are_done_and_return_what_they_did(device):
    h = device.handler
    assert h.make_dir("/device/new/deep/") == "/device/new/deep"
    assert h.touch("/device/new/file.txt") == "/device/new/file.txt"
    assert h.touch("/device/new/file.txt") == "/device/new/file.txt"  # there already: fine
    assert h.copy("/device/new", "/device/copy") == "/device/copy"
    assert device.names("copy") == ["deep", "file.txt"]
    assert h.move("/device/copy", "/device/moved") == "/device/moved"
    assert h.chmod("/device/new/file.txt", "600") is True
    assert h.remove(["/device/new", "/device/moved"], recursive=True) == [
        "/device/new", "/device/moved"]
    assert device.names() == []


def test_a_folder_the_device_could_not_make_is_an_error_in_its_words(device):
    (device.root / "file").write_text("x")
    with pytest.raises(ADBCommandError, match="(?i)not a directory") as refused:
        device.handler.make_dir("/device/file/sub")
    assert "mkdir /device/file/sub" in str(refused.value)
    assert not refused.value.result.ok  # also where adb itself said 0
    failed = device.handler.make_dir("/device/file/sub", safe=True)
    assert not failed.success and isinstance(failed.error, ADBCommandError)
    assert device.names() == ["file"]


def test_a_file_the_device_could_not_create_is_an_error(device):
    (device.root / "file").write_text("x")
    with pytest.raises(ADBError):
        device.handler.touch("/device/file/new.txt")
    assert device.names() == ["file"] and (device.root / "file").read_text() == "x"


def test_a_copy_the_device_could_not_make_is_an_error(device):
    (device.root / "file").write_text("x")
    with pytest.raises(ADBError, match="(?i)no such file"):
        device.handler.copy("/device/file", "/device/missing/folder/copy")
    assert device.names() == ["file"]


def test_a_move_onto_a_taken_name_is_an_error_and_moves_nothing(device):
    (device.root / "a").write_text("A")
    (device.root / "b").write_text("B")
    with pytest.raises(ADBError, match="already exists"):
        device.handler.move("/device/a", "/device/b")
    assert (device.root / "a").read_text() == "A" and (device.root / "b").read_text() == "B"
    assert device.handler.move("/device/a", "/device/b", overwrite=True) == "/device/b"
    assert device.names() == ["b"] and (device.root / "b").read_text() == "A"


def test_a_delete_the_device_refused_is_an_error(device):
    (device.root / "kept.txt").write_text("x")
    device.refuse_rm()
    with pytest.raises(ADBError, match="Permission denied"):
        device.handler.remove("/device/kept.txt")
    assert device.names() == ["kept.txt"]


def test_a_complaint_that_names_the_mark_does_not_pass_for_done(device):
    (device.root / remotefs.DONE_MARK).write_text("a file of that very name")
    with pytest.raises(ADBError):
        device.handler.make_dir(f"/device/{remotefs.DONE_MARK}/sub")
    with pytest.raises(ADBError):
        device.handler.copy("/device/" + remotefs.DONE_MARK,
                            f"/device/missing/{remotefs.DONE_MARK}")


def test_the_mark_comes_only_after_a_command_that_worked(tmp_path):
    (tmp_path / "file").write_text("x")

    def marked(command):
        script = f'cd "{tmp_path.as_posix()}" || exit 96; ' + remotefs._marked(command)
        res = _real_run([_SH, "-c", script], capture_output=True, text=True)
        assert res.returncode != 96
        return res

    assert remotefs._marked_done(marked(remotefs._mkdir_cmd("new/deep")))
    assert remotefs._marked_done(marked(remotefs._rename_cmd("file", "renamed")))
    refused = marked(remotefs._mkdir_cmd("renamed/sub"))
    assert not remotefs._marked_done(refused) and refused.stderr.strip()
    (tmp_path / "taken").write_text("y")
    assert not remotefs._marked_done(marked(remotefs._rename_cmd("renamed", "taken")))
    assert (tmp_path / "renamed").read_text() == "x"


def test_a_warning_before_the_mark_does_not_undo_a_change_that_worked(device):
    # what a tool says besides doing its job comes before the mark, not after it
    device.stand_in("rm", '#!/bin/sh\necho "rm: warning: slow storage" >&2\nexec /bin/rm "$@"\n')
    (device.root / "old.log").write_text("x")
    assert device.handler.remove("/device/old.log") == ["/device/old.log"]
    assert device.names() == []


# --------------------------------------------------------------------------- #
# a folder that cannot be listed
# --------------------------------------------------------------------------- #
def test_a_folder_that_cannot_be_listed_is_an_error_not_an_empty_folder(device):
    """Without an exit status ``ls`` ended 0 for a folder that is not there:
    a mistyped path was an empty folder."""
    (device.root / "empty").mkdir()
    (device.root / "full").mkdir()
    (device.root / "full" / "a.txt").write_text("x")
    assert device.handler.list_dir("/device/empty") == []
    assert [entry["name"] for entry in device.handler.list_dir("/device/full")] == ["a.txt"]
    with pytest.raises(ADBError, match="(?i)could not list /device/missing: .*no such file"):
        device.handler.list_dir("/device/missing")


# The old toolbox ls: it lists no "." and "..", and on some builds every start
# of it comes with a line from the linker.
_NOISY_OLD_LS = """#!/bin/sh
echo "WARNING: linker: /system/bin/ls has text relocations." >&2
for last in "$@"; do :; done
[ -d "$last" ] || { echo "$last: No such file or directory" >&2; exit 1; }
for name in $(/bin/ls -A "$last"); do
  echo "-rw-rw---- root     sdcard_r        1 2014-01-01 00:00 $name"
done
"""


def test_an_empty_folder_is_empty_also_where_the_listing_tool_is_noisy(device):
    """What ls said in place of entries is not always a refusal: the device
    is asked, with a mark, whether the folder can be listed."""
    device.stand_in("ls", _NOISY_OLD_LS)
    (device.root / "empty").mkdir()
    (device.root / "full").mkdir()
    (device.root / "full" / "a.txt").write_text("x")
    assert device.handler.list_dir("/device/empty") == []
    assert [entry["name"] for entry in device.handler.list_dir("/device/full")] == ["a.txt"]
    with pytest.raises(ADBError, match="(?is)could not list /device/missing: .*no such file"):
        device.handler.list_dir("/device/missing")


# --------------------------------------------------------------------------- #
# a save's room and stamp
# --------------------------------------------------------------------------- #
def test_the_room_and_the_stamp_of_a_file_are_read_by_a_real_shell(tmp_path):
    name = "it's a $HOME file.txt"
    (tmp_path / name).write_bytes(b"x" * 1234)

    def asked(target):
        script = f'cd "{tmp_path.as_posix()}" || exit 96; ' + remotefs._write_over_check_cmd(target)
        res = _real_run([_SH, "-c", script], capture_output=True)
        assert res.returncode != 96
        return remotefs._parse_write_over_check(res.stdout)

    free, size, stamp = asked(name)
    if free is None:
        pytest.skip("this sh's stat is not the device's (no -f -c)")
    assert free > 0 and size == 1234 and stamp.startswith("1234 ")
    assert asked(name) == (free, size, stamp) or asked(name)[1:] == (size, stamp)
    assert asked("missing.txt") == (None, None, None)


# --------------------------------------------------------------------------- #
# a folder given its own name in other letters
# --------------------------------------------------------------------------- #
def _ignores_case(folder):
    (folder / "probe.txt").write_text("")
    found = (folder / "PROBE.TXT").exists()
    (folder / "probe.txt").unlink()
    return found


def test_a_folder_is_renamed_to_its_own_name_in_other_letters(device):
    """/sdcard ignores case: ``mv /sdcard/Docs /sdcard/docs`` found the folder
    "docs" there and tried to move Docs into itself."""
    if not _ignores_case(device.root):
        pytest.skip("this folder tells Docs from docs")
    (device.root / "Docs").mkdir()
    (device.root / "Docs" / "one.txt").write_text("1")
    assert device.handler.move("/device/Docs", "/device/docs") == "/device/docs"
    assert device.names() == ["docs"]
    assert (device.root / "docs" / "one.txt").read_text() == "1"
    assert device.handler.move("/device/docs/one.txt", "/device/docs/ONE.txt") == (
        "/device/docs/ONE.txt")
    assert device.names("docs") == ["ONE.txt"]


def test_a_folder_moved_into_another_one_still_goes_into_it(device):
    (device.root / "Docs").mkdir()
    (device.root / "Archive").mkdir()
    assert device.handler.move("/device/Docs", "/device/Archive") == "/device/Archive/Docs"
    assert device.names() == ["Archive"] and device.names("Archive") == ["Docs"]


def test_a_folder_is_never_copied_onto_its_own_name_in_other_letters(device):
    """``cp /sdcard/Docs /sdcard/docs`` found the folder "docs" there and began
    to copy Docs into itself."""
    if not _ignores_case(device.root):
        pytest.skip("this folder tells Docs from docs")
    (device.root / "Docs").mkdir()
    (device.root / "Docs" / "one.txt").write_text("1")
    with pytest.raises(ADBError, match="the same"):
        device.handler.copy("/device/Docs", "/device/docs")
    assert device.names() == ["Docs"] and device.names("Docs") == ["one.txt"]
