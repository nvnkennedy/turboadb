"""Engine fixes from the 2.5.0 review: log files flushed per line, adb's last
stderr words kept, the startup-command guard, --audio-dup, and re-entrant adb
resolution."""

import subprocess
import sys
import threading
import time

import pytest

from turboadb import devices
from turboadb.config import ADBConfig, ScrcpyOptions
from turboadb.core import ADBHandler


def handler():
    return ADBHandler(ADBConfig(serial="x"))


# --------------------------------------------------------------------------- #
# stream(save_to=...) writes every line through at once
# --------------------------------------------------------------------------- #
def test_a_saved_stream_is_on_disk_line_by_line(fake_adb, monkeypatch, tmp_path):
    h = handler()
    saved = tmp_path / "live.log"
    on_disk = []

    def lines(*_a, **_kw):
        yield "first"
        on_disk.append(saved.read_text())  # read while the stream still runs
        yield "second"
        on_disk.append(saved.read_text())

    monkeypatch.setattr(h, "iter_lines", lines)
    h.stream(["logcat"], save_to=str(saved), append=False)
    assert on_disk == ["first\n", "first\nsecond\n"]
    assert saved.read_text() == "first\nsecond\n"


# --------------------------------------------------------------------------- #
# the stderr drain: never blocks adb, and keeps its final message
# --------------------------------------------------------------------------- #
class _SlowPipe:
    """A stderr pipe whose last chunk arrives a moment after the process ended,
    like adb's final error line still in flight."""

    def __init__(self, chunks, delay):
        self._chunks = list(chunks)
        self._delay = delay

    def read(self, _n):
        if not self._chunks:
            return b""
        time.sleep(self._delay)
        return self._chunks.pop(0)


class _EndedProc:
    def __init__(self, pipe):
        self.stderr = pipe

    def poll(self):
        return 1


class _RunningProc(_EndedProc):
    def poll(self):
        return None


def test_the_last_stderr_words_survive_a_process_that_just_ended():
    proc = _EndedProc(_SlowPipe([b"error: device offline"], delay=0.2))
    ADBHandler._drain_stderr(proc)
    assert ADBHandler.collected_stderr(proc) == b"error: device offline"


def test_asking_while_the_process_runs_never_waits():
    proc = _RunningProc(_SlowPipe([b"late"], delay=2.0))
    ADBHandler._drain_stderr(proc)
    started = time.monotonic()
    assert ADBHandler.collected_stderr(proc) == b""
    assert time.monotonic() - started < 0.5


def test_the_wait_for_the_last_words_is_bounded():
    proc = _EndedProc(_SlowPipe([b"never in time"], delay=3.0))
    ADBHandler._drain_stderr(proc)
    started = time.monotonic()
    ADBHandler.collected_stderr(proc, wait=0.2)
    assert time.monotonic() - started < 1.0


def test_a_real_process_that_floods_stderr_neither_hangs_nor_loses_its_tail():
    script = (
        "import sys\n"
        "sys.stderr.write('x' * (1024 * 1024))\n"  # far past any pipe buffer
        "sys.stderr.flush()\n"
        "sys.stdout.write('done')\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ADBHandler._drain_stderr(proc)
        assert proc.stdout.read() == b"done"  # would block forever without the drain
        proc.wait(timeout=30)
        kept = ADBHandler.collected_stderr(proc)
        assert len(kept) == ADBHandler._STDERR_KEEP_BYTES and set(kept) == {ord("x")}
    finally:
        proc.stdout.close()
        proc.stderr.close()


def test_the_last_line_of_a_real_process_is_collected():
    script = "import sys; sys.stderr.write('error: closed')"
    proc = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        ADBHandler._drain_stderr(proc)
        proc.wait(timeout=30)
        assert ADBHandler.collected_stderr(proc) == b"error: closed"
    finally:
        proc.stderr.close()


def test_an_undrained_process_still_reports_its_stderr():
    class Pipe:
        def read(self):
            return b"adb: error"

    class Proc:
        stderr = Pipe()

    assert ADBHandler.collected_stderr(Proc()) == b"adb: error"


# --------------------------------------------------------------------------- #
# the startup command: only what really breaks it is refused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    r"C:\Tools & Apps\Python\pythonw.exe",
    r"C:\Users\a^b\Python\pythonw.exe",
    r"D:\(x86)\Python\pythonw.exe",
    r"C:\Users\Jürgen\Python\pythonw.exe",
])
def test_legal_windows_paths_are_accepted(path):
    assert devices._quotable(path, "interpreter") == path


@pytest.mark.parametrize("bad", ['"', "\n", "\r", "%"])
def test_what_would_break_the_command_is_refused(bad):
    with pytest.raises(RuntimeError, match="refusing to build a startup command"):
        devices._quotable(rf"C:\Py{bad}thon\pythonw.exe", "interpreter")


def test_the_launcher_quotes_a_path_with_an_ampersand(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(devices, "windowless_python", lambda: r"C:\A & B\pythonw.exe")
    cmd = devices._serve_launcher(5037, r"C:\A & B\adb.exe")
    assert cmd == (r'"C:\A & B\pythonw.exe" -m turboadb serve --port 5037'
                   r' --adb-path "C:\A & B\adb.exe"')


# --------------------------------------------------------------------------- #
# --audio-dup only where scrcpy accepts it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("source, expected", [
    (None, ["--audio-dup"]),                                   # scrcpy implies playback
    ("", ["--audio-dup"]),
    ("playback", ["--audio-source=playback", "--audio-dup"]),
    ("mic", ["--audio-source=mic"]),                           # scrcpy would refuse to start
    ("output", ["--audio-source=output"]),
])
def test_audio_dup_goes_only_with_the_playback_source(source, expected):
    args = ScrcpyOptions(audio_source=source, audio_dup=True).to_args()
    assert [a for a in args if a.startswith("--audio-")] == expected


def test_audio_dup_is_dropped_with_audio_off():
    args = ScrcpyOptions(no_audio=True, audio_dup=True).to_args()
    assert "--audio-dup" not in args and "--no-audio" in args


# --------------------------------------------------------------------------- #
# adb resolution is re-entrant on one thread, as it was without a lock
# --------------------------------------------------------------------------- #
def test_resolving_adb_is_reentrant(fake_adb):
    h = handler()
    got = []

    def inner():
        with h._adb_lock:  # e.g. a log callback during resolution
            got.append(h.adb_path)

    worker = threading.Thread(target=inner, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "adb resolution deadlocked on its own thread"
    assert got and got[0].endswith(("adb", "adb.exe"))


def test_one_resolution_for_many_threads(fake_adb, monkeypatch):
    import turboadb.core as core

    h = handler()
    calls = []
    real = core.find_adb

    def slow_find(*a, **kw):
        calls.append(1)
        time.sleep(0.05)
        return real(*a, **kw)

    monkeypatch.setattr(core, "find_adb", slow_find)
    paths = []
    workers = [threading.Thread(target=lambda: paths.append(h.adb_path)) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(5)
    assert len(calls) == 1 and len(set(paths)) == 1 and len(paths) == 8
