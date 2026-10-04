"""The PyPI wheel ships the Windows GUI executable: `turboadb-gui` starts it
when PyQt5 isn't installed, and scripts/release.py bundles it and refuses a
wheel without it."""
import importlib.util
import json
import os
import sys
import zipfile

import pytest

import turboadb.cli as cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _release_module():
    spec = importlib.util.spec_from_file_location(
        "turboadb_release_script", os.path.join(ROOT, "scripts", "release.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_data_ships_the_bundled_exe():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        text = fh.read()
    package_data = text.split("[tool.setuptools.package-data]", 1)[1].split("\n[", 1)[0]
    assert '"bin/*.exe"' in package_data


@pytest.mark.skipif(os.name != "nt", reason="the bundled executable is Windows-only")
def test_without_pyqt5_the_bundled_exe_is_started(tmp_path, monkeypatch):
    exe = tmp_path / "turboadb-gui.exe"
    exe.write_bytes(b"MZ")
    calls = []
    monkeypatch.setitem(sys.modules, "PyQt5", None)  # import PyQt5 -> ImportError
    monkeypatch.setattr(cli, "_gui_exe_path", lambda: str(exe))
    monkeypatch.setattr(cli, "_staged_exe", lambda src: src)
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, **kwargs: calls.append(argv) or 0)
    monkeypatch.setattr(cli, "_prewarm_gui_adb_server", lambda: calls.append("prewarm"))
    assert cli.launch_gui(["--flag"]) == 0
    assert calls == [[str(exe), "--flag"]]


@pytest.mark.skipif(os.name != "nt", reason="the bundled executable is Windows-only")
def test_the_bundled_exe_is_told_what_started_it(tmp_path, monkeypatch):
    """Its shortcuts and pins must start that again, not the copy it runs
    from, which the next upgrade replaces and deletes."""
    exe, staged = tmp_path / "turboadb-gui.exe", tmp_path / "staged.exe"
    scripts = tmp_path / "Python" / "Scripts"
    scripts.mkdir(parents=True)
    for path in (exe, staged, scripts / "turboadb-gui.exe"):
        path.write_bytes(b"MZ")
    calls = []
    monkeypatch.setitem(sys.modules, "PyQt5", None)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Python" / "pythonw.exe"))
    monkeypatch.setattr(cli, "_gui_exe_path", lambda: str(exe))
    monkeypatch.setattr(cli, "_staged_exe", lambda src: str(staged))
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, env=None: calls.append((argv, env)) or 0)
    assert cli.launch_gui([]) == 0
    (argv, env), = calls
    assert argv == [str(staged)]
    assert json.loads(env["TURBOADB_GUI_STARTED_BY"]) == [
        str(staged), str(scripts / "turboadb-gui.exe"), "", str(scripts)]
    assert env["PATH"] == os.environ["PATH"]  # with the rest of the environment


def test_the_exe_started_by_turboadb_gui_points_shortcuts_and_pins_at_it(tmp_path, monkeypatch):
    """It pointed them at itself: after pip install -U they still started
    the old version, and nothing once its copy was deleted."""
    staged = tmp_path / "gui" / "turboadb-gui-4.0.0-20.exe"
    scripts = tmp_path / "Scripts"
    for folder in (staged.parent, scripts):
        folder.mkdir()
    starter = scripts / "turboadb-gui.exe"
    for path in (staged, starter):
        path.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(staged))
    itself = (str(staged), "", str(staged.parent))
    monkeypatch.delenv("TURBOADB_GUI_STARTED_BY", raising=False)
    assert cli._resolve_gui_target() == itself  # the exe someone downloaded and started

    started_by = [str(staged), str(starter), "", str(scripts)]
    monkeypatch.setenv("TURBOADB_GUI_STARTED_BY", json.dumps(started_by))
    assert cli._resolve_gui_target() == (str(starter), "", str(scripts))
    assert cli._resolve_gui_target(build=False) == (str(starter), "", str(scripts))  # a pin's command

    # said of another exe (this variable reaches whatever the app starts), not
    # readable, or of a launcher that is gone: the exe itself
    monkeypatch.setenv("TURBOADB_GUI_STARTED_BY", json.dumps([str(tmp_path / "other.exe")] + started_by[1:]))
    assert cli._resolve_gui_target() == itself
    monkeypatch.setenv("TURBOADB_GUI_STARTED_BY", "{}")
    assert cli._resolve_gui_target() == itself
    os.remove(starter)
    monkeypatch.setenv("TURBOADB_GUI_STARTED_BY", json.dumps(started_by))
    assert cli._resolve_gui_target() == itself


