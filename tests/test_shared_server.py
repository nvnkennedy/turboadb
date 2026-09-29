"""Sharing this PC's devices: the SYSTEM startup task, its adb keys, the
firewall rules and the record a tools update restarts shared servers from.

No test here creates a scheduled task, changes the firewall or runs adb:
schtasks, netsh and PowerShell are fakes, and so is every socket probe.
"""
import json
import os
import types

import pytest

from turboadb import devices, tools
from turboadb.scrcpy import TUNNEL_PORT_FIREWALL_RANGE


@pytest.fixture
def home(monkeypatch, tmp_path):
    """This test's own ~ (and so its own ~/.turboadb/sharing.json)."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    return path


# --------------------------------------------------------------------------- #
# the SYSTEM startup task
# --------------------------------------------------------------------------- #
@pytest.fixture
def task(monkeypatch, home):
    """schtasks, the adb server on the port and its stopper, all fakes.

    ``on_run`` is what running the task does; by default nothing (a task whose
    Python cannot even import turboadb)."""
    world = types.SimpleNamespace(alive=True, everywhere=True, calls=[], on_run=lambda: None,
                                  last_result=b"Last Result: 0x1\r\n")
    monkeypatch.setattr(devices.os, "name", "nt")
    monkeypatch.setattr(devices, "find_adb", lambda p=None: r"C:\Users\me\adb.exe")
    monkeypatch.setattr(devices, "windowless_python", lambda: r"C:\Py\pythonw.exe")
    monkeypatch.setattr(devices, "_share_keys_with_system", lambda: None)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: world.alive)
    monkeypatch.setattr(devices, "_listens_on_all_interfaces",
                        lambda port, timeout=0.5: world.everywhere if world.alive else False)

    def kill(adb, port=5037, **kw):
        world.calls.append(("kill", port))
        world.alive = False

    def start_shared(port=5037, adb_path=None, restart=True):
        world.calls.append(("share again", port))
        world.alive = world.everywhere = True
        return "shared"

    def run(cmd, **kw):
        world.calls.append(tuple(cmd[:2]))
        if "/run" in cmd:
            world.on_run()
        out = world.last_result if "/query" in cmd else b""
        return types.SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    monkeypatch.setattr(tools, "kill_adb_server", kill)
    monkeypatch.setattr(tools, "ensure_adb_server", lambda adb=None, timeout=15.0, port=5037:
                        world.calls.append(("start local", port)) or True)
    monkeypatch.setattr(devices, "start_shared_server", start_shared)
    monkeypatch.setattr(devices.subprocess, "run", run)
    return world


def test_a_server_already_listening_does_not_pass_for_the_tasks(task):
    """`turboadb serve --startup-task` starts a shared server, then installs
    the task: that server answered the readiness check at once, so a task
    that could not even start Python was reported as working."""
    with pytest.raises(RuntimeError, match="no adb server is listening") as err:
        devices.install_serve_task(port=5037, ready_timeout=0.3)
    # the server that was there went first, and came back after the failure
    assert task.calls.index(("kill", 5037)) < task.calls.index(("schtasks", "/run"))
    assert ("share again", 5037) in task.calls
    assert "0x1" in str(err.value) and "until logoff" in str(err.value)


def test_the_tasks_own_server_is_what_counts(task):
    def task_starts_its_server():
        task.alive = task.everywhere = True

    task.on_run = task_starts_its_server
    assert devices.install_serve_task(port=5037, ready_timeout=2.0) == devices._SERVE_TASK
    assert ("share again", 5037) not in task.calls
    assert devices._task_port() == 5037 and 5037 in devices.recorded_shared_ports()


def test_a_localhost_only_server_is_not_the_tasks(task):
    """Something else starting a plain server in the gap answers too."""
    task.alive, task.everywhere = False, False

    def plain_server_appears():
        task.alive = True

    task.on_run = plain_server_appears
    with pytest.raises(RuntimeError, match="for other machines"):
        devices.install_serve_task(port=5037, ready_timeout=0.3)
    assert not any(call[0] == "kill" for call in task.calls)  # nothing was there to stop


def test_a_tools_update_restarts_the_task_only_for_its_own_port(task, monkeypatch):
    monkeypatch.setattr(devices, "_serve_task_installed", lambda: True)
    devices._change_sharing(lambda data: data.__setitem__("task_port", 5050))
    assert devices.restart_shared_server(5037) == "shared"  # as this user
    assert ("schtasks", "/run") not in task.calls


def test_uninstalling_the_task_forgets_its_port(task):
    devices._change_sharing(lambda data: data.__setitem__("task_port", 5050))
    assert devices.uninstall_serve_task() is True
    assert "task_port" not in devices._read_sharing()


# --------------------------------------------------------------------------- #
# the task signs in with this user's adb key
# --------------------------------------------------------------------------- #
def test_the_task_gets_this_users_adb_key(monkeypatch, home, tmp_path):
    system = tmp_path / "systemprofile"
    monkeypatch.setattr(devices, "_system_home", lambda: str(system))
    monkeypatch.delenv("ANDROID_USER_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_HOME", raising=False)
    monkeypatch.delenv("ADB_VENDOR_KEYS", raising=False)
    key = home / ".android" / "adbkey"
    key.parent.mkdir()
    key.write_text("-----BEGIN PRIVATE KEY-----")
    devices._share_keys_with_system()
    record = json.loads((system / ".turboadb" / "sharing.json").read_text(encoding="utf-8"))
    assert record["adb_vendor_keys"] == [str(key)]


def test_without_a_key_nothing_is_recorded(monkeypatch, home, tmp_path, caplog):
    system = tmp_path / "systemprofile"
    monkeypatch.setattr(devices, "_system_home", lambda: str(system))
    monkeypatch.delenv("ANDROID_USER_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_HOME", raising=False)
    monkeypatch.delenv("ADB_VENDOR_KEYS", raising=False)
    devices._share_keys_with_system()
    assert not system.exists()
    assert "accept the startup task's own key once" in caplog.text


def test_the_shared_server_passes_the_recorded_keys_to_adb(monkeypatch, home, tmp_path):
    """What the task's `turboadb serve` does, running as SYSTEM."""
    key = tmp_path / "adbkey"
    key.write_text("key")
    devices._change_sharing(lambda data: data.__setitem__("adb_vendor_keys", [str(key)]))
    launched = {}

    class Proc:
        def poll(self):
            return None

    def popen(cmd, **kw):
        launched.update(kw)
        return Proc()

    monkeypatch.setattr(devices, "find_adb", lambda p=None: "adb")
    monkeypatch.setattr(devices.subprocess, "Popen", popen)
    monkeypatch.setattr(tools, "kill_adb_server", lambda adb, port=5037, **kw: None)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: True)
    monkeypatch.setattr(devices, "_lan_reachable", lambda port, timeout=2.0: True)
    monkeypatch.setattr(devices, "_list_devices_socket", lambda *a, **k: [])
    devices.start_shared_server(5037)
    assert str(key) in launched["env"]["ADB_VENDOR_KEYS"].split(os.pathsep)


