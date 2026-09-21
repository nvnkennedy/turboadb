"""The ADB screencap renderer: stream parsing, input mapping and its lifecycle."""
import gzip
import io
import struct
import threading
import time
import zlib

import pytest

pytest.importorskip("PyQt5")

from turboadb import ADBHandler  # noqa: E402
from turboadb.gui import screencap_view as sv  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def raw_capture(width, height, *, header=16, pixel_format=1, pixel=b"\x10\x20\x30\xff"):
    head = struct.pack("<III", width, height, pixel_format)
    if header == 16:
        head += struct.pack("<I", 1)  # sRGB
    bpp = sv.RAW_BYTES_PER_PIXEL[pixel_format]
    return head + (pixel[:bpp] * (width * height))


def png_capture(width, height, extra_idat=b""):
    """A small, valid PNG (optionally with 'IEND' bytes inside its IDAT data)."""

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + b"\x40\x80\xc0" * width for _ in range(height))
    return (
        sv.PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"tEXt", b"note\x00IEND\xaeB`\x82" + extra_idat)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def feed_in_pieces(parser, data, size):
    frames = []
    for start in range(0, len(data), size):
        frames.extend(parser.feed(data[start:start + size]))
    return frames


# --------------------------------------------------------------------------- #
# raw stream parser
# --------------------------------------------------------------------------- #
def test_raw_frames_with_16_byte_headers_split_across_reads():
    stream = raw_capture(4, 3) + raw_capture(4, 3, pixel=b"\x01\x02\x03\xff")
    parser = sv.RawStreamParser()
    frames = feed_in_pieces(parser, stream, 5)
    assert [(f.width, f.height, f.pixel_format) for f in frames] == [(4, 3, 1), (4, 3, 1)]
    assert frames[0].pixels == b"\x10\x20\x30\xff" * 12
    assert frames[1].pixels == b"\x01\x02\x03\xff" * 12
    assert parser.header_size == 16 and parser.confirmed and parser.buffered == 0


def test_raw_frames_with_12_byte_headers_are_detected():
    stream = b"".join(raw_capture(5, 2, header=12, pixel=bytes([i, 9, 9, 255])) for i in range(3))
    frames = sv.RawStreamParser().feed(stream)
    assert [f.pixels[:1] for f in frames] == [b"\x00", b"\x01", b"\x02"]


def test_a_wrong_first_header_guess_is_corrected_on_the_second_frame():
    # a 12-byte header whose first pixel reads as colour space 1 fools the guess
    stream = (
        raw_capture(2, 2, header=12, pixel=b"\x01\x00\x00\x00")
        + raw_capture(2, 2, header=12, pixel=b"\x05\x06\x07\x08")
        + raw_capture(2, 2, header=12, pixel=b"\x0a\x0b\x0c\x0d")
    )
    parser = sv.RawStreamParser()
    # enough for frame 1 under the (wrong) 16-byte guess, not for the next header
    first = parser.feed(stream[:32])
    assert len(first) == 1 and parser.header_size == 16 and not parser.confirmed
    frames = first + feed_in_pieces(parser, stream[32:], 3)
    assert parser.header_size == 12 and parser.confirmed
    assert frames[-1].pixels == b"\x0a\x0b\x0c\x0d" * 4
    assert frames[-2].pixels == b"\x05\x06\x07\x08" * 4


def test_raw_noise_before_the_first_frame_is_skipped():
    stream = b"sh: set: pipefail: unknown option\n" + raw_capture(3, 3)
    parser = sv.RawStreamParser()
    frames = feed_in_pieces(parser, stream, 7)
    assert len(frames) == 1 and frames[0].width == 3
    assert parser.skipped == len(b"sh: set: pipefail: unknown option\n")


