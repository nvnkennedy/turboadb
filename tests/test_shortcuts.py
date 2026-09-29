"""The Desktop and Start-menu shortcuts start the TurboADB installed in this
Python. They are never written by a process running another copy (a source
checkout started with ``python main.py`` while an older release is installed),
a refresh that failed is reported as failed, and the bundled-exe fallback does
not leave a copy of every old build in the temp folder."""
import os

import pytest

import turboadb.cli as cli
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
# the bundled exe's temp copies
# --------------------------------------------------------------------------- #
def test_older_staged_copies_are_removed(tmp_path, monkeypatch):
    import tempfile

    src = tmp_path / "turboadb-gui.exe"
    src.write_bytes(b"MZ" * 10)
    temp = tmp_path / "temp"
    temp.mkdir()
    stale = temp / "turboadb-gui-0.0.1-1.exe"
    stale.write_bytes(b"old build")
    unrelated = [temp / "turboadb-gui.exe", temp / "turboadb-gui-notes.txt", temp / "other.exe"]
    for path in unrelated:
        path.write_bytes(b"keep")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    staged = cli._staged_exe(str(src))
    assert os.path.isfile(staged) and not stale.exists()
    assert all(path.exists() for path in unrelated)


def test_a_copy_that_cannot_be_removed_does_not_stop_the_launch(tmp_path, monkeypatch):
    import tempfile

    src = tmp_path / "turboadb-gui.exe"
    src.write_bytes(b"MZ" * 10)
    temp = tmp_path / "temp"
    temp.mkdir()
    running = temp / "turboadb-gui-0.0.1-1.exe"
    running.write_bytes(b"still running")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))

    def locked(path):
        raise PermissionError(32, "in use", path)

    monkeypatch.setattr(os, "remove", locked)
    staged = cli._staged_exe(str(src))
    assert os.path.dirname(staged) == str(temp) and running.exists()
