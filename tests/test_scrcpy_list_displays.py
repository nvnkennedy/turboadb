"""``scrcpy --list-displays`` ends within its timeout, and its output is read
whatever bytes are in it.

It was run with ``subprocess.run`` and captured pipes.  On Windows a timeout
killed scrcpy alone and then waited for the pipes, which scrcpy's adb clients
had inherited: with a head unit that had stopped answering, the call came back
only when that adb gave up.  And the output was decoded with the system code
page: one byte it has no character for (adb names the pushed server by its
path, "C:\\Users\\Łukasz\\…") and the whole output was dropped, which read as
"no displays".  The stand-in for scrcpy here is a Python child: no scrcpy, no
adb, no device.
"""
import os
import subprocess
import sys
import time

import pytest

import turboadb.scrcpy as scrcpy
from turboadb import proctree
from turboadb.exceptions import ScrcpyError


@pytest.fixture
def stand_in(monkeypatch):
    """``scrcpy`` is a Python child running the code given to the returned
    function; what the listing started it with is in ``.commands``."""
    real = subprocess.Popen
    commands = []

    def use(code):
        class StandIn(real):
            def __init__(self, cmd, **kw):
                commands.append(list(cmd))
                super().__init__([sys.executable, "-c", code], **kw)

        monkeypatch.setattr(scrcpy.subprocess, "Popen", StandIn)

    monkeypatch.setattr(scrcpy, "find_scrcpy", lambda path=None: "scrcpy")
    use.commands = commands
    return use


def _alive(pid):
    return pid in (proctree.snapshot() or {})


def test_the_timeout_holds_while_an_adb_client_keeps_the_output_open(stand_in, tmp_path):
    """The stand-in's child is scrcpy's adb client: it inherits the output and
    outlives scrcpy unless it is ended with it."""
    note = tmp_path / "client.pid"
    stand_in(
        "import subprocess, sys, time\n"
        "client = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(note)!r}, 'w').write(str(client.pid))\n"
        "print('INFO: scrcpy-server: 1 file pushed', flush=True)\n"
        "time.sleep(60)\n"
    )
    client = None
    try:
        started = time.monotonic()
        with pytest.raises(ScrcpyError, match="timed out"):
            scrcpy.list_displays("HU1", timeout=2.0)
        assert time.monotonic() - started < 15  # before: when the client ended, a minute later
        client = int(note.read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _alive(client):
            time.sleep(0.1)
        assert not _alive(client)  # ended with scrcpy, not left pushing to the device
    finally:
        if client is not None and _alive(client):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(client), "/F"], capture_output=True)
            else:
                os.kill(client, 9)


def test_bytes_the_code_page_has_no_character_for_do_not_hide_the_displays(stand_in):
    stand_in(
        "import sys\n"
        "out = sys.stdout.buffer\n"
        # adb's line about the server it pushed, with a profile folder the
        # ANSI code page cannot decode, and a stray byte no encoding takes
        "out.write(b'C:\\\\Users\\\\\\xc5\\x81ukasz\\\\scrcpy-server: 1 file pushed \\x81\\xff\\n')\n"
        "out.write(b'[server] INFO: List of displays:\\n')\n"
        "out.write(b'    --display-id=0    (1920x1080)\\n')\n"
        "out.flush()\n"
        "sys.stderr.write('    --display-id=2    (1280x480)\\n')\n"
    )
    assert scrcpy.list_displays("HU1") == [{"id": 0, "size": "1920x1080"},
                                           {"id": 2, "size": "1280x480"}]
    assert stand_in.commands == [["scrcpy", "--serial", "HU1", "--list-displays"]]


def test_a_scrcpy_that_fails_says_what_it_printed(stand_in):
    stand_in("import sys; sys.stderr.write('ERROR: Could not find any ADB device\\n'); sys.exit(1)")
    with pytest.raises(ScrcpyError, match="could not list displays: ERROR: Could not find any ADB"):
        scrcpy.list_displays("HU1")
