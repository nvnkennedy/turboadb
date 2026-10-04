"""The Desktop and Start-menu shortcuts start the TurboADB installed in this
Python. They are never written by a process running another copy (a source
checkout started with ``python main.py`` while an older release is installed),
a refresh that failed is reported as failed, and the bundled-exe fallback does
not leave a copy of every old build behind."""
import os
import sys

import pytest

import turboadb.cli as cli
import turboadb.launcher as launcher
import turboadb.update as update


@pytest.fixture
def links(tmp_path, monkeypatch):
    """Two shortcut locations in tmp_path; each maker call is recorded and
    writes the .lnk only when ``state['works']``."""
    state = {"works": True, "made": []}

    def maker_for(path):
        def make(name):
            state["made"].append(path.name)
            if state["works"]:
                path.write_text("lnk")
            return state["works"]

        return make

    desktop, start = tmp_path / "desktop.lnk", tmp_path / "start.lnk"
    monkeypatch.setattr(cli, "_shortcut_paths", lambda name="TurboADB": [
        ("desktop", str(desktop), maker_for(desktop)),
        ("start menu", str(start), maker_for(start)),
    ])
    # as the turboadb-shortcut script runs it: a fresh process, no --json
    monkeypatch.setitem(cli._JSON, "on", False)
    state.update(desktop=desktop, start=start)
    return state


def _copy(monkeypatch, same, installed_path="C:/Python/Lib/site-packages/turboadb"):
    note = None if same else f"TurboADB 9.9.9 runs from X, but this Python has 2.0.0 from {installed_path}"
    monkeypatch.setattr(update, "running_copy", lambda: {
        "version": "9.9.9", "path": os.path.join("E:/work/turboadb", "turboadb"),
        "installed": "2.0.0", "installed_path": installed_path, "same": same,
        "editable": False, "pip_managed": same, "note": note,
    })


def test_a_failed_refresh_of_an_existing_shortcut_is_a_failure(links, monkeypatch, capsys):
    """PowerShell refused (a blocked COM object) while the old .lnk stayed: the
    command said 'Created/refreshed' and exited 0, and the link kept pointing
    at a deleted venv."""
    _copy(monkeypatch, same=True)
    links["desktop"].write_text("old")
    links["start"].write_text("old")
    links["works"] = False
    assert cli.ensure_shortcuts(force=True) == {"desktop": False, "start menu": False}
    monkeypatch.setattr(os, "name", "nt")
    assert cli.create_shortcut([]) == 1
    assert "Could not create or refresh" in capsys.readouterr().err


def test_a_launch_repairs_only_what_is_missing_and_reports_it(links, monkeypatch):
    _copy(monkeypatch, same=True)
    links["desktop"].write_text("old")
    assert cli.ensure_shortcuts() == {"start menu": True}
    assert links["made"] == ["start.lnk"]
    assert cli.ensure_shortcuts() == {}  # both there: nothing written, nothing said


def test_a_checkout_leaves_the_shortcuts_alone(links, monkeypatch, capsys):
    _copy(monkeypatch, same=False)
    assert cli.ensure_shortcuts() == {}  # a GUI launch: quietly
    assert cli.ensure_shortcuts(force=True) == {"desktop": False, "start menu": False}
    assert links["made"] == []
    monkeypatch.setattr(os, "name", "nt")
    assert cli.create_shortcut([]) == 1
    err = capsys.readouterr().err
    assert "2.0.0" in err and "would start that copy" in err


def test_nothing_is_asked_while_both_shortcuts_exist(links, monkeypatch):
    links["desktop"].write_text("lnk")
    links["start"].write_text("lnk")
    monkeypatch.setattr(update, "running_copy", lambda: pytest.fail("asked which copy runs"))
    assert cli.ensure_shortcuts() == {}


