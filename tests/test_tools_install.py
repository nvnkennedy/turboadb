"""Installing downloaded tools: file permissions, the folder swap, and the
shared adb servers an update has to stop and bring back.  No network, no adb.
"""
import os
import pathlib
import stat
import time
import warnings
import zipfile

import pytest

from turboadb import devices, tools, toolsdl

_ADB = toolsdl._exe("adb")


# --------------------------------------------------------------------------- #
# extraction keeps the Unix permissions
# --------------------------------------------------------------------------- #
def _entry(name, mode):
    info = zipfile.ZipInfo(name)
    info.external_attr = mode << 16
    return info


@pytest.mark.skipif(os.name == "nt", reason="Unix permission bits")
def test_extracted_tools_keep_their_permissions(tmp_path):
    """Only adb was made executable: fastboot and the rest came out 0644."""
    zp = tmp_path / "pt.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(_entry("platform-tools/fastboot", 0o100755), b"x")
        z.writestr(_entry("platform-tools/NOTICE.txt", 0o100644), b"x")
        z.writestr(_entry("platform-tools/open-to-all", 0o100777), b"x")
        z.writestr(_entry("platform-tools/made-on-windows", 0), b"x")
    toolsdl._extract_zip(str(zp), str(tmp_path / "out"))
    folder = tmp_path / "out" / "platform-tools"

    def mode(name):
        return stat.S_IMODE(os.stat(folder / name).st_mode)

    assert mode("fastboot") == 0o755
    assert mode("NOTICE.txt") == 0o644
    assert mode("open-to-all") == 0o755  # nobody else's write
    assert mode("made-on-windows") & 0o600 == 0o600  # left as written


def test_an_entry_listed_twice_keeps_its_own_permissions(tmp_path, monkeypatch):
    zp = tmp_path / "dup.zip"
    with warnings.catch_warnings(), zipfile.ZipFile(zp, "w") as z:
        warnings.simplefilter("ignore")  # "Duplicate name": the point of the test
        z.writestr(_entry("pt/tool", 0o100644), b"first")
        z.writestr(_entry("pt/tool", 0o100755), b"second")
    kept = []
    monkeypatch.setattr(toolsdl, "_keep_mode", lambda path, mode: kept.append(mode & 0o777))
    toolsdl._extract_zip(str(zp), str(tmp_path / "out"))
    assert kept == [0o644, 0o755]
    assert (tmp_path / "out" / "pt" / "tool").read_bytes() == b"second"


# --------------------------------------------------------------------------- #
# the folder swap
# --------------------------------------------------------------------------- #
def _folder(path, text):
    path.mkdir(parents=True, exist_ok=True)
    (path / _ADB).write_text(text)
    return path


def test_a_stuck_old_copy_no_longer_blocks_the_update(tmp_path, monkeypatch):
    """A terminal whose working folder was inside platform-tools.old made the
    next update fail, blaming a running adb server."""
    live = _folder(tmp_path / "platform-tools", "v2")
    stuck = _folder(tmp_path / "platform-tools.old", "v1")
    real_rmtree = toolsdl.shutil.rmtree

    def rmtree(path, ignore_errors=False, **kw):
        if os.path.abspath(path) == os.path.abspath(stuck):
            return  # still in use: it stays
        real_rmtree(path, ignore_errors=ignore_errors, **kw)

    monkeypatch.setattr(toolsdl.shutil, "rmtree", rmtree)
    toolsdl._swap_dir(str(_folder(tmp_path / "new", "v3")), str(live))
    assert (live / _ADB).read_text() == "v3"
    assert (tmp_path / "platform-tools.old-1" / _ADB).read_text() == "v2"  # the rollback copy
    assert (stuck / _ADB).read_text() == "v1"


def test_leftover_old_copies_are_cleared_by_the_next_update(tmp_path):
    live = _folder(tmp_path / "platform-tools", "v2")
    _folder(tmp_path / "platform-tools.old-1", "v0")
    toolsdl._swap_dir(str(_folder(tmp_path / "new", "v3")), str(live))
    assert not (tmp_path / "platform-tools.old-1").exists()
    assert (tmp_path / "platform-tools.old" / _ADB).read_text() == "v2"


