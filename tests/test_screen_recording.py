"""Screen recording: a recorder that survives the terminal's Ctrl+C, is
cleaned up when the program exits under it, and back-to-back parts with no
gap while the previous part is pulled."""
import os
import threading
import time

import pytest

import turboadb.core as core
from turboadb import ADBConfig, ADBHandler
from turboadb.exceptions import ADBError, ADBTransferError
from turboadb.results import CommandResult


class _Recorder:
    """The ``adb shell screenrecord …`` child: runs until *ends* is set (or
    it is terminated), then exits with *code*."""

    stdout = None

    def __init__(self, code=0):
        self.code = code
        self.ends = threading.Event()
        self.kwargs = None

    def __call__(self, cmd, **kwargs):
        self.cmd, self.kwargs = list(cmd), kwargs
        return self

    def poll(self):
        return self.code if self.ends.is_set() else None

    def wait(self, timeout=None):
        if not self.ends.wait(timeout):
            raise core.subprocess.TimeoutExpired("adb", timeout)
        return self.code

    def terminate(self):
        self.code = -15 if self.code == 0 else self.code
        self.ends.set()

    kill = terminate


def _handler(monkeypatch, recorder, commands=None):
    h = ADBHandler(ADBConfig(serial="rec"))
    h._adb = "adb"
    commands = [] if commands is None else commands

    def run(args, **_kw):
        commands.append(list(args))
        if "kill -INT" in " ".join(args):
            recorder.ends.set()  # the device's screenrecord finalises and exits
        return CommandResult(" ".join(args), 0, "", "", 0.0)

    h._run = run
    monkeypatch.setattr(core.subprocess, "Popen", recorder)
    monkeypatch.setattr(core.time, "sleep", lambda _s: None if recorder.ends.is_set()
                        else recorder.ends.wait(0.01))
    return h, commands


# --------------------------------------------------------------------------- #
# Ctrl+C in a terminal
# --------------------------------------------------------------------------- #
def test_the_recorder_runs_in_a_session_of_its_own_off_windows(monkeypatch, tmp_path):
    for own in (True, False):
        monkeypatch.setattr(core, "_OWN_SESSION", own)
        recorder = _Recorder()
        recorder.ends.set()  # a clip that reached its time limit
        h, _commands = _handler(monkeypatch, recorder)
        out = tmp_path / "clip.mp4"
        h._transfer = lambda *a: out.write_bytes(b"video")
        assert h.screen_record(str(out), time_limit=5) == str(out)
        # the terminal's Ctrl+C then reaches only Python, which stops the
        # recording itself so the device writes the MP4 index
        assert recorder.kwargs["start_new_session"] is own
        assert recorder.kwargs["stdin"] is core.subprocess.DEVNULL


def _abandon_a_recording(monkeypatch, tmp_path, *, server_up):
    """Start a recording, then run what the interpreter's exit runs (a second
    Ctrl+C aborts outright) while the recording thread is stuck, as it is at a
    real exit; returns the recorder and the adb calls made at exit."""
    monkeypatch.setattr(core, "_TRACKED", {})
    recorder = _Recorder()
    h, commands = _handler(monkeypatch, recorder)
    h._transfer = lambda *a: None
    h._server_answers = lambda: server_up
    frozen = threading.Event()
    monkeypatch.setattr(core.time, "sleep", lambda _s: frozen.wait())
    stop = threading.Event()
    worker = threading.Thread(
        target=lambda: pytest.raises(Exception, h.screen_record, str(tmp_path / "c.mp4"),
                                     stop_event=stop),
        daemon=True)
    worker.start()
    tick = threading.Event()
    deadline = time.monotonic() + 5
    while recorder not in core._TRACKED and time.monotonic() < deadline:
        tick.wait(0.01)
    assert recorder in core._TRACKED
    before = len(commands)
    try:
        core._end_tracked_children()
        return recorder, commands[before:]
    finally:
        frozen.set()
        stop.set()
        worker.join(5)


def test_a_recorder_the_program_exits_under_is_ended_and_its_files_removed(monkeypatch,
                                                                          tmp_path):
    recorder, at_exit = _abandon_a_recording(monkeypatch, tmp_path, server_up=True)
    assert recorder.ends.is_set()
    assert len(at_exit) == 1 and at_exit[0][:3] == ["shell", "rm", "-f"]
    cleanup = " ".join(at_exit[0])
    assert "turboadb_rec_" in cleanup and "turboadb_screenrecord_" in cleanup