def test_the_blocker_names_the_fix(monkeypatch, tmp_path):
    root = tmp_path / "checkout"
    (root / "turboadb").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n")
    note = "TurboADB 9.9.9 runs from here, but this Python has TurboADB 2.0.0 installed"
    monkeypatch.setattr(update, "running_copy", lambda: {
        "path": str(root / "turboadb"), "same": False, "installed_path": "site", "note": note})
    reason = cli._shortcut_blocker()
    assert reason.startswith(note) and f'pip install -e "{root}"' in reason

    monkeypatch.setattr(update, "running_copy", lambda: {"same": None})  # couldn't tell
    assert cli._shortcut_blocker() is None
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr(update, "running_copy", lambda: pytest.fail("asked"))
    assert cli._shortcut_blocker() is None  # the exe is its own launcher


@pytest.mark.skipif(os.name != "nt", reason="known folders are Windows-only")
def test_an_unresolved_known_folder_is_an_error_not_a_relative_path(monkeypatch):
    """SHGetFolderPathW's failure was ignored: '' made the shortcut path
    relative, so its existence was checked in the current folder."""
    import ctypes

    shell32 = ctypes.windll.shell32
    monkeypatch.setattr(shell32, "SHGetFolderPathW", lambda *a: -2147467259)  # E_FAIL
    with pytest.raises(OSError):
        cli._windows_folder(0x10)
    monkeypatch.setattr(shell32, "SHGetFolderPathW", lambda *a: 0)  # S_OK, but no path
    with pytest.raises(OSError):
        cli._windows_folder(0x10)
    paths = [path for _loc, path, _maker in cli._shortcut_paths()]
    assert all(os.path.isabs(path) for path in paths)


