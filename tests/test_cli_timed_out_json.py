"""--json and a one-shot command that runs out of time.

``ADBTimeoutError.result`` holds what the command printed before it was
stopped, but ``turboadb --json shell`` / ``adb`` printed only an error on
stderr and no JSON document: the output was lost.  The document now carries
that output (``exit_code`` -1) with the error and ``"timed_out": true``; the
exit status stays 1.
"""
import json
import subprocess

import pytest

import turboadb.cli as cli
import turboadb.core as core
from turboadb.devices import Device

_PARTIAL = b"PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.\n64 bytes from 8.8.8.8: icmp_seq=1\n"


@pytest.fixture
def slow_ping(fake_adb, monkeypatch, capsys):
    """A device on which ``ping`` never ends: adb times out after printing."""
    monkeypatch.setattr(core, "is_adb_server_alive", lambda **_k: True)
    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: True)
    answer = fake_adb.run

    def run(cmd, **kwargs):
        if "ping" in " ".join(cmd) and "R58M2" not in cmd:
            fake_adb.calls.append(list(cmd))
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"), output=_PARTIAL,
                                            stderr=b"")
        return answer(cmd, **kwargs)

    monkeypatch.setattr(core.subprocess, "run", run)
    fake_adb.add("shell", stdout="64 bytes from 8.8.8.8: icmp_seq=1\n")

    def go(argv):
        rc = cli.main(argv)
        out, err = capsys.readouterr()
        return rc, out, err

    return go


def _check(doc, command):
    assert doc["timed_out"] is True and doc["exit_code"] == -1
    assert doc["stdout"] == _PARTIAL.decode()
    assert "timed out after 5" in doc["error"] and command in doc["command"]


def test_shell_keeps_what_the_command_printed(slow_ping):
    rc, out, err = slow_ping(["-s", "R58M1", "--timeout", "5", "--json", "shell", "--",
                              "ping", "8.8.8.8"])
    assert rc == 1
    _check(json.loads(out), "ping 8.8.8.8")  # one document on stdout
    assert "ERROR:" in err and "timed out" in err


def test_adb_keeps_what_the_command_printed(slow_ping):
    rc, out, err = slow_ping(["-s", "R58M1", "--timeout", "5", "--json", "adb", "--",
                              "shell", "ping", "8.8.8.8"])
    assert rc == 1
    _check(json.loads(out), "shell ping 8.8.8.8")
    assert "timed out" in err


def test_shell_on_every_device_keeps_the_devices_done_and_the_partial_one(slow_ping, monkeypatch):
    monkeypatch.setattr(cli, "_server_devices",
                        lambda cfg: [Device("R58M2", "device"), Device("R58M1", "device")])
    rc, out, err = slow_ping(["--timeout", "5", "--json", "shell", "--all", "--",
                              "ping", "8.8.8.8"])
    assert rc == 1
    first, second = json.loads(out)
    assert first["serial"] == "R58M2" and first["exit_code"] == 0 and "timed_out" not in first
    assert second["serial"] == "R58M1"
    _check(second, "ping 8.8.8.8")


def test_a_command_that_ends_in_time_has_the_usual_document(slow_ping):
    rc, out, err = slow_ping(["-s", "R58M1", "--timeout", "5", "--json", "shell", "--",
                              "getprop", "ro.serialno"])
    doc = json.loads(out)
    assert rc == 0 and doc["exit_code"] == 0 and "timed_out" not in doc
    assert "ERROR" not in err
