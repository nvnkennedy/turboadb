"""The local adb server: one starter, one stopper, and knowing whose it is.

A cold GUI start used to launch `adb start-server` up to three times (the
pre-warm, the main window's check and the first device tab), the exit stopped
servers TurboADB had only joined, and ANDROID_ADB_SERVER_PORT split TurboADB
across two servers.  No test here runs a real adb.
"""
import os
import socket
import sys
import threading
import time
import types

import pytest

from turboadb import devices, tools, toolsdl


class _Launcher:
    """A fake ``adb start-server`` launcher: runs until told to exit."""

    def __init__(self, made, cmd, kw, *, code=None, prints=b""):
        self.cmd = list(cmd)
        self.code = code
        self.killed = False
        out = kw.get("stdout")
        if prints and hasattr(out, "write"):
            out.write(prints)
        made.append(self)

    def poll(self):
        return self.code

    def kill(self):
        self.killed = True
        self.code = -9


@pytest.fixture
def launches(monkeypatch, tmp_path):
    """Fake launchers, a socket probe the test switches on, and an adb file."""
    adb = tmp_path / ("adb.exe" if os.name == "nt" else "adb")
    adb.write_text("")
    world = types.SimpleNamespace(made=[], alive=False, adb=str(adb), code=None, prints=b"")

    def popen(cmd, **kw):
        return _Launcher(world.made, cmd, kw, code=world.code, prints=world.prints)

    monkeypatch.setattr(tools.subprocess, "Popen", popen)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: world.alive)
    monkeypatch.setattr(tools, "find_adb", lambda path=None: world.adb)
    monkeypatch.setattr(tools, "adb_server_status", lambda *a, **k: None)
    return world


# --------------------------------------------------------------------------- #
# one starter
# --------------------------------------------------------------------------- #
def test_overlapping_starts_wait_on_one_launcher(launches):
    """The pre-warm, the startup check and a tab connect asked at once while the
    daemon was still scanning USB: each launched its own `adb start-server`."""
    results = []
    callers = [
        threading.Thread(target=lambda: results.append(tools.ensure_adb_server(launches.adb, timeout=0.3)))
        for _ in range(3)
    ]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(10)
    assert results == [False, False, False]
    assert len(launches.made) == 1
    # given up on, but not ended: the next caller waits for it instead
    assert launches.made[0].code is None and not launches.made[0].killed
    assert "did not become ready" in tools.last_adb_server_error()

    launches.alive = True
    assert tools.ensure_adb_server(launches.adb, timeout=1.0) is True
    assert len(launches.made) == 1
    assert tools.owned_adb_server() == launches.adb
    assert not tools._INFLIGHT  # nothing left to wait for


def test_a_late_caller_joins_before_the_daemon_answers(launches):
    assert tools.ensure_adb_server(launches.adb, timeout=0.05) is False
    threading.Timer(0.3, lambda: setattr(launches, "alive", True)).start()
    assert tools.ensure_adb_server(launches.adb, timeout=5.0) is True
    assert len(launches.made) == 1


def test_a_failed_launcher_is_reaped_and_reported(launches, monkeypatch):
    monkeypatch.setattr(tools, "_LAUNCH_EXIT_GRACE", 0.05)
    launches.code = 1
    launches.prints = b"* cannot bind 'tcp:5037': Only one usage of each socket address"
    assert tools.ensure_adb_server(launches.adb, timeout=5.0) is False
    assert "cannot bind" in tools.last_adb_server_error()
    assert not tools._INFLIGHT
    # a failure is not remembered: the next start tries again
    assert tools.ensure_adb_server(launches.adb, timeout=5.0) is False
    assert len(launches.made) == 2


def test_a_stuck_launcher_is_ended_and_replaced(launches, monkeypatch):
    monkeypatch.setattr(tools, "_LAUNCH_MAX_AGE", 0.05)
    assert tools.ensure_adb_server(launches.adb, timeout=0.02) is False
    time.sleep(0.1)
    assert tools.ensure_adb_server(launches.adb, timeout=0.02) is False
    assert len(launches.made) == 2 and launches.made[0].killed