@pytest.mark.skipif(os.name != "nt", reason="shortcut files are Windows-only")
def test_the_exe_started_by_turboadb_gui_repairs_a_shortcut_to_a_copy_and_keeps_a_good_one(tmp_path, monkeypatch):
    """Since 3.0 a shortcut to turboadb-gui was "out of date" for the exe, and
    rewritten to the copy it ran from."""
    from turboadb import winshell

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    staged = tmp_path / ".turboadb" / "gui" / "turboadb-gui-4.0.0-20.exe"
    scripts, temp = tmp_path / "Scripts", tmp_path / "temp"
    for folder in (staged.parent, scripts, temp):
        folder.mkdir(parents=True)
    starter, old_copy = scripts / "turboadb-gui.exe", temp / "turboadb-gui-3.1.0-20.exe"
    for path in (staged, starter):
        path.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(staged))
    monkeypatch.setenv("TURBOADB_GUI_STARTED_BY", json.dumps([str(staged), str(starter), "", str(scripts)]))
    monkeypatch.setattr(cli, "_desktop_folder", lambda: str(tmp_path / "Desktop"))
    monkeypatch.setattr(cli, "_programs_folder", lambda: str(tmp_path / "Programs"))
    # the PowerShell fallback would write to the real Desktop and Start menu
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("PowerShell was started"))
    (desk, _make), (start, _make2) = [(path, maker) for _loc, path, maker in cli._shortcut_paths()]
    for folder in (tmp_path / "Desktop", tmp_path / "Programs"):
        folder.mkdir()
    icon = cli._shortcut_icon()
    assert winshell.write_shortcut(desk, str(starter), "", str(scripts), icon, "TurboADB")
    assert winshell.write_shortcut(start, str(old_copy), "", str(temp), icon, "TurboADB")  # as 3.1 left it
    before = os.stat(desk).st_mtime_ns

    assert cli.ensure_shortcuts() == {}  # quietly
    assert winshell.read_shortcut(start)["target"] == str(starter)
    assert winshell.read_shortcut(desk)["target"] == str(starter) and os.stat(desk).st_mtime_ns == before


def test_without_pyqt5_or_exe_the_install_hint_is_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "PyQt5", None)
    monkeypatch.setattr(cli, "_gui_exe_path", lambda: str(tmp_path / "missing.exe"))
    monkeypatch.setattr(cli, "_prewarm_gui_adb_server", lambda: None)
    assert cli.launch_gui([]) == 1
    assert 'pip install "turboadb[gui]"' in capsys.readouterr().err


@pytest.fixture
def staging(tmp_path, monkeypatch):
    """A packaged exe, and a home and a temp folder of the test's own."""
    import tempfile
    import types

    home, temp = tmp_path / "home", tmp_path / "temp"
    home.mkdir()
    temp.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    src = tmp_path / "turboadb-gui.exe"
    src.write_bytes(b"MZ" * 10)
    return types.SimpleNamespace(src=src, temp=temp, folder=home / ".turboadb" / "gui")


def test_staged_exe_runs_a_copy_in_the_users_own_folder(staging):
    staged = cli._staged_exe(str(staging.src))
    assert os.path.dirname(staged) == str(staging.folder)
    assert open(staged, "rb").read() == staging.src.read_bytes()
    assert cli._staged_exe(str(staging.src)) == staged
    assert os.listdir(staging.temp) == [] and os.listdir(staging.folder) == [os.path.basename(staged)]


