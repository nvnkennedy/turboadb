"""The first start after upgrading a plain ``pip install turboadb`` (no PyQt5)
from 3.0 or 3.1.

There the GUI is the executable the wheel brings, run from a copy.  Up to 3.1
that copy was in the temp folder and was what the Desktop and Start-menu
shortcuts and a taskbar pin started: the old version for as long as the copy
was there, and nothing once it was deleted.  ``turboadb shortcut`` wrote
shortcuts to TurboADB.exe instead, which opened nothing without PyQt5.

Whatever is done first after the upgrade, TurboADB starts, and one start with
``turboadb-gui`` leaves every shortcut and the pin starting ``turboadb-gui``.
(A shortcut whose copy is still there starts that old executable until then,
and Windows itself says so when the copy is gone: neither runs anything of
this version.)

The shortcuts are real ``.lnk`` files in folders of the test; the executables
are stand-ins that are never started.
"""
import json
import os
import subprocess
import sys
import tempfile
import types

import pytest

from turboadb import cli, launcher, update, winshell

pytestmark = pytest.mark.skipif(os.name != "nt", reason="shortcut files and pins are Windows-only")

_OLD_COPY = "turboadb-gui-3.1.0-60109646.exe"


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """A PC as 3.1 left it, with the new TurboADB installed in a Python
    without PyQt5: ``links`` are the Desktop and Start-menu shortcuts and a
    taskbar pin, all starting the old copy in the temp folder."""
    home, temp, appdata = tmp_path / "home", tmp_path / "temp", tmp_path / "appdata"
    python, desk, programs = tmp_path / "Python", tmp_path / "Desktop", tmp_path / "Programs"
    pins = appdata / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "TaskBar"
    for folder in (home, temp, python / "Scripts", desk, programs, pins):
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("TURBOADB_GUI_STARTED_BY", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    starter, packaged = python / "Scripts" / "turboadb-gui.exe", tmp_path / "turboadb-gui.exe"
    old_copy = temp / _OLD_COPY
    for path in (python / "pythonw.exe", starter, old_copy):
        path.write_bytes(b"MZ")
    packaged.write_bytes(b"MZ the new build")
    monkeypatch.setattr(sys, "executable", str(python / "pythonw.exe"))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setitem(sys.modules, "PyQt5", None)  # import PyQt5 -> ImportError
    monkeypatch.setattr(cli, "_gui_exe_path", lambda: str(packaged))
    monkeypatch.setattr(cli, "_desktop_folder", lambda: str(desk))
    monkeypatch.setattr(cli, "_programs_folder", lambda: str(programs))
    monkeypatch.setitem(cli._JSON, "on", False)
    monkeypatch.setattr(update, "running_copy", lambda: {"same": True, "note": None})
    monkeypatch.setattr(winshell, "notify_changed", lambda paths: None)
    # the PowerShell fallback would write to the real Desktop and Start menu
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(f"started {a}"))
    started = []
    monkeypatch.setattr(subprocess, "call", lambda argv, env=None: started.append((argv, env)) or 0)
    links = {"desktop": str(desk / "TurboADB.lnk"), "start menu": str(programs / "TurboADB.lnk"),
             "pin": str(pins / "TurboADB.lnk")}
    icon = winshell.icon_file()
    for path in links.values():
        assert winshell.write_shortcut(path, str(old_copy), "", str(temp), icon, "TurboADB")
    return types.SimpleNamespace(home=home, temp=temp, starter=str(starter), old_copy=old_copy,
                                 scripts=str(python / "Scripts"), links=links, started=started,
                                 monkeypatch=monkeypatch)


def _starts(pc, *names):
    """What each of the shortcuts *names* (all of them by default) starts."""
    found = {name: winshell.read_shortcut(pc.links[name]) for name in names or pc.links}
    return {name: (info["target"], info["args"]) for name, info in found.items()}


def _turboadb_gui(pc, upkeep=True):
    """``turboadb-gui`` in the Python without PyQt5, then the start of the
    executable it ran: its shortcut upkeep (unless turned off in Settings),
    and what still starts a copy of the executable brought in line."""
    del pc.started[:]
    assert cli.launch_gui([]) == 0
    (argv, env), = pc.started
    staged, = argv
    assert os.path.dirname(staged) == str(pc.home / ".turboadb" / "gui") and os.path.isfile(staged)
    with pc.monkeypatch.context() as exe:  # the one-file executable, running from its copy
        exe.setattr(sys, "frozen", True, raising=False)
        exe.setattr(sys, "executable", staged)
        exe.setenv("TURBOADB_GUI_STARTED_BY", env["TURBOADB_GUI_STARTED_BY"])
        cli._revive_shortcuts(winshell.pinned_shortcuts())  # gui.taskbar, as the window comes up
        if upkeep:
            assert cli.ensure_shortcuts() == {}  # quietly: nothing was missing
    return staged


def test_turboadb_gui_once_repairs_the_shortcuts_and_the_pin(pc):
    old = (str(pc.old_copy), "")
    assert _starts(pc) == {"desktop": old, "start menu": old, "pin": old}
    _turboadb_gui(pc)
    assert not pc.old_copy.exists()  # the copy of the old build went with the start
    new = (pc.starter, "")
    assert _starts(pc) == {"desktop": new, "start menu": new, "pin": new}
    for path in pc.links.values():
        info = winshell.read_shortcut(path)
        assert info["app_id"] == winshell.APP_ID and info["workdir"] == pc.scripts
    before = {path: os.stat(path).st_mtime_ns for path in pc.links.values()}
    _turboadb_gui(pc)  # and they are right from then on: nothing is written again
    assert {path: os.stat(path).st_mtime_ns for path in pc.links.values()} == before


