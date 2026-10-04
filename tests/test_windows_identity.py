"""TurboADB as its own app on Windows: TurboADB.exe, pins and shortcuts.

The GUI ran in pythonw.exe, so Task Manager named it "Python" with Python's
icon, and a taskbar pin started a bare pythonw.  Now it runs in a branded
copy of the interpreter (turboadb.launcher), its window tells the taskbar
what a pin shows and starts, and its shortcuts carry its app ID
(turboadb.winshell)."""

import contextlib
import ctypes
import json
import os
import subprocess
import sys
import types

import pytest

from turboadb import cli, launcher, winshell

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows program files, COM and windows")


@pytest.fixture
def own_home(tmp_path, monkeypatch):
    """A home of the test's own, with the launcher turned back on."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("TURBOADB_NO_LAUNCHER", raising=False)
    return tmp_path


# ------------------------------------------------------------------ icons
def test_the_icon_comes_dark_and_light_in_every_size():
    for name in ("icon.ico", "icon-light.ico"):
        sizes = sorted(dims[0] or 256 for dims, _data in launcher._ico_images(winshell.asset(name)))
        assert sizes == [16, 24, 32, 48, 64, 128, 256], name


def test_the_icon_for_the_taskbar_is_a_lasting_copy_of_its_mode(own_home):
    dark, light = winshell.icon_file(False), winshell.icon_file(True)
    assert dark.startswith(str(own_home)) and light.startswith(str(own_home))
    with open(dark, "rb") as a, open(winshell.asset("icon.ico"), "rb") as b:
        assert a.read() == b.read()
    with open(light, "rb") as a, open(winshell.asset("icon-light.ico"), "rb") as b:
        assert a.read() == b.read()
    with open(dark, "wb") as fh:
        fh.write(b"stale")  # an older icon: brought up to date
    winshell.icon_file(False)
    with open(dark, "rb") as a, open(winshell.asset("icon.ico"), "rb") as b:
        assert a.read() == b.read()


def test_the_taskbar_mode_is_read_from_windows(monkeypatch):
    values = {"SystemUsesLightTheme": 1}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def query(_key, name):
        if name not in values:
            raise FileNotFoundError(2, "no such value")
        return values[name], 4  # REG_DWORD

    fake = types.SimpleNamespace(HKEY_CURRENT_USER=0, OpenKey=lambda *_a: Key(), QueryValueEx=query)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    assert winshell.taskbar_is_light() is True
    values["SystemUsesLightTheme"] = 0
    assert winshell.taskbar_is_light() is False
    values.clear()  # Windows 10 before the setting existed: a dark taskbar
    assert winshell.taskbar_is_light() is False


# -------------------------------------------------------- version resource
def test_version_numbers_are_the_digits_of_each_part():
    assert launcher._version_numbers("3.0.0") == (3, 0, 0, 0)
    assert launcher._version_numbers("3.1.0rc1") == (3, 1, 0, 0)
    assert launcher._version_numbers("10.2") == (10, 2, 0, 0)


def _blocks(data, offset=0, end=None):
    """Parse VS_VERSIONINFO blocks: [(key, value, children)]."""
    import struct

    end = len(data) if end is None else end
    out = []
    while offset + 6 <= end:
        length, value_len, kind = struct.unpack_from("<HHH", data, offset)
        if not length:
            break
        pos = offset + 6
        key_end = data.index(b"\0\0", pos)
        while (key_end - pos) % 2:
            key_end = data.index(b"\0\0", key_end + 1)
        key = data[pos:key_end].decode("utf-16-le")
        pos = key_end + 2
        pos += -pos % 4
        size = value_len * 2 if kind == 1 else value_len
        value = data[pos:pos + size]
        pos += size
        pos += -pos % 4
        out.append((key, value, _blocks(data, pos, offset + length)))
        offset += length
        offset += -offset % 4
    return out


def test_the_version_resource_holds_turboadbs_strings():
    import struct

    data = launcher._version_resource({"FileDescription": "TurboADB", "ProductName": "TurboADB"}, (3, 0, 1, 0))
    (root_key, fixed, children), = _blocks(data)
    assert root_key == "VS_VERSION_INFO" and fixed[:4] == b"\xbd\x04\xef\xfe"  # VS_FIXEDFILEINFO
    assert struct.unpack_from("<II", fixed, 8) == ((3 << 16) | 0, (1 << 16) | 0)  # 3.0.1.0
    info = {key: kids for key, _value, kids in children}
    (table_key, _v, entries), = info["StringFileInfo"]
    assert table_key == "040904b0"
    values = {k: v.decode("utf-16-le").rstrip("\0") for k, v, _kids in entries}
    assert values == {"FileDescription": "TurboADB", "ProductName": "TurboADB"}
    (translation, lang, _kids), = info["VarFileInfo"]
    assert translation == "Translation" and struct.unpack("<HH", lang) == (0x0409, 0x04B0)


def test_the_one_file_exe_names_itself_turboadb():
    """Task Manager names a process after its FileDescription, and showed the
    one-file exe's file name without one."""
    spec = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "turboadb-gui.spec"), encoding="utf-8").read()
    assert "StringStruct('FileDescription', 'TurboADB')" in spec
    assert "version=VERSION_INFO" in spec
    assert "'turboadb/assets/icon-light.ico'" in spec  # the light taskbar's icon travels too


