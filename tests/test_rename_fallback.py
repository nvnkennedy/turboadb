"""Device rename works on shells whose mv has no -T (toybox before Android 11)
and still never moves a file INTO a folder or replaces without being asked."""
import os
import posixpath
import shlex
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


def _run(cmd, cwd, old_mv, loud_n=None, from_file=False):
    """*loud_n*: the words of a loud no-clobber mv (with %s for the name).
    *from_file*: the sh reads the command from a file (a Windows sh takes
    only some 8,000 characters after ``-c``)."""
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
    words = ["-c", script + cmd]
    if from_file:
        (cwd / "command.sh").write_bytes((script + cmd + "\n").encode())
        words = [(cwd / "command.sh").as_posix()]
    res = subprocess.run([_SH] + words, capture_output=True, text=True, env=env)
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


# --------------------------------------------------------------------------- #
# a new name that is the old one in other letters
# --------------------------------------------------------------------------- #
def _ignores_case(folder):
    (folder / "probe.txt").write_text("")
    found = (folder / "PROBE.TXT").exists()
    (folder / "probe.txt").unlink()
    return found


def test_a_new_name_in_other_letters_is_a_rename(tmp_path, old_mv):
    """/sdcard ignores case: ``mv -n`` skipped a.txt -> A.txt because "A.txt
    exists", and the check then said the name was taken, although nothing
    else has it."""
    if not _ignores_case(tmp_path):
        pytest.skip("this folder tells a.txt from A.txt")
    (tmp_path / "a.txt").write_text("A")
    for old, new, overwrite in (("a.txt", "A.txt", False), ("A.txt", "a.TXT", True)):
        res = _run(remotefs._rename_cmd(old, new, overwrite=overwrite), tmp_path, old_mv)
        assert res.returncode == 0, res.stderr
        names = [n for n in os.listdir(tmp_path) if n.lower() == "a.txt"]
        assert names == [new] and (tmp_path / new).read_text() == "A"


def test_a_folder_is_renamed_in_other_letters_too(tmp_path, old_mv):
    if not _ignores_case(tmp_path):
        pytest.skip("this folder tells photos from Photos")
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "one.jpg").write_text("1")
    res = _run(remotefs._rename_cmd("photos", "Photos"), tmp_path, old_mv)
    assert res.returncode == 0, res.stderr
    assert [n for n in os.listdir(tmp_path) if n.lower() == "photos"] == ["Photos"]
    assert (tmp_path / "Photos" / "one.jpg").read_text() == "1"


def _two_entries(tmp_path):
    """a.txt and A.txt as two names of one file, where a folder can hold both."""
    (tmp_path / "a.txt").write_text("A")
    if _ignores_case(tmp_path):
        pytest.skip("this folder cannot hold a.txt and A.txt")
    try:
        os.link(tmp_path / "a.txt", tmp_path / "A.txt")
    except (OSError, NotImplementedError):
        pytest.skip("hard links can't be made here")


def test_another_entry_for_the_same_file_is_a_taken_name(tmp_path, old_mv):
    """Only on storage that ignores case is A.txt the same entry as a.txt."""
    _two_entries(tmp_path)
    res = _run(remotefs._rename_cmd("a.txt", "A.txt"), tmp_path, old_mv)
    assert res.returncode != 0 and "already exists" in res.stderr
    assert sorted(os.listdir(tmp_path)) == sorted(
        ["a.txt", "A.txt"] + [n for n in os.listdir(tmp_path) if n.endswith("bin")])


def test_a_leftover_of_an_interrupted_rename_is_named(tmp_path, old_mv):
    (tmp_path / "a.txt").write_text("A")
    if not _ignores_case(tmp_path):
        try:
            os.link(tmp_path / "a.txt", tmp_path / "A.txt")
        except (OSError, NotImplementedError):
            pytest.skip("hard links can't be made here")
    (tmp_path / "A.txt.turboadb-rename").write_text("left behind")
    res = _run(remotefs._rename_cmd("a.txt", "A.txt"), tmp_path, old_mv)
    assert res.returncode != 0 and "A.txt.turboadb-rename is in the way" in res.stderr
    assert (tmp_path / "a.txt").read_text() == "A"
    assert (tmp_path / "A.txt.turboadb-rename").read_text() == "left behind"


def test_names_that_differ_in_more_than_letters_run_the_plain_rename():
    """The two-step rename is only for a name in other letters."""
    assert ".turboadb-rename" not in remotefs._rename_cmd("/sdcard/a.txt", "/sdcard/b.txt")
    assert ".turboadb-rename" in remotefs._rename_cmd("/sdcard/a.txt", "/sdcard/A.txt")


# --------------------------------------------------------------------------- #
# the temporary name of the two steps
# --------------------------------------------------------------------------- #
def test_the_temporary_name_fits_where_the_new_name_is_long():
    """It is the new name with a suffix: for a name close to the 255 bytes a
    folder takes, that was a name too long, and the rename failed."""
    short = remotefs._rename_temp("/sdcard/DCIM/a.JPG")
    assert short == "/sdcard/DCIM/a.JPG.turboadb-rename"
    assert remotefs._rename_temp("b.txt") == "b.txt.turboadb-rename"
    for name in ("A" * 251 + ".JPG", "é" * 125 + ".JPG", "写" * 83 + ".JPG"):
        temp = remotefs._rename_temp("/sdcard/DCIM/" + name)
        folder, last = posixpath.split(temp)
        assert folder == "/sdcard/DCIM" and last.endswith(".turboadb-rename")
        assert len(last.encode("utf-8")) <= 255 and last[:20] == name[:20]
    old, new = "/sdcard/DCIM/" + "A" * 251 + ".JPG", "/sdcard/DCIM/" + "A" * 251 + ".jpg"
    temp = shlex.quote(remotefs._rename_temp(new))
    assert remotefs._rename_cmd(old, new).count(temp) >= 4
    assert remotefs._rename_cmd(old, new).count(".turboadb-rename") == remotefs._rename_cmd(
        old, new).count(temp)  # the one name, everywhere


def test_a_long_name_is_renamed_in_other_letters(tmp_path, old_mv):
    if not _ignores_case(tmp_path):
        pytest.skip("this folder tells a.txt from A.txt")
    old, new = "A" * 246 + ".TXT", "a" * 246 + ".txt"
    assert _run(f"printf A > {old}", tmp_path, old_mv=False).returncode == 0
    try:
        res = _run(remotefs._rename_cmd(old, new), tmp_path, old_mv, from_file=True)
        assert res.returncode == 0, res.stderr[-200:]
        listed = _run("ls", tmp_path, old_mv=False).stdout.split()
        assert new in listed and old not in listed
    finally:  # by the sh: this Python may not reach a path this long
        _run(f"rm -f -- {old} {new}", tmp_path, old_mv=False)