def test_a_server_that_came_up_after_every_caller_gave_up_is_still_ours(launches):
    # what adb's launcher prints when the daemon it started is up
    launches.prints = b"* daemon not running; starting now at tcp:5037\n* daemon started successfully\n"
    assert tools.ensure_adb_server(launches.adb, timeout=0.02) is False
    launches.made[0].code = 0
    launches.alive = True
    assert tools.owned_adb_server() == launches.adb


def test_the_exit_waits_for_a_start_under_way_and_ends_stray_launchers(launches):
    assert tools.ensure_adb_server(launches.adb, timeout=0.02) is False
    threading.Timer(0.2, lambda: setattr(launches, "alive", True)).start()
    tools.settle_adb_server_start(timeout=3.0)
    assert tools.owned_adb_server() == launches.adb

    launches.alive = False
    assert tools.ensure_adb_server(launches.adb, timeout=0.02) is False
    tools.end_server_launchers()
    assert launches.made[-1].killed and not tools._INFLIGHT


def test_a_server_joined_at_launch_is_not_ours(launches):
    launches.alive = True
    assert tools.ensure_adb_server(launches.adb, timeout=1.0) is True
    assert launches.made == [] and tools.owned_adb_server() is None


def test_a_server_another_adb_replaced_is_not_ours(launches, monkeypatch, tmp_path):
    assert tools.ensure_adb_server(launches.adb, timeout=0.02) is False
    launches.alive = True
    other = tmp_path / "sdk-adb.exe"
    other.write_text("")
    monkeypatch.setattr(tools, "adb_server_status", lambda *a, **k: {"executable": str(other)})
    assert tools.owned_adb_server() is None
    monkeypatch.setattr(tools, "adb_server_status", lambda *a, **k: {"executable": launches.adb})
    assert tools.owned_adb_server() == launches.adb


# --------------------------------------------------------------------------- #
# one stopper
# --------------------------------------------------------------------------- #
def test_kill_waits_for_the_port_and_forgets_the_server(launches, monkeypatch):
    launches.alive = True
    tools._STARTED_HERE[5037] = launches.adb
    calls = []
    monkeypatch.setattr(
        tools.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    answers = iter([True, True, False])
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: next(answers, False))
    done = tools.kill_adb_server(launches.adb)
    assert calls == [[launches.adb, "kill-server"]] and done.returncode == 0
    assert next(answers, "drained") == "drained"  # it waited for the port to close
    assert tools.owned_adb_server() is None


def test_kill_ends_a_launcher_still_starting_the_server(launches, monkeypatch):
    assert tools.ensure_adb_server(launches.adb, timeout=0.02) is False
    monkeypatch.setattr(tools.subprocess, "run", lambda cmd, **kw: None)
    tools.kill_adb_server(launches.adb, wait=0)
    assert launches.made[0].killed and not tools._INFLIGHT
    # the next start launches afresh instead of waiting on the ended one
    assert tools.ensure_adb_server(launches.adb, timeout=0.02) is False
    assert len(launches.made) == 2


def test_stop_sharing_and_restart_go_through_the_one_stopper_and_starter(monkeypatch, tmp_path):
    from turboadb.config import ADBConfig
    from turboadb.core import ADBHandler

    adb = tmp_path / ("adb.exe" if os.name == "nt" else "adb")
    adb.write_text("")
    adb = str(adb)
    seen = []
    monkeypatch.setattr(devices, "find_adb", lambda p=None: adb)
    monkeypatch.setattr(tools, "kill_adb_server", lambda exe, port=5037, **kw: seen.append(("kill", exe, port)))
    monkeypatch.setattr(tools, "ensure_adb_server",
                        lambda exe, timeout, port: seen.append(("start", exe, port)) or True)
    # Stop sharing also closes its firewall ports: never the real firewall here
    monkeypatch.setattr(devices, "_close_firewall", lambda ports: ([], []))
    devices.stop_shared_server()
    ADBHandler(ADBConfig(adb_path=adb)).restart_server()
    assert seen == [("kill", adb, 5037), ("start", adb, 5037)] * 2


