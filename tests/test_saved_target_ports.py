"""The ports of a saved target are checked by the engine's one port check.

The target store had a copy of ``config.validate_port`` of its own, which had
already drifted in what it said about a bad port.  It keeps only its default
for a port that was left out.
"""
import pytest

from turboadb.config import validate_port
from turboadb.gui.sessions import normalize_session


def _network(port):
    return {"name": "unit", "type": "network", "host": "10.0.0.5", "port": port}


def _remote(port):
    return {"name": "lab", "type": "remote", "adb_host": "lab-pc", "adb_port": port}


@pytest.mark.parametrize("port", [None, ""])
def test_a_port_left_out_is_the_usual_one(port):
    assert normalize_session(_network(port))["port"] == 5555
    assert normalize_session(_remote(port))["adb_port"] == 5037


def test_a_port_given_as_text_is_a_number():
    assert normalize_session(_network(" 5556 "))["port"] == 5556
    assert normalize_session(_remote("5038"))["adb_port"] == 5038


@pytest.mark.parametrize("port", [True, "fast", 0, 70000, -1, [5555]])
def test_a_bad_port_is_refused_as_the_engine_refuses_it(port):
    with pytest.raises(ValueError) as engine:
        validate_port(port, "port")
    with pytest.raises(ValueError) as saved:
        normalize_session(_network(port))
    assert str(saved.value) == str(engine.value)
    with pytest.raises(ValueError) as saved_remote:
        normalize_session(_remote(port))
    assert str(saved_remote.value) == str(engine.value).replace("port", "adb_port", 1)