def test_a_move_that_fails_halfway_puts_the_old_version_back(tmp_path, monkeypatch):
    """A copy between volumes that failed (a full disk) left a partial folder
    where the old one should have come back."""
    live = _folder(tmp_path / "platform-tools", "v2")

    def half_move(src, dst):
        os.makedirs(dst)
        (open(os.path.join(dst, "partial.dll"), "w")).close()
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(toolsdl.shutil, "move", half_move)
    with pytest.raises(OSError):
        toolsdl._swap_dir(str(_folder(tmp_path / "new", "v3")), str(live))
    assert (live / _ADB).read_text() == "v2"
    assert not (live / "partial.dll").exists()


@pytest.fixture
def home(monkeypatch, tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    return path


def test_a_download_is_staged_beside_the_tools(home, monkeypatch):
    """Staged under %TEMP%, the final move became a copy whenever the profile
    was on another volume."""
    old = os.path.join(toolsdl._ensure_tools_dir(), ".download-left-by-a-crash")
    os.makedirs(old)
    os.utime(old, (time.time() - 7200,) * 2)
    swapped = []

    def extract(zip_path, dest_parent, strip_top_to=None):
        _folder(pathlib.Path(dest_parent) / "platform-tools", "new")

    def swap(new_dir, dest):
        swapped.append(new_dir)
        toolsdl.shutil.move(new_dir, dest)

    monkeypatch.setattr(toolsdl, "_archive_to_download", lambda: {"url": "https://example/pt.zip"})
    monkeypatch.setattr(toolsdl, "_download", lambda url, dest, on_progress=None: open(dest, "wb").close())
    monkeypatch.setattr(toolsdl, "_check_zip", lambda path: None)
    monkeypatch.setattr(toolsdl, "_verify_platform_tools", lambda path, archive: None)
    monkeypatch.setattr(toolsdl, "_extract_zip", extract)
    monkeypatch.setattr(toolsdl, "_swap_dir", swap)
    monkeypatch.setattr(toolsdl, "_sync_scrcpy_adb", lambda: None)
    assert toolsdl.download_platform_tools(force=True) == toolsdl.managed_adb()
    tools_dir = os.path.realpath(toolsdl.tools_dir())
    assert os.path.dirname(os.path.dirname(os.path.realpath(swapped[0]))).startswith(tools_dir)
    assert os.path.basename(os.path.dirname(os.path.dirname(swapped[0]))).startswith(".download-")
    leftovers = [n for n in os.listdir(tools_dir) if n.startswith(".download-")]
    assert leftovers == []  # this one cleaned up, the old one from the crash too


# --------------------------------------------------------------------------- #
# shared servers across an update
# --------------------------------------------------------------------------- #
@pytest.fixture
def update(monkeypatch, home):
    """Both downloads faked; shared servers on the ports in ``world.shared``."""
    world = type("World", (), {})()
    world.shared, world.events = {5037}, []
    managed = {"adb": None, "scrcpy": None}
    for name, folder in (("adb", toolsdl.adb_dir()), ("scrcpy", toolsdl.scrcpy_dir())):
        path = os.path.join(folder, toolsdl._exe(name))
        _folder(pathlib.Path(folder), "old")
        open(path, "w").close()
        managed[name] = path

    def swap(new_dir, dest):
        world.events.append(("swap", os.path.basename(dest)))

    def kill(adb, port=5037, **kw):
        world.events.append(("stop", port))
        world.shared.discard(port)

    def restart(port, adb_path=None):
        world.events.append(("share", port))
        world.shared.add(port)
        return "shared again"

    def extract(zip_path, dest_parent, strip_top_to=None):
        target = strip_top_to or os.path.join(dest_parent, "platform-tools")
        name = "scrcpy" if strip_top_to else "adb"
        os.makedirs(target, exist_ok=True)
        open(os.path.join(target, toolsdl._exe(name)), "w").close()

    monkeypatch.setattr(toolsdl, "managed_adb", lambda: managed["adb"])
    monkeypatch.setattr(toolsdl, "managed_scrcpy", lambda: managed["scrcpy"])
    monkeypatch.setattr(toolsdl, "_archive_to_download", lambda: {"url": "https://example/pt.zip"})
    monkeypatch.setattr(toolsdl, "_scrcpy_assets", lambda: ("https://example/s.zip", "s.zip", None))
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: True)
    monkeypatch.setattr(toolsdl, "_download", lambda url, dest, on_progress=None: open(dest, "wb").close())
    monkeypatch.setattr(toolsdl, "_check_zip", lambda path: None)
    monkeypatch.setattr(toolsdl, "_verify_platform_tools", lambda path, archive: None)
    monkeypatch.setattr(toolsdl, "_extract_zip", extract)
    monkeypatch.setattr(toolsdl, "_swap_dir", swap)
    monkeypatch.setattr(toolsdl, "_sync_scrcpy_adb", lambda: None)
    monkeypatch.setattr(tools, "kill_adb_server", kill)
    monkeypatch.setattr(devices, "server_is_shared",
                        lambda port=5037, adb_path=None: port in world.shared)
    monkeypatch.setattr(devices, "restart_shared_server", restart)
    return world