def test_raw_frames_that_stop_lining_up_raise():
    parser = sv.RawStreamParser()
    parser.feed(raw_capture(4, 4) + raw_capture(4, 4))
    assert parser.confirmed
    with pytest.raises(sv.StreamFormatError):
        for _ in range(8):  # a size that doesn't match: text where headers belong
            parser.feed(b"\xff" * 5 + raw_capture(4, 4)[:-3])


def test_a_capture_block_whose_size_does_not_match_its_header_is_rejected():
    block = raw_capture(10, 10)
    assert sv.raw_frame_from_block(block).width == 10
    assert sv.raw_frame_from_block(raw_capture(10, 10, header=12)).pixels[:4] == b"\x10\x20\x30\xff"
    with pytest.raises(sv.StreamFormatError):
        sv.raw_frame_from_block(block[:-7])  # neither a 12- nor a 16-byte header
    with pytest.raises(sv.StreamFormatError):
        sv.raw_frame_from_block(b"not a capture at all")


# --------------------------------------------------------------------------- #
# PNG and gzip streams, and the decoder
# --------------------------------------------------------------------------- #
def test_png_frames_split_on_their_iend_chunk():
    one, two = png_capture(3, 2), png_capture(2, 5)
    splitter = sv.PngStreamSplitter()
    frames = feed_in_pieces(splitter, b"noise\n" + one + two, 4)
    assert frames == [one, two]  # the 'IEND' bytes inside tEXt never split a frame
    assert sv.png_size(frames[1]) == (2, 5)


def test_gzip_members_are_one_capture_each_and_empty_ones_are_counted():
    capture = raw_capture(6, 4)
    stream = gzip.compress(capture) + gzip.compress(b"") + gzip.compress(raw_capture(6, 4, header=12))
    decoder = sv.ScreencapStreamDecoder()
    newest = None
    for start in range(0, len(stream), 11):
        frame = decoder.feed(stream[start:start + 11])
        newest = frame or newest
    assert decoder.kind == "gzip" and decoder.frames == 2
    assert (newest.width, newest.height) == (6, 4)


def test_the_decoder_recognises_each_format_and_keeps_only_the_newest_frame():
    raw = sv.ScreencapStreamDecoder()
    frame = raw.feed(raw_capture(2, 2) + raw_capture(2, 2, pixel=b"\x09\x09\x09\xff"))
    assert raw.kind == "raw" and frame.pixels[:1] == b"\x09" and raw.dropped == 1
    png = sv.ScreencapStreamDecoder()
    assert isinstance(png.feed(png_capture(2, 2)), sv.PngFrame) and png.kind == "png"


def test_the_device_error_text_ends_the_stream():
    decoder = sv.ScreencapStreamDecoder()
    marker = ADBHandler.SCREENCAP_ERROR_MARKER
    decoder.feed(gzip.compress(b"") + marker[:10])
    decoder.feed(marker[10:] + b" Display Id '9' is not valid.\n")
    assert "is not valid" in decoder.device_error


# --------------------------------------------------------------------------- #
# geometry and gestures
# --------------------------------------------------------------------------- #
def test_letterboxed_points_map_to_device_pixels():
    # a 1080x2400 portrait capture in a 1000x1000 area: 450 wide, bars left/right
    assert sv.fit_rect(1080, 2400, 1000, 1000) == (275.0, 0.0, 450.0, 1000.0)
    assert sv.map_to_device(500, 500, 1000, 1000, 1080, 2400) == (540, 1200)
    assert sv.map_to_device(275, 0, 1000, 1000, 1080, 2400) == (0, 0)
    assert sv.map_to_device(100, 500, 1000, 1000, 1080, 2400) is None  # on the bar
    assert sv.map_to_device(100, 500, 1000, 1000, 1080, 2400, clamp=True) == (0, 1200)
    # captured at the panel's physical size, input in the logical (override) size
    assert sv.map_to_device(500, 500, 1000, 1000, 1260, 2800, (1080, 2400)) == (540, 1200)
    # the logical size follows a rotation of the capture
    assert sv.input_size(2400, 1080, (1080, 2400)) == (2400, 1080)