def test_no_clean_up_at_exit_starts_an_adb_server_again(monkeypatch, tmp_path):
    """The GUI stops the adb server it started before Python exits: any adb
    command then would start a new one and leave it running."""
    recorder, at_exit = _abandon_a_recording(monkeypatch, tmp_path, server_up=False)
    assert recorder.ends.is_set() and at_exit == []


def test_the_server_is_only_probed_never_started(monkeypatch):
    asked = []
    monkeypatch.setattr(core, "is_adb_server_alive",
                        lambda host, port, timeout: asked.append((host, port)) or False)
    assert ADBHandler(ADBConfig(serial="a", adb_server_port=5199))._server_answers() is False
    ADBHandler(ADBConfig(serial="b", adb_server_host="10.0.0.9"))._server_answers()
    assert asked == [("127.0.0.1", 5199), ("10.0.0.9", 5037)]


def test_a_recorder_that_ends_itself_is_not_tracked_any_more(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "_TRACKED", {})
    recorder = _Recorder()
    recorder.ends.set()
    h, _commands = _handler(monkeypatch, recorder)
    out = tmp_path / "clip.mp4"
    h._transfer = lambda *a: out.write_bytes(b"video")
    h.screen_record(str(out))
    assert core._TRACKED == {}


def test_a_signal_that_crosses_the_stop_is_still_a_stop(monkeypatch, tmp_path):
    """adb killed by a signal just as the user stopped: pull the clip, don't
    call the recording failed and delete it."""
    recorder = _Recorder(code=-2)
    h, commands = _handler(monkeypatch, recorder)
    stop = threading.Event()
    pulled = []
    out = tmp_path / "clip.mp4"

    def transfer(*args):
        pulled.append(args)
        out.write_bytes(b"video")

    h._transfer = transfer

    def ends_with_the_stop(_s):
        stop.set()
        recorder.ends.set()  # SIGINT reached adb itself first

    monkeypatch.setattr(core.time, "sleep", ends_with_the_stop)
    assert h.screen_record(str(out), stop_event=stop) == str(out)
    assert pulled and pulled[0][0] == "pull"
    assert any("kill -INT" in " ".join(c) for c in commands)  # the device recorder got it too


# --------------------------------------------------------------------------- #
# back-to-back parts
# --------------------------------------------------------------------------- #
def test_the_next_part_records_while_the_last_one_is_pulled(fake_adb, tmp_path, monkeypatch):
    h = ADBHandler(ADBConfig(serial="x"))
    stop = threading.Event()
    recording = []  # which part is being recorded, as it starts
    second_started = threading.Event()

    def record(_limit, **kw):
        recording.append(len(recording) + 1)
        if len(recording) == 2:
            second_started.set()
            stop.set()
        return core._Clip(f"/sdcard/rec{len(recording)}.mp4", False)

    def save(clip, path):
        if clip.remote_path == "/sdcard/rec1.mp4":
            # the first pull is slow; the second part must not wait for it
            assert second_started.wait(5), "part 2 waited for part 1's pull"
        return path

    monkeypatch.setattr(h, "_record_clip", record)
    monkeypatch.setattr(h, "_save_clip", save)
    paths = h.screen_record_continuous(str(tmp_path / "drive.mp4"), stop_event=stop)
    assert [os.path.basename(p) for p in paths] == ["drive.mp4", "drive-part02.mp4"]


def test_parts_are_reported_in_order_on_the_calling_thread(fake_adb, tmp_path, monkeypatch):
    h = ADBHandler(ADBConfig(serial="x"))
    stop = threading.Event()
    count = [0]

    def record(_limit, *, on_tick=None, **kw):
        count[0] += 1
        if count[0] > 1:
            deadline = time.monotonic() + 5
            while len(reported) < count[0] - 1 and time.monotonic() < deadline:
                on_tick()  # a saved part is handed on while the next one records
                time.sleep(0.01)
        if count[0] == 3:
            stop.set()
        return core._Clip(f"/sdcard/rec{count[0]}.mp4", False)

    monkeypatch.setattr(h, "_record_clip", record)
    monkeypatch.setattr(h, "_save_clip", lambda clip, path: path)
    reported, threads = [], []

    def on_part(path):
        reported.append(os.path.basename(path))
        threads.append(threading.get_ident())

    h.screen_record_continuous(str(tmp_path / "drive.mp4"), stop_event=stop, on_part=on_part)
    assert reported == ["drive.mp4", "drive-part02.mp4", "drive-part03.mp4"]
    assert set(threads) == {threading.get_ident()}


