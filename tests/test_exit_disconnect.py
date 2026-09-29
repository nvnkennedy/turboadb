"""A closing tab's ``adb disconnect`` must never bring back a stopped adb server.

Closing a device tab drops a network connection this app made from a daemon
thread (``_drop_connection_later``).  When that thread ran its ``adb
disconnect`` just after TurboADB's exit had stopped its own server (or in the
kill -> start gap of a restart), adb found no server and started a new one
("* daemon not running; starting now"), and that adb.exe outlived the app —
reproduced with a real adb on a private port.  The disconnect now runs only
while the local server answers, under the lock the one stopper
(``tools.kill_adb_server``) holds, so the two can never interleave.
"""
import threading
import time
import types

import pytest

pytest.importorskip("PyQt5")

from turboadb.config import ADBConfig  # noqa: E402


class _Handler:
    """A handler that made a connection to 10.0.0.5:5555 and records its drop."""

    def __init__(self, *, block=None, server=None):
        self.config = ADBConfig(serial="10.0.0.5:5555", adb_server_host=server)
        self.serial = "10.0.0.5:5555"
        self.owns_connection = True
        self.block = block
        self.events = []

    def disconnect(self, safe=None):
        self.events.append("disconnect")
        if self.block is not None:
            self.block.wait(5)
        self.events.append("disconnected")
        return True


@pytest.fixture
def local_server(monkeypatch):
    """The local adb server's state, as the socket probe reports it."""
    import turboadb.devices as devices
    import turboadb.tools as tools

    state = {"alive": True}
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda *_a, **_k: state["alive"])
    monkeypatch.setattr(devices, "server_is_shared", lambda *_a, **_k: False)
    return state


def test_a_disconnect_after_the_server_stopped_runs_no_adb(local_server):
    from turboadb.gui import device_tab

    local_server["alive"] = False  # the exit (or a restart) has stopped it
    handler = _Handler()
    device_tab._disconnect_unless_shared(handler)
    assert handler.events == []  # `adb disconnect` would have started a new server


def test_a_disconnect_while_the_server_runs_still_drops_the_connection(local_server):
    from turboadb.gui import device_tab

    handler = _Handler()
    device_tab._disconnect_unless_shared(handler)
    assert handler.events == ["disconnect", "disconnected"]


def test_a_remote_servers_connection_is_left_alone(local_server):
    from turboadb.gui import device_tab

    handler = _Handler(server="10.0.0.9")
    device_tab._disconnect_unless_shared(handler)
    assert handler.events == []


def test_the_exits_kill_waits_for_a_disconnect_under_way(local_server, monkeypatch):
    """The stopper takes the same lock: its kill-server comes after the
    disconnect, never in the middle of it (and a disconnect that comes after
    the kill finds no server and runs nothing, see above)."""
    import turboadb.tools as tools
    from turboadb.gui import device_tab

    order = []
    release = threading.Event()
    handler = _Handler(block=release)

    def run(cmd, **_kw):
        order.append(cmd[-1])
        local_server["alive"] = False
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(tools.subprocess, "run", run)
    dropper = threading.Thread(target=device_tab._disconnect_unless_shared, args=(handler,))
    dropper.start()
    deadline = time.monotonic() + 5
    while "disconnect" not in handler.events and time.monotonic() < deadline:
        time.sleep(0.01)
    assert handler.events == ["disconnect"]
    killer = threading.Thread(target=tools.kill_adb_server, args=("adb",), kwargs={"wait": 0.0})
    killer.start()
    time.sleep(0.3)
    assert order == []  # kill-server waits for the disconnect under way
    release.set()
    dropper.join(5)
    killer.join(5)
    assert handler.events == ["disconnect", "disconnected"]
    assert order == ["kill-server"]
