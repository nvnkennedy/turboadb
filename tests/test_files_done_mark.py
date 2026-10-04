"""The mark that says a device command of the Files tab worked.

Before Android 7 ``adb shell`` gives no exit status, so the tab ends New
folder, New file, Paste and Rename with a mark and reads that (a scripted
device without exit status is in test_files_device_operations.py).  Here the
marked commands run in a real ``sh``: the mark comes when the command did its
job, and only then."""
import os
import shutil
import subprocess

import pytest

pytest.importorskip("PyQt5")

from turboadb import remotefs  # noqa: E402
from turboadb.gui import file_browser as fb  # noqa: E402

_SH = shutil.which("sh") or shutil.which("bash")
pytestmark = pytest.mark.skipif(_SH is None, reason="needs a POSIX sh")

# An mv that refuses -T, as toybox did before Android 11: the rename then
# takes its other way, which ends the shell itself when it fails.
_OLD_MV = """#!/bin/sh
for a in "$@"; do
  case "$a" in
    --) break ;;
    -T) echo "mv: Unknown option 'T'" >&2; exit 1 ;;
  esac
done
exec "$REAL_MV" "$@"
"""


def _marked_run(command, cwd, old_mv=False):
    """Run *command*, marked, in a real sh in *cwd* (with the old mv first in
    PATH when *old_mv*)."""
    env = dict(os.environ)
    script = f'cd "{cwd.as_posix()}" || exit 96; '
    if old_mv:
        env["REAL_MV"] = subprocess.run([_SH, "-c", "command -v mv"], capture_output=True,
                                        text=True).stdout.strip()
        bindir = cwd / "oldbin"
        bindir.mkdir(exist_ok=True)
        (bindir / "mv").write_bytes(_OLD_MV.encode())  # no CRLF on Windows
        (bindir / "mv").chmod(0o755)
        # $PWD, not the path as Python writes it: "C:/…" would split PATH at
        # the drive's colon under a Windows sh
        script += 'PATH="$PWD/oldbin:$PATH"; command -v mv | grep -q oldbin || exit 97; '
    res = subprocess.run([_SH, "-c", script + fb._marked(command)], capture_output=True,
                         text=True, env=env)
    assert res.returncode not in (96, 97), "the sh was not set up for the test"
    return res


def test_a_command_that_worked_ends_with_the_mark(tmp_path):
    for command in (remotefs._mkdir_cmd("new/deep"), remotefs._touch_cmd("new/file.txt"),
                    remotefs._touch_cmd("new/file.txt"),  # there already: still fine
                    remotefs._copy_into_cmd("new", "copy", False)):
        res = _marked_run(command, tmp_path)
        assert fb._marked_done(res), (command, res.stderr)
    assert (tmp_path / "copy" / "file.txt").exists() and (tmp_path / "copy" / "deep").is_dir()


def test_a_command_that_failed_prints_no_mark(tmp_path):
    (tmp_path / "file").write_text("x")
    for command in (remotefs._mkdir_cmd("file/sub"), remotefs._touch_cmd("file/new.txt"),
                    remotefs._copy_into_cmd("missing", "copy", False)):
        res = _marked_run(command, tmp_path)
        assert not fb._marked_done(res), command
        assert res.stderr.strip(), command  # and the shell says why
    assert sorted(p.name for p in tmp_path.iterdir()) == ["file"]


@pytest.mark.parametrize("old_mv", [False, True], ids=["mv-T", "old-toybox"])
def test_a_rename_is_marked_only_when_it_renamed(tmp_path, old_mv):
    (tmp_path / "a").write_text("A")
    (tmp_path / "b").write_text("B")
    res = _marked_run(remotefs._rename_cmd("a", "c"), tmp_path, old_mv)
    assert fb._marked_done(res), res.stderr
    assert (tmp_path / "c").read_text() == "A" and not (tmp_path / "a").exists()

    res = _marked_run(remotefs._rename_cmd("c", "b"), tmp_path, old_mv)  # "b" is taken
    assert not fb._marked_done(res) and res.stderr.strip()
    assert (tmp_path / "c").read_text() == "A" and (tmp_path / "b").read_text() == "B"

    res = _marked_run(remotefs._rename_cmd("c", "b", overwrite=True), tmp_path, old_mv)
    assert fb._marked_done(res), res.stderr
    assert (tmp_path / "b").read_text() == "A" and not (tmp_path / "c").exists()

    res = _marked_run(remotefs._rename_cmd("gone", "d"), tmp_path, old_mv)
    assert not fb._marked_done(res) and not (tmp_path / "d").exists()
