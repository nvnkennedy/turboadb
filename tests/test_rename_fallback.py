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


# GNU mv 9.2 and later: "-n" refuses loudly ("not replacing 'b'", exit 1),
# where older ones and toybox skip quietly; other mvs and languages word it
# their own way ($LOUD_N_WORDS). Stands in whatever the host has.
_LOUD_N_MV = """#!/bin/sh
last=""; noclobber=""
for a in "$@"; do
  case "$a" in -n) noclobber=1 ;; esac
  last="$a"
done
if [ -n "$noclobber" ] && { [ -e "$last" ] || [ -L "$last" ]; }; then
  printf "$LOUD_N_WORDS\\n" "$last" >&2; exit 1
fi
exec "$HOST_MV" "$@"
"""
_GNU_WORDS = "mv: not replacing '%s'"


def _run(cmd, cwd, old_mv, loud_n=None):
    """*loud_n*: the words of a loud no-clobber mv (with %s for the name)."""
    env = dict(os.environ)
    real = subprocess.run([_SH, "-c", "command -v mv"], capture_output=True, text=True).stdout.strip()
    env["REAL_MV"] = env["HOST_MV"] = real
    script = f'cd "{cwd.as_posix()}"; '
    if loud_n:
        env["LOUD_N_WORDS"] = loud_n
        bindir = cwd / "loudbin"
        bindir.mkdir(exist_ok=True)
        fake = bindir / "mv"
        fake.write_bytes(_LOUD_N_MV.encode())  # no CRLF on Windows (write_text's newline= is 3.10+)
        fake.chmod(0o755)
        # the old-toybox stand-in below runs this one as its "real" mv
        script += ('PATH="$PWD/loudbin:$PATH"; REAL_MV="$PWD/loudbin/mv"; export REAL_MV; '
                   'command -v mv | grep -q loudbin || exit 97; ')
    if old_mv:
        bindir = cwd / "oldbin"
        bindir.mkdir(exist_ok=True)
        fake = bindir / "mv"
        fake.write_bytes(_OLD_MV.encode())
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


def test_the_loud_no_clobber_stand_in_refuses_loudly(tmp_path):
    (tmp_path / "a").write_text("A")
    (tmp_path / "b").write_text("B")
    res = _run("mv -n -T -- a b", tmp_path, old_mv=False, loud_n=_GNU_WORDS)
    assert res.returncode == 1 and "not replacing 'b'" in res.stderr
    assert (tmp_path / "a").read_text() == "A" and (tmp_path / "b").read_text() == "B"


@pytest.mark.parametrize("words", [_GNU_WORDS, "mv: %s wird nicht ersetzt"], ids=["gnu", "translated"])
def test_an_existing_file_is_named_plainly_with_a_loud_no_clobber_mv(tmp_path, old_mv, words):
    """GNU mv 9.2+ (Ubuntu 24.04) refuses in words of its own where toybox
    skips quietly: the rename still says the name exists, and nothing is
    replaced."""
    (tmp_path / "a").write_text("A")
    (tmp_path / "b").write_text("B")
    res = _run(remotefs._rename_cmd("a", "b"), tmp_path, old_mv, loud_n=words)
    assert res.returncode != 0 and "already exists" in res.stderr, res.stderr
    assert (tmp_path / "a").read_text() == "A" and (tmp_path / "b").read_text() == "B"
    free = _run(remotefs._rename_cmd("a", "c"), tmp_path, old_mv, loud_n=words)
    assert free.returncode == 0 and (tmp_path / "c").read_text() == "A"


def test_a_missing_file_is_not_renamed_onto_an_existing_name(tmp_path, old_mv):
    """Both names still there is a refusal; a source that is gone is not."""
    (tmp_path / "b").write_text("B")
    for loud_n in (None, _GNU_WORDS):
        res = _run(remotefs._rename_cmd("missing", "b"), tmp_path, old_mv, loud_n=loud_n)
        assert res.returncode != 0 and "already exists" not in res.stderr, res.stderr
        assert res.stderr.strip() and (tmp_path / "b").read_text() == "B"


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