# --------------------------------------------------------------------------- #
# a localhost-only server that takes the port during the start
# --------------------------------------------------------------------------- #
def test_a_plain_server_that_took_the_port_is_replaced_once(monkeypatch, home):
    world = types.SimpleNamespace(kills=0, launches=0, alive=False, lan=True)

    class Proc:
        def __init__(self, dies):
            self.dies = dies

        def poll(self):
            return 1 if self.dies else None

    def popen(cmd, **kw):
        world.launches += 1
        first = world.launches == 1
        # the first start loses the port to a plain server a terminal started
        world.alive, world.lan = True, not first
        return Proc(dies=first)

    def kill(adb, port=5037, **kw):
        world.kills += 1
        world.alive = False

    monkeypatch.setattr(devices, "find_adb", lambda p=None: "adb")
    monkeypatch.setattr(devices.subprocess, "Popen", popen)
    monkeypatch.setattr(tools, "kill_adb_server", kill)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda **kw: world.alive)
    monkeypatch.setattr(devices, "_lan_reachable", lambda port, timeout=2.0: world.lan)
    monkeypatch.setattr(devices, "_list_devices_socket", lambda *a, **k: [])
    assert "listening on 0.0.0.0:5037" in devices.start_shared_server(5037)
    assert (world.kills, world.launches) == (2, 2)


# --------------------------------------------------------------------------- #
# firewall rules
# --------------------------------------------------------------------------- #
@pytest.fixture
def netsh(monkeypatch, home):
    """A firewall and PowerShell that record what they are asked."""
    world = types.SimpleNamespace(calls=[], rules=set(), public=b"", refuse=False)

    def run(cmd, **kw):
        cmd = list(cmd)
        world.calls.append(cmd)
        if cmd[0] == "powershell":
            return types.SimpleNamespace(returncode=0, stdout=world.public, stderr=b"")
        name = next((a[5:] for a in cmd if a.startswith("name=")), "")
        if "show" in cmd:
            return types.SimpleNamespace(returncode=0 if name in world.rules else 1,
                                         stdout=b"", stderr=b"")
        if world.refuse:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="elevation required")
        if "add" in cmd:
            world.rules.add(name)
        elif "delete" in cmd:
            world.rules.discard(name)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(devices.os, "name", "nt")
    monkeypatch.setattr(devices.subprocess, "run", run)
    return world