def test_a_shared_start_holds_the_server_lock(monkeypatch):
    """A tab reconnecting between the stop and the shared start used to start
    a localhost-only server that took the port first."""
    monkeypatch.setattr(devices, "find_adb", lambda p=None: "adb")
    monkeypatch.setattr(devices, "_start_shared_server",
                        lambda port, adb, restart: tools.adb_server_lock()._is_owned())
    assert devices.start_shared_server() is True


def test_a_remote_restart_is_refused_before_it_reaches_the_remote_server(fake_adb):
    """adb cannot start a server on another machine: the kill-server went
    through and the start-server failed, leaving that machine without one."""
    from turboadb.config import ADBConfig
    from turboadb.core import ADBHandler
    from turboadb.exceptions import ADBError

    with pytest.raises(ADBError, match="10.0.0.2:5037"):
        ADBHandler(ADBConfig(adb_server_host="10.0.0.2")).restart_server()
    assert fake_adb.calls == []


# --------------------------------------------------------------------------- #
# ANDROID_ADB_SERVER_PORT
# --------------------------------------------------------------------------- #
class _FakeAdbServer:
    """Answers one-shot host services on 127.0.0.1 like an adb server."""

    def __init__(self, replies):
        self.replies = replies
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.asked = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    size = int(conn.recv(4), 16)
                    service = conn.recv(size).decode()
                    self.asked.append(service)
                    payload = self.replies.get(service)
                    if payload is None:
                        conn.sendall(b"FAIL0007unknown")
                    else:
                        conn.sendall(b"OKAY" + b"%04x" % len(payload) + payload)
                except (OSError, ValueError):
                    pass

    def close(self):
        self.sock.close()


def test_the_default_port_follows_android_adb_server_port(monkeypatch):
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5099")
    assert tools.local_adb_port() == tools.local_adb_port(5037) == 5099
    assert tools.local_adb_port(6000) == 6000
    assert tools.adb_server_env_note()[0] == "INFO"
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "fifty")
    assert tools.local_adb_port() == 5037
    assert tools.adb_server_env_note()[0] == "WARNING"
    monkeypatch.delenv("ANDROID_ADB_SERVER_PORT")
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:10.0.0.9:5037")
    assert "ADB_SERVER_SOCKET" in tools.adb_server_env_note()[1]


def test_probes_the_device_list_and_the_start_use_the_moved_port(monkeypatch, tmp_path):
    """ANDROID_ADB_SERVER_PORT moved every adb command but not the probes, so
    each start reported failure and left an extra server on the other port."""
    adb = tmp_path / ("adb.exe" if os.name == "nt" else "adb")
    adb.write_text("")
    server = _FakeAdbServer({
        "host:version": b"0029",
        "host:devices-l": b"R58M123 device product:x model:Pixel_7 transport_id:1\n",
    })
    try:
        monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", str(server.port))
        assert tools.is_adb_server_alive()
        assert tools.adb_server_version() == 41
        assert [d.serial for d in devices._list_devices_socket(timeout=1.0)] == ["R58M123"]
        assert [d.serial for d in devices.list_devices(timeout=1.0)] == ["R58M123"]
    finally:
        server.close()
    made = []
    monkeypatch.setattr(tools.subprocess, "Popen", lambda cmd, **kw: _Launcher(made, cmd, kw))
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: False)
    monkeypatch.setattr(tools, "find_adb", lambda path=None: str(adb))
    assert tools.ensure_adb_server(str(adb), timeout=0.02) is False
    assert made[0].cmd == [str(adb), "-P", str(server.port), "start-server"]


# --------------------------------------------------------------------------- #
# which server is it?
# --------------------------------------------------------------------------- #
def _pb_string(field, text):
    data = text.encode()
    return bytes([(field << 3) | 2, len(data)]) + data


_EXE = r"C:\Users\me\.turboadb\tools\platform-tools\adb.exe"


def _status(version="37.0.1"):
    """A ``host:server-status`` reply laid out as adb 37.0.1 sends it (varint
    backends first, then version, build, executable, log path, OS, flags)."""
    return (b"\x08\x04\x18\x03" + _pb_string(5, version) + _pb_string(6, "15733141")
            + _pb_string(7, _EXE) + _pb_string(8, r"C:\T\adb.log")
            + _pb_string(9, "Windows 10.0.26200") + b"R\x00X\x00`\x01")