def test_the_generated_script_is_python_for_any_folder():
    """A path with backslashes (C:\\Users\\x...) in a plain string literal was a
    syntax error: every value goes in as a repr."""
    origin = {"executable": "C:\\Users\\x\\python\\pythonw.exe", "root": "E:\\new\\U\\turboadb",
              "path": ["E:\\new\\U\\turboadb", "C:\\Users\\x'y"], "sites": ["C:\\a b\\site-packages"]}
    text = launcher._script_text(origin, "C:\\Users\\x\\.turboadb\\launcher\\py311-0")
    compile(text, "turboadb_launch.py", "exec")
    assert "HOME = 'C:\\\\Users\\\\x\\\\.turboadb\\\\launcher\\\\py311-0'" in text


# -------------------------------------------------------------- relaunch
def test_nothing_is_relaunched_when_turned_off(monkeypatch):
    monkeypatch.setenv("TURBOADB_NO_LAUNCHER", "1")
    assert not launcher.supported()
    assert launcher.relaunch([]) is None
    assert launcher.gui_command() is None


def test_nothing_is_relaunched_off_windows_or_frozen(monkeypatch):
    monkeypatch.delenv("TURBOADB_NO_LAUNCHER", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert not launcher.supported()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not launcher.supported()


class _Proc:
    def __init__(self, early=None):
        self.early = early
        self.waited = []

    def wait(self, timeout=None):
        self.waited.append(timeout)
        if timeout is not None and self.early is None:
            raise subprocess.TimeoutExpired("TurboADB.exe", timeout)
        return self.early if self.early is not None else 7


@pytest.fixture
def ready(monkeypatch, tmp_path):
    """A TurboADB.exe that is ready, and the processes started of it."""
    rt = launcher.Runtime(str(tmp_path), str(tmp_path / "bin" / "TurboADB.exe"), str(tmp_path / "m.py"))
    monkeypatch.setattr(launcher, "supported", lambda: True)
    monkeypatch.setattr(launcher, "runtime", lambda build=True: rt)
    monkeypatch.setattr(sys, "gettrace", lambda: None)
    started = []

    def popen(cmd, creationflags=0, **kwargs):
        started.append((cmd, creationflags, kwargs))
        return state["proc"]

    state = {"proc": _Proc(), "started": started, "rt": rt}
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    return state


def test_the_gui_starts_as_turboadb_exe_and_this_process_ends(ready):
    assert launcher.relaunch(["--flag"]) == 0
    (cmd, flags, kwargs), = ready["started"]
    assert cmd == [ready["rt"].exe, "-m", "turboadb_launch", "--flag"]
    assert flags == launcher._BREAKAWAY and kwargs["cwd"] == ready["rt"].home
    assert kwargs["stdin"] == subprocess.DEVNULL and "__PYVENV_LAUNCHER__" not in kwargs["env"]


def test_a_gui_that_ends_at_once_runs_here_instead(ready):
    ready["proc"] = _Proc(early=1)
    assert launcher.relaunch([]) is None
    ready["proc"] = _Proc(early=0)  # e.g. "TurboADB is already open"
    assert launcher.relaunch([]) == 0


def test_inside_a_job_that_allows_no_breakaway_it_stays_with_the_gui(ready, monkeypatch):
    calls = []

    def popen(cmd, creationflags=0, **kwargs):
        calls.append(creationflags)
        if creationflags:
            raise PermissionError(5, "Access is denied")
        return ready["proc"]

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    assert launcher.relaunch([]) == 7  # the GUI's own exit code, waited for
    assert calls == [launcher._BREAKAWAY, 0] and ready["proc"].waited == [launcher._EARLY_EXIT_S, None]


def test_under_a_debugger_the_gui_stays_in_this_process(ready, monkeypatch):
    monkeypatch.setattr(sys, "gettrace", lambda: (lambda *a: None))
    assert launcher.relaunch([]) is None and not ready["started"]


def test_launch_gui_hands_over_to_turboadb_exe(monkeypatch):
    pytest.importorskip("PyQt5")
    prewarmed = []
    monkeypatch.setattr(cli, "_prewarm_gui_adb_server", lambda: prewarmed.append(1))
    monkeypatch.setattr(launcher, "relaunch", lambda argv: 0)
    assert cli.launch_gui(["x"]) == 0
    assert prewarmed == []  # TurboADB.exe starts its own adb server


def test_launch_gui_runs_here_when_turboadb_exe_can_not(monkeypatch):
    pytest.importorskip("PyQt5")
    import turboadb.gui.app as app_mod

    ran = []
    monkeypatch.setattr(cli, "_prewarm_gui_adb_server", lambda: ran.append("prewarm"))
    monkeypatch.setattr(launcher, "relaunch", lambda argv: None)
    monkeypatch.setattr(app_mod, "main", lambda: ran.append("gui") or 0)
    assert cli.launch_gui([]) == 0
    assert ran == ["prewarm", "gui"]


def test_python_m_turboadb_gui_starts_as_turboadb_exe_too():
    import turboadb.gui.__main__ as entry

    assert entry.launch_gui is cli.launch_gui


# ------------------------------------ a start from a shortcut or a pin
_STAMP = {"format": 1, "version": "4.0.0", "icon": "0a", "files": {"pythonw.exe": [100, 200]}}


@pytest.fixture
def direct(monkeypatch, tmp_path):
    """TurboADB.exe as a shortcut or a pin starts it, with an up-to-date copy
    and PyQt5 installed.  ``run()`` is its work and ``done`` what came of it:
    "gui", or the command it handed the start over to."""
    home = tmp_path / "launcher" / "py311-0"
    home.mkdir(parents=True)
    (home / "state.json").write_text(json.dumps({"built": {"stamp": _STAMP, "files": "0b"}, "ok": True}))
    monkeypatch.setattr(launcher, "_stamp", lambda: json.loads(json.dumps(_STAMP)))
    monkeypatch.setattr(launcher, "has_qt", lambda: True)
    monkeypatch.setattr(launcher, "BRANDED", False)  # run_branded sets both: put back after the test
    monkeypatch.setattr(launcher, "_HOME", None)
    monkeypatch.delenv("TURBOADB_NO_LAUNCHER", raising=False)
    monkeypatch.delenv(launcher._CHECKED, raising=False)
    monkeypatch.setattr(sys, "executable", "C:\\Python\\pythonw.exe")
    done = []
    app = types.ModuleType("turboadb.gui.app")
    app.main = lambda: done.append("gui") or 0
    monkeypatch.setitem(sys.modules, "turboadb.gui.app", app)

    def popen(cmd, creationflags=0, **kwargs):
        done.append((cmd, kwargs))
        return _Proc()

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    return types.SimpleNamespace(home=str(home), done=done,
                                 run=lambda argv=(): launcher.run_branded(list(argv), str(home)))


def test_a_copy_built_of_another_turboadb_or_python_hands_the_start_over(direct, monkeypatch):
    """A shortcut or a pin starts TurboADB.exe itself, so nothing rebuilt the
    copy after pip install -U turboadb or a Python update: the app kept
    running the old python3X.dll under the old version."""
    assert direct.run() == 0 and direct.done == ["gui"]  # up to date: the GUI, here
    assert launcher.BRANDED and launcher.gui_command(build=False)[2] == direct.home
    del direct.done[:]
    monkeypatch.setattr(launcher, "_stamp", lambda: dict(_STAMP, version="4.0.1"))
    assert direct.run(["-style", "fusion"]) == 0
    (cmd, kwargs), = direct.done  # and no GUI in the copy
    root = os.path.dirname(os.path.dirname(os.path.abspath(launcher.__file__)))
    assert cmd == ["C:\\Python\\pythonw.exe", "-c", launcher._HAND_OVER, root, "-style", "fusion"]
    assert kwargs["cwd"] == direct.home and kwargs["stdin"] == subprocess.DEVNULL
    assert launcher._CHECKED not in kwargs["env"]


def test_a_copy_that_failed_its_check_hands_the_start_over(direct):
    """The GUI then runs in the real Python, as turboadb-gui has it."""
    with open(os.path.join(direct.home, "state.json"), "w", encoding="utf-8") as fh:
        json.dump({"built": {"stamp": _STAMP, "files": "0b"}, "ok": False}, fh)
    assert direct.run() == 0
    (cmd, _kwargs), = direct.done
    assert cmd[1] == "-c"


def test_turned_off_a_copy_started_directly_hands_the_start_over(direct, monkeypatch):
    """TURBOADB_NO_LAUNCHER=1 was ignored by a shortcut or a pin."""
    monkeypatch.setenv("TURBOADB_NO_LAUNCHER", "1")
    assert direct.run() == 0
    (cmd, kwargs), = direct.done
    assert cmd[1] == "-c" and kwargs["env"]["TURBOADB_NO_LAUNCHER"] == "1"


def test_without_pyqt5_a_copy_hands_the_start_over(direct, monkeypatch):
    """The GUI is the wheel's executable there, which turboadb-gui starts: a
    TurboADB.exe shortcut written on such an install opened nothing."""
    monkeypatch.setattr(launcher, "has_qt", lambda: False)
    assert direct.run() == 0
    (cmd, _kwargs), = direct.done
    assert cmd[1] == "-c"


def test_a_copy_without_a_record_of_its_build_hands_the_start_over(direct):
    os.remove(os.path.join(direct.home, "state.json"))
    assert direct.run() == 0
    (cmd, _kwargs), = direct.done
    assert cmd[1] == "-c"


def test_a_copy_that_can_not_hand_over_runs_the_gui_after_all(direct, monkeypatch):
    monkeypatch.setattr(launcher, "_stamp", lambda: dict(_STAMP, version="4.0.1"))

    def popen(cmd, **kwargs):
        raise FileNotFoundError(2, "the interpreter is gone", cmd[0])

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    assert direct.run() == 0 and direct.done == ["gui"]


def test_without_pyqt5_a_copy_that_can_not_hand_over_starts_the_wheels_exe_itself(direct, monkeypatch):
    """There is no GUI of this Python to run in the copy: turboadb-gui's way
    to the executable in the wheel is taken there."""
    monkeypatch.setattr(launcher, "has_qt", lambda: False)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd, **kwargs: (_ for _ in ()).throw(OSError(2, "gone")))
    monkeypatch.setattr(cli, "launch_gui", lambda argv=None: direct.done.append(("launch_gui", argv)) or 5)
    assert direct.run(["-style", "fusion"]) == 5
    assert direct.done == [("launch_gui", ["-style", "fusion"])]