def test_tap_long_press_and_drag_become_the_right_input():
    assert sv.gesture_for((10, 20), (11, 21), 120, moved=False) == ("tap", 10, 20)
    assert sv.gesture_for((10, 20), (10, 20), 900, moved=False) == ("swipe", 10, 20, 10, 20, 900)
    assert sv.gesture_for((10, 20), (300, 20), 250, moved=True) == ("swipe", 10, 20, 300, 20, 250)
    assert sv.gesture_for((10, 20), (300, 20), 5, moved=True)[-1] == 80  # never a zero-ms fling


def test_the_wheel_scrolls_with_a_short_swipe():
    up = sv.wheel_swipe((540, 1200), 0, 120, (1080, 2400))
    assert up == ("swipe", 540, 1200, 540, 1200 + int(2400 * 0.18), 150)
    down = sv.wheel_swipe((540, 1200), 0, -120, (1080, 2400))
    assert down[4] < 1200
    assert sv.wheel_swipe((540, 0), 0, -120, (1080, 2400)) is None  # nowhere to go
    assert sv.wheel_swipe((500, 10), -120, 0, (1080, 2400))[3] < 500


# --------------------------------------------------------------------------- #
# the device-side command
# --------------------------------------------------------------------------- #
def test_the_stream_command_uses_the_physical_id_for_capture_only():
    phone = ADBHandler.screencap_stream_script(None, interval=0.2)
    assert "screencap 2>/dev/null" in phone and "-d" not in phone
    ivi = ADBHandler.screencap_stream_script(4619827259835644672, interval=0.5, fmt="png")
    assert "screencap -d 4619827259835644672 -p 2>/dev/null" in ivi and "sleep 0.5" in ivi
    assert "gzip" in ADBHandler.screencap_stream_script(3) and "command -v gzip" in (
        ADBHandler.screencap_stream_script(3)
    )
    with pytest.raises(ValueError):
        ADBHandler.screencap_stream_script("1; reboot")
    assert ADBHandler.screencap_display_arg({"id": 0, "physical_id": 99}) is None
    assert ADBHandler.screencap_display_arg({"id": 2, "physical_id": 4619827259835644672}) == (
        4619827259835644672
    )
    assert ADBHandler.screencap_display_arg({"id": 3, "unique_id": "virtual:x"}) == 3


def test_open_screencap_stream_starts_one_exec_out_process(fake_adb, monkeypatch):
    import turboadb.core as core
    from turboadb import ADBConfig

    started = []

    class Popen:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs))

    monkeypatch.setattr(core.subprocess, "Popen", Popen)
    ADBHandler(ADBConfig(serial="ivi")).open_screencap_stream(capture_id=77, max_fps=4)
    (argv, kwargs), = started
    assert argv[argv.index("-s") + 1] == "ivi" and argv[-2] == "exec-out"
    assert "screencap -d 77" in argv[-1] and "sleep 0.25" in argv[-1]
    assert kwargs["stdout"] == core.subprocess.PIPE and kwargs["stderr"] == core.subprocess.PIPE


# --------------------------------------------------------------------------- #
# the worker and the view
# --------------------------------------------------------------------------- #
class _Stdout:
    def __init__(self, chunks, block):
        self.chunks = list(chunks)
        self.block = block
        self.gone = threading.Event()
        self.reading = threading.Event()

    def read(self, _size):
        self.reading.set()
        if self.chunks:
            return self.chunks.pop(0)
        if self.block:
            self.gone.wait(5)
        return b""

    def close(self):
        pass


class FakeProc:
    def __init__(self, chunks=(), *, block=True, stderr=b""):
        self.stdout = _Stdout(chunks, block)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self.kills = 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.kills += 1
        self.returncode = -9
        self.stdout.gone.set()

    def wait(self, timeout=None):
        return self.returncode


