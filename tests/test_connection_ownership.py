"""A handler keeps the network connection it made.

``owns_connection`` says whether this handler's own ``adb connect`` made the
connection, so that closing its tab drops it and nothing else. The tab's
reconnect loop calls ``connect_tcp`` again for the same target after a reboot;
adb then answers "already connected", and that used to end the ownership: the
device stayed connected in the adb server after the tab was closed."""
import pytest

import turboadb.core as core
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler

_TARGET = "10.0.0.5:5555"


@pytest.fixture
def adb(fake_adb, monkeypatch):
    """A local adb server that answers; ``adb.says(text)`` sets what ``adb
    connect`` answers from now on."""
    monkeypatch.setattr(core, "is_adb_server_alive", lambda **_kw: True)
    answer = {"text": f"connected to {_TARGET}"}
    monkeypatch.setattr(
        fake_adb, "_answer",
        lambda argv: (0, answer["text"].encode(), b"") if "connect" in argv
        else (0, b"device\n", b"") if "get-state" in argv else (0, b"", b""))
    fake_adb.says = lambda text: answer.update(text=text)
    return fake_adb


def test_already_connected_keeps_a_connection_the_handler_made(adb):
    handler = ADBHandler(ADBConfig(host="10.0.0.5"))
    handler.connect(safe=False)
    assert handler.owns_connection is True
    adb.says(f"already connected to {_TARGET}")
    for _poll in range(3):  # the reconnect loop, every two seconds
        assert handler.connect_tcp("10.0.0.5", 5555) == _TARGET
    assert handler.owns_connection is True


def test_connecting_again_keeps_it_too(adb):
    handler = ADBHandler(ADBConfig(host="10.0.0.5"))
    handler.connect(safe=False)
    adb.says(f"already connected to {_TARGET}")
    handler.connect(safe=False)
    assert handler.owns_connection is True


def test_a_connection_somebody_else_made_is_never_taken_over(adb):
    adb.says(f"already connected to {_TARGET}")
    handler = ADBHandler(ADBConfig(host="10.0.0.5"))
    handler.connect(safe=False)
    assert handler.connect_tcp("10.0.0.5", 5555) == _TARGET
    assert handler.owns_connection is False


def test_moving_to_a_target_somebody_else_connected_ends_the_ownership(adb):
    handler = ADBHandler(ADBConfig(host="10.0.0.5"))
    handler.connect(safe=False)
    adb.says("already connected to 10.0.0.9:5555")
    assert handler.connect_tcp("10.0.0.9", 5555) == "10.0.0.9:5555"
    assert handler.owns_connection is False


def test_a_disconnect_ends_the_ownership(adb):
    handler = ADBHandler(ADBConfig(host="10.0.0.5"))
    handler.connect(safe=False)
    handler.disconnect()
    adb.says(f"already connected to {_TARGET}")  # another program connected it since
    handler.connect_tcp("10.0.0.5", 5555)
    assert handler.owns_connection is False