_STATUS = _status()


def _serve_as_adb(monkeypatch, replies):
    server = _FakeAdbServer(replies)
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", str(server.port))
    return server


def test_the_version_is_the_payload_not_its_length(monkeypatch):
    """The reply is OKAY, a 4-hex length, then the version: reading the first
    four bytes after OKAY gave 4 for every server."""
    server = _serve_as_adb(monkeypatch, {"host:version": b"0028"})
    try:
        assert tools.adb_server_version() == 40
    finally:
        server.close()


def test_server_status_names_the_executable(monkeypatch):
    server = _serve_as_adb(monkeypatch, {"host:server-status": _STATUS})
    try:
        status = tools.adb_server_status()
    finally:
        server.close()
    assert status == {"version": "37.0.1", "build": "15733141", "executable": _EXE}


def test_an_older_server_without_status_is_not_an_error(monkeypatch):
    server = _serve_as_adb(monkeypatch, {"host:version": b"0029"})
    try:
        assert tools.adb_server_status() is None
    finally:
        server.close()


def test_another_protocol_is_a_warning_and_nothing_is_stopped(monkeypatch, tmp_path):
    ours = tmp_path / "adb.exe"
    ours.write_text("")
    stopped = []
    monkeypatch.setattr(tools.subprocess, "run", lambda cmd, **kw: stopped.append(cmd))
    monkeypatch.setattr(tools, "adb_protocol_version", lambda path: 41)
    server = _serve_as_adb(monkeypatch, {"host:version": b"0028",
                                         "host:server-status": _status("30.0.5")})
    try:
        level, text = tools.describe_adb_server(str(ours))
    finally:
        server.close()
    assert level == "WARNING"
    assert "protocol 40" in text and "protocol 41" in text and "30.0.5" in text
    assert stopped == []


def test_same_protocol_from_another_adb_is_only_noted(monkeypatch, tmp_path):
    ours = tmp_path / "adb.exe"
    ours.write_text("")
    monkeypatch.setattr(tools, "adb_protocol_version", lambda path: 41)
    server = _serve_as_adb(monkeypatch, {"host:version": b"0029", "host:server-status": _STATUS})
    try:
        level, text = tools.describe_adb_server(str(ours))
    finally:
        server.close()
    assert level == "INFO" and "leaves it running" in text


def test_our_own_server_is_not_worth_a_line(monkeypatch):
    monkeypatch.setattr(tools, "_same_file", lambda a, b: a == b)
    monkeypatch.setattr(tools, "adb_protocol_version", lambda path: pytest.fail("ran adb version"))
    server = _serve_as_adb(monkeypatch, {"host:version": b"0029", "host:server-status": _STATUS})
    try:
        assert tools.describe_adb_server(_EXE) is None
    finally:
        server.close()


def test_the_client_protocol_comes_from_adb_version_once(fake_adb, tmp_path):
    fake_adb.add("version", stdout="Android Debug Bridge version 1.0.41\nVersion 37.0.1-15733141\n")
    adb = tmp_path / "adb.exe"
    adb.write_text("")
    assert tools.adb_protocol_version(str(adb)) == 41
    assert tools.adb_protocol_version(str(adb)) == 41
    assert len(fake_adb.calls) == 1


# --------------------------------------------------------------------------- #
# shared servers
# --------------------------------------------------------------------------- #
def test_a_localhost_server_on_a_pc_without_lan_is_not_shared(monkeypatch):
    """With Wi-Fi off, every local server counted as shared, so the exit never
    stopped it and `serve --status` said "shared on the network"."""
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: True)
    monkeypatch.setattr(devices, "_listens_on_all_interfaces", lambda port, timeout=0.5: None)
    monkeypatch.setattr(devices, "_lan_addresses", lambda: set())
    assert devices.server_is_shared() is False
    # the bind address decides when it can be read
    monkeypatch.setattr(devices, "_listens_on_all_interfaces", lambda port, timeout=0.5: True)
    assert devices.server_is_shared() is True
    monkeypatch.setattr(devices, "_listens_on_all_interfaces", lambda port, timeout=0.5: False)
    monkeypatch.setattr(devices, "_lan_reachable", lambda port, timeout=2.0: True)
    assert devices.server_is_shared() is False


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS has no 127.0.0.2 by default")
def test_a_loopback_only_listener_is_told_apart(monkeypatch):
    loopback = socket.socket()
    loopback.bind(("127.0.0.1", 0))
    loopback.listen(1)
    try:
        assert devices._listens_on_all_interfaces(loopback.getsockname()[1], timeout=0.3) is False
    finally:
        loopback.close()
    # (a real all-interfaces bind would pop a firewall prompt on Windows)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: socket.socket())
    assert devices._listens_on_all_interfaces(5037) is True


