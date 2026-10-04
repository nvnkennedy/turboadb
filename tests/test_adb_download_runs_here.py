"""Only an adb that runs on this machine is installed.

Google builds platform-tools for Windows, macOS and x86-64 Linux with glibc.
On an ARM board (or with musl, or on NixOS) the Linux download cannot execute,
yet an update check installed it: no managed copy counted as "update
available".  The managed copy comes before the system's adb, so every command
then failed with "Exec format error" although a working adb was installed, and
each later check, which could read no version from it, downloaded it again.

The download is not attempted where no build exists, and one that does not
run is not installed.  No network and no real adb: the archive is made here.
"""
import os
import stat
import subprocess
import zipfile

import pytest

from turboadb import tools, toolsdl
from turboadb.exceptions import ADBError, ADBNotFoundError

_ADB = toolsdl._exe("adb")
# conftest stands in for both, so that every other test's faked download installs
_supported = toolsdl.adb_download_supported
_require_runnable = toolsdl._require_runnable


@pytest.fixture
def checks(monkeypatch):
    """The real checks, on a machine Google builds adb for."""
    monkeypatch.setattr(toolsdl, "adb_download_supported", lambda: True)
    monkeypatch.setattr(toolsdl, "_require_runnable", _require_runnable)


@pytest.fixture
def download(monkeypatch, tmp_path):
    """A platform-tools archive whose adb is a text file; an adb of the system
    on PATH.  Returns what happened to the adb server and the downloads made."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    archive = tmp_path / "platform-tools.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("platform-tools/" + _ADB, "\x7fELF: an adb for another kind of machine")
    system = tmp_path / "usr-bin" / _ADB
    system.parent.mkdir()
    system.write_text("")
    system.chmod(system.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(system.parent))
    monkeypatch.delenv("TURBOADB_ADB", raising=False)
    monkeypatch.setattr(tools, "_gui_setting", lambda name: None)
    monkeypatch.setattr(tools, "_bundled_candidates", lambda name: [])
    monkeypatch.setattr(tools, "_sdk_candidates", lambda name: [])
    events = []

    def fetch(url, dest, on_progress=None):
        events.append("download")
        with open(archive, "rb") as src, open(dest, "wb") as out:
            out.write(src.read())

    monkeypatch.setattr(toolsdl, "_archive_to_download", lambda: {"url": "https://example/pt.zip"})
    monkeypatch.setattr(toolsdl, "_download", fetch)
    monkeypatch.setattr(toolsdl, "_verify_platform_tools", lambda path, archive: None)
    monkeypatch.setattr(toolsdl, "_kill_adb_server", lambda adb=None: events.append("stop") or [])
    monkeypatch.setattr(toolsdl, "_sync_scrcpy_adb", lambda: None)
    tools.clear_tools_cache()
    yield events, str(system)
    tools.clear_tools_cache()


def test_a_download_that_cannot_run_here_is_not_installed(download, checks):
    events, system = download
    with pytest.raises(ADBError, match="does not run on this machine"):
        toolsdl.download_platform_tools(force=True)
    assert toolsdl.managed_adb() is None
    assert os.path.samefile(tools.find_adb(), system)  # still the adb that works
    leftovers = [n for n in os.listdir(toolsdl.tools_dir()) if n.startswith(".download-")]
    assert leftovers == []

    result = toolsdl.upgrade_tools(checks={"adb": {"upgrade": True, "installed": None,
                                                   "latest": "37.0.1"},
                                           "scrcpy": {"upgrade": False}})
    assert "does not run on this machine" in result["errors"]["adb"] and not result["updated"]
    assert toolsdl.managed_adb() is None


def test_the_server_is_left_alone_when_the_new_adb_does_not_run(download, checks, monkeypatch):
    """The adb being replaced keeps running its server: nothing replaces it."""
    events, _system = download
    managed = os.path.join(toolsdl.adb_dir(), _ADB)
    os.makedirs(os.path.dirname(managed))
    with open(managed, "w") as fh:
        fh.write("the adb in use")
    with pytest.raises(ADBError):
        toolsdl.download_platform_tools(force=True)
    assert events == ["download"]  # never "stop"
    with open(managed) as fh:
        assert fh.read() == "the adb in use"


@pytest.mark.parametrize("said, problem", [
    (OSError(8, "Exec format error"), "Exec format error"),
    (subprocess.TimeoutExpired("adb version", 30), "timed out"),
    ("Error relocating adb: __strtol_internal: symbol not found", "symbol not found"),
    ("", "printed nothing"),
])
def test_what_counts_as_not_running(checks, monkeypatch, said, problem):
    def version(adb, timeout=15.0):
        if isinstance(said, Exception):
            raise said
        return said

    monkeypatch.setattr(toolsdl, "_adb_version_output", version)
    with pytest.raises(ADBError, match=problem):
        toolsdl._require_runnable("/staging/platform-tools/adb")


def test_a_download_that_runs_is_installed(download, checks, monkeypatch):
    events, _system = download
    ran = []

    def version(adb, timeout=15.0):
        ran.append(adb)
        return "Android Debug Bridge version 1.0.41\nVersion 37.0.1-14910828\n"

    monkeypatch.setattr(toolsdl, "_adb_version_output", version)
    installed = toolsdl.download_platform_tools(force=True)
    assert installed == toolsdl.managed_adb() and os.path.isfile(installed)
    # asked before anything was replaced, in the folder it was unpacked to
    assert os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(ran[0])))).startswith(
        ".download-")
    assert os.path.samefile(tools.find_adb(), installed)


# --------------------------------------------------------------------------- #
# machines Google builds no adb for
# --------------------------------------------------------------------------- #
@pytest.fixture
def arm_linux(monkeypatch):
    monkeypatch.setattr(toolsdl, "adb_download_supported", _supported)
    monkeypatch.setattr(toolsdl, "_os_key", lambda: "linux")
    monkeypatch.setattr(toolsdl.platform, "system", lambda: "Linux")
    monkeypatch.setattr(toolsdl.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(toolsdl, "_download",
                        lambda *a, **k: pytest.fail("downloaded an adb that cannot run here"))
    monkeypatch.setattr(toolsdl, "_archive_to_download", lambda: {"url": "https://example/pt.zip"})
    monkeypatch.setattr(toolsdl, "managed_adb", lambda: None)
    monkeypatch.setattr(toolsdl, "managed_scrcpy", lambda: None)


@pytest.mark.parametrize("key, system, machine, supported", [
    ("windows", "Windows", "AMD64", True),
    ("windows", "Windows", "ARM64", True),  # the x86 build runs there
    ("darwin", "Darwin", "arm64", True),  # a universal build
    ("linux", "Linux", "x86_64", True),
    ("linux", "Linux", "aarch64", False),
    ("linux", "Linux", "armv7l", False),
    ("linux", "FreeBSD", "amd64", False),  # every other system is "linux" to _os_key
])
def test_where_google_builds_adb(monkeypatch, key, system, machine, supported):
    monkeypatch.setattr(toolsdl, "_os_key", lambda: key)
    monkeypatch.setattr(toolsdl.platform, "system", lambda: system)
    monkeypatch.setattr(toolsdl.platform, "machine", lambda: machine)
    assert _supported() is supported


def test_nothing_is_downloaded_where_no_build_exists(arm_linux, monkeypatch):
    with pytest.raises(ADBNotFoundError, match="aarch64.*package manager"):
        toolsdl.download_platform_tools(force=True)
    assert "aarch64" in toolsdl.fetch_tools(adb=True, scrcpy=False)["errors"]["adb"]

    monkeypatch.setattr(toolsdl, "latest_adb_version", lambda: "37.0.1")
    monkeypatch.setattr(toolsdl, "latest_scrcpy_version", lambda: None)
    monkeypatch.setattr(toolsdl, "installed_adb_version", lambda p=None: "34.0.5")
    monkeypatch.setattr(toolsdl, "installed_scrcpy_version", lambda p=None: None)
    found = toolsdl.check_updates()
    assert found["adb"]["supported"] is False and found["adb"]["system"] == "34.0.5"
    # "Check for updates": the system's adb is all there is, and it stays
    result = toolsdl.upgrade_tools(checks=found)
    assert result["updated"] == {} and result["errors"] == {} and result["up_to_date"]


def test_the_first_run_fetch_leaves_adb_to_the_system_there(arm_linux, monkeypatch):
    monkeypatch.setattr(toolsdl, "_ensured", set())
    monkeypatch.setenv("TURBOADB_AUTO_FETCH", "1")
    monkeypatch.setattr(toolsdl, "_read_stamp", lambda: "")
    monkeypatch.setattr(toolsdl, "_sync_scrcpy_adb", lambda: None)
    monkeypatch.setattr(toolsdl, "fetch_tools", lambda **kw: pytest.fail(f"fetched {kw}"))
    assert toolsdl.ensure_tools()["errors"] == {}


# --------------------------------------------------------------------------- #
# reading `adb version`
# --------------------------------------------------------------------------- #
def test_the_version_is_read_whatever_bytes_the_install_path_has(tmp_path):
    """adb prints where it is installed.  Decoded with the system code page, a
    profile folder it has no character for cost the whole output: no version
    (an update "available" at every check), and now no proof that it runs."""
    (tmp_path / "said.bin").write_bytes(
        b"Android Debug Bridge version 1.0.41\nVersion 37.0.1-14910828\n"
        b"Installed as C:\\Users\\\xc5\x81ukasz \x81\xff\\adb.exe\n")
    if os.name == "nt":
        adb = tmp_path / "adb.cmd"
        adb.write_text('@type "%~dp0said.bin"\r\n')
    else:
        adb = tmp_path / "adb"
        adb.write_text('#!/bin/sh\ncat "$(dirname "$0")/said.bin"\n')
        adb.chmod(0o755)
    assert "Android Debug Bridge version 1.0.41" in tools._adb_version_output(str(adb))
    assert toolsdl.installed_adb_version(str(adb)) == "37.0.1"