class StreamHandler:
    serial = "ivi"
    config = None

    def __init__(self, procs):
        self.procs = list(procs)
        self.opened = []
        self.calls = []

    def open_screencap_stream(self, **kwargs):
        self.opened.append(kwargs)
        return self.procs.pop(0)

    def list_displays(self, method="auto", safe=None):
        return [{"id": 3, "size": "1280x720", "unique_id": "local:555", "physical_id": 555}]

    def tap(self, x, y, **kwargs):
        self.calls.append(("tap", x, y, kwargs))
        return True

    def swipe(self, x1, y1, x2, y2, ms=200, **kwargs):
        self.calls.append(("swipe", x1, y1, x2, y2, ms, kwargs))
        return True

    def keyevent(self, code, **kwargs):
        self.calls.append(("key", code, kwargs))
        return True


class RunNowDispatcher:
    """Runs each command at once, in order, and records it."""

    submitted = 0

    def __init__(self):
        self.order = []

    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        self.submitted += 1
        result = fn()
        self.order.append(result)
        if on_done:
            on_done(result)
        return True

    def stop(self):
        pass


def _wait(qapp, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_stopping_the_view_kills_the_capture_process_and_joins_the_worker(qapp):
    proc = FakeProc([gzip.compress(raw_capture(8, 16))])
    handler = StreamHandler([proc])
    view = sv.ScreencapView(handler, {"id": 0, "size": "8x16"}, dispatcher=RunNowDispatcher())
    try:
        view.resize(200, 200)
        view.show()
        view.start()
        worker = view._worker
        assert _wait(qapp, lambda: view.frame_size() == (8, 16))
        assert view._image is not None and view.streamed_format == "gzip"
        assert handler.opened == [{"capture_id": None, "max_fps": 5, "fmt": "auto", "safe": True}]
        started = time.monotonic()
        view.stop()
        assert time.monotonic() - started < 0.5  # never blocks the UI thread
        assert proc.kills >= 1 and not view.is_running() and view._image is None
        from turboadb.gui.qtutil import thread_running

        assert _wait(qapp, lambda: not thread_running(worker))
        assert view.running_threads() == []
    finally:
        view.close_view()
        view.close()


def test_a_non_default_display_is_captured_by_physical_id_and_touched_by_logical_id(qapp):
    proc = FakeProc([raw_capture(64, 36)])
    handler = StreamHandler([proc])
    dispatcher = RunNowDispatcher()
    view = sv.ScreencapView(handler, {"id": 3, "size": "1280x720"}, dispatcher=dispatcher)
    try:
        view.resize(640, 480)  # 640x360 picture, 60 px bars above and below
        view.show()
        view.start()
        assert _wait(qapp, lambda: view.frame_size() == (64, 36))
        assert handler.opened[0]["capture_id"] == 555  # looked up with list_displays
        view.send_gesture(sv.gesture_for(
            view.device_point(320, 240), view.device_point(320, 240), 50, moved=False
        ))
        view.send_gesture(sv.gesture_for(
            view.device_point(0, 60), view.device_point(700, 470, clamp=True), 300, moved=True
        ))
        assert handler.calls[0] == ("tap", 640, 360, {"safe": True, "display_id": 3})
        assert handler.calls[1] == ("swipe", 0, 0, 1279, 719, 300, {"safe": True, "display_id": 3})
        assert view.device_point(320, 20) is None  # the letterbox is not the screen
    finally:
        view.close_view()
        view.close()


def test_mouse_buttons_and_the_wheel_reach_the_device_in_order(qapp):
    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtTest import QTest

    proc = FakeProc([raw_capture(100, 200)])
    handler = StreamHandler([proc])
    view = sv.ScreencapView(handler, {"id": 0}, dispatcher=RunNowDispatcher())
    try:
        view.resize(100, 200)
        view.show()
        view.start()
        assert _wait(qapp, lambda: view.frame_size() == (100, 200))
        QTest.mouseClick(view, Qt.LeftButton, Qt.NoModifier, QPoint(50, 100))
        QTest.mouseClick(view, Qt.RightButton, Qt.NoModifier, QPoint(50, 100))
        from PyQt5.QtCore import QPointF
        from PyQt5.QtGui import QWheelEvent

        wheel = QWheelEvent(
            QPointF(50, 100), QPointF(50, 100), QPoint(0, 0), QPoint(0, 120),
            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
        )
        view.wheelEvent(wheel)
        kinds = [call[0] for call in handler.calls]
        assert kinds == ["tap", "key", "swipe"]
        assert handler.calls[0][1:3] == (50, 100) and handler.calls[0][3] == {"safe": True}
        assert handler.calls[1][1] == 4  # right click = Back
        assert handler.calls[2][2] == 100 and handler.calls[2][4] > 100  # wheel up drags down
    finally:
        view.close_view()
        view.close()


def test_raw_captures_that_cannot_be_read_fall_back_to_png(qapp):
    bad_gzip = FakeProc([gzip.compress(b"\x01\x00\x00\x00" * 40)], block=False)
    png = FakeProc([png_capture(3, 4)])
    handler = StreamHandler([bad_gzip, png])
    notes = []
    view = sv.ScreencapView(handler, {"id": 0}, dispatcher=RunNowDispatcher())
    view.log.connect(notes.append)
    try:
        view.resize(60, 80)
        view.show()
        view.start()
        assert _wait(qapp, lambda: view.frame_size() == (3, 4))
        assert [o["fmt"] for o in handler.opened] == ["auto", "png"]
        assert bad_gzip.kills >= 1
        assert any("switching to PNG" in note for note in notes)
    finally:
        view.close_view()
        view.close()


def test_a_dead_capture_process_reports_why(qapp):
    proc = FakeProc([], block=False, stderr=b"adb.exe: device 'ivi' not found\n")
    handler = StreamHandler([proc])
    ended = []
    view = sv.ScreencapView(handler, {"id": 0}, dispatcher=RunNowDispatcher())
    view.ended.connect(lambda message, failed: ended.append((message, failed)))
    try:
        view.show()
        view.start()
        assert _wait(qapp, lambda: ended)
        assert ended == [("adb.exe: device 'ivi' not found", True)]
        assert not view.is_running()
    finally:
        view.close_view()
        view.close()


def test_a_hidden_view_stops_reading_and_releases_its_image(qapp):
    proc = FakeProc([raw_capture(10, 10)])
    view = sv.ScreencapView(StreamHandler([proc]), {"id": 0}, dispatcher=RunNowDispatcher())
    try:
        view.resize(50, 50)
        view.show()
        view.start()
        assert _wait(qapp, lambda: view._image is not None)
        view.hide()
        assert view._image is None and not view._worker._resume.is_set()
        view.show()
        assert view._worker._resume.is_set()
    finally:
        view.close_view()
        view.close()


# --------------------------------------------------------------------------- #
# review regressions
# --------------------------------------------------------------------------- #
def test_a_view_started_inside_a_hidden_parent_starts_paused(qapp):
    """Issue: _paused only flipped on show/hide events, which a view inside a
    hidden tab never gets, so a wall tile started as the user switched tabs
    streamed at full rate while hidden."""
    from PyQt5.QtWidgets import QWidget

    parent = QWidget()
    proc = FakeProc([raw_capture(4, 4)] * 5)
    view = sv.ScreencapView(StreamHandler([proc]), {"id": 0}, dispatcher=RunNowDispatcher(),
                            parent=parent)
    try:
        view.show()  # the parent itself is hidden: no show event reaches the view
        view.start()
        assert view._paused and not view._worker._resume.is_set()
        time.sleep(0.3)
        qapp.processEvents()
        assert not proc.stdout.reading.is_set()  # nothing was read while hidden
        parent.show()
        assert _wait(qapp, lambda: view.frame_size() == (4, 4))
    finally:
        view.close_view()
        parent.close()


def test_stopping_during_the_display_lookup_never_blocks_and_never_starts_adb(qapp):
    """Issue: stop() blocked the UI for 1.5 s while the worker waited on
    list_displays, and the parked worker then still opened adb exec-out."""
    from turboadb.gui.qtutil import thread_running

    class SlowLookup(StreamHandler):
        def list_displays(self, method="auto", safe=None):
            time.sleep(0.8)
            return super().list_displays(method, safe)

    handler = SlowLookup([FakeProc([raw_capture(4, 4)])])
    view = sv.ScreencapView(handler, {"id": 3}, dispatcher=RunNowDispatcher())
    try:
        view.show()
        view.start()
        time.sleep(0.1)  # the worker is inside list_displays now
        worker = view._worker
        started = time.monotonic()
        view.close_view()
        assert time.monotonic() - started < 0.3
        assert view.running_threads() == [worker]  # still finishing its lookup
        assert _wait(qapp, lambda: not thread_running(worker), timeout=3)
        assert handler.opened == []  # no adb process after the stop
    finally:
        view.close()


def test_gzip_captures_report_the_devices_own_error_text(qapp):
    """Issue: three empty gzip members ended the stream as "empty captures"
    before the device's error text that follows them was read."""
    marker = ADBHandler.SCREENCAP_ERROR_MARKER
    failing = (
        gzip.compress(b"") * 3
        + marker
        + b" Failed to take take screenshot. Display Id '999' is not valid.\n"
    )
    ended = []
    view = sv.ScreencapView(StreamHandler([FakeProc([failing], block=False)]), {"id": 0},
                            dispatcher=RunNowDispatcher())
    view.ended.connect(lambda message, failed: ended.append((message, failed)))
    try:
        view.show()
        view.start()
        assert _wait(qapp, lambda: ended)
        (message, failed), = ended
        assert failed and "Display Id '999' is not valid" in message
        assert "empty captures" not in message
    finally:
        view.close_view()
        view.close()

    # a shell without pipefail never prints the error: empty captures end it
    ended.clear()
    empties = [gzip.compress(b"")] * (sv.ScreencapWorker.EMPTY_CAPTURE_LIMIT + 1)
    view = sv.ScreencapView(StreamHandler([FakeProc(empties)]), {"id": 0},
                            dispatcher=RunNowDispatcher())
    view.ended.connect(lambda message, failed: ended.append((message, failed)))
    try:
        view.show()
        view.start()
        assert _wait(qapp, lambda: ended)
        assert ended[0][1] and "empty captures" in ended[0][0]
    finally:
        view.close_view()
        view.close()


def test_closing_a_panel_mid_lookup_lists_the_worker_for_shutdown(qapp):
    """The device tab waits for shutdown_threads() before disconnecting the
    handler: a capture worker still finishing a display lookup must be there."""
    from turboadb.gui.mirror_panel import MirrorPanel
    from turboadb.gui.qtutil import thread_running

    class SlowLookup(StreamHandler):
        def list_displays(self, method="auto", safe=None):
            time.sleep(0.8)
            return super().list_displays(method, safe)

    handler = SlowLookup([FakeProc([raw_capture(4, 4)])])
    panel = MirrorPanel(handler, {"name": "ivi"}, dispatcher=RunNowDispatcher())
    try:
        panel.set_screen_backend("screencap")
        panel.start(display_id=2)
        worker = panel.screencap_view()._worker
        time.sleep(0.1)
        started = time.monotonic()
        panel.close_panel()
        assert time.monotonic() - started < 0.3  # the UI thread is not held up
        assert worker in panel.shutdown_threads()
        assert _wait(qapp, lambda: not thread_running(worker), timeout=3)
        assert handler.opened == []  # adb never started for the closed panel
    finally:
        panel.close()


# --------------------------------------------------------------------------- #
# the frame-rate indicator
# --------------------------------------------------------------------------- #
def _solid(width, height, colour):
    from PyQt5.QtGui import QColor, QImage

    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor(colour))
    return image


