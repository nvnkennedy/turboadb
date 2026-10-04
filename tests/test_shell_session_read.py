"""ShellSession.read() hands over what the shell has printed and never waits
for more: ``b""`` while the shell is idle, and ``at_eof`` once it is gone.

The shell here is a small Python child on the pipes ``open_shell`` uses: it
prints a line, stays silent until it is sent one, prints another and ends."""
import subprocess
import sys
import threading
import time

from turboadb.core import ShellSession

_CHILD = (
    "import sys\n"
    "print('hello'); sys.stdout.flush()\n"
    "sys.stdin.readline()\n"
    "print('bye'); sys.stdout.flush()\n"
)


def _shell() -> ShellSession:
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
    )
    return ShellSession(proc)


def _read(session, patience=5.0) -> bytes:
    """``session.read()`` on a thread of its own, so that a read which waits
    for output fails the test instead of hanging it."""
    got = []
    reader = threading.Thread(target=lambda: got.append(session.read()), daemon=True)
    reader.start()
    reader.join(patience)
    assert not reader.is_alive(), "read() waited for output instead of returning"
    return got[0]


def _read_until(session, marker, patience=20.0) -> bytes:
    data = b""
    deadline = time.monotonic() + patience
    while marker not in data and time.monotonic() < deadline:
        chunk = _read(session)
        data += chunk
        if not chunk:
            time.sleep(0.02)
    return data


def test_reading_an_idle_shell_returns_nothing_at_once():
    with _shell() as session:
        assert b"hello" in _read_until(session, b"hello")
        time.sleep(0.2)  # the line's end has arrived too
        _read(session)
        for _poll in range(3):
            started = time.monotonic()
            assert _read(session) == b""
            assert time.monotonic() - started < 2.0
        assert session.running and not session.at_eof  # idle, not gone


def test_output_arrives_after_an_idle_spell_and_the_end_is_told_apart():
    with _shell() as session:
        assert b"hello" in _read_until(session, b"hello")
        time.sleep(0.2)
        _read(session)
        assert _read(session) == b"" and not session.at_eof
        assert session.send_line("go")
        assert b"bye" in _read_until(session, b"bye")
        deadline = time.monotonic() + 20.0
        while not session.at_eof and time.monotonic() < deadline:
            _read(session)  # the line's end, then nothing until the pipe is over
            time.sleep(0.02)
        assert session.at_eof and _read(session) == b""
