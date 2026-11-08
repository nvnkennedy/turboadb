"""TurboADB ships neither adb nor scrcpy: the first run installs them through winget.

Where winget is missing or fails, the publishers' own archives are fetched, and
those downloads ride out a server's bad moment: GitHub answers a release file
with a 504 now and then, one file at a time.

No device, adb, scrcpy, winget or network is used.
"""

import os
import socket
import sys
import urllib.error

import pytest

from turboadb import tools, toolsdl
from turboadb.exceptions import ADBError

_real_winget_exe = toolsdl.winget_exe
_real_package_roots = tools._winget_package_roots
_ADB_PACKAGE = "Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe"
_SCRCPY_PACKAGE = "Genymobile.scrcpy_Microsoft.Winget.Source_8wekyb3d8bbwe"
_NOTHING_NEWER = 0x8A15002B
_NO_SUCH_PACKAGE = 0x8A150014
_DOWNLOAD_FAILED = 0x8A150008


def _http_error(code):
    return urllib.error.HTTPError("https://github.com/x", code, "Gateway Time-out", {}, None)


class _Response:
    headers = {"Content-Length": "4"}

    def __init__(self):
        self._chunks = [b"data", b""]

    def read(self, _size):
        return self._chunks.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def waits(monkeypatch):
    slept = []
    monkeypatch.setattr(toolsdl.time, "sleep", slept.append)
    return slept


def _answers(monkeypatch, *script):
    """urlopen answers from *script*: an exception to raise, or a good response."""
    calls = []

    def urlopen(_request, timeout=None):
        calls.append(timeout)
        step = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(step, Exception):
            raise step
        return _Response()

    monkeypatch.setattr(toolsdl.urllib.request, "urlopen", urlopen)
    return calls


# ---- a server's bad moment ---------------------------------------------------
def test_a_504_is_asked_again_and_the_download_completes(monkeypatch, waits, tmp_path):
    calls = _answers(monkeypatch, _http_error(504), _http_error(504), "ok")
    dest = tmp_path / "f.zip"
    toolsdl._download("https://github.com/x/f.zip", str(dest))
    assert dest.read_bytes() == b"data"
    assert len(calls) == 3 and len(waits) == 2


def test_a_server_that_keeps_failing_says_nothing_was_changed(monkeypatch, waits, tmp_path):
    calls = _answers(monkeypatch, _http_error(504))
    with pytest.raises(toolsdl.ServerDidNotAnswer) as caught:
        toolsdl._download("https://github.com/x/f.zip", str(tmp_path / "f.zip"))
    assert len(calls) == toolsdl._DOWNLOAD_TRIES
    assert "github.com did not answer" in str(caught.value)
    assert "Nothing was changed" in str(caught.value)


def test_a_missing_file_is_not_asked_again(monkeypatch, waits, tmp_path):
    calls = _answers(monkeypatch, _http_error(404))
    with pytest.raises(urllib.error.HTTPError):
        toolsdl._download("https://github.com/x/f.zip", str(tmp_path / "f.zip"))
    assert len(calls) == 1 and waits == []


def test_a_pc_with_no_internet_is_not_kept_waiting(monkeypatch, waits, tmp_path):
    calls = _answers(monkeypatch, urllib.error.URLError(socket.gaierror(11001, "no name")))
    with pytest.raises(urllib.error.URLError):
        toolsdl._download("https://github.com/x/f.zip", str(tmp_path / "f.zip"))
    assert len(calls) == 1 and waits == []


def test_a_checksum_file_gets_the_same_second_chance(monkeypatch, waits, tmp_path):
    payload = tmp_path / "f.zip"
    payload.write_bytes(b"data")
    calls = _answers(monkeypatch, _http_error(504))
    toolsdl._verify_sha256(str(payload), "f.zip", "https://github.com/x/SHA256SUMS.txt")
    assert len(calls) == toolsdl._DOWNLOAD_TRIES


def test_the_failure_is_an_adb_error_the_ui_can_show():
    assert issubclass(toolsdl.ServerDidNotAnswer, ADBError)