def test_the_frame_rate_is_never_painted_over_the_device_picture(qapp):
    """Issue: a "screencap · 4.5 fps" chip sat over the bottom-left corner of
    the phone picture."""
    from PyQt5.QtGui import QColor

    view = sv.ScreencapView(StreamHandler([]), {"id": 0}, dispatcher=RunNowDispatcher())
    try:
        view.resize(120, 240)
        view._image = _solid(100, 200, "#3366aa")  # fills the whole view
        view._frame_size = (1080, 2400)
        view._fps = 4.5
        assert view.renderer_text() == "screencap · 4.5 fps"
        shot = view.grab().toImage()
        left, top, width, height = (int(v) for v in sv.fit_rect(100, 200, 120, 240))
        expected = QColor("#3366aa").rgb()
        for y in range(top, top + height, 3):
            for x in range(left, left + width, 3):
                assert shot.pixel(x, y) == expected, (x, y)
    finally:
        view.close_view()
        view.close()


def test_the_view_reports_frame_rate_changes_for_its_owner(qapp):
    view = sv.ScreencapView(StreamHandler([]), {"id": 0}, dispatcher=RunNowDispatcher())
    changes = []
    view.stats_changed.connect(lambda: changes.append(view.renderer_text()))
    try:
        assert view.renderer_text() == "" and view.fps() == 0.0

        class Worker:
            frames = 0
            kind = "raw"
            last_frame_at = 0.0

            def take_frame(self):
                return _solid(10, 20, "#000000"), (10, 20)

        worker = Worker()
        view._worker = worker
        view._first = False
        view._fps_mark = (time.monotonic() - 2.5, 0)
        worker.frames = 10
        view._on_frame()
        assert changes == ["screencap · 4.0 fps"]
        view._fps_mark = (time.monotonic() - 2.5, 10)
        worker.frames = 20
        view._on_frame()  # the same rate: nothing to repaint beside the screen
        assert changes == ["screencap · 4.0 fps"]
    finally:
        view._worker = None
        view.close_view()
        view.close()