_BOTH = {"adb": {"upgrade": True, "installed": "36.0.0", "latest": "37.0.1"},
         "scrcpy": {"upgrade": True, "installed": "3.0", "latest": "3.1"}}


def test_updating_adb_and_scrcpy_restarts_a_shared_server_once(update):
    """Each restart drops every other machine's shells, logcats and mirrors."""
    result = toolsdl.upgrade_tools(checks=_BOTH)
    assert set(result["updated"]) == {"adb", "scrcpy"} and not result["errors"]
    shares = [e for e in update.events if e[0] == "share"]
    assert shares == [("share", 5037)]
    assert update.events[-1] == ("share", 5037)  # after both swaps
    assert update.events.index(("swap", "scrcpy")) < update.events.index(("share", 5037))


def test_a_server_shared_on_another_port_is_stopped_and_brought_back(update):
    """`turboadb serve --port 5050` ran the same adb, which an update replaced,
    and nothing brought it back."""
    devices._remember_shared_port(5050)
    update.shared = {5050}
    toolsdl.download_platform_tools(force=True)
    assert ("stop", 5050) in update.events
    assert update.events.index(("stop", 5050)) < update.events.index(("swap", "platform-tools"))
    assert update.events[-1] == ("share", 5050)
    assert ("share", 5037) not in update.events  # the local one was never shared


def test_every_restart_problem_is_reported(update, monkeypatch):
    devices._remember_shared_port(5050)
    update.shared = {5037, 5050}

    def refuse(port, adb_path=None):
        raise RuntimeError(f"port {port} in use")

    monkeypatch.setattr(devices, "restart_shared_server", refuse)
    problem = toolsdl.upgrade_tools(checks=_BOTH)["errors"]["sharing"]
    assert "port 5037 in use" in problem and "port 5050 in use" in problem
    assert "turboadb serve --port 5050" in problem


# --------------------------------------------------------------------------- #
# Google's manifest, read once per update
# --------------------------------------------------------------------------- #
def test_the_check_and_the_download_read_the_manifest_once(monkeypatch):
    reads = []
    archive = {"version": "37.0.1", "url": "https://example/pt.zip", "size": 1}
    monkeypatch.setattr(toolsdl, "_CHECKED_ARCHIVE", None)
    monkeypatch.setattr(toolsdl, "latest_adb_archive", lambda: reads.append(1) or dict(archive))
    assert toolsdl.latest_adb_version() == "37.0.1"
    assert toolsdl._archive_to_download() == archive
    assert len(reads) == 1
    assert toolsdl._archive_to_download() == archive  # taken once: read again
    assert len(reads) == 2
    stale = (time.monotonic() - toolsdl._CHECKED_ARCHIVE_TTL - 1, dict(archive, url="old"))
    monkeypatch.setattr(toolsdl, "_CHECKED_ARCHIVE", stale)
    assert toolsdl._archive_to_download() == archive  # too old to trust: read again
    assert len(reads) == 3
