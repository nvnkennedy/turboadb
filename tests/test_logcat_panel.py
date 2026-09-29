"""The Logcat tab: the archive gets every line before any cap (hidden page or
not), the filter runs ahead of the on-screen caps and searches the whole
capture, any case with a plain-text fallback, Tag/Level changes restart a live
capture where it left off, status is truthful, and a capture resumes once the
device is back."""
import re
import threading
import time

import pytest

pytest.importorskip("PyQt5")

from turboadb.core import ADBHandler  # noqa: E402


def _pump(qapp, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return until()


def _line(i, level="I", text="noise", stamp=None):
    stamp = stamp or f"09-27 10:{i // 60000 % 60:02d}:{i // 1000 % 60:02d}.{i % 1000:03d}"
    return f"{stamp}  1234  5678 {level} Tag: {text} #{i}"


def _chunks(lines, size=65536):
    data = ("\n".join(lines) + "\n").encode()
    return [data[i:i + size] for i in range(0, len(data), size)]


class _Pipe:
    """A child's stdout or stderr: its chunks, then EOF — or, for a live
    stream, nothing more until the process is killed."""

    def __init__(self, chunks=(), until=None):
        self._chunks = list(chunks)
        self._until = until
        self._lock = threading.Lock()
        self.closed = False

    def read1(self, _size=-1):
        while True:
            with self._lock:
                if self._chunks:
                    return self._chunks.pop(0)
            if self._until is None or self._until.is_set():
                return b""
            self._until.wait(0.01)

    read = read1

    def close(self):
        self.closed = True


class _Proc:
    def __init__(self, args, out=(), err=None, code=0, live=False):
        self.args = list(args)
        self.killed = threading.Event()
        self.stdout = _Pipe(out, self.killed if live else None)
        self.stderr = None if err is None else _Pipe(err)
        self.code = code

    def kill(self):
        self.killed.set()

    def wait(self, timeout=None):
        return 1 if self.killed.is_set() else self.code  # a killed adb exits 1 on Windows

    poll = wait


class _Handler:
    """Each popen() plays the next output: out/err chunks, exit code, live."""

    serial = "FAKE"

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.spawned = []
        self.stderr_modes = []
        self.cleared = 0

    def popen(self, args, stderr="merge"):
        self.stderr_modes.append(stderr)
        proc = _Proc(args, **(self.outputs.pop(0) if self.outputs else {"live": True}))
        self.spawned.append(proc)
        return proc

    def logcat_clear(self):
        self.cleared += 1

    def pid_of(self, name, safe=None):
        return 4242 if name == "com.example.app" else None


@pytest.fixture
def panel(qapp):
    from turboadb.gui.logcat_view import LogcatPanel

    made = []

    def make(handler=None):
        p = LogcatPanel(handler)
        p.logged = []
        p.log.connect(p.logged.append)
        made.append(p)
        return p

    yield make
    for p in made:
        p.close_panel()


def _view(p):
    return p.view.toPlainText().splitlines()


def _capture():
    from PyQt5.QtWidgets import QPlainTextEdit
    from turboadb.gui.logcat_view import _Capture
    from turboadb.gui.scrollback import Scrollback

    return _Capture(Scrollback(QPlainTextEdit()))  # the archive keeps its widget


# --------------------------------------------------------------------------- #
# nothing is lost (F1): archive first, filter before the caps
# --------------------------------------------------------------------------- #
def test_the_reader_archives_every_line_before_its_hand_off_cap(qapp, tmp_path):
    from turboadb.gui.logcat_view import _LogcatThread

    capture = _capture()
    lines = [_line(i) for i in range(50000)]
    thread = _LogcatThread(_Handler({"out": _chunks(lines)}), ["logcat"], capture=capture)
    thread._LINES_MAX = 5000
    thread.run()
    assert thread.take_dropped() == 45000
    assert thread.take_lines() == lines[-5000:]
    saved = tmp_path / "saved.log"
    capture.sb.save_to(str(saved))
    assert saved.read_text(encoding="utf-8").splitlines() == lines
    capture.sb.close()


def test_the_filter_runs_before_the_cap_so_a_flood_keeps_every_match(qapp):
    from turboadb.gui.logcat_view import _LogcatThread

    capture = _capture()
    capture.set_keep(re.compile("needle", re.I).search)
    lines = [_line(i, text="NEEDLE" if i % 1000 == 0 else "noise") for i in range(50000)]
    thread = _LogcatThread(_Handler({"out": _chunks(lines)}), ["logcat"], capture=capture)
    thread._LINES_MAX = 5000
    thread.run()
    assert thread.take_dropped() == 0
    assert thread.take_lines() == [ln for ln in lines if "NEEDLE" in ln]
    assert thread.received == 50000
    capture.sb.close()


def test_adb_errors_pass_any_filter_and_the_exit_code_is_kept(qapp, tmp_path):
    from turboadb.gui.logcat_view import _LogcatThread

    capture = _capture()
    capture.set_keep(re.compile("MyApp").search)
    handler = _Handler({"out": _chunks([_line(0, text="hello")]),
                        "err": [b"error: device offline\n"], "code": 1})
    thread = _LogcatThread(handler, ["logcat"], capture=capture)
    thread.run()
    assert handler.stderr_modes == ["pipe"]  # adb's words on a pipe of their own
    assert thread.take_lines() == ["error: device offline"]
    assert thread.returncode == 1 and list(thread.stderr_lines) == ["error: device offline"]
    saved = tmp_path / "saved.log"
    capture.sb.save_to(str(saved))
    assert sorted(saved.read_text(encoding="utf-8").splitlines()) == sorted(
        [_line(0, text="hello"), "error: device offline"])
    capture.sb.close()


def test_a_hidden_page_loses_nothing_and_shows_every_match(qapp, panel, tmp_path):
    from PyQt5.QtWidgets import QStackedWidget, QWidget

    lines = [_line(i, "E" if i % 997 == 0 else "I", "FATAL NEEDLE" if i % 997 == 0 else "noise")
             for i in range(60000)]
    p = panel(_Handler({"out": _chunks(lines)}))
    stack = QStackedWidget()
    other = QWidget()
    stack.addWidget(p)
    stack.addWidget(other)
    stack.show()
    try:
        p.hist.setCurrentIndex(p.hist.findText("Dump buffer once"))
        p.filt.setText("needle")
        p._refilter_view()
        p.start()
        stack.setCurrentWidget(other)  # the user is on another sub-tab meanwhile
        assert _pump(qapp, lambda: p.thread is None, timeout=30)
        stack.setCurrentWidget(p)
        p._render_pending()
        saved = tmp_path / "saved.log"
        p._sb.save_to(str(saved))
        assert saved.read_text(encoding="utf-8").splitlines() == lines
        assert _view(p) == [ln for ln in lines if "NEEDLE" in ln]  # and no "skipped" marker
        assert "[OK] logcat dump finished (60000 lines)" in p.logged
    finally:
        p.close_panel()
        stack.close()
        stack.deleteLater()


def test_clear_leaves_out_lines_read_before_it(panel):
    from turboadb.gui.logcat_view import _LogcatThread

    p = panel()
    thread = _LogcatThread(None, ["logcat"], capture=p._capture)
    thread._ingest(["read before Clear"])
    p._clear_view()
    thread._ingest(["read after Clear"])
    p._pull(thread)
    p._render_pending()
    assert _view(p) == ["read after Clear"]
    assert p._sb.memory_text() == "read after Clear\n"


# --------------------------------------------------------------------------- #
# the filter: whole capture, any case, plain-text fallback, status bypass
# --------------------------------------------------------------------------- #
def test_a_new_filter_searches_the_whole_capture_even_once_it_spilled(qapp, panel):
    p = panel()
    lines = [_line(i, text="NEEDLE" if i % 1000 == 0 else "noise") for i in range(50000)]
    for start in range(0, len(lines), 1000):
        p._on_batch(lines[start:start + 1000])
    assert p._sb.memory_text() is None  # on disk: searched off the UI thread
    p.filt.setText("needle")
    p._refilter_view()
    assert p._searching
    assert _pump(qapp, lambda: not p._searching, timeout=30)
    assert _view(p) == [ln for ln in lines if "NEEDLE" in ln]
    p.filt.setText("")
    p._refilter_view()
    assert _pump(qapp, lambda: not p._searching, timeout=30)
    shown = _view(p)
    assert shown[0] == "… (45000 earlier lines not redrawn — saved log has all)"
    assert shown[1:] == lines[-5000:]


def test_the_filter_ignores_case_unless_match_case_is_on(panel):
    p = panel()
    error = _line(0, "E", "Error connecting")
    p._on_batch([error, _line(1, "F", "FATAL EXCEPTION"), _line(2, "I", "fine")])
    p.filt.setText("error")
    p._refilter_view()
    assert _view(p) == [error]
    p.btn_case.setChecked(True)
    p._refilter_view()
    assert _view(p) == []
    p.filt.setText("Error")
    p._refilter_view()
    assert _view(p) == [error]
    assert "any case" in p.filt.placeholderText()


def test_text_that_is_not_a_usable_regex_is_matched_as_plain_text(panel):
    from turboadb.gui.logcat_view import compile_filter

    p = panel()
    lines = ["a *crash* here", "crash", "onCreate(bundle)", "C++ rocks", "a" * 30 + "!"]
    p._on_batch(lines)
    for text, expected in (("*crash*", [lines[0]]), ("onCreate(", [lines[2]]),
                           ("C++", [lines[3]]), ("(a+)+$", [])):
        p.filt.setText(text)
        p._refilter_view()  # "(a+)+$" would backtrack for minutes as a regex
        assert _view(p) == expected, text
        assert p._filt_note.isVisible() and "plain text" in p.filt.toolTip()
    p.filt.setText("crash|rocks")
    p._refilter_view()
    assert _view(p) == [lines[0], lines[1], lines[3]] and not p._filt_note.isVisible()
    assert compile_filter("C++")[0].pattern == re.escape("C++")  # the same on every Python
    assert compile_filter("")[0] is None


def test_status_lines_are_never_hidden_by_the_filter(panel):
    p = panel()
    status = ["error: device offline", "- waiting for device -", "[TurboADB] a note"]
    mine = _line(0, text="MyApp hi")
    p._on_batch(status + [mine, _line(1, text="other")])
    p.filt.setText("MyApp")
    p._refilter_view()
    assert _view(p) == status + [mine]
    p._on_batch(["adb: device unauthorized", _line(2, text="nope")])
    p._render_pending()
    assert _view(p)[-1] == "adb: device unauthorized"


def test_pause_then_refilter_holds_what_arrived_during_the_pause(panel):
    p = panel()
    p._on_batch(["a1", "b1"])
    p._render_pending()
    p._toggle_pause()
    p._on_batch(["a2", "b2"])
    p.filt.setText("a")
    p._refilter_view()
    assert _view(p) == ["a1"] and p._pending == ["a2"]
    p._render_pending()
    assert _view(p) == ["a1"]  # still paused
    p._toggle_pause()
    p._render_pending()
    assert _view(p) == ["a1", "a2"]  # and no stale "skipped" marker


def test_long_format_entries_are_filtered_whole(qapp):
    from turboadb.gui.logcat_view import _LogcatThread, _search_lines

    entries = ["[ 09-27 10:00:00.001  1: 1 I/Boot ]", "starting up", "",
               "[ 09-27 10:00:00.002  1: 1 E/AndroidRuntime ]", "FATAL EXCEPTION: main",
               "java.lang.RuntimeException", "",
               "[ 09-27 10:00:00.003  1: 1 I/Other ]", "fine", ""]
    data = ("\n".join(entries) + "\n").encode()
    keep = re.compile("fatal", re.I).search
    capture = _capture()
    capture.set_keep(keep)
    handler = _Handler({"out": [data[:70], data[70:130], data[130:]]})  # entries split across reads
    thread = _LogcatThread(handler, ["logcat"], capture=capture, long_entries=True)
    thread.run()
    assert thread.take_lines() == entries[3:7]
    found, total = _search_lines(entries, keep, len(entries), 100, entries=True)
    assert [line for _i, line in found] == entries[3:7] and total == 4
    # while the capture runs, an unfinished last entry is left to the live stream
    assert _search_lines(entries[:5], keep, 5, 100, entries=True, partial=True) == ([], 0)
    assert [ln for _i, ln in _search_lines(entries[:5], keep, 5, 100, entries=True)[0]] == entries[3:5]
    capture.sb.close()


# --------------------------------------------------------------------------- #
# highlight and colours
# --------------------------------------------------------------------------- #
def _marked(p):
    from PyQt5.QtGui import QColor
    from turboadb.gui import theme

    colour = QColor(theme.HIGHLIGHT_BG)
    marked = 0
    block = p.view.document().firstBlock()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            if it.fragment().charFormat().background().color() == colour:
                marked += 1
                break
            it += 1
        block = block.next()
    return marked


def test_a_new_highlight_remarks_the_lines_already_on_screen(panel):
    p = panel()
    p._on_batch([_line(0, "E", "ANR in com.example"), _line(1, "I", "fine")])
    p._render_pending()
    assert _marked(p) == 0
    p.hl.setText("anr")
    p._remark_view()
    assert _marked(p) == 1
    p.hl.setText("fine")
    p._remark_view()
    assert _marked(p) == 1 and "fine" in p.view.document().findBlockByNumber(_marked(p)).text()
    p.hl.setText("")
    p._remark_view()
    assert _marked(p) == 0
    p.hl.setText("anr(")  # not a regex: highlighted as plain text, with a note
    assert p._hl_note.isVisible()


def test_a_slow_highlight_never_holds_the_window(panel):
    """The Highlight searched every line being drawn, and 2,000 lines per
    re-mark slice, on the UI thread: 8 ms with "Exception", 8 s with
    "\\w+.*\\w+Exception" (which the guard now refuses; a pattern just under
    its limit is slow the same way, only less)."""
    p = panel()
    text = "ActivityManager: Slow operation: " + "done updating battery stats " * 5
    lines = [_line(i, "W", text) for i in range(1000)]
    slow = re.compile(r"\w+.*\w+Exception", re.IGNORECASE)
    p._hl_re = slow
    started = time.perf_counter()
    p._draw_lines(lines)  # the redraw after a new filter is one call like this
    assert time.perf_counter() - started < 1.0  # was about 4 s
    assert _view(p) == lines
    assert p._remark_job is not None and p._remark_job.spans  # the rest are marked later
    p._reset_view()
    p._hl_re = None
    p._draw_lines(lines)
    p._hl_re = slow
    started = time.perf_counter()
    p._remark_view()  # a new highlight: its first slice runs at once
    assert time.perf_counter() - started < 1.0
    assert p._remark_job is not None and p._remark_job.spans  # the next after other events


def test_lines_drawn_past_the_highlight_budget_get_their_marks(qapp, panel, monkeypatch):
    from turboadb.gui.logcat_view import LogcatPanel

    monkeypatch.setattr(LogcatPanel, "_HIGHLIGHT_SLICE_S", 0.0)  # one line per slice
    p = panel()
    p.hl.setText("anr")
    p._hl_timer.stop()  # nothing on screen to re-mark yet
    lines = [_line(i, "E", "ANR in app") if i % 3 == 0 else _line(i, "I", "fine")
             for i in range(30)]
    p._on_batch(lines)
    p._render_pending()
    assert _view(p) == lines and _marked(p) <= 1  # drawn at once, marked later
    assert _pump(qapp, lambda: p._remark_job is None)
    assert _marked(p) == 10
    # A new highlight while lines still wait for the old one's marks: only the
    # new one's marks end up on screen.
    p._on_batch([_line(i, "E", "ANR in app") for i in range(30, 36)])
    p._render_pending()
    p.hl.setText("fine")
    p._hl_timer.stop()
    p._remark_view()
    assert _pump(qapp, lambda: p._remark_job is None)
    assert _marked(p) == 20
    assert all("fine" in p.view.document().findBlockByNumber(n).text()
               for n in range(p.view.document().blockCount() - 1) if _block_marked(p, n))


def _block_marked(p, number):
    from PyQt5.QtGui import QColor
    from turboadb.gui import theme

    it = p.view.document().findBlockByNumber(number).begin()
    while not it.atEnd():
        if it.fragment().charFormat().background().color() == QColor(theme.HIGHLIGHT_BG):
            return True
        it += 1
    return False


def test_every_logcat_format_gets_its_level_colour(panel):
    from turboadb.gui import theme

    p = panel()
    red = theme.LOGCAT_LEVELS["E"]
    samples = [
        "09-27 10:00:00.123  1234  5678 E AndroidRuntime: boom",  # threadtime
        "09-27 10:00:00.123 E/AndroidRuntime( 1234): boom",  # time
        "E/AndroidRuntime( 1234): boom",  # brief
        "E/AndroidRuntime: boom",  # tag
        "[ 09-27 10:00:00.123  1234: 5678 E/AndroidRuntime ]",  # long
    ]
    for sample in samples:
        assert p._line_color(sample, None)[0] == red, sample
    _colour, carry = p._line_color(samples[-1], None)
    # a long entry's message lines have no level of their own
    assert p._line_color("java.lang.RuntimeException: I said no", carry) == (red, red)
    assert p._line_color("", carry) == (theme.LOGCAT_DEFAULT, None)
    assert p._line_color("09-27 10:00:00.123  1  1 W Tag: careful", None)[0] == theme.LOGCAT_LEVELS["W"]


# --------------------------------------------------------------------------- #
# the one argv builder; Tag / Level / App
# --------------------------------------------------------------------------- #
def test_every_history_mode_uses_the_engine_builder(panel, monkeypatch):
    from turboadb.gui.logcat_view import _LogcatThread

    monkeypatch.setattr(_LogcatThread, "start", lambda self: None)
    p = panel(_Handler())
    try:
        for index in range(p.hist.count()):
            p.hist.setCurrentIndex(index)
            mode, tail = p.hist.currentData()
            p.start()
            assert p.thread.args == ADBHandler.logcat_args(dump=mode == "dump", tail=tail)
    finally:
        p.thread = None  # never started: nothing to stop


def test_tags_become_one_spec_each_and_a_stop_is_not_a_failure(qapp, panel):
    handler = _Handler()
    p = panel(handler)
    p.tag.setText("ActivityManager, CarService")
    p.level.setCurrentText("Error")
    p.start()
    assert _pump(qapp, lambda: handler.spawned)
    args = handler.spawned[0].args
    assert args == ADBHandler.logcat_args(tag="ActivityManager, CarService", priority="E", tail=1)
    assert args[-3:] == ["ActivityManager:E", "CarService:E", "*:S"]
    p.stop()
    assert _pump(qapp, lambda: p.thread is None)
    assert p.logged[-1] == "[OK] logcat stopped"  # though a killed adb exits 1


def test_an_app_is_filtered_by_its_process(qapp, panel):
    handler = _Handler()
    p = panel(handler)
    p.app.setText("com.example.app")
    p.start()
    assert _pump(qapp, lambda: handler.spawned)
    assert handler.spawned[0].args[:2] == ["logcat", "--pid=4242"]
    p.stop()
    assert _pump(qapp, lambda: p.thread is None)
    p.app.setText("com.not.running")
    p.start()
    assert _pump(qapp, lambda: p.thread is None)
    assert len(handler.spawned) == 1  # nothing to follow: no adb started
    assert any("com.not.running is not running" in m for m in p.logged if m.startswith("[ERROR]"))
    p._render_pending()
    assert any("com.not.running is not running" in line for line in _view(p))


def test_changing_the_level_restarts_a_live_capture_where_it_left_off(qapp, panel):
    first = [_line(0, text="one", stamp="09-27 10:00:00.001"),
             _line(1, text="two", stamp="09-27 10:00:00.002")]
    again = [first[1], _line(2, text="three", stamp="09-27 10:00:00.002"),
             _line(3, "E", "four", stamp="09-27 10:00:00.003")]
    handler = _Handler({"out": _chunks(first), "live": True},
                       {"out": [b"--------- beginning of main\n"] + _chunks(again), "live": True})
    p = panel(handler)
    p.clear_first.setChecked(True)
    p.start()
    assert _pump(qapp, lambda: p.thread is not None and p.thread.received == 2)
    assert handler.cleared == 1
    p.level.setCurrentText("Error")  # debounced; apply it now
    p._apply_restart()
    assert _pump(qapp, lambda: len(handler.spawned) == 2 and p.thread is not None
                 and p.thread.received == 2)
    assert handler.spawned[0].killed.is_set()
    assert handler.spawned[1].args == ADBHandler.logcat_args(priority="E", tail="09-27 10:00:00.002")
    assert handler.cleared == 1  # "Clear first" is never repeated
    p._render_pending()
    shown = _view(p)
    assert shown[:2] == first
    assert shown[2] == "[TurboADB] filter changed (all tags, level E) — capture restarted"
    assert shown[3:] == again[1:]  # the line logcat repeated is not shown twice
    p.stop()
    assert _pump(qapp, lambda: p.thread is None)


def test_the_crash_preset_switches_a_running_capture(qapp, panel):
    handler = _Handler()
    p = panel(handler)
    p._crash_preset()
    assert p.logged[-1].startswith("[INFO] crash preset set — press Start")
    p.level.setCurrentText("Verbose (all)")  # moving on clears the preset
    p.start()
    assert _pump(qapp, lambda: handler.spawned)
    assert handler.spawned[0].args == ADBHandler.logcat_args(tail=1)
    p._crash_preset()
    assert p.logged[-1] == "[INFO] crash preset set — switching the capture to crashes & ANRs"
    p._apply_restart()
    assert _pump(qapp, lambda: len(handler.spawned) == 2 and p.thread is not None)
    assert handler.spawned[1].args == ADBHandler.logcat_args(
        buffers=["crash", "main", "system"], priority="E", tail=1)
    p.stop()
    assert _pump(qapp, lambda: p.thread is None)


def test_the_restart_skips_the_lines_it_already_has(qapp):
    from turboadb.gui.logcat_view import _LogcatThread

    seen = _line(1, stamp="09-27 10:00:00.002")
    same_moment = _line(2, stamp="09-27 10:00:00.002")
    later = _line(3, stamp="09-27 10:00:00.003")
    thread = _LogcatThread(None, ["logcat"], skip_seen=("09-27 10:00:00.002", [seen]))
    thread._ingest(["--------- beginning of main", seen, same_moment, later, seen])
    assert thread.take_lines() == [same_moment, later, seen]
    # without timestamps (brief, tag) only the first line can be a repeat (-T 1)
    thread = _LogcatThread(None, ["logcat"], skip_seen=(None, ["I/Tag( 1): last"]))
    thread._ingest(["I/Tag( 1): last", "I/Tag( 1): last", "I/Tag( 1): new"])
    assert thread.take_lines() == ["I/Tag( 1): last", "I/Tag( 1): new"]


# --------------------------------------------------------------------------- #
# truthful status, and resuming after the device comes back
# --------------------------------------------------------------------------- #
def test_a_capture_that_dies_says_why_and_resumes_once_the_device_is_back(qapp, panel):
    handler = _Handler({"out": _chunks([_line(0, text="hello")]),
                        "err": [b"error: device offline\n"], "code": 1}, {"live": True})
    p = panel(handler)
    p.filt.setText("MyApp")
    p._refilter_view()
    p.start()
    assert _pump(qapp, lambda: p.thread is None)
    why = "logcat ended (exit 1): error: device offline; it resumes when the device is back"
    assert [m for m in p.logged if m.startswith(("[WARNING]", "[OK] logcat stopped"))] == [
        "[WARNING] " + why]
    p._render_pending()
    assert _view(p) == ["error: device offline", "[TurboADB] " + why]  # the filter hides neither
    p.resume_after_reconnect()
    assert _pump(qapp, lambda: len(handler.spawned) == 2 and p.thread is not None)
    p.stop()
    assert _pump(qapp, lambda: p.thread is None)
    p.resume_after_reconnect()  # stopped by the user: nothing to resume
    assert len(handler.spawned) == 2


def test_a_live_capture_pauses_and_resumes_around_adb_restarts_and_reboots(qapp, panel):
    lines = [_line(0, stamp="09-27 10:00:00.001"), _line(1, stamp="09-27 10:00:00.002")]
    handler = _Handler({"out": _chunks(lines), "live": True}, {"live": True}, {"live": True})
    p = panel(handler)
    p.start()
    assert _pump(qapp, lambda: p.thread is not None and p.thread.received == 2)
    p.suspend_for_device_loss()  # an ADB server restart
    assert _pump(qapp, lambda: p.thread is None)
    assert p.logged[-1] == "[INFO] logcat paused while the device reconnects; it resumes by itself"
    p.resume_after_reconnect()
    assert _pump(qapp, lambda: len(handler.spawned) == 2 and p.thread is not None)
    assert handler.spawned[1].args == ADBHandler.logcat_args(tail="09-27 10:00:00.002")
    p.suspend_for_device_loss(reboot=True)
    assert _pump(qapp, lambda: p.thread is None)
    p.resume_after_reconnect()
    assert _pump(qapp, lambda: len(handler.spawned) == 3 and p.thread is not None)
    assert handler.spawned[2].args == ADBHandler.logcat_args()  # the new boot's whole log
    assert not any(m.startswith("[WARNING]") for m in p.logged)
    p._render_pending()
    notes = [line for line in _view(p) if line.startswith("[TurboADB]")]
    assert notes == [
        "[TurboADB] device reconnecting — capture paused",
        "[TurboADB] device back — capture resumed where it stopped",
        "[TurboADB] device rebooting — capture paused",
        "[TurboADB] device rebooted — capturing its new log from the start",
    ]


def test_the_device_tab_pauses_and_resumes_its_logcat(qapp):
    from test_adb_processes import _Device, _close, _connect, _tab

    device = _Device()
    tab = _tab(qapp)
    messages = []
    tab.log.connect(messages.append)
    try:
        _connect(tab, device)
        tab.show()
        tab.prepare_for_adb_restart()  # a Logcat page never shown stays unbuilt
        assert tab._lazy_pages["logcat"].page is None
        tab._adb_restart_in_progress = False
        tab.show_subtab("logcat")
        assert _pump(qapp, lambda: tab._lazy_pages["logcat"].page is not None)
        tab.logcat.start()
        assert _pump(qapp, lambda: len(device.spawned("logcat")) == 1)

        tab.prepare_for_adb_restart()
        assert _pump(qapp, lambda: tab.logcat.thread is None)
        assert not device.spawned("logcat")[0].alive
        assert any("paused while the device reconnects" in m for m in messages)
        tab._adb_restart_in_progress = False
        tab._on_reconnected(True)
        assert _pump(qapp, lambda: len(device.spawned("logcat")) == 2)
        assert device.spawned("logcat")[1].argv == ADBHandler.logcat_args(tail=1)

        tab._begin_reboot_recovery("recovery")  # a reboot TurboADB sent
        assert _pump(qapp, lambda: tab.logcat.thread is None)
        tab._on_reconnected(True)
        assert _pump(qapp, lambda: len(device.spawned("logcat")) == 3)
        assert device.spawned("logcat")[2].argv == ADBHandler.logcat_args()
        assert "logcat_clear" not in device.commands
    finally:
        _close(qapp, tab)