def test_a_part_that_cannot_be_pulled_stops_the_recording(fake_adb, tmp_path, monkeypatch):
    h = ADBHandler(ADBConfig(serial="x"))
    stops = []

    def record(_limit, *, stop_event=None, **kw):
        stops.append(stop_event)
        if len(stops) == 2:
            deadline = time.monotonic() + 5
            while not stop_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)  # recording part 2 until something stops it
        return core._Clip(f"/sdcard/rec{len(stops)}.mp4", False)

    def save(clip, path):
        if clip.remote_path == "/sdcard/rec1.mp4":
            raise ADBTransferError("adb pull failed (exit 1): device offline")
        return path

    monkeypatch.setattr(h, "_record_clip", record)
    monkeypatch.setattr(h, "_save_clip", save)
    reported = []
    with pytest.raises(ADBTransferError, match="device offline"):
        h.screen_record_continuous(str(tmp_path / "drive.mp4"), stop_event=threading.Event(),
                                   on_part=reported.append)
    assert len(stops) == 2 and stops[1].is_set()  # the failed pull stopped part 2 ...
    assert [os.path.basename(p) for p in reported] == ["drive-part02.mp4"]  # ... which was kept


def test_a_clip_that_could_not_be_pulled_stays_on_the_device(monkeypatch, tmp_path):
    h = ADBHandler(ADBConfig(serial="x"))
    logged = []
    h.set_log_callback(logged.append)
    removed = []
    h._run = lambda args, **kw: removed.append(args) or CommandResult("", 0, "", "", 0.0)

    def transfer(*_args):
        raise ADBTransferError("adb pull failed (exit 1): no space left")

    h._transfer = transfer
    with pytest.raises(ADBTransferError, match="no space"):
        h._save_clip(core._Clip("/sdcard/turboadb_rec_1.mp4", False), str(tmp_path / "c.mp4"))
    assert not any("rm" in a for a in removed)
    assert any("remains on the device at /sdcard/turboadb_rec_1.mp4" in line for line in logged)


# --------------------------------------------------------------------------- #
# the GUI's recorder uses the same engine loop
# --------------------------------------------------------------------------- #
def test_the_panel_recorder_records_through_the_engine(qapp, tmp_path):
    from turboadb.gui.mirror_panel import _RecordThread

    class Handler:
        def __init__(self):
            self.kwargs = None

        def screen_record_continuous(self, path, **kwargs):
            self.kwargs = kwargs
            kwargs["on_part"](path)  # the 3-min cap: part 1 saved, part 2 records
            kwargs["stop_event"].set()
            kwargs["on_part"](path.replace(".mp4", "-part02.mp4"))
            return []

    handler, stop = Handler(), threading.Event()
    thread = _RecordThread(handler, str(tmp_path / "clip.mp4"), stop, bit_rate="8M",
                           size="1280x720", display_id=2)
    done, parts = [], []
    thread.done.connect(done.append)
    thread.part.connect(parts.append)
    thread.run()
    assert done == [[str(tmp_path / "clip.mp4"), str(tmp_path / "clip-part02.mp4")]]
    assert parts == [2]  # "part 2" once part 1 was saved; nothing after the stop
    assert handler.kwargs["display_id"] == 2 and handler.kwargs["bit_rate"] == "8M"
    assert handler.kwargs["size"] == "1280x720" and handler.kwargs["safe"] is False

    # stopped before it started: nothing is recorded, and that is no failure
    stopped = threading.Event()
    stopped.set()
    idle = Handler()
    early = _RecordThread(idle, "x.mp4", stopped)
    done.clear()
    early.done.connect(done.append)
    early.run()
    assert done == [[]] and idle.kwargs is None


def test_the_panel_recorder_keeps_saved_parts_when_a_later_one_fails(qapp, tmp_path):
    from turboadb.gui.mirror_panel import _RecordThread

    class Handler:
        def screen_record_continuous(self, path, **kwargs):
            kwargs["on_part"](path)
            raise ADBError("the device went away")

    thread = _RecordThread(Handler(), str(tmp_path / "clip.mp4"), threading.Event())
    done, failed = [], []
    thread.done.connect(done.append)
    thread.fail.connect(failed.append)
    thread.run()
    assert done == [[str(tmp_path / "clip.mp4")]] and failed == []

    class Broken:
        def screen_record_continuous(self, path, **kwargs):
            raise ADBError("screenrecord is blocked on this build")

    thread = _RecordThread(Broken(), str(tmp_path / "clip.mp4"), threading.Event())
    thread.fail.connect(failed.append)
    thread.run()
    assert failed and "blocked" in failed[0]
