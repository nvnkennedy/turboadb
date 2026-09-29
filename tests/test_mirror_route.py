"""Whether a mirror tunnels its video from another machine is decided once.

``ADBHandler.mirror`` asked whether the adb server was this PC and logged the
answer, then ``launch_scrcpy`` asked again: two lookups, and two answers that
could differ (the host lookups are only remembered for 30 s).  launch_scrcpy
decides now, the session says what it decided, and the log follows it.
"""
import pytest

from turboadb import scrcpy
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler


@pytest.fixture
def started(monkeypatch):
    """scrcpy "starts" instantly: the command line and environment are kept."""
    launched = []

    class Process:
        pid = 4321

        @staticmethod
        def poll():
            return 0

    monkeypatch.delenv("ANDROID_ADB_SERVER_ADDRESS", raising=False)
    monkeypatch.setattr(scrcpy, "find_scrcpy", lambda _path=None: "scrcpy")
    monkeypatch.setattr(scrcpy.subprocess, "Popen",
                        lambda cmd, **kw: launched.append((cmd, kw["env"] or {})) or Process())
    return launched


def _mirror(monkeypatch, host, answers):
    """Mirror through the adb server *host*; the n-th "is this PC?" lookup
    answers ``answers[n]``."""
    asked = []

    def is_local_host(name):
        asked.append(name)
        return answers[len(asked) - 1]

    monkeypatch.setattr(scrcpy, "is_local_host", is_local_host)
    monkeypatch.setattr(scrcpy, "resolve_host", lambda name: "10.0.0.4" if name == "lab-pc" else name)
    messages = []
    handler = ADBHandler(ADBConfig(serial="R58M1", adb_server_host=host),
                         log_callback=messages.append)
    handler._adb = "adb"
    return handler.mirror(safe=False), asked, messages


def test_another_machines_server_is_looked_up_once_and_the_tunnel_follows_it(started, monkeypatch):
    session, asked, messages = _mirror(monkeypatch, "lab-pc", [False, True])
    assert asked == ["lab-pc"]
    cmd, env = started[0]
    assert "--tunnel-host=10.0.0.4" in cmd
    assert env["ANDROID_ADB_SERVER_ADDRESS"] == "10.0.0.4"
    assert session.tunnel_host == "10.0.0.4"
    assert any("from 10.0.0.4 via tunnel ports" in m for m in messages)
    assert not any("THIS machine" in m for m in messages)


def test_a_server_that_is_this_pc_is_mirrored_locally_and_said_so(started, monkeypatch):
    session, asked, messages = _mirror(monkeypatch, "192.168.1.5", [True, False])
    assert asked == ["192.168.1.5"]
    cmd, env = started[0]
    assert not any(arg.startswith("--tunnel-host") for arg in cmd)
    assert "ANDROID_ADB_SERVER_ADDRESS" not in env
    assert session.tunnel_host is None
    assert any("192.168.1.5 is THIS machine" in m for m in messages)
    assert not any("tunnel ports" in m for m in messages)
