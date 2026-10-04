"""The suite leaves the machine it runs on alone.

A developer's PC often has an adb server running, with devices attached.  No
test may reach it: inside the suite a connection to the adb port of this PC is
refused, as on a machine where no server runs (the tests' own stand-in servers
listen on ports of their own)."""
import socket

import pytest

import conftest


def _listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server, server.getsockname()[1]


def test_the_adb_port_of_this_pc_is_one_of_the_refused():
    assert 5037 in conftest.ADB_PORTS_OF_THIS_PC


def test_no_test_reaches_a_server_on_an_adb_port_of_this_pc():
    server, port = _listener()  # stands in for the PC's own adb server
    conftest.ADB_PORTS_OF_THIS_PC.add(port)
    try:
        for host in ("127.0.0.1", "localhost"):
            with pytest.raises(ConnectionRefusedError):
                socket.create_connection((host, port), timeout=2)
        probe = socket.socket()
        try:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        finally:
            probe.close()
    finally:
        conftest.ADB_PORTS_OF_THIS_PC.discard(port)
        server.close()


def test_a_tests_own_server_is_reached_as_ever():
    server, port = _listener()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            accepted, _addr = server.accept()
            accepted.close()
            assert client.getpeername()[1] == port
    finally:
        server.close()