# ---- winget ------------------------------------------------------------------
class _Winget:
    """Stands in for winget: unpacks what it is asked for where the real one does."""

    def __init__(self, root):
        self.root = root
        self.commands = []
        self.answers = []  # codes to end with, one per run; then 0

    @property
    def packages(self):
        return [cmd[cmd.index("--id") + 1] for cmd in self.commands]

    def unpack(self, package):
        if package == "Google.PlatformTools":
            folder = self.root / _ADB_PACKAGE / "platform-tools"
            names = ["adb"]
        else:
            folder = self.root / _SCRCPY_PACKAGE / "scrcpy-win64-v5.0.1"
            names = ["scrcpy", "adb"]
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / tools._exe(name)).write_text(name)

    def run(self, cmd, on_progress=None, timeout=None):
        self.commands.append(cmd)
        code = self.answers.pop(0) if self.answers else 0
        if code == 0:
            self.unpack(cmd[cmd.index("--id") + 1])
        return code, ["Successfully installed" if code == 0 else "It went wrong"]


@pytest.fixture
def pc(monkeypatch, tmp_path):
    """A PC with winget and neither tool: no PATH or SDK copy, an empty profile."""
    home = tmp_path / "home"
    (home / ".turboadb").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TURBOADB_ADB", raising=False)
    monkeypatch.delenv("TURBOADB_SCRCPY", raising=False)
    monkeypatch.delenv("TURBOADB_AUTO_FETCH", raising=False)
    winget = _Winget(tmp_path / "Packages")
    monkeypatch.setattr(tools, "_winget_package_roots", lambda: [str(winget.root)])
    monkeypatch.setattr(tools, "_on_path", lambda _name: None)
    monkeypatch.setattr(tools, "_sdk_candidates", lambda _name: [])
    monkeypatch.setattr(tools, "_bundled_candidates", lambda _name: [])
    monkeypatch.setattr(tools, "_gui_setting", lambda _name: None)
    monkeypatch.setattr(toolsdl, "winget_exe", lambda: "winget.exe")
    monkeypatch.setattr(toolsdl, "_run_winget", winget.run)
    monkeypatch.setattr(toolsdl, "_kill_adb_server", lambda *_a, **_k: [])
    monkeypatch.setattr(toolsdl, "scrcpy_download_supported", lambda: True)
    monkeypatch.setattr(toolsdl, "adb_download_supported", lambda: True)
    monkeypatch.setattr(toolsdl.time, "sleep", lambda _s: None)
    monkeypatch.setattr(toolsdl, "_ensured", set())
    monkeypatch.setattr(toolsdl, "_pkg_version", lambda: "5.0.0")

    def no_network(*_a, **_k):
        raise AssertionError("the publishers' download was used")

    monkeypatch.setattr(toolsdl, "_download", no_network)
    monkeypatch.setattr(toolsdl, "_scrcpy_assets", no_network)
    monkeypatch.setattr(toolsdl, "_archive_to_download", no_network)
    toolsdl.clear_tools_cache()
    yield winget
    toolsdl.clear_tools_cache()


def _publishers_download_used(monkeypatch):
    """The publishers' download, recorded, and ending there."""
    reached = []

    class Reached(Exception):
        pass

    def assets():
        reached.append("scrcpy")
        raise Reached()

    def archive():
        reached.append("adb")
        raise Reached()

    monkeypatch.setattr(toolsdl, "_scrcpy_assets", assets)
    monkeypatch.setattr(toolsdl, "_archive_to_download", archive)
    return reached, Reached


def test_the_first_run_installs_both_together(pc):
    result = toolsdl.ensure_tools()
    assert result["errors"] == {}
    assert pc.packages == ["Google.PlatformTools", "Genymobile.scrcpy"]


def test_what_was_just_installed_is_found_without_a_restart(pc):
    toolsdl.ensure_tools()  # PATH of this process knows nothing about it
    assert _ADB_PACKAGE in tools.find_adb()
    assert _SCRCPY_PACKAGE in tools.find_scrcpy()