def test_a_file_planted_in_the_temp_folder_is_never_run(staging):
    """The copy went to the temp folder and was run again whenever a file of
    its name and size was there.  Where other accounts can write to that
    folder (TEMP=C:\\Temp), one of them could leave its own program there."""
    from turboadb import __version__

    planted = staging.temp / f"turboadb-gui-{__version__}-{staging.src.stat().st_size}.exe"
    planted.write_bytes(b"XX" * 10)
    staged = cli._staged_exe(str(staging.src))
    assert os.path.dirname(staged) == str(staging.folder)
    assert open(staged, "rb").read() == staging.src.read_bytes()


def test_a_damaged_copy_of_the_right_size_is_replaced(staging):
    staged = cli._staged_exe(str(staging.src))
    with open(staged, "wb") as fh:
        fh.write(b"\0" * staging.src.stat().st_size)
    assert cli._staged_exe(str(staging.src)) == staged
    assert open(staged, "rb").read() == staging.src.read_bytes()


def test_a_damaged_copy_that_cannot_be_replaced_is_not_run(staging, monkeypatch):
    """Windows keeps a program file that is running locked: the packaged exe
    runs in place then, as when no copy can be made at all."""
    staged = cli._staged_exe(str(staging.src))
    with open(staged, "wb") as fh:
        fh.write(b"\0" * staging.src.stat().st_size)

    def locked(src, dst):
        raise PermissionError(13, "in use", dst)

    monkeypatch.setattr(os, "replace", locked)
    assert cli._staged_exe(str(staging.src)) == str(staging.src)
    assert os.listdir(staging.folder) == [os.path.basename(staged)]  # no half-made copy is left


def test_release_bundles_the_versioned_exe(tmp_path, monkeypatch):
    release = _release_module()
    monkeypatch.setattr(release, "ROOT", tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "TurboADB-9.9.9-win64.exe").write_bytes(b"MZ-9.9.9")
    monkeypatch.setattr(release, "run", lambda cmd: pytest.fail(f"unexpected build: {cmd}"))
    target = release.bundle_exe("9.9.9")
    assert target == tmp_path / "turboadb" / "bin" / "turboadb-gui.exe"
    assert target.read_bytes() == b"MZ-9.9.9"


def test_release_detects_a_wheel_without_the_exe(tmp_path):
    release = _release_module()
    good = tmp_path / "good.whl"
    bad = tmp_path / "bad.whl"
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("turboadb/__init__.py", "")
        zf.writestr("turboadb/bin/turboadb-gui.exe", "MZ")
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("turboadb/__init__.py", "")
    assert release.wheel_has_exe(good)
    assert not release.wheel_has_exe(bad)


def _gui_entry_module():
    spec = importlib.util.spec_from_file_location(
        "turboadb_gui_entry_script", os.path.join(ROOT, "scripts", "gui_entry.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exe_self_test_finishes_without_a_stderr(tmp_path, monkeypatch):
    """A windowed exe has sys.stderr = None: writing the verdict there raised,
    and the frozen exe then waited on an error dialog until CI cancelled it."""
    import types

    entry = _gui_entry_module()
    fake_winrm = types.ModuleType("winrm")
    fake_winrm.Session = lambda *args, **kwargs: object()
    monkeypatch.setitem(sys.modules, "winrm", fake_winrm)
    monkeypatch.setattr(importlib, "import_module", lambda name: types.ModuleType(name))
    monkeypatch.setattr(os.path, "expanduser", lambda path: str(tmp_path))
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setenv("TURBOADB_SELFTEST", "winrm")
    assert entry.run() == 0
    report = (tmp_path / ".turboadb" / "winrm-selftest.txt").read_text(encoding="utf-8")
    assert report.startswith("WINRM-SELFTEST: ALL OK")


def test_exe_startup_failure_is_reported_without_a_stderr(tmp_path, monkeypatch):
    entry = _gui_entry_module()
    monkeypatch.setattr(os.path, "expanduser", lambda path: str(tmp_path))
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setitem(sys.modules, "ctypes", None)  # no message box either
    entry._fatal("boom")  # must not raise
    assert "boom" in (tmp_path / ".turboadb" / "crash.log").read_text(encoding="utf-8")
