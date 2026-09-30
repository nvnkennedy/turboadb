"""A device rename onto a name that is a symbolic link to a folder never moves
the item into that folder.

``mv`` without ``-T`` treats a destination that resolves to a folder as the
place to move INTO, link or not.  On a head unit whose toybox has no ``-T``,
"Replace" of such a link (the Files tab asks, since a link is not a real
folder) moved the file into the linked folder instead, over any file of the
same name there."""
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
        fake.write_bytes(_OLD_MV.encode())  # no CRLF on Windows (write_text's newline= is 3.10+)
        fake.chmod(0o755)
        # $PWD, not the path as Python writes it: "C:/…" would split PATH at
        # the drive's colon under a Windows sh, and the real mv would run
        script += 'PATH="$PWD/oldbin:$PATH"; command -v mv | grep -q oldbin || exit 97; '
    res = subprocess.run([_SH, "-c", script + cmd], capture_output=True, text=True, env=env)
    assert res.returncode != 97, "the sh did not pick up the old-toybox mv"
    return res


def _link_to_folder(tmp_path):
    """``b`` -> ``real/``, a folder that holds its own ``a``."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "a").write_text("KEEP")
    try:
        os.symlink(real, tmp_path / "b", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links can't be made here")
    return real


def test_replacing_a_link_to_a_folder_never_moves_into_it_on_an_old_toybox(tmp_path):
    (tmp_path / "a").write_text("A")
    real = _link_to_folder(tmp_path)

    res = _run(remotefs._rename_cmd("a", "b", overwrite=True), tmp_path, old_mv=True)

    assert res.returncode != 0 and "folder" in res.stderr
    assert (real / "a").read_text() == "KEEP"  # the linked folder's own file is untouched
    assert sorted(p.name for p in real.iterdir()) == ["a"]
    assert (tmp_path / "a").read_text() == "A"  # and nothing moved


def test_a_rename_that_may_not_replace_leaves_the_linked_folder_alone(tmp_path):
    (tmp_path / "a").write_text("A")
    real = _link_to_folder(tmp_path)
    (real / "a").unlink()  # room in the folder: a plain mv would move a into it

    res = _run(remotefs._rename_cmd("a", "b"), tmp_path, old_mv=True)

    assert res.returncode != 0
    assert list(real.iterdir()) == []
    assert (tmp_path / "a").read_text() == "A"


def test_mv_with_T_still_replaces_the_link_itself(tmp_path):
    (tmp_path / "a").write_text("A")
    real = _link_to_folder(tmp_path)

    res = _run(remotefs._rename_cmd("a", "b", overwrite=True), tmp_path, old_mv=False)

    if res.returncode == 0:  # a system whose rename can replace a folder link
        assert not os.path.islink(tmp_path / "b") and (tmp_path / "b").read_text() == "A"
    assert (real / "a").read_text() == "KEEP"
    assert sorted(p.name for p in real.iterdir()) == ["a"]
