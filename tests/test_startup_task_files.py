"""What the SYSTEM startup task runs is the administrators' alone to change.

``turboadb serve --startup-task`` pinned the adb it found into the task, which
is the one in ``~/.turboadb/tools``: any program of that user could replace
the file and run as SYSTEM from the next start on.  The task now gets a copy
under Program Files, and says so when the Python it runs is open to accounts
without administrator rights.

No scheduled task is created and nothing is written under the real Program
Files: schtasks is a fake and ``ProgramFiles`` a folder of the test.
"""
import logging
import os
import shutil
import subprocess
import time
import types

import pytest

from turboadb import devices, tools


@pytest.fixture
def pc(monkeypatch, tmp_path):
    """A PC whose adb is in the user's profile and whose Program Files is a
    folder of the test; schtasks records what it is asked.  ``open_to_others``
    holds the folders accounts without administrator rights can write to."""
    home = tmp_path / "Users" / "me"
    tools_folder = home / ".turboadb" / "tools" / "platform-tools"
    tools_folder.mkdir(parents=True)
    for name in tools.ADB_WINDOWS_FILES:
        (tools_folder / name).write_text(f"{name} 36.0.0")
    program_files = tmp_path / "Program Files"
    program_files.mkdir()
    world = types.SimpleNamespace(
        adb=str(tools_folder / "adb.exe"), program_files=str(program_files), commands=[],
        python=str(program_files / "Python312" / "pythonw.exe"), open_to_others=[str(home)])

    def others_can_change(path):
        return any(os.path.abspath(path).startswith(folder) for folder in world.open_to_others)

    def run(cmd, **kw):
        world.commands.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.setattr(devices.os, "name", "nt")
    monkeypatch.setattr(devices, "find_adb", lambda p=None: p or world.adb)
    monkeypatch.setattr(devices, "windowless_python", lambda: world.python)
    monkeypatch.setattr(devices, "_others_can_change", others_can_change)
    monkeypatch.setattr(devices, "_share_keys_with_system", lambda: None)
    monkeypatch.setattr(devices, "_listens_on_all_interfaces", lambda port, timeout=0.5: True)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: True)
    monkeypatch.setattr(tools, "kill_adb_server", lambda adb, port=5037, **kw: None)
    monkeypatch.setattr(devices.subprocess, "run", run)
    return world


def _task_command(world):
    create = next(cmd for cmd in world.commands if "/create" in cmd)
    return create[create.index("/tr") + 1]


def test_the_task_runs_a_copy_of_adb_only_administrators_can_change(pc):
    assert devices.install_serve_task(port=5037, ready_timeout=1.0) == devices._SERVE_TASK
    folder = os.path.join(pc.program_files, "TurboADB", "adb")
    assert f'--adb-path "{os.path.join(folder, "adb.exe")}"' in _task_command(pc)
    assert pc.adb not in _task_command(pc)
    for name in tools.ADB_WINDOWS_FILES:  # the DLLs adb loads go with it
        with open(os.path.join(folder, name)) as fh:
            assert fh.read() == f"{name} 36.0.0"


def test_installing_again_brings_the_copy_up_to_date(pc):
    devices.install_serve_task(port=5037, ready_timeout=1.0)
    with open(pc.adb, "w") as fh:
        fh.write("adb.exe 37.0.1")  # a tools update since
    devices.install_serve_task(port=5037, ready_timeout=1.0)
    folder = os.path.join(pc.program_files, "TurboADB", "adb")
    with open(os.path.join(folder, "adb.exe")) as fh:
        assert fh.read() == "adb.exe 37.0.1"
    assert sorted(os.listdir(folder)) == sorted(tools.ADB_WINDOWS_FILES)  # nothing left aside


def test_an_adb_only_administrators_can_change_is_pinned_where_it_is(pc):
    pc.open_to_others = []
    devices.install_serve_task(port=5037, ready_timeout=1.0)
    assert f'--adb-path "{pc.adb}"' in _task_command(pc)
    assert not os.path.exists(os.path.join(pc.program_files, "TurboADB"))


def test_no_task_is_made_when_adb_cannot_be_put_out_of_reach(pc):
    pc.open_to_others.append(pc.program_files)  # a Program Files anyone may write to
    with pytest.raises(RuntimeError, match="administrator"):
        devices.install_serve_task(port=5037, ready_timeout=1.0)
    assert not any("/create" in cmd for cmd in pc.commands)
    assert not os.path.exists(os.path.join(pc.program_files, "TurboADB", "adb", "adb.exe"))


def test_a_python_others_can_change_is_named(pc, caplog):
    pc.python = os.path.join(os.path.dirname(pc.adb), "pythonw.exe")  # in the user's profile
    with caplog.at_level(logging.WARNING, logger="turboadb.devices"):
        assert devices.install_serve_task(port=5037, ready_timeout=1.0) == devices._SERVE_TASK
    warning = next(r.getMessage() for r in caplog.records if "SYSTEM" in r.getMessage())
    assert pc.python in warning and "for all users" in warning


def test_a_python_only_administrators_can_change_is_not_warned_about(pc, caplog):
    with caplog.at_level(logging.WARNING, logger="turboadb.devices"):
        devices.install_serve_task(port=5037, ready_timeout=1.0)
    assert not [r for r in caplog.records if "SYSTEM" in r.getMessage()]


# --------------------------------------------------------------------------- #
# what Windows itself says
# --------------------------------------------------------------------------- #
windows_only = pytest.mark.skipif(os.name != "nt", reason="reads Windows access lists")


@windows_only
def test_who_may_change_a_file_is_read_from_its_access_lists(tmp_path):
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    assert devices._others_can_change(os.path.join(system32, "cmd.exe")) is False
    mine = tmp_path / "adb.exe"  # a temp folder: this user's own
    mine.write_text("")
    assert devices._others_can_change(str(mine)) is True
    assert devices._others_can_change(str(tmp_path / "no-such-file.exe")) is None


@pytest.mark.skipif(os.name == "nt", reason="nothing to read elsewhere")
def test_elsewhere_it_cannot_tell(tmp_path):
    assert devices._others_can_change(str(tmp_path)) is None


@windows_only
def test_a_copy_that_is_running_is_moved_aside_and_replaced(tmp_path):
    """The adb of the task's server cannot be written over while it runs."""
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    running = tmp_path / "adb.exe"
    shutil.copy2(os.path.join(system32, "ping.exe"), running)
    newer = tmp_path / "newer.exe"
    newer.write_text("adb 37.0.1")
    proc = subprocess.Popen([str(running), "-n", "30", "127.0.0.1"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=0x08000000)
    try:
        time.sleep(0.5)
        devices._replace_file(str(newer), str(running))
        assert running.read_text() == "adb 37.0.1" and proc.poll() is None
    finally:
        proc.kill()
        proc.wait(10)
    aside = str(running) + ".old"
    deadline = time.monotonic() + 10
    while os.path.exists(aside) and time.monotonic() < deadline:
        devices._replace_file(str(newer), str(running))  # the next copy takes it away
        time.sleep(0.1)
    assert not os.path.exists(aside)
