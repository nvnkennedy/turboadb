"""adb and scrcpy are never taken from the folder a command is run in.

Auto-detection asked ``shutil.which``, which looks in the current folder
before PATH on Windows (and for an empty or relative PATH entry anywhere), and
then tried install locations built on variables that may be unset: without
``LOCALAPPDATA`` that was ``Android/Sdk/platform-tools/adb`` in the current
folder, and on Windows ``/usr/bin/adb.exe`` is a file on whichever drive is
current, in a folder any account may create.  An ``adb.bat`` lying in a
downloaded project was started as adb, and the path that came back was
relative.  Only absolute locations are looked at now, and every answer is an
absolute path.
"""
import os
import stat

import pytest

from turboadb import tools
from turboadb.exceptions import ADBNotFoundError

_WINDOWS = os.name == "nt"


def _tool(folder, name, ext=None):
    """An executable stand-in for *name* in *folder*; its path."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + (ext if ext is not None else (".exe" if _WINDOWS else "")))
    path.write_text("")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _names_one_place(path):
    """True for a path that means the same file from any folder and drive."""
    return os.path.isabs(path) and bool(not _WINDOWS or os.path.splitdrive(path)[0])


@pytest.fixture
def nothing_installed(monkeypatch, tmp_path):
    """No managed, bundled or configured tool, an empty PATH folder, and a
    working folder of its own (returned)."""
    monkeypatch.setattr(tools, "_managed_candidates", lambda name: [])
    monkeypatch.setattr(tools, "_bundled_candidates", lambda name: [])
    monkeypatch.setattr(tools, "_gui_setting", lambda name: None)
    for var in ("TURBOADB_ADB", "TURBOADB_SCRCPY", "ANDROID_HOME", "ANDROID_SDK_ROOT",
                "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    empty = tmp_path / "on-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    tools.clear_tools_cache()
    yield work
    tools.clear_tools_cache()


def _inside(path, folder):
    return os.path.normcase(os.path.abspath(path)).startswith(
        os.path.normcase(str(folder)) + os.sep)


def _is(found, wanted):
    """*found* is an absolute path of the file *wanted*."""
    return _names_one_place(found) and os.path.samefile(found, wanted)


@pytest.mark.parametrize("name", ["adb", "scrcpy"])
def test_a_tool_in_the_working_folder_is_not_found(nothing_installed, monkeypatch, name):
    work = nothing_installed
    _tool(work, name)
    if _WINDOWS:
        _tool(work, name, ".bat")
    # "." and an empty entry mean the current folder, as a relative one does
    _tool(work / "bin", name)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin", os.environ["PATH"]]))
    monkeypatch.setattr(tools, "_sdk_candidates", lambda name: [])
    assert tools._discover(name) is None
    find = tools.find_adb if name == "adb" else tools.find_scrcpy
    with pytest.raises(ADBNotFoundError):
        find()


@pytest.mark.parametrize("name", ["adb", "scrcpy"])
def test_a_tool_in_a_path_folder_is_found_by_its_absolute_path(nothing_installed, monkeypatch,
                                                               tmp_path, name):
    _tool(nothing_installed, name)  # the one in the working folder loses
    wanted = _tool(tmp_path / "sdk" / "platform-tools", name)
    monkeypatch.setenv("PATH", os.pathsep.join([".", os.environ["PATH"],
                                                os.path.dirname(wanted)]))
    monkeypatch.setattr(tools, "_sdk_candidates", lambda name: [])
    assert _is((tools.find_adb if name == "adb" else tools.find_scrcpy)(), wanted)


@pytest.mark.skipif(not _WINDOWS, reason="PATHEXT is Windows'")
def test_a_batch_file_wrapper_on_path_still_counts(nothing_installed, monkeypatch, tmp_path):
    wrapper = _tool(tmp_path / "shims", "scrcpy", ".cmd")
    monkeypatch.setenv("PATH", os.path.dirname(wrapper))
    monkeypatch.setattr(tools, "_sdk_candidates", lambda name: [])
    assert _is(tools.find_scrcpy(), wrapper)


@pytest.mark.parametrize("name", ["adb", "scrcpy"])
def test_no_install_location_depends_on_the_working_folder(nothing_installed, monkeypatch, name):
    """Without LOCALAPPDATA (Linux, macOS) its SDK folder was looked up in the
    current folder; on Windows /usr and /opt were folders of the current drive."""
    work = nothing_installed
    _tool(work / "Android" / "Sdk" / "platform-tools", "adb")
    _tool(work / "scrcpy", "scrcpy")
    monkeypatch.setenv("ANDROID_HOME", "Android" + os.sep + "Sdk")  # relative: the same trap
    asked = []
    with monkeypatch.context() as patched:
        patched.setattr(tools.os.path, "isfile", lambda path: asked.append(path) or False)
        tools._discover(name)
    assert asked and [path for path in asked if not _names_one_place(path)] == []
    found = tools._discover(name)
    assert found is None or (_names_one_place(found) and not _inside(found, work))


@pytest.mark.skipif(_WINDOWS, reason="package-manager folders of Linux and macOS")
def test_the_unix_install_folders_are_still_searched(nothing_installed):
    assert "/usr/bin/adb" in tools._sdk_candidates("adb")
    assert "/usr/local/bin/scrcpy" in tools._sdk_candidates("scrcpy")


@pytest.mark.skipif(not _WINDOWS, reason="LOCALAPPDATA is Windows'")
def test_the_sdk_under_localappdata_is_still_searched(nothing_installed, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    adb = _tool(tmp_path / "local" / "Android" / "Sdk" / "platform-tools", "adb")
    scrcpy = _tool(tmp_path / "local" / "scrcpy", "scrcpy")
    assert tools.find_adb() == adb and tools.find_scrcpy() == scrcpy


def test_a_relative_path_given_on_purpose_comes_back_absolute(nothing_installed, monkeypatch):
    """--adb-path tools/adb is the user's own choice of a file in the working
    folder; the answer no longer depends on that folder staying current."""
    work = nothing_installed
    chosen = _tool(work / "tools", "adb")
    relative = os.path.join("tools", os.path.basename(chosen))
    assert _is(tools.find_adb(relative), chosen)
    tools.clear_tools_cache()
    assert _is(tools.find_adb("tools"), chosen)  # a folder holding adb
    monkeypatch.setenv("TURBOADB_ADB", relative)
    tools.clear_tools_cache()
    assert _is(tools.find_adb(), chosen)


@pytest.mark.skipif(not _WINDOWS, reason="only Windows looks in the current folder for a bare name")
def test_a_bare_name_given_on_purpose_is_looked_up_on_path_only(nothing_installed, monkeypatch,
                                                                tmp_path):
    _tool(nothing_installed, "adb", ".bat")
    monkeypatch.setenv("TURBOADB_ADB", "adb")
    with pytest.raises(ADBNotFoundError, match="TURBOADB_ADB=adb"):
        tools.find_adb()
    wanted = _tool(tmp_path / "sdk", "adb")
    monkeypatch.setenv("PATH", os.path.dirname(wanted))
    tools.clear_tools_cache()
    assert _is(tools.find_adb(), wanted)


# --------------------------------------------------------------------------- #
# Windows' own programs (netsh, schtasks, PowerShell)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _WINDOWS, reason="System32 is Windows'")
def test_windows_own_programs_are_named_by_their_full_path(nothing_installed):
    """Started by its bare name, a program is looked for in the current folder
    before System32: a netsh.exe lying in the folder `turboadb serve` was run
    in was started in its place."""
    for name in ("netsh.exe", "schtasks.exe", "powershell.exe"):
        (nothing_installed / name).write_text("")  # in the working folder
    system32 = os.path.join(os.environ["SystemRoot"], "System32")
    for name in ("netsh.exe", "schtasks.exe", tools.POWERSHELL):
        found = tools.windows_program(name)
        assert found == os.path.join(system32, name) and os.path.isfile(found)
    assert tools.windows_program("no-such-program.exe") is None  # left to Windows to find


def test_a_windows_folder_that_is_not_named_whole_is_not_looked_into(nothing_installed, monkeypatch):
    (nothing_installed / "System32").mkdir()
    (nothing_installed / "System32" / "netsh.exe").write_text("")
    monkeypatch.setenv("SystemRoot", ".")  # would be the working folder
    monkeypatch.delenv("windir", raising=False)
    assert tools.windows_program("netsh.exe") is None