def test_it_asks_nothing_of_the_user(pc):
    toolsdl.ensure_tools()
    cmd = pc.commands[0]
    for flag in ("--silent", "--disable-interactivity", "--accept-package-agreements",
                 "--accept-source-agreements", "--exact"):
        assert flag in cmd
    assert cmd[cmd.index("--scope") + 1] == "user"  # no administrator prompt
    assert cmd[cmd.index("--source") + 1] == "winget"
    assert "--force" not in cmd


def test_tools_already_there_are_left_alone(pc):
    pc.unpack("Google.PlatformTools")
    pc.unpack("Genymobile.scrcpy")
    toolsdl.ensure_tools()
    assert pc.commands == []


def test_only_what_is_missing_is_installed(pc):
    pc.unpack("Google.PlatformTools")
    toolsdl.ensure_tools()
    assert pc.packages == ["Genymobile.scrcpy"]


def test_start_with_no_scrcpy_installs_it(pc):
    assert _SCRCPY_PACKAGE in toolsdl.install_missing_scrcpy()
    assert pc.packages == ["Genymobile.scrcpy"]


def test_standalone_adb_comes_before_the_one_inside_scrcpy(pc):
    pc.unpack("Genymobile.scrcpy")
    assert _SCRCPY_PACKAGE in tools.find_adb()  # better than none
    assert tools.winget_copy("adb") is None and toolsdl.managed_adb() is None
    pc.unpack("Google.PlatformTools")
    toolsdl.clear_tools_cache()
    assert _ADB_PACKAGE in tools.find_adb()


def test_the_publisher_s_adb_comes_before_the_one_inside_scrcpy(pc):
    """winget could not deliver adb, the publisher did, and scrcpy came from winget."""
    cache = toolsdl.adb_dir()
    os.makedirs(cache)
    with open(os.path.join(cache, tools._exe("adb")), "w") as fh:
        fh.write("from Google")
    pc.unpack("Genymobile.scrcpy")
    toolsdl.clear_tools_cache()
    assert tools.find_adb().startswith(cache)


def test_the_newest_release_folder_wins(pc):
    pc.unpack("Genymobile.scrcpy")
    older = pc.root / _SCRCPY_PACKAGE / "scrcpy-win64-v4.10"
    older.mkdir()
    (older / tools._exe("scrcpy")).write_text("old")
    assert "v5.0.1" in tools.winget_copy("scrcpy")


def test_winget_goes_before_the_old_cache(pc):
    cache = toolsdl.adb_dir()
    os.makedirs(cache)
    with open(os.path.join(cache, tools._exe("adb")), "w") as fh:
        fh.write("cached")
    pc.unpack("Google.PlatformTools")
    toolsdl.clear_tools_cache()
    assert _ADB_PACKAGE in tools.find_adb()
    assert _ADB_PACKAGE in toolsdl.managed_adb()


def test_no_winget_uses_the_publishers_download(pc, monkeypatch):
    reached, stop = _publishers_download_used(monkeypatch)
    monkeypatch.setattr(toolsdl, "winget_exe", lambda: None)
    with pytest.raises(stop):
        toolsdl.download_scrcpy()
    assert reached == ["scrcpy"] and pc.commands == []


def test_a_winget_that_fails_falls_back_to_the_publisher(pc, monkeypatch):
    reached, stop = _publishers_download_used(monkeypatch)
    pc.answers = [_DOWNLOAD_FAILED, _DOWNLOAD_FAILED]
    with pytest.raises(stop):
        toolsdl.download_platform_tools()
    assert reached == ["adb"]


def test_a_second_try_rides_out_a_bad_moment(pc):
    pc.answers = [_DOWNLOAD_FAILED, 0]
    assert _SCRCPY_PACKAGE in toolsdl.download_scrcpy()
    assert len(pc.commands) == 2


def test_a_final_answer_is_not_asked_again(pc, monkeypatch):
    _reached, stop = _publishers_download_used(monkeypatch)
    pc.answers = [_NO_SUCH_PACKAGE]
    with pytest.raises(stop):
        toolsdl.download_scrcpy()
    assert len(pc.commands) == 1