def _fake_platform_tools_download(monkeypatch, tmp_path, order, *, shared):
    adb = tmp_path / ("adb.exe" if os.name == "nt" else "adb")
    adb.write_text("")

    def extract(zip_path, dest_parent, strip_top_to=None):
        folder = os.path.join(dest_parent, "platform-tools")
        os.makedirs(folder)
        open(os.path.join(folder, toolsdl._exe("adb")), "w").close()

    monkeypatch.setattr(toolsdl, "managed_adb", lambda: str(adb))
    monkeypatch.setattr(toolsdl, "latest_adb_archive", lambda: {"url": "https://example/pt.zip"})
    monkeypatch.setattr(toolsdl, "_download", lambda url, dest, on_progress=None: open(dest, "wb").close())
    monkeypatch.setattr(toolsdl, "_check_zip", lambda path: None)
    monkeypatch.setattr(toolsdl, "_verify_platform_tools", lambda path, archive: None)
    monkeypatch.setattr(toolsdl, "_extract_zip", extract)
    monkeypatch.setattr(toolsdl, "_swap_dir", lambda new, dest: order.append("swap"))
    monkeypatch.setattr(toolsdl, "_sync_scrcpy_adb", lambda: None)
    monkeypatch.setattr(devices, "server_is_shared", lambda port=5037, adb_path=None: shared)
    monkeypatch.setattr(tools, "kill_adb_server", lambda adb, port=5037, **kw: order.append(("stop", port)))
    return str(adb)


def test_a_tools_upgrade_brings_a_shared_server_back(monkeypatch, tmp_path):
    """`turboadb serve` machines lost this PC's devices after every adb update."""
    order = []
    adb = _fake_platform_tools_download(monkeypatch, tmp_path, order, shared=True)
    monkeypatch.setattr(devices, "restart_shared_server",
                        lambda port, adb_path=None: order.append(("share", port, adb_path)) or "ok")
    assert toolsdl.download_platform_tools(force=True) == adb
    assert order == [("stop", 5037), "swap", ("share", 5037, adb)]


def test_a_local_server_is_not_turned_into_a_shared_one(monkeypatch, tmp_path):
    order = []
    _fake_platform_tools_download(monkeypatch, tmp_path, order, shared=False)
    monkeypatch.setattr(devices, "restart_shared_server", lambda *a, **k: pytest.fail("shared"))
    toolsdl.download_platform_tools(force=True)
    assert order == [("stop", 5037), "swap"]


def test_a_shared_server_that_cannot_come_back_is_reported(monkeypatch, tmp_path):
    order = []
    _fake_platform_tools_download(monkeypatch, tmp_path, order, shared=True)

    def refuse(port, adb_path=None):
        raise RuntimeError("port 5037 in use")

    monkeypatch.setattr(devices, "restart_shared_server", refuse)
    checks = {"adb": {"upgrade": True, "installed": "36.0.0", "latest": "37.0.1"},
              "scrcpy": {"upgrade": False}}
    result = toolsdl.upgrade_tools(checks=checks)
    assert "adb" in result["updated"]
    assert "could not be restarted" in result["errors"]["sharing"]
    assert "port 5037 in use" in result["errors"]["sharing"]


def test_a_failed_swap_still_brings_sharing_back(monkeypatch, tmp_path):
    order = []
    _fake_platform_tools_download(monkeypatch, tmp_path, order, shared=True)

    def locked(new, dest):
        raise toolsdl.ADBError("cannot replace platform-tools")

    monkeypatch.setattr(toolsdl, "_swap_dir", locked)
    monkeypatch.setattr(devices, "restart_shared_server",
                        lambda port, adb_path=None: order.append(("share", port)) or "ok")
    with pytest.raises(toolsdl.ADBError):
        toolsdl.download_platform_tools(force=True)
    assert order == [("stop", 5037), ("share", 5037)]


