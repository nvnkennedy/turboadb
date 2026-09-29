"""One rule for "this PC's adb server".

The device list called a server local only when it was addressed as
127.0.0.1, localhost or ::1, while the mirror (scrcpy.is_local_host) also knew
this PC by its LAN addresses and its name: the same "remote" target was this
PC for the screen and another machine for the device list.  Both follow the
mirror's rule now.
"""
import types

import pytest

from turboadb import devices, scrcpy, tools


@pytest.fixture
def this_pc(monkeypatch):
    """192.168.1.5 is this PC; the first socket query finds no server there."""
    monkeypatch.setattr(scrcpy, "is_local_host",
                        lambda host: host in ("127.0.0.1", "192.168.1.5"))
    world = {"queries": [], "started": [], "ran": []}

    def query(host, port, timeout=1.0):
        world["queries"].append((host, port, timeout))
        return None if len(world["queries"]) == 1 else [devices.Device("R58M1", "device")]

    monkeypatch.setattr(devices, "_list_devices_socket", query)
    monkeypatch.setattr(tools, "ensure_adb_server",
                        lambda adb, timeout=15.0, port=5037: world["started"].append(port) or True)
    monkeypatch.setattr(devices, "find_adb", lambda explicit=None: "adb")
    monkeypatch.setattr(devices.subprocess, "run", lambda cmd, **kw: world["ran"].append(cmd)
                        or types.SimpleNamespace(returncode=1, stdout="", stderr="cannot connect"))
    return world


def test_a_lan_address_of_this_pc_is_its_own_server_for_the_device_list(this_pc):
    found = devices.list_devices(server_host="192.168.1.5", server_port=5038)
    assert [d.serial for d in found] == ["R58M1"]
    # as for 127.0.0.1: the quick probe, and a server that does not answer is started
    assert this_pc["queries"][0] == ("192.168.1.5", 5038, 0.25)
    assert this_pc["started"] == [5038] and this_pc["ran"] == []


def test_another_machine_is_still_remote(this_pc):
    with pytest.raises(ConnectionError, match="10.0.0.9:5038"):
        devices.list_devices(server_host="10.0.0.9", server_port=5038)
    assert this_pc["queries"] == [("10.0.0.9", 5038, 2.0)]
    assert this_pc["started"] == []  # never a server of our own for another machine
    assert this_pc["ran"][0][1:] == ["-H", "10.0.0.9", "-P", "5038", "devices", "-l"]


def test_the_mirror_and_the_device_list_ask_the_same_question(monkeypatch):
    asked = []
    monkeypatch.setattr(scrcpy, "is_local_host", lambda host: asked.append(host) or False)
    monkeypatch.setattr(devices, "_list_devices_socket", lambda *a, **k: [])
    devices.list_devices(server_host="lab-pc", server_port=5037)
    assert asked == ["lab-pc"]