def test_a_copy_started_by_relaunch_does_not_check_itself_again(direct, monkeypatch):
    """relaunch() built or checked the copy a moment ago; a copy that took a
    start it was just handed for out of date would hand it back for ever."""
    monkeypatch.setattr(launcher, "_stamp", lambda: dict(_STAMP, version="4.0.1"))
    monkeypatch.setenv(launcher._CHECKED, "1")
    assert direct.run() == 0 and direct.done == ["gui"]
    assert launcher._CHECKED not in os.environ  # nothing the GUI starts inherits it


def test_relaunch_tells_the_copy_that_it_was_checked(ready):
    assert launcher.relaunch([]) == 0
    (_cmd, _flags, kwargs), = ready["started"]
    assert kwargs["env"][launcher._CHECKED] == "1"


def test_the_hand_over_runs_this_turboadb_in_the_real_python(tmp_path):
    """A source checkout goes first on the path, as python main.py has it; an
    installed TurboADB is taken from where the interpreter finds it."""
    root = tmp_path / "checkout"
    (root / "turboadb").mkdir(parents=True)
    (root / "turboadb" / "__init__.py").write_text("")
    (root / "turboadb" / "cli.py").write_text(
        "import json, sys\n"
        "def launch_gui(argv):\n"
        "    print(json.dumps({'argv': argv, 'path': sys.path, 'file': __file__}))\n"
        "    return 7\n")

    def hand_over(**env):
        env = dict({k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, **env)
        # -S: without its site folders this Python has no TurboADB of its own
        # that a wrong path could start in place of the stand-in
        out = subprocess.run([sys.executable, "-S", "-c", launcher._HAND_OVER, str(root), "-style", "fusion"],
                             capture_output=True, text=True, timeout=60, cwd=str(tmp_path), env=env)
        assert out.returncode == 7, out.stderr
        return json.loads(out.stdout)

    seen = hand_over()
    assert seen["argv"] == ["-style", "fusion"] and seen["path"][0] == str(root)
    assert os.path.dirname(seen["file"]) == str(root / "turboadb")
    seen = hand_over(PYTHONPATH=str(root))  # on the interpreter's own path, as an installed one is
    assert seen["path"].count(str(root)) == 1 and os.path.dirname(seen["file"]) == str(root / "turboadb")


def test_a_copy_takes_its_python_from_its_own_cfg(monkeypatch, tmp_path):
    """Before Python 3.11 sys._base_executable names TurboADB.exe itself in
    the copy, so it looked for pythonw.exe in its own folder and could not
    tell what it was built of."""
    home = tmp_path / "copy"
    home.mkdir()
    (home / "pyvenv.cfg").write_text("home = C:\\Python38\ninclude-system-site-packages = false\n")
    monkeypatch.setattr(sys, "_base_executable", str(home / "bin" / "TurboADB.exe"), raising=False)
    assert launcher._base_dir() == str(home / "bin")  # any other Python: its own
    monkeypatch.setattr(launcher, "BRANDED", True)
    monkeypatch.setattr(launcher, "_HOME", str(home))
    assert launcher._base_dir() == "C:\\Python38"


def test_a_program_file_in_use_is_replaced_once_it_is_free(monkeypatch, tmp_path):
    """An update starts the new TurboADB while the old TurboADB.exe closes."""
    tries = []

    def replace(src, dst):
        tries.append(dst)
        if len(tries) < 3:
            raise PermissionError(32, "in use", dst)

    monkeypatch.setattr(launcher.os, "replace", replace)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    launcher._replace("new", "TurboADB.exe")
    assert len(tries) == 3
    tries.clear()
    with pytest.raises(PermissionError):
        launcher._replace("new", "TurboADB.exe", wait=-1)


def test_copies_for_a_python_that_is_gone_are_removed(tmp_path):
    python = tmp_path / "Python311"
    python.mkdir()
    (python / "pythonw.exe").write_bytes(b"MZ")
    root = tmp_path / "launcher"
    for name, home in (("keep", python), ("alive", python), ("gone", tmp_path / "Uninstalled"), ("junk", None)):
        (root / name).mkdir(parents=True)
        if home is not None:
            (root / name / "pyvenv.cfg").write_text(f"home = {home}\ninclude-system-site-packages = false\n")
    launcher._forget_orphans(str(root / "keep"))
    assert sorted(os.listdir(root)) == ["alive", "junk", "keep"]


def _copy_for(folder, name, python, **state):
    """A launcher copy in ``folder/name`` for the Python in *python*, with
    *state* in its ``state.json``."""
    home = folder / name
    home.mkdir(parents=True)
    (home / "pyvenv.cfg").write_text(f"home = {python}\ninclude-system-site-packages = false\n")
    (home / "state.json").write_text(json.dumps(dict({"built": {}, "ok": True}, **state)))
    return home


def test_copies_for_a_deleted_environment_or_checkout_are_removed(tmp_path):
    """Each environment and each checkout has a copy of its own, and only the
    ones of an uninstalled Python ever went: those of a deleted venv, a
    removed worktree or a recycled uvx environment stayed for good."""
    python = tmp_path / "Python311"
    python.mkdir()
    (python / "pythonw.exe").write_bytes(b"MZ")
    venv, checkout = tmp_path / "venv", tmp_path / "checkout"
    venv.mkdir()
    checkout.mkdir()
    root = tmp_path / "launcher"
    here = {"prefix": str(venv), "root": str(checkout)}
    _copy_for(root, "keep", python, **here)
    _copy_for(root, "alive", python, **here)
    _copy_for(root, "venv-gone", python, prefix=str(tmp_path / "deleted-venv"), root=str(checkout))
    _copy_for(root, "checkout-gone", python, prefix=str(venv), root=str(tmp_path / "removed-worktree"))
    _copy_for(root, "unrecorded", python)
    kept = ["alive", "keep", "unrecorded"]
    if os.name == "nt":  # a drive that is not connected says nothing about what is on it
        import string

        free = next(f"{letter}:/" for letter in reversed(string.ascii_uppercase)
                    if not os.path.exists(f"{letter}:/"))
        _copy_for(root, "unplugged", python, prefix=free + "venv", root=free + "checkout")
        kept.append("unplugged")
    launcher._forget_orphans(str(root / "keep"))
    assert sorted(os.listdir(root)) == sorted(kept)


def test_a_new_copy_records_its_environment_and_its_turboadb(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    origin = {"executable": sys.executable, "root": str(tmp_path / "checkout"), "path": [], "sites": []}
    monkeypatch.setattr(launcher, "supported", lambda: True)
    monkeypatch.setattr(launcher, "_origin", lambda: origin)
    monkeypatch.setattr(launcher, "_stamp", lambda: {"format": 1, "version": "4.0.0"})
    monkeypatch.setattr(launcher, "_build", lambda rt: os.makedirs(os.path.dirname(rt.exe)) or open(rt.exe, "wb").close())
    monkeypatch.setattr(launcher, "_probe", lambda rt: True)
    monkeypatch.setattr(launcher, "_Lock", lambda home: contextlib.nullcontext())
    rt = launcher.runtime()
    assert rt is not None
    with open(os.path.join(rt.home, "state.json"), encoding="utf-8") as fh:
        state = json.load(fh)
    assert state["prefix"] == os.path.abspath(sys.prefix) and state["root"] == origin["root"]
    assert state["ok"] is True and launcher.runtime(build=False) == rt  # and it is still read as built


# the head of turboadb_launch.py as 3.0 and 3.1 wrote it
_OLDER_SCRIPT = (
    "# TurboADB's GUI in TurboADB.exe, for {root!r} on Python 3.11.9.\n"
    "# Generated by turboadb.launcher: edits are overwritten.\n"
    "import os\n"
    "import site\n"
    "import sys\n"
    "\n"
    "HOME = {home!r}\n"
    "EXECUTABLE = {executable!r}\n"
    "PATH = {path!r}\n"
    "SITES = {sites!r}\n"
)


def test_a_copy_made_before_the_record_is_judged_by_its_script(tmp_path):
    """Copies of 3.0 and 3.1 have no record: their script names the
    interpreter and the TurboADB folder they were made for."""
    python = tmp_path / "Python311"
    python.mkdir()
    (python / "pythonw.exe").write_bytes(b"MZ")
    venv, checkout = tmp_path / "venv", tmp_path / "checkout"
    (venv / "Scripts").mkdir(parents=True)
    checkout.mkdir()
    root = tmp_path / "launcher"

    def older(name, executable, folder):
        home = _copy_for(root, name, python)
        script = launcher._runtime_at(str(home)).script
        os.makedirs(os.path.dirname(script))
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_OLDER_SCRIPT.format(root=str(folder), home=str(home), executable=str(executable),
                                          path=[str(folder)], sites=[]))

    older("keep", venv / "Scripts" / "pythonw.exe", checkout)
    older("alive", venv / "Scripts" / "pythonw.exe", checkout)
    older("venv-gone", tmp_path / "deleted-venv" / "Scripts" / "pythonw.exe", checkout)
    older("checkout-gone", venv / "Scripts" / "pythonw.exe", tmp_path / "removed's worktree")
    launcher._forget_orphans(str(root / "keep"))
    assert sorted(os.listdir(root)) == ["alive", "keep"]


def test_shortcuts_start_turboadb_exe(monkeypatch):
    monkeypatch.setattr(launcher, "has_qt", lambda: True)  # it runs the GUI of this Python: only with PyQt5
    monkeypatch.setattr(launcher, "gui_command",
                        lambda build=True: ("C:\\t\\bin\\TurboADB.exe", ["-m", "turboadb_launch"], "C:\\t"))
    assert cli._resolve_gui_target() == ("C:\\t\\bin\\TurboADB.exe", "-m turboadb_launch", "C:\\t")


# ------------------------------------------------------ shortcut upkeep
def test_an_out_of_date_shortcut_is_refreshed_quietly(monkeypatch, tmp_path):
    made = []
    desk, start = tmp_path / "desk.lnk", tmp_path / "start.lnk"
    for path in (desk, start):
        path.write_text("lnk")
    monkeypatch.setattr(cli, "_shortcut_paths", lambda name="TurboADB": [
        ("desktop", str(desk), lambda n: made.append("desktop") or True),
        ("start menu", str(start), lambda n: made.append("start menu") or True)])
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(cli, "_resolve_gui_target", lambda build=True: ("C:\\t\\TurboADB.exe", "-m turboadb_launch", "C:\\t"))
    monkeypatch.setattr(cli, "_shortcut_icon", lambda: "C:\\i\\dark.ico")
    monkeypatch.setattr(cli, "_shortcut_blocker", lambda: None)
    current = {"target": "C:\\t\\TurboADB.exe", "args": "-m turboadb_launch", "icon": "C:\\i\\dark.ico",
               "app_id": winshell.APP_ID}
    infos = {str(desk): dict(current), str(start): dict(current, target="C:\\Python\\Scripts\\turboadb-gui.exe")}
    monkeypatch.setattr(winshell, "read_shortcut", lambda path: infos.get(path))
    assert cli.ensure_shortcuts() == {}  # refreshed, not reported
    assert made == ["start menu"]
    made.clear()
    infos[str(start)] = dict(current, app_id=None)  # a pin of it would show beside the window
    cli.ensure_shortcuts()
    assert made == ["start menu"]
    made.clear()
    infos[str(start)] = dict(current, icon="C:\\i\\light.ico")  # the other mode's icon
    cli.ensure_shortcuts()
    assert made == ["start menu"]
    made.clear()
    infos[str(start)] = dict(current)
    infos[str(desk)] = None  # unreadable: not ours to judge
    assert cli.ensure_shortcuts() == {} and made == []
    infos[str(start)] = dict(current, args="")
    monkeypatch.setattr(cli, "_shortcut_blocker", lambda: "runs another copy")
    assert cli.ensure_shortcuts() == {} and made == []  # never while blocked


# ------------------------------------------------- Windows: the real things
@windows_only
def test_turboadb_exe_is_built_named_and_started(own_home):
    rt = launcher.runtime()
    assert rt is not None, "TurboADB.exe could not be built or did not start"
    assert rt.exe.startswith(str(own_home)) and os.path.basename(rt.exe) == "TurboADB.exe"
    assert launcher._file_version_string(rt.exe, "FileDescription") == "TurboADB"  # Task Manager's name
    assert launcher._file_version_string(rt.exe, "ProductName") == "TurboADB"
    kinds = {}
    for rtype, _name, _lang in launcher._resources(rt.exe, (launcher.RT_ICON, launcher.RT_GROUP_ICON, 24)):
        kinds[rtype] = kinds.get(rtype, 0) + 1
    assert kinds == {launcher.RT_ICON: 7, launcher.RT_GROUP_ICON: 1, 24: 1}  # TurboADB's icon, Python's manifest
    with open(os.path.join(rt.home, "state.json"), encoding="utf-8") as fh:
        assert json.load(fh)["ok"] is True
    assert launcher.runtime() == rt and launcher.runtime(build=False) == rt
    assert launcher.gui_command() == (rt.exe, ["-m", "turboadb_launch"], rt.home)
    out = subprocess.run([rt.exe, "-m", "turboadb_launch", "--probe"], capture_output=True, text=True,
                         timeout=60, cwd=os.environ.get("SystemRoot", "C:\\Windows"))
    report = json.loads(next(line for line in out.stdout.splitlines() if line.startswith(launcher._MARK))
                        [len(launcher._MARK):])
    import turboadb
    from turboadb.tools import windowless_python

    assert os.path.normcase(report["turboadb"]) == os.path.normcase(turboadb.__file__)
    # pip, the update checks and helper processes run the real Python
    assert os.path.normcase(report["executable"]) == os.path.normcase(windowless_python())
    assert os.path.normcase(report["image"]) == os.path.normcase(rt.exe)


@windows_only
def test_turboadb_exe_sees_what_it_was_built_of_as_its_builder_does(own_home):
    """Else every start from a shortcut would take the copy for out of date."""
    rt = launcher.runtime()
    assert rt is not None, "TurboADB.exe could not be built or did not start"
    out = subprocess.run([rt.exe, "-m", "turboadb_launch", "--probe"], capture_output=True, text=True,
                         timeout=60, cwd=rt.home)
    report = json.loads(next(line for line in out.stdout.splitlines() if line.startswith(launcher._MARK))
                        [len(launcher._MARK):])
    with open(os.path.join(rt.home, "state.json"), encoding="utf-8") as fh:
        state = json.load(fh)
    assert report["stamp"] == state["built"]["stamp"] == json.loads(json.dumps(launcher._stamp()))
    import turboadb

    assert state["prefix"] == os.path.abspath(sys.prefix)
    assert state["root"] == os.path.dirname(os.path.dirname(os.path.abspath(turboadb.__file__)))


@windows_only
def test_a_copy_that_failed_its_check_is_not_retried_until_something_changes(own_home, monkeypatch):
    probes = []
    monkeypatch.setattr(launcher, "_probe", lambda rt: probes.append(rt) or False)
    assert launcher.runtime() is None and launcher.runtime() is None
    assert len(probes) == 1
    monkeypatch.setattr(launcher, "_stamp", lambda: {"format": -1})  # e.g. Python was updated
    assert launcher.runtime() is None and len(probes) == 2


def _window():
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    user32.DestroyWindow.argtypes = (wintypes.HWND,)
    return user32, user32.CreateWindowExW(0, "STATIC", "turboadb-test", 0, 0, 0, 10, 10,
                                          None, None, None, None)


@windows_only
def test_a_window_tells_the_taskbar_what_a_pin_shows_and_starts():
    user32, hwnd = _window()
    try:
        command = winshell.command_line("C:\\Program Files\\T\\TurboADB.exe", "-m turboadb_launch")
        assert command == '"C:\\Program Files\\T\\TurboADB.exe" -m turboadb_launch'
        assert winshell.set_window_identity(hwnd, command, "C:\\t\\icon.ico")
        assert winshell.window_identity(hwnd) == {
            "ID": winshell.APP_ID, "RelaunchCommand": command,
            "RelaunchDisplayNameResource": "TurboADB", "RelaunchIconResource": "C:\\t\\icon.ico,0"}
    finally:
        user32.DestroyWindow(hwnd)


@windows_only
def test_a_pins_command_never_exceeds_what_windows_keeps(tmp_path):
    """A pin's command holds 255 characters: a longer path goes in its 8.3
    form, and a command that still won't fit sets the app ID alone."""
    deep = tmp_path / ("folder with a long name " * 5).strip() / "bin"  # under MAX_PATH
    deep.mkdir(parents=True)
    exe = deep / "TurboADB.exe"
    exe.write_bytes(b"MZ")
    line = winshell.command_line(str(exe), "-m turboadb_launch")
    assert len(line) <= 255 or len(winshell.short_path(str(exe))) > 240  # 8.3 names can be off
    user32, hwnd = _window()
    try:
        assert not winshell.set_window_identity(hwnd, "C:\\" + "x" * 300, "C:\\t\\icon.ico")
        assert winshell.window_identity(hwnd)["ID"] == winshell.APP_ID
    finally:
        user32.DestroyWindow(hwnd)


@windows_only
def test_shortcuts_carry_the_app_id_and_keep_it_when_their_icon_follows_the_mode(tmp_path):
    ours, older, other = (str(tmp_path / n) for n in ("ours.lnk", "older.lnk", "other.lnk"))
    assert winshell.write_shortcut(ours, sys.executable, "-m turboadb_launch", str(tmp_path),
                                   str(tmp_path / "dark.ico"), "TurboADB")
    info = winshell.read_shortcut(ours)
    assert info == {"target": sys.executable, "args": "-m turboadb_launch", "workdir": str(tmp_path),
                    "icon": str(tmp_path / "dark.ico"), "app_id": winshell.APP_ID}
    # an earlier TurboADB's, without the app ID, and somebody else's
    launcher_exe = tmp_path / "Scripts" / "turboadb-gui.exe"
    launcher_exe.parent.mkdir()
    launcher_exe.write_bytes(b"MZ")
    assert winshell.write_shortcut(older, str(launcher_exe), "", str(tmp_path), str(tmp_path / "dark.ico"),
                                   "TurboADB", app_id=None)
    assert winshell.read_shortcut(older)["app_id"] is None
    assert winshell.write_shortcut(other, sys.executable, "", str(tmp_path), str(tmp_path / "dark.ico"),
                                   "Other", app_id="Someone.Else")

    light = str(tmp_path / "light.ico")
    changed = winshell.sync_shortcut_icons(light, [ours, older, other])
    assert sorted(changed) == sorted([ours, older])
    assert winshell.read_shortcut(ours) == dict(info, icon=light)  # the app ID stays
    assert winshell.read_shortcut(older)["icon"] == light
    assert winshell.read_shortcut(other)["icon"] == str(tmp_path / "dark.ico")
    assert winshell.sync_shortcut_icons(light, [ours, older]) == []  # already


@windows_only
def test_a_shortcut_to_a_copy_of_the_exe_is_pointed_at_what_starts_turboadb(tmp_path, monkeypatch):
    """Up to 3.1 a pin made of the wheel's exe started the copy it ran from,
    in the temp folder: the old version for as long as that copy was there
    (which wrote the other shortcuts back to itself), and nothing once it was
    deleted."""
    monkeypatch.setattr(winshell, "notify_changed", lambda paths: None)
    icon = str(tmp_path / "dark.ico")
    temp, scripts = tmp_path / "temp", tmp_path / "Scripts"
    for folder in (temp, scripts):
        folder.mkdir()
    starter, still_there = scripts / "turboadb-gui.exe", temp / "turboadb-gui-3.1.0-21.exe"
    for path in (starter, still_there):
        path.write_bytes(b"MZ")
    gone = str(temp / "turboadb-gui-3.1.0-20.exe")
    dead, alive, branded, other = (str(tmp_path / n) for n in ("dead.lnk", "alive.lnk", "branded.lnk", "other.lnk"))
    assert winshell.write_shortcut(dead, gone, "--x", str(temp), icon, "TurboADB")
    assert winshell.write_shortcut(alive, str(still_there), "", str(temp), icon, "TurboADB")
    assert winshell.write_shortcut(branded, str(tmp_path / "gone" / "TurboADB.exe"), "-m turboadb_launch",
                                   str(tmp_path), icon, "TurboADB")  # not a copy of the exe: not ours to judge
    assert winshell.write_shortcut(other, str(tmp_path / "gone" / "notepad.exe"), "", str(tmp_path), icon,
                                   "Other", app_id="Someone.Else")
    before = {path: winshell.read_shortcut(path) for path in (branded, other)}

    # what starts TurboADB is itself such a copy: nothing to point at
    assert winshell.revive_shortcuts([dead, alive], str(still_there), "", str(temp)) == []
    changed = winshell.revive_shortcuts([dead, alive, branded, other], str(starter), "", str(scripts))
    assert changed == [dead, alive]
    for path in changed:  # its app ID and icon stay
        assert winshell.read_shortcut(path) == {"target": str(starter), "args": "", "workdir": str(scripts),
                                                "icon": icon, "app_id": winshell.APP_ID}
    assert {path: winshell.read_shortcut(path) for path in before} == before
    assert winshell.revive_shortcuts([dead, alive, branded, other], str(starter), "", str(scripts)) == []


def test_a_pin_made_of_a_window_is_found_beside_the_pinned_shortcuts(tmp_path, monkeypatch):
    """A pin of a shortcut is kept in User Pinned\\TaskBar.  Of a window whose
    app ID no Start-menu shortcut has, Windows makes a shortcut of its own in
    a folder under ImplicitAppShortcuts: there a pin of TurboADB's window
    never followed the taskbar's mode, and stayed on a copy of the exe that
    was gone."""
    pinned = tmp_path / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned"
    of_shortcuts, of_windows = pinned / "TaskBar", pinned / "ImplicitAppShortcuts"
    for folder in (of_shortcuts, of_windows / "58dc9c9c4a87b968", of_windows / "d356105fac5527ef"):
        folder.mkdir(parents=True)
    wanted = [of_shortcuts / "TurboADB.lnk", of_windows / "58dc9c9c4a87b968" / "TurboADB.lnk",
              of_windows / "d356105fac5527ef" / "Other.LNK"]
    for path in wanted + [of_shortcuts / "desktop.ini", of_windows / "notes.txt",
                          of_windows / "58dc9c9c4a87b968" / "desktop.ini"]:
        path.write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert sorted(winshell.pinned_shortcuts()) == sorted(str(path) for path in wanted)
    monkeypatch.setenv("APPDATA", str(tmp_path / "no-such-profile"))
    assert winshell.pinned_shortcuts() == []


def test_turboadbs_shortcuts_are_known_with_or_without_the_app_id():
    known = winshell.is_turboadb_shortcut
    assert known({"app_id": winshell.APP_ID, "target": "C:\\anything.exe"})
    assert known({"target": "C:\\Users\\u\\.turboadb\\launcher\\py311-x\\bin\\TurboADB.exe"})
    assert known({"target": "C:\\Python\\Scripts\\turboadb-gui.exe"})
    assert known({"target": "D:\\Downloads\\TurboADB-3.0.0-win64.exe"})
    assert known({"target": "C:\\Python\\pythonw.exe", "args": "-m turboadb gui"})
    assert not known({"target": "C:\\Python\\pythonw.exe", "args": "-m idlelib"})
    assert not known({"target": "C:\\Windows\\notepad.exe", "app_id": "Other.App"})