# --------------------------------------------------------------------------- #
# a chatty stderr must never stall the capture stream
# --------------------------------------------------------------------------- #
def test_a_chatty_stderr_never_blocks_the_frame_stream():
    """A long-lived ``adb exec-out`` writes to stderr while it streams frames.

    Nothing read that pipe until the stdout loop ended, so once the OS pipe
    buffer (~64 KB) filled, adb blocked on the write, stopped producing frames,
    and the live view froze for good with no error. The background drain is
    what keeps stdout flowing."""
    import subprocess
    import sys

    # writes far more than any pipe buffer to stderr BEFORE its first frame
    code = (
        "import sys\n"
        "sys.stderr.buffer.write(b'e' * 300000)\n"
        "sys.stderr.buffer.flush()\n"
        "sys.stdout.buffer.write(b'FRAME')\n"
        "sys.stdout.buffer.flush()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    ADBHandler._drain_stderr(proc)
    got = []
    reader = threading.Thread(target=lambda: got.append(proc.stdout.read(5)), daemon=True)
    reader.start()
    reader.join(timeout=30)
    try:
        assert not reader.is_alive(), "the frame stream blocked on a full stderr pipe"
        assert got == [b"FRAME"]
        proc.wait(timeout=10)
        stderr = ADBHandler.collected_stderr(proc)
        assert stderr.startswith(b"eee")                      # it was captured
        assert len(stderr) <= ADBHandler._STDERR_KEEP_BYTES   # and it is bounded
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_stderr_is_still_read_for_a_process_nobody_drained():
    """A caller that builds its own Popen keeps the old behaviour."""
    proc = type("P", (), {"stderr": io.BytesIO(b"adb: device offline")})()
    assert ADBHandler.collected_stderr(proc) == b"adb: device offline"
    assert ADBHandler.collected_stderr(type("P", (), {})()) == b""
