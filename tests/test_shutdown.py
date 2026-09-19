"""Closing TurboADB closes what it started: scrcpy windows and the adb server."""
import pytest

pytest.importorskip("PyQt5")

import turboadb.devices as devices_mod  # noqa: E402
import turboadb.scrcpy as scrcpy  # noqa: E402
import turboadb.tools as tools_mod  # noqa: E402
from turboadb.core import ADBHandler  # noqa: E402


class _Proc:
    """A scrcpy process that ends when it is asked to."""

    def __init__(self, pid=4321):
        self.pid = pid
        self.alive = True
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.alive = False

    def wait(self, timeout=None):
        self.alive = False
        return 0


@pytest.fixture
def quiet_scrcpy(monkeypatch):
    """No Win32 calls for made-up process ids, and an empty registry."""
    monkeypatch.setattr(scrcpy, "_post_close_to_pid", lambda pid: None, raising=False)
    monkeypatch.setattr(scrcpy, "_send_ctrl_break", lambda pid: None, raising=False)
    monkeypatch.setattr(scrcpy, "_LIVE_SESSIONS", [])
    return scrcpy


def test_every_scrcpy_started_here_is_stopped_on_the_way_out(quiet_scrcpy):
    first = scrcpy.ScrcpySession(_Proc(1), "S1")
    second = scrcpy.ScrcpySession(_Proc(2), "S2")
    assert scrcpy.live_sessions() == [first, second]

    second.stop()  # closed by the user: gone from the registry, not stopped twice
    assert scrcpy.live_sessions() == [first]
    assert scrcpy.stop_all() == 1
    assert not first.running and scrcpy.live_sessions() == []
    assert scrcpy.stop_all() == 0  # nothing left to close


def test_a_scrcpy_that_exited_by_itself_is_not_counted(quiet_scrcpy):
    session = scrcpy.ScrcpySession(_Proc(3), "S1")
    session._proc.alive = False  # the user closed the mirror window
    assert scrcpy.live_sessions() == [] and scrcpy.stop_all() == 0


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """A settings file of this test's own: never the developer's."""
    from turboadb.gui import settings

    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "_FILE", str(path))
    monkeypatch.setattr(settings, "_cache", None)
    return path


@pytest.fixture
def exit_world(monkeypatch, tmp_path, settings_file):
    """The world ``_stop_started_tools`` looks at, with everything recorded."""
    from turboadb.gui import adb_path as adb_path_mod
    from turboadb.gui import app as app_mod

    state = {"scrcpy": 0, "stopped": 0, "alive": True, "shared": False}
    adb = tmp_path / "adb.exe"
    adb.write_text("")
    monkeypatch.setattr(scrcpy, "stop_all", lambda *a, **k: state.__setitem__("scrcpy", 1) or 1)
    monkeypatch.setattr(adb_path_mod, "gui_adb_path", lambda: str(adb))
    monkeypatch.setattr(tools_mod, "is_adb_server_alive", lambda *a, **k: state["alive"])
    monkeypatch.setattr(devices_mod, "server_is_shared", lambda *a, **k: state["shared"])

    def stop_server(self, *, safe=None):
        state["stopped"] += 1
        from turboadb.results import OperationResult

        return OperationResult(True, "stop_server", value=True)

    monkeypatch.setattr(ADBHandler, "stop_server", stop_server)
    state["run"] = app_mod._stop_started_tools
    return state


def test_closing_the_app_closes_scrcpy_and_the_adb_server(exit_world):
    exit_world["run"]()
    assert exit_world["scrcpy"] == 1 and exit_world["stopped"] == 1


def test_the_setting_turns_the_adb_stop_off_but_scrcpy_still_closes(exit_world):
    from turboadb.gui import settings as settings_mod

    settings_mod.set("stop_adb_on_exit", False)
    exit_world["run"]()
    assert exit_world["scrcpy"] == 1 and exit_world["stopped"] == 0
    assert settings_mod.DEFAULTS["stop_adb_on_exit"] is True  # on unless turned off


def test_a_shared_server_is_left_running_for_the_other_machines(exit_world):
    exit_world["shared"] = True
    exit_world["run"]()
    assert exit_world["scrcpy"] == 1 and exit_world["stopped"] == 0


def test_nothing_is_stopped_when_no_adb_server_is_running(exit_world):
    exit_world["alive"] = False
    exit_world["run"]()
    assert exit_world["stopped"] == 0


def test_a_failure_on_the_way_out_never_stops_the_app_from_closing(exit_world, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("no adb here")

    monkeypatch.setattr(scrcpy, "stop_all", boom)
    monkeypatch.setattr(ADBHandler, "stop_server", boom)
    exit_world["run"]()  # must not raise