def test_nothing_newer_is_not_a_failure(pc):
    pc.unpack("Genymobile.scrcpy")
    pc.answers = [_NOTHING_NEWER]
    assert _SCRCPY_PACKAGE in toolsdl.download_scrcpy(force=True)


def test_reinstalling_forces_it_only_once_there_is_nothing_newer(pc):
    pc.unpack("Genymobile.scrcpy")
    pc.answers = [_NOTHING_NEWER, 0]
    toolsdl.download_scrcpy(force=True)
    assert ["--force" in cmd for cmd in pc.commands] == [False, True]


def test_an_update_never_forces(pc):
    pc.unpack("Genymobile.scrcpy")
    toolsdl.download_scrcpy(force=True)  # winget updated it: code 0
    assert ["--force" in cmd for cmd in pc.commands] == [False]


def test_a_failed_update_of_winget_s_copy_says_the_copy_still_works(pc, monkeypatch):
    reached, _stop = _publishers_download_used(monkeypatch)
    pc.unpack("Genymobile.scrcpy")
    pc.answers = [_DOWNLOAD_FAILED, _DOWNLOAD_FAILED]
    with pytest.raises(ADBError) as caught:
        toolsdl.download_scrcpy(force=True)
    assert "still works" in str(caught.value) and "0x8A150008" in str(caught.value)
    assert reached == []  # a download would go where nothing looks


def test_a_failure_that_took_the_copy_away_falls_back_to_the_publisher(pc, monkeypatch):
    """winget's --force removes the old copy first and can fail after that."""
    import shutil

    reached, stop = _publishers_download_used(monkeypatch)
    pc.unpack("Genymobile.scrcpy")

    def removes_then_fails(*_a, **_k):
        shutil.rmtree(pc.root / _SCRCPY_PACKAGE, ignore_errors=True)
        return _DOWNLOAD_FAILED, ["It went wrong"]

    monkeypatch.setattr(toolsdl, "_run_winget", removes_then_fails)
    with pytest.raises(stop):
        toolsdl.download_scrcpy(force=True)
    assert reached == ["scrcpy"]


def test_a_file_the_publisher_took_down_is_not_asked_again(pc, monkeypatch):
    _reached, stop = _publishers_download_used(monkeypatch)
    pc.answers = [0x80190194]
    with pytest.raises(stop):
        toolsdl.download_platform_tools()
    assert len(pc.commands) == 1


def test_the_error_does_not_repeat_the_code(pc, monkeypatch):
    pc.unpack("Genymobile.scrcpy")
    monkeypatch.setattr(toolsdl, "_run_winget",
                        lambda *_a, **_k: (0x80190194, ["0x80190194 : Not found (404)."]))
    with pytest.raises(ADBError) as caught:
        toolsdl.download_scrcpy(force=True)
    assert str(caught.value).count("0x80190194") == 1


def test_replacing_adb_stops_its_server_first(pc, monkeypatch):
    pc.unpack("Google.PlatformTools")
    order = []
    monkeypatch.setattr(toolsdl, "_kill_adb_server", lambda *_a: order.append("stopped") or [])
    monkeypatch.setattr(toolsdl, "_run_winget",
                        lambda *_a, **_k: order.append("winget") or (0, []))
    toolsdl.download_platform_tools(force=True)
    assert order == ["stopped", "winget"]


def test_a_first_install_leaves_a_running_server_alone(pc, monkeypatch):
    monkeypatch.setattr(toolsdl, "_kill_adb_server", lambda *_a: pytest.fail("stopped"))
    toolsdl.download_platform_tools()


# ---- what winget says --------------------------------------------------------
@pytest.mark.parametrize("line", ["Version: 37.0.2", "Versión: 37.0.2", "版本： 37.0.2"])
def test_the_version_winget_offers_is_read_in_any_language(pc, monkeypatch, line):
    class Done:
        returncode = 0
        stdout = f"Found Android SDK Platform-Tools [Google.PlatformTools]\r\n{line}\r\nPublisher: Google LLC\r\n"

    monkeypatch.setattr(toolsdl.subprocess, "run", lambda *_a, **_k: Done)
    assert toolsdl.winget_latest("adb") == "37.0.2"


