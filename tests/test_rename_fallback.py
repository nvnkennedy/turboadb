"""Device rename works on shells whose mv has no -T (toybox before Android 11)
and still never moves a file INTO a folder or replaces without being asked."""
import os
import shutil
import subprocess

import pytest

from turboadb import remotefs

_SH = shutil.which("sh") or shutil.which("bash")
pytestmark = pytest.mark.skipif(_SH is None, reason="needs a POSIX sh with mv")

# An old toybox: '-T' is refused by the option parser before anything moves.
_OLD_MV = """#!/bin/sh
for a in "$@"; do
  case "$a" in
    --) break ;;
    -T) echo "mv: Unknown option 'T' (see \\"mv --help\\")" >&2; exit 1 ;;
  esac
done
exec "$REAL_MV" "$@"
"""


def _run(cmd, cwd, old_mv):
    env = dict(os.environ)
    real = subprocess.run([_SH, "-c", "command -v mv"], capture_output=True, text=True).stdout.strip()
    env["REAL_MV"] = real
    script = f'cd "{cwd.as_posix()}"; '
    if old_mv:
        bindir = cwd / "oldbin"
        bindir.mkdir(exist_ok=True)
        fake = bindir / "mv"
        fake.write_text(_OLD_MV, newline="\n")
        fake.chmod(0o755)
        # $PWD, not the path as Python writes it: "C:/…" would split PATH at
        # the drive's colon under a Windows sh, and the real mv would run
        script += 'PATH="$PWD/oldbin:$PATH"; command -v mv | grep -q oldbin || exit 97; '
    res = subprocess.run([_SH, "-c", script + cmd], capture_output=True, text=True, env=env)
    assert res.returncode != 97, "the sh did not pick up the old-toybox mv"
    return res


@pytest.fixture(params=[False, True], ids=["mv-T", "old-toybox"])
def old_mv(request):
    return request.param


def test_the_old_toybox_stand_in_refuses_T(tmp_path):
    """The old-toybox cases test the fallback only if their mv really refuses
    -T; one that quietly ran the system's mv would pass them all."""
    (tmp_path / "a").write_text("A")
    res = _run("mv -T -- a b", tmp_path, old_mv=True)
    assert res.returncode != 0 and "Unknown option 'T'" in res.stderr
    assert (tmp_path / "a").read_text() == "A" and not (tmp_path / "b").exists()


def test_a_free_name_is_renamed(tmp_path, old_mv):
    (tmp_path / "a").write_text("A")
    res = _run(remotefs._rename_cmd("a", "b"), tmp_path, old_mv)
    assert res.returncode == 0, res.stderr
    assert not (tmp_path / "a").exists() and (tmp_path / "b").read_text() == "A"


def test_an_existing_file_is_not_replaced_without_overwrite(tmp_path, old_mv):
    (tmp_path / "a").write_text("A")
    (tmp_path / "b").write_text("B")
    res = _run(remotefs._rename_cmd("a", "b"), tmp_path, old_mv)
    assert res.returncode != 0 and "already exists" in res.stderr
    assert (tmp_path / "a").read_text() == "A" and (tmp_path / "b").read_text() == "B"


def test_overwrite_replaces_an_existing_file(tmp_path, old_mv):
    (tmp_path / "a").write_text("A")
    (tmp_path / "b").write_text("B")
    res = _run(remotefs._rename_cmd("a", "b", overwrite=True), tmp_path, old_mv)
    assert res.returncode == 0, res.stderr
    assert not (tmp_path / "a").exists() and (tmp_path / "b").read_text() == "A"


def test_a_folder_at_the_new_name_is_never_moved_into(tmp_path, old_mv):
    (tmp_path / "a").write_text("A")
    (tmp_path / "b").mkdir()
    for overwrite in (False, True):
        res = _run(remotefs._rename_cmd("a", "b", overwrite=overwrite), tmp_path, old_mv)
        assert res.returncode != 0
        assert (tmp_path / "a").read_text() == "A"
        assert list((tmp_path / "b").iterdir()) == []


def test_other_mv_errors_are_reported_not_retried(tmp_path, old_mv):
    res = _run(remotefs._rename_cmd("missing", "b"), tmp_path, old_mv)
    assert res.returncode != 0 and res.stderr.strip()
    assert not (tmp_path / "b").exists()