def _added(world):
    return [c for c in world.calls if "add" in c]


def test_the_rules_leave_public_networks_out(netsh):
    status = devices.open_firewall((5037, TUNNEL_PORT_FIREWALL_RANGE))
    added = _added(netsh)
    assert len(added) == 2
    assert all("profile=domain,private" in c for c in added)
    assert not any(a.startswith("remoteip=") for c in added for a in c)
    assert "Domain and Private networks" in status and "no password" in status


def test_a_public_network_is_named(netsh):
    netsh.public = "Hotel Wi-Fi\r\n".encode()
    status = devices.open_firewall((5037,))
    assert "'Hotel Wi-Fi', a Public network" in status and "Private" in status


def test_the_rules_can_be_narrowed_or_widened(netsh):
    devices.open_firewall((5037,), remote_ip="localsubnet")
    assert "remoteip=localsubnet" in _added(netsh)[-1]
    before = len(netsh.calls)
    devices.open_firewall((5037,), profiles="any")
    assert "profile=any" in _added(netsh)[-1]
    # every network is covered: no Public network to point out
    assert not any(c[0] == "powershell" for c in netsh.calls[before:])
    before = len(netsh.calls)
    assert "not changed" in devices.open_firewall((5037,), remote_ip="any dir=out")
    assert "not changed" in devices.open_firewall((5037,), profiles="cafe")
    assert len(netsh.calls) == before


def test_stopping_the_share_closes_its_rules(netsh, monkeypatch):
    devices.open_firewall((5037, TUNNEL_PORT_FIREWALL_RANGE))
    monkeypatch.setattr(devices, "find_adb", lambda p=None: "adb")
    monkeypatch.setattr(tools, "kill_adb_server", lambda adb, port=5037, **kw: None)
    monkeypatch.setattr(tools, "ensure_adb_server", lambda adb, timeout, port: True)
    status = devices.stop_shared_server(5037)
    assert netsh.rules == set()
    assert f"firewall: closed TCP 5037, {TUNNEL_PORT_FIREWALL_RANGE}" in status


def test_the_video_rule_stays_while_another_port_is_shared(netsh, monkeypatch):
    devices.open_firewall((5037, 5050, TUNNEL_PORT_FIREWALL_RANGE))
    devices._remember_shared_port(5050)
    monkeypatch.setattr(devices, "find_adb", lambda p=None: "adb")
    monkeypatch.setattr(tools, "kill_adb_server", lambda adb, port=5037, **kw: None)
    monkeypatch.setattr(tools, "ensure_adb_server", lambda adb, timeout, port: True)
    monkeypatch.setattr(devices, "server_is_shared", lambda port=5037, adb_path=None: port == 5050)
    devices.stop_shared_server(5037)
    assert netsh.rules == {"TurboADB TCP 5050", f"TurboADB TCP {TUNNEL_PORT_FIREWALL_RANGE}"}


def test_closing_without_admin_rights_says_so(netsh):
    netsh.rules = {"TurboADB TCP 5037"}
    netsh.refuse = True
    assert "could not close TCP 5037" in devices.close_firewall((5037,))
    netsh.refuse = False
    netsh.rules = set()
    assert devices.close_firewall((5037,)) == "firewall: no TurboADB rule was open"


# --------------------------------------------------------------------------- #
# the record of shared ports
# --------------------------------------------------------------------------- #
def test_shared_ports_are_recorded_and_forgotten(home, monkeypatch):
    monkeypatch.setattr(devices, "find_adb", lambda p=None: "adb")
    monkeypatch.setattr(devices, "_start_shared_server", lambda port, adb, restart: "shared")
    devices.start_shared_server(5050)
    devices.start_shared_server(5037)
    assert devices.recorded_shared_ports() == [5037, 5050]
    monkeypatch.setattr(tools, "kill_adb_server", lambda adb, port=5037, **kw: None)
    monkeypatch.setattr(tools, "ensure_adb_server", lambda adb, timeout, port: True)
    monkeypatch.setattr(devices, "_close_firewall", lambda ports: ([], []))
    devices.stop_shared_server(5050)
    assert devices.recorded_shared_ports() == [5037]
    (home / ".turboadb" / "sharing.json").write_text("not json", encoding="utf-8")
    assert devices.recorded_shared_ports() == []  # a damaged record is only a lost hint