def test_shortcuts_and_a_pin_whose_copy_is_gone_are_repaired_too(pc):
    """The temp folder was cleaned: Windows says the shortcut's program is
    gone, and the next ``turboadb-gui`` puts that right."""
    os.remove(pc.old_copy)
    _turboadb_gui(pc)
    new = (pc.starter, "")
    assert _starts(pc) == {"desktop": new, "start menu": new, "pin": new}


def test_with_shortcut_upkeep_turned_off_the_shortcuts_are_repaired_too(pc):
    """Nothing rewrote them then, and the start that deletes the old copy left
    them opening nothing.  They are only pointed at what starts TurboADB: a
    shortcut that was removed is not made again."""
    os.remove(pc.links["desktop"])
    _turboadb_gui(pc, upkeep=False)
    new = (pc.starter, "")
    assert not os.path.exists(pc.links["desktop"])
    assert _starts(pc, "start menu", "pin") == {"start menu": new, "pin": new}


def test_a_copy_that_can_not_be_deleted_no_longer_keeps_the_pin(pc):
    """An old copy that stayed (in use, or in the temp folder of another
    session) kept the pin: the pin went on starting the old version, which
    pointed the other shortcuts back at itself."""
    real_remove = os.remove

    def remove(path):
        if os.path.basename(path) == _OLD_COPY:
            raise PermissionError(32, "in use", path)
        real_remove(path)

    pc.monkeypatch.setattr(os, "remove", remove)
    _turboadb_gui(pc)
    assert pc.old_copy.exists()
    new = (pc.starter, "")
    assert _starts(pc) == {"desktop": new, "start menu": new, "pin": new}


def test_turboadb_shortcut_first_writes_shortcuts_that_open_the_app(pc, capsys):
    assert cli.create_shortcut([]) == 0
    assert "Created/refreshed" in capsys.readouterr().out
    new, old = (pc.starter, ""), (str(pc.old_copy), "")
    # what the shortcuts start now is turboadb-gui, which starts the wheel's executable
    assert _starts(pc) == {"desktop": new, "start menu": new, "pin": old}
    assert pc.old_copy.exists() and not pc.started  # the pin still opens the old version
    _turboadb_gui(pc)
    assert _starts(pc, "pin") == {"pin": new}


def test_a_shortcut_to_turboadb_exe_opens_the_app_and_is_repaired(pc, monkeypatch):
    """What ``turboadb shortcut`` of 3.0 and 3.1 wrote here: TurboADB.exe runs
    the GUI of this Python, which has none without PyQt5."""
    copy = pc.home / ".turboadb" / "launcher" / "py311-0123456789"
    (copy / "bin").mkdir(parents=True)
    branded = copy / "bin" / "TurboADB.exe"
    branded.write_bytes(b"MZ")
    (copy / "state.json").write_text(json.dumps({  # as 3.1 recorded its build
        "built": {"stamp": {"format": 1, "version": "3.1.0", "icon": "0a", "files": {}}, "files": "0b"},
        "ok": True}))
    for name in ("desktop", "start menu"):
        assert winshell.write_shortcut(pc.links[name], str(branded), "-m turboadb_launch", str(copy),
                                       winshell.icon_file(), "TurboADB")
    handed = []
    monkeypatch.setattr(launcher, "BRANDED", False)  # run_branded sets both: put back after the test
    monkeypatch.setattr(launcher, "_HOME", None)
    monkeypatch.delenv("TURBOADB_NO_LAUNCHER", raising=False)
    monkeypatch.delenv(launcher._CHECKED, raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: handed.append(cmd) or object())

    # the shortcut: TurboADB.exe -m turboadb_launch, whose script calls this
    assert launcher.run_branded([], str(copy)) == 0
    (command,), root = handed, os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    # turboadb-gui's work, in the real Python
    assert command == [sys.executable, "-c", launcher._HAND_OVER, root]
    monkeypatch.setattr(launcher, "BRANDED", False)
    monkeypatch.setattr(launcher, "_HOME", None)
    _turboadb_gui(pc)  # which is what that command runs
    new = (pc.starter, "")
    assert _starts(pc) == {"desktop": new, "start menu": new, "pin": new}


def test_with_pyqt5_installed_since_the_pin_starts_turboadb_exe(pc, monkeypatch):
    """PyQt5 came with the upgrade: the GUI now runs as TurboADB.exe, nothing
    deletes the old copy in the temp folder, and the pin kept starting it."""
    copy = pc.home / ".turboadb" / "launcher" / "py311-0123456789"
    branded = (str(copy / "bin" / "TurboADB.exe"), ["-m", "turboadb_launch"], str(copy))
    monkeypatch.setattr(launcher, "has_qt", lambda: True)
    monkeypatch.setattr(launcher, "gui_command", lambda build=True: branded)
    cli._revive_shortcuts(winshell.pinned_shortcuts())
    assert cli.ensure_shortcuts() == {}
    assert pc.old_copy.exists()
    new = (branded[0], "-m turboadb_launch")
    assert _starts(pc) == {"desktop": new, "start menu": new, "pin": new}