def test_sharing_comes_back_through_the_startup_task_when_installed(monkeypatch):
    ran = []
    monkeypatch.setattr(devices.os, "name", "nt")
    monkeypatch.setattr(devices, "_serve_task_installed", lambda: True)
    monkeypatch.setattr(devices.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: True)
    # the task's server is up once other machines can reach it
    monkeypatch.setattr(devices, "_listens_on_all_interfaces", lambda port, timeout=0.5: True)
    monkeypatch.setattr(devices, "start_shared_server", lambda *a, **k: pytest.fail("as this user"))
    assert "startup task" in devices.restart_shared_server(5037, "adb")
    assert ran == [["schtasks", "/run", "/tn", devices._SERVE_TASK]]


def test_sharing_comes_back_as_this_user_without_the_task(monkeypatch):
    started = []
    monkeypatch.setattr(devices, "_serve_task_installed", lambda: False)
    monkeypatch.setattr(devices, "start_shared_server",
                        lambda port, adb_path, restart: started.append((port, adb_path, restart)) or "ok")
    assert devices.restart_shared_server(5037, "adb") == "ok"
    assert started == [(5037, "adb", True)]


# --------------------------------------------------------------------------- #
# the GUI's adb, and its one pre-warm (no PyQt needed)
# --------------------------------------------------------------------------- #
def test_gui_adb_path_honours_turboadb_adb_like_find_adb(monkeypatch, tmp_path):
    """The GUI ignored TURBOADB_ADB once the managed copy existed, while the
    CLI honoured it: two adbs restarting each other's server."""
    from turboadb.gui import adb_path, settings

    exe = "adb.exe" if os.name == "nt" else "adb"
    paths = {}
    for name in ("env", "setting", "managed"):
        (tmp_path / name).mkdir()
        paths[name] = tmp_path / name / exe
        paths[name].write_text("")
    configured = {"adb_path": str(paths["setting"])}
    monkeypatch.setattr(settings, "get", lambda key, default=None: configured.get(key, default))
    monkeypatch.setattr(toolsdl, "managed_adb", lambda: str(paths["managed"]))
    tools.clear_tools_cache()

    monkeypatch.setenv("TURBOADB_ADB", str(paths["env"]))
    assert adb_path.gui_adb_path() == str(paths["env"]) == tools.find_adb()
    monkeypatch.setenv("TURBOADB_ADB", str(paths["env"].parent))  # a folder holding adb
    assert adb_path.gui_adb_path() == str(paths["env"])
    monkeypatch.setenv("TURBOADB_ADB", str(tmp_path / "missing"))
    assert adb_path.gui_adb_path() == str(paths["setting"])
    monkeypatch.delenv("TURBOADB_ADB")
    configured.clear()
    assert adb_path.gui_adb_path() == str(paths["managed"])


def test_the_gui_prewarm_runs_once_per_process(monkeypatch):
    """`turboadb-gui` and gui.app each started a pre-warm thread (12 s and
    2.5 s), so every launch ran two."""
    from turboadb import cli
    from turboadb.gui import adb_path

    monkeypatch.setattr(adb_path, "_prewarm_thread", None)
    monkeypatch.setattr(adb_path, "gui_adb_path", lambda: None)
    adb_path.prewarm_adb_server()  # no adb of the GUI's own yet: nothing starts
    assert adb_path._prewarm_thread is None

    calls = []
    monkeypatch.setattr(adb_path, "gui_adb_path", lambda: "adb.exe")
    monkeypatch.setattr(tools, "ensure_adb_server",
                        lambda path, timeout: calls.append((path, timeout)) or True)
    cli._prewarm_gui_adb_server()  # the console script, before PyQt
    adb_path.prewarm_adb_server()  # gui.app.main
    adb_path.prewarm_adb_server()
    adb_path._prewarm_thread.join(5)
    assert calls == [("adb.exe", adb_path.PREWARM_TIMEOUT_S)]