# --------------------------------------------------------------------------- #
# a Python without PyQt5: the GUI is the executable the wheel brings
# --------------------------------------------------------------------------- #
def test_without_pyqt5_a_shortcut_starts_turboadb_gui_not_turboadb_exe(monkeypatch, tmp_path):
    """TurboADB.exe runs the GUI of this Python, so without PyQt5 it opens
    nothing: `turboadb shortcut` wrote it all the same and said "Created"."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "turboadb-gui.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(launcher, "has_qt", lambda: False)
    monkeypatch.setattr(launcher, "gui_command", lambda build=True: pytest.fail("TurboADB.exe was asked for"))
    assert cli._resolve_gui_target() == (str(scripts / "turboadb-gui.exe"), "", str(scripts))
    os.remove(scripts / "turboadb-gui.exe")  # no launcher beside the interpreter (pip install --user)
    assert cli._resolve_gui_target() == (str(tmp_path / "python.exe"), "-m turboadb gui", str(tmp_path))


@pytest.fixture
def real_makers(monkeypatch, tmp_path):
    """`turboadb shortcut` with its own writers, into the test's folders; the
    paths written through COM (the files are made empty) or PowerShell."""
    import turboadb.winshell as winshell

    written = []

    def write_shortcut(path, *args, **kwargs):
        written.append(path)
        open(path, "w").close()
        return True

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(winshell, "write_shortcut", write_shortcut)
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kwargs: written.append(cmd))
    monkeypatch.setattr(cli, "_desktop_folder", lambda: str(tmp_path / "desk"))
    monkeypatch.setattr(cli, "_programs_folder", lambda: str(tmp_path / "start"))
    monkeypatch.setattr(cli, "_shortcut_icon", lambda: str(tmp_path / "icon.ico"))
    monkeypatch.setitem(cli._JSON, "on", False)
    _copy(monkeypatch, same=True)
    return written


def test_no_shortcut_is_written_where_nothing_starts_the_gui(real_makers, monkeypatch, tmp_path, capsys):
    """Neither PyQt5 nor the wheel's executable (an install from source): the
    shortcut would open nothing, and the command says what is missing."""
    monkeypatch.setattr(launcher, "has_qt", lambda: False)
    monkeypatch.setattr(cli, "_gui_exe_path", lambda: str(tmp_path / "missing.exe"))
    assert cli.create_shortcut([]) == 1
    assert real_makers == []
    err = capsys.readouterr().err
    assert "Could not create or refresh" in err and 'pip install "turboadb[gui]"' in err

    exe = tmp_path / "turboadb-gui.exe"  # the wheel's executable: turboadb-gui starts it
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(cli, "_gui_exe_path", lambda: str(exe))
    assert cli.create_shortcut([]) == 0
    assert sorted(os.path.basename(os.path.dirname(path)) for path in real_makers) == ["desk", "start"]
    assert "Created/refreshed" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# the PowerShell fallback, when the shortcut can't be written through COM
# --------------------------------------------------------------------------- #
@pytest.fixture
def no_com(monkeypatch):
    """The COM write fails, so the shortcut is written through PowerShell (in
    a Python that has the GUI to start)."""
    import turboadb.winshell as winshell

    monkeypatch.setattr(winshell, "write_shortcut", lambda *a, **k: False)
    monkeypatch.setattr(launcher, "has_qt", lambda: True)
    monkeypatch.setattr(os, "name", "nt")


def test_the_powershell_fallback_keeps_every_value_out_of_its_script(no_com, monkeypatch, tmp_path):
    """PowerShell ends a quoted string at a typographic quote too, so a user
    folder named O’Brien broke the script (no shortcut), and a path that
    went on with a command had it run."""
    home = tmp_path / "O\u2019Brien"
    home.mkdir()
    icon = home / "icon.ico"
    icon.write_bytes(b"ico")
    target = str(home / "Scripts" / "turboadb-gui.exe")
    monkeypatch.setattr(cli, "_resolve_gui_target", lambda build=True: (target, "-m turboadb gui", str(home)))
    monkeypatch.setattr(cli, "_shortcut_icon", lambda: str(icon))
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kwargs: ran.append((cmd, kwargs)))
    assert cli._write_shortcut("[Environment]::GetFolderPath('Desktop')", "Turbo\u2018ADB", str(tmp_path / "desk"))
    cmd, kwargs = ran.pop()
    script = cmd[-1]
    assert "Brien" not in script and "Turbo" not in script
    assert not set("\u2018\u2019\u201a\u201b") & set(script)
    env = kwargs["env"]
    assert env["TURBOADB_LNK_NAME"] == "Turbo\u2018ADB" and env["TURBOADB_LNK_TARGET"] == target
    assert env["TURBOADB_LNK_ARGS"] == "-m turboadb gui" and env["TURBOADB_LNK_WORKDIR"] == str(home)
    assert env["TURBOADB_LNK_ICON"] == str(icon)
    for key in ("NAME", "TARGET", "ARGS", "WORKDIR", "ICON", "DESCRIPTION"):
        assert f"$env:TURBOADB_LNK_{key}" in script

    # no arguments and no icon file: neither is set, whatever the environment holds
    monkeypatch.setattr(cli, "_resolve_gui_target", lambda build=True: (target, "", str(home)))
    monkeypatch.setattr(cli, "_shortcut_icon", lambda: str(home / "missing.ico"))
    monkeypatch.setenv("TURBOADB_LNK_ARGS", "left over")
    assert cli._write_shortcut("[Environment]::GetFolderPath('Desktop')", "TurboADB")
    cmd, kwargs = ran.pop()
    assert "$env:TURBOADB_LNK_TARGET" in cmd[-1]
    assert "TURBOADB_LNK_ARGS" not in cmd[-1] and "TURBOADB_LNK_ICON" not in cmd[-1]


def test_the_powershell_fallback_starts_windows_own_powershell(no_com, monkeypatch, tmp_path):
    """Named bare, a powershell.exe lying in the folder `turboadb shortcut`
    was run in was started in its place."""
    monkeypatch.setattr(cli, "windows_program", lambda name: "W:\\System32\\" + name)
    monkeypatch.setattr(cli, "_resolve_gui_target", lambda build=True: ("C:\\t\\turboadb-gui.exe", "", "C:\\t"))
    monkeypatch.setattr(cli, "_shortcut_icon", lambda: str(tmp_path / "missing.ico"))
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kwargs: ran.append((cmd, kwargs)))
    assert cli._write_shortcut("[Environment]::GetFolderPath('Desktop')", "TurboADB")
    (cmd, kwargs), = ran
    assert cmd[0] == "powershell"
    assert kwargs["executable"] == "W:\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"


windows_only = pytest.mark.skipif(os.name != "nt", reason="PowerShell and .lnk files are Windows-only")


@pytest.fixture
def written(no_com, monkeypatch, tmp_path):
    """Writes a shortcut through the real PowerShell into the test's folder
    and returns what it holds (None when none was written)."""
    import turboadb.winshell as winshell

    read_shortcut = winshell.read_shortcut
    links = tmp_path / "links"
    monkeypatch.setenv("TURBOADB_TEST_LINKS", str(links))

    def write(target, args, workdir, icon):
        monkeypatch.setattr(cli, "_resolve_gui_target", lambda build=True: (target, args, workdir))
        monkeypatch.setattr(cli, "_shortcut_icon", lambda: icon)
        ok = cli._write_shortcut("$env:TURBOADB_TEST_LINKS", "TurboADB", str(links))
        return read_shortcut(str(links / "TurboADB.lnk")) if ok else None

    write.links = links
    return write


@windows_only
def test_the_powershell_fallback_writes_a_shortcut_under_a_name_with_a_typographic_quote(written, tmp_path):
    scripts = tmp_path / "O\u2019Brien" / "Scripts"
    scripts.mkdir(parents=True)
    exe, icon = scripts / "turboadb-gui.exe", scripts / "icon.ico"
    exe.write_bytes(b"MZ")
    icon.write_bytes(b"ico")
    info = written(str(exe), "-m turboadb gui", str(scripts), str(icon))
    assert info is not None, "no shortcut was written"
    assert info["target"] == str(exe) and info["args"] == "-m turboadb gui"
    assert info["workdir"] == str(scripts) and info["icon"] == str(icon)


@windows_only
def test_the_powershell_fallback_never_runs_a_path_as_a_command(written, tmp_path):
    exe = tmp_path / "turboadb-gui.exe"
    exe.write_bytes(b"MZ")
    # a folder name that goes on as PowerShell once the quote ended its string
    workdir = ("C:\\x\u2019; Set-Content -LiteralPath (Join-Path $env:TURBOADB_TEST_LINKS "
               "\u2018injected.txt\u2019) -Value 1; $y=\u2018z")
    info = written(str(exe), "", workdir, str(tmp_path / "missing.ico"))
    assert not (written.links / "injected.txt").exists()
    assert info is not None and info["target"] == str(exe) and info["workdir"] == workdir


# --------------------------------------------------------------------------- #
# the bundled exe's copies
# --------------------------------------------------------------------------- #
@pytest.fixture
def staging(tmp_path, monkeypatch):
    """A packaged exe, the folder its copies go to (in a home of the test's
    own) and a temp folder, where they went up to 3.1.0."""
    import tempfile
    import types

    home, temp = tmp_path / "home", tmp_path / "temp"
    folder = home / ".turboadb" / "gui"
    folder.mkdir(parents=True)
    temp.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    src = tmp_path / "turboadb-gui.exe"
    src.write_bytes(b"MZ" * 10)
    return types.SimpleNamespace(src=src, temp=temp, folder=folder)


def test_older_staged_copies_are_removed(staging):
    stale = [staging.folder / "turboadb-gui-0.0.1-1.exe",
             staging.folder / "turboadb-gui-0.0.1-1.exe.4242.new",  # a copy that was never finished
             staging.temp / "turboadb-gui-0.0.1-1.exe"]
    unrelated = [staging.temp / "turboadb-gui.exe", staging.temp / "turboadb-gui-notes.txt",
                 staging.temp / "other.exe", staging.folder / "other.exe"]
    for path in stale:
        path.write_bytes(b"old build")
    for path in unrelated:
        path.write_bytes(b"keep")
    staged = cli._staged_exe(str(staging.src))
    assert os.path.isfile(staged) and not any(path.exists() for path in stale)
    assert all(path.exists() for path in unrelated)


def test_a_copy_that_cannot_be_removed_does_not_stop_the_launch(staging, monkeypatch):
    running = staging.folder / "turboadb-gui-0.0.1-1.exe"
    running.write_bytes(b"still running")

    def locked(path):
        raise PermissionError(32, "in use", path)

    monkeypatch.setattr(os, "remove", locked)
    staged = cli._staged_exe(str(staging.src))
    assert os.path.dirname(staged) == str(staging.folder) and running.exists()