def test_a_winget_that_cannot_say_gives_no_version(pc, monkeypatch):
    def fails(*_a, **_k):
        raise OSError("gone")

    monkeypatch.setattr(toolsdl.subprocess, "run", fails)
    assert toolsdl.winget_latest("adb") is None


def test_updates_are_only_offered_when_winget_has_them(pc, monkeypatch):
    pc.unpack("Google.PlatformTools")
    pc.unpack("Genymobile.scrcpy")
    monkeypatch.setattr(toolsdl, "installed_adb_version", lambda _p=None: "37.0.2")
    monkeypatch.setattr(toolsdl, "installed_scrcpy_version", lambda _p=None: "5.0.1")
    monkeypatch.setattr(toolsdl, "winget_latest", {"adb": "37.0.2", "scrcpy": "5.0.2"}.get)
    monkeypatch.setattr(toolsdl, "latest_adb_version", lambda: "99.0.0")
    monkeypatch.setattr(toolsdl, "latest_scrcpy_version", lambda: "99.0.0")
    checks = toolsdl.check_updates()
    assert checks["adb"]["upgrade"] is False
    assert checks["scrcpy"]["upgrade"] is True and checks["scrcpy"]["latest"] == "5.0.2"


def test_a_publisher_s_copy_is_compared_with_the_publisher(pc, monkeypatch):
    """winget listed adb 37.0.2 after Google had withdrawn it for 37.0.1."""
    cache = toolsdl.adb_dir()
    os.makedirs(cache)
    with open(os.path.join(cache, tools._exe("adb")), "w") as fh:
        fh.write("from Google")
    monkeypatch.setattr(toolsdl, "installed_adb_version", lambda _p=None: "37.0.1")
    monkeypatch.setattr(toolsdl, "installed_scrcpy_version", lambda _p=None: None)
    monkeypatch.setattr(toolsdl, "winget_latest", lambda _tool: "37.0.2")
    monkeypatch.setattr(toolsdl, "latest_adb_version", lambda: "37.0.1")
    monkeypatch.setattr(toolsdl, "latest_scrcpy_version", lambda: "5.0.1")
    assert toolsdl.check_updates()["adb"]["upgrade"] is False


def test_running_winget_reports_its_code_lines_and_progress():
    script = "import sys; print('one'); print(); print('two'); sys.exit(3)"
    seen = []
    code, lines = toolsdl._run_winget([sys.executable, "-c", script], seen.append)
    assert code == 3 and lines == ["one", "two"]
    assert seen and seen == sorted(seen) and max(seen) < 100


def test_a_winget_that_hangs_is_ended():
    code, _lines = toolsdl._run_winget(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    assert code is None


@pytest.mark.skipif(os.name != "nt", reason="winget is Windows-only")
def test_winget_is_found_without_it_being_on_path(monkeypatch, tmp_path):
    alias = tmp_path / "Microsoft" / "WindowsApps"
    alias.mkdir(parents=True)
    (alias / "winget.exe").write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(toolsdl, "_on_path", lambda _name: None)
    assert _real_winget_exe() == str(alias / "winget.exe")


@pytest.mark.skipif(os.name != "nt", reason="winget is Windows-only")
def test_an_unset_local_app_data_is_never_the_current_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(toolsdl, "_on_path", lambda _name: None)
    monkeypatch.chdir(tmp_path)
    assert _real_winget_exe() is None


@pytest.mark.skipif(os.name == "nt", reason="the other systems have no winget")
def test_elsewhere_there_is_no_winget():
    assert _real_winget_exe() is None and _real_package_roots() == []


# ---- nothing is shipped ------------------------------------------------------
def _repo_text(*parts):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, *parts), encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("parts", [
    ("turboadb-gui.spec",),
    ("scripts", "build_exe.py"),
    (".github", "workflows", "release.yml"),
])
def test_no_build_path_packs_the_tools(parts):
    text = _repo_text(*parts)
    for name in ("platform-tools.zip", "scrcpy.zip", "fetch_bundled_tools"):
        assert name not in text
