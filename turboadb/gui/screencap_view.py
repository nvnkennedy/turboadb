"""Show a device screen from ``adb screencap`` frames, with touch and keys.

The fallback renderer for builds where scrcpy does not work (Settings →
Screen renderer → ADB screencap, or a screen's own Options). It is slower than
scrcpy's video but needs nothing except adb:

* ONE long-lived ``adb exec-out`` process per screen runs a device-side
  capture loop (:meth:`ADBHandler.open_screencap_stream`). A worker thread
  splits its output into frames: raw captures piped through ``gzip -1`` when
  the device has gzip, else PNG captures (faster than plain raw ones over
  USB), and PNG as the fallback whenever raw captures cannot be read.
* Only the newest frame is converted and scaled to the view, on the worker
  thread; frames never queue up. A hidden view stops reading (the device loop
  then waits on its pipe) and drops its image.
* Mouse and keys become ``input -d <display>`` taps, swipes and key events,
  run in order by the device command dispatcher.

The stream parsers and the input mapping at the top of this module never touch
Qt, so they are unit-tested directly.
"""

from __future__ import annotations

import re
import struct
import threading
import time
import zlib
from collections import namedtuple

from PyQt5.QtCore import QEvent, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter
from PyQt5.QtWidgets import QWidget

from ..core import ADBHandler
from ..results import OperationResult
from .qtutil import park_thread

# --------------------------------------------------------------------------- #
# Stream parsing (no Qt)
# --------------------------------------------------------------------------- #
RawFrame = namedtuple("RawFrame", "width height pixel_format pixels")
PngFrame = namedtuple("PngFrame", "data")

# Android PixelFormat values raw ``screencap`` writes, and bytes per pixel:
# RGBA_8888, RGBX_8888, RGB_888, RGB_565, BGRA_8888.
RAW_BYTES_PER_PIXEL = {1: 4, 2: 4, 3: 3, 4: 2, 5: 4}
MAX_EDGE = 8192
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
GZIP_MAGIC = b"\x1f\x8b\x08"
ERROR_MARKER = ADBHandler.SCREENCAP_ERROR_MARKER

# Candidate raw headers: width and height below 8448 and a known pixel format.
_RAW_HEADER_RE = re.compile(
    rb".[\x00-\x20]\x00\x00.[\x00-\x20]\x00\x00[\x01-\x05]\x00\x00\x00", re.S
)


class StreamFormatError(ValueError):
    """The capture stream is not in the format its parser reads."""


def raw_header(buf, offset=0):
    """``(width, height, pixel_format)`` of a plausible raw screencap header at
    *offset*, else None."""
    if len(buf) - offset < 12:
        return None
    width, height, pixel_format = struct.unpack_from("<III", buf, offset)
    if not (0 < width <= MAX_EDGE and 0 < height <= MAX_EDGE):
        return None
    if pixel_format not in RAW_BYTES_PER_PIXEL:
        return None
    return width, height, pixel_format


def _find_raw_header(buf, start=0):
    """Offset of the first plausible raw header at or after *start*, or None.

    No match object outlives the search: one holds a view of a bytearray
    buffer, which could then no longer be resized."""
    pos = start
    while True:
        match = _RAW_HEADER_RE.search(buf, pos)
        if match is None:
            return None
        index = match.start()
        del match
        if raw_header(buf, index) is not None:
            return index
        pos = index + 1


def raw_frame_from_block(block) -> RawFrame:
    """One complete raw screencap output (a gzip member) as a frame.

    Its length says exactly how long the header is: 12 bytes, or 16 on
    Android 10+, which adds a colour space. The pixels are a view of *block*
    (a 1080x2400 capture is 10 MB: it is never copied)."""
    header = raw_header(block)
    if header is None:
        raise StreamFormatError("the capture is not a raw screencap frame")
    width, height, pixel_format = header
    payload = width * height * RAW_BYTES_PER_PIXEL[pixel_format]
    size = len(block) - payload
    if size not in (12, 16):
        raise StreamFormatError(
            f"a {width}x{height} capture of {len(block)} bytes does not match its header"
        )
    return RawFrame(width, height, pixel_format, memoryview(block)[size:])


class RawStreamParser:
    """Frames from back-to-back raw ``screencap`` outputs (no ``-p``).

    Each output is a little-endian header (width, height, pixel format, and on
    Android 10+ a colour space) followed by the pixels. The header size is
    exact once the next header is buffered; for the very first frame it is
    guessed from the colour-space field (0-2 on builds that write one), so the
    screen shows at once, and a wrong guess is corrected on the second frame.
    Bytes that are not a frame (shell noise) are skipped up to the next header;
    frames that keep failing to line up raise :class:`StreamFormatError`.
    """

    MAX_SKIP = 1 << 20  # noise tolerated between two frames
    MAX_MISMATCHES = 3

    def __init__(self, header_size=None):
        self._buf = bytearray()
        self.header_size = header_size
        self.confirmed = header_size is not None
        self.skipped = 0
        self.mismatches = 0
        self._run = 0
        self._since_mismatch = 0
        self._shape = None
        self._tail = b""

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def feed(self, data) -> list:
        if data:
            self._buf += data
        frames = []
        buf = self._buf
        while len(buf) >= 16:
            header = raw_header(buf)
            if self._shape is not None and not self.confirmed:
                header = self._confirm(header)
            if header is None:
                if self.confirmed and self._shape is not None:
                    self.mismatches += 1
                    self._since_mismatch = 0
                    if self.mismatches > self.MAX_MISMATCHES:
                        raise StreamFormatError(
                            "raw captures stopped lining up with their headers"
                        )
                if not self._resync():
                    break
                continue
            width, height, pixel_format = header
            payload = width * height * RAW_BYTES_PER_PIXEL[pixel_format]
            size = self.header_size
            if size is None:
                size = self._guess_header_size(buf, header, payload)
            end = size + payload
            if len(buf) < end:
                break
            with memoryview(buf) as view:  # one copy (a bytearray slice is another)
                pixels = bytes(view[size:end])
            frames.append(RawFrame(width, height, pixel_format, pixels))
            self._tail = bytes(buf[end - 4:end])
            del buf[:end]
            self.header_size = size
            self._shape = header
            self._run = 0
            self._since_mismatch += 1
            if self._since_mismatch >= 3:  # lined up again for a while
                self.mismatches = 0
        return frames

    def _guess_header_size(self, buf, header, payload) -> int:
        for size in (16, 12):
            if raw_header(buf, size + payload) == header:
                self.confirmed = True
                return size
        colour_space = struct.unpack_from("<I", buf, 12)[0]
        return 16 if colour_space in (0, 1, 2) else 12

    def _confirm(self, header):
        """Check the guessed header size where the second header starts."""
        if header is not None:
            self.confirmed = True
            return header
        if self.header_size == 16:
            # really 12: the first 4 bytes of this header went with frame 1
            fixed = raw_header(self._tail + bytes(self._buf[:8]))
            if fixed is not None:
                self._buf[:0] = self._tail
                self.header_size, self.confirmed = 12, True
                return fixed
        elif self.header_size == 12:
            # really 16: 4 bytes of frame 1 are still in front of this header
            fixed = raw_header(self._buf, 4)
            if fixed is not None:
                del self._buf[:4]
                self.header_size, self.confirmed = 16, True
                return fixed
        return None

    def _resync(self) -> bool:
        """Skip noise up to the next plausible header; False = need more data."""
        buf = self._buf
        if self._shape is not None:
            width, height, pixel_format = self._shape
            for shape in ((width, height), (height, width)):  # also a rotation
                index = buf.find(struct.pack("<III", shape[0], shape[1], pixel_format), 1)
                if index > 0:
                    self._skip(index)
                    return True
        index = _find_raw_header(buf, 1)
        if index is not None:
            self._skip(index)
            return True
        self._skip(max(0, len(buf) - 11))  # the next header may still be arriving
        return False

    def _skip(self, count) -> None:
        if count <= 0:
            return
        del self._buf[:count]
        self.skipped += count
        self._run += count
        if self._run > self.MAX_SKIP:
            raise StreamFormatError("no raw screencap frame found in the stream")


class PngStreamSplitter:
    """Complete PNG files out of back-to-back ``screencap -p`` outputs.

    It walks the PNG chunk structure and cuts after each ``IEND`` chunk, so
    compressed data that happens to contain the bytes ``IEND`` never splits a
    frame; noise before a PNG signature is skipped.
    """

    MAX_CHUNK = 1 << 28
    MAX_SKIP = 1 << 20

    def __init__(self):
        self._buf = bytearray()
        self._pos = 0
        self._run = 0
        self.skipped = 0

    def feed(self, data) -> list:
        if data:
            self._buf += data
        frames = []
        buf = self._buf
        while True:
            if self._pos == 0:
                if len(buf) < 8:
                    break
                if bytes(buf[:8]) != PNG_SIGNATURE:
                    index = buf.find(PNG_SIGNATURE, 1)
                    self._skip(index if index > 0 else max(0, len(buf) - 7))
                    if index <= 0:
                        break
                    continue
                self._pos = 8
            pos = self._pos
            if len(buf) < pos + 8:
                break
            length = int.from_bytes(buf[pos:pos + 4], "big")
            kind = bytes(buf[pos + 4:pos + 8])
            if length > self.MAX_CHUNK or not kind.isalpha():
                self._pos = 0
                self._skip(1)  # not a real PNG: look for the next signature
                continue
            end = pos + 12 + length
            if kind == b"IEND":
                if len(buf) < end:
                    break
                frames.append(bytes(buf[:end]))
                del buf[:end]
                self._pos = 0
                self._run = 0
                continue
            self._pos = end
            if len(buf) < end:
                break
        return frames

    def _skip(self, count) -> None:
        if count <= 0:
            return
        del self._buf[:count]
        self.skipped += count
        self._run += count
        if self._run > self.MAX_SKIP:
            raise StreamFormatError("no PNG capture found in the stream")


class GzipStreamSplitter:
    """The decompressed content of back-to-back gzip members, one per capture.

    A failed capture still produces a (tiny, empty) member: it comes out
    empty. Each member is decompressed straight into one buffer sized from the
    raw header, so a 10 MB capture is held once, not as pieces plus a join."""

    MAX_OUTPUT = MAX_EDGE * MAX_EDGE * 4 + 16

    def __init__(self):
        self.skipped = 0
        self._pending = b""
        self._reset()

    def _reset(self):
        self._z = zlib.decompressobj(31)
        self._out = None
        self._head = b""
        self._size = 0
        self._started = False

    def _append(self, data) -> None:
        if not data:
            return
        if self._out is None:
            self._head += data
            if len(self._head) < 12:
                return
            header = raw_header(self._head)
            capacity = len(self._head)
            if header is not None:
                width, height, pixel_format = header
                capacity = max(capacity, 16 + width * height * RAW_BYTES_PER_PIXEL[pixel_format])
            self._out = bytearray(capacity)
            data, self._head = self._head, b""
        end = self._size + len(data)
        if end > self.MAX_OUTPUT:
            raise StreamFormatError("a compressed capture is larger than any screen")
        if end <= len(self._out):
            self._out[self._size:end] = data
        else:
            del self._out[self._size:]
            self._out += data
        self._size = end

    def _finish(self):
        if self._out is None:
            return bytearray(self._head)
        out = self._out
        del out[self._size:]  # shrinks in place
        return out

    def feed(self, data) -> list:
        frames = []
        data = self._pending + bytes(data or b"")
        self._pending = b""
        while data:
            if not self._started:
                if len(data) < 3:
                    self._pending = data
                    break
                if data[:3] != GZIP_MAGIC:
                    index = data.find(GZIP_MAGIC, 1)
                    if index < 0:
                        self.skipped += len(data) - 2
                        self._pending = data[-2:]
                        break
                    self.skipped += index
                    data = data[index:]
                    continue
                self._started = True
            try:
                self._append(self._z.decompress(data))
            except zlib.error as exc:
                raise StreamFormatError(f"a compressed capture is corrupt ({exc})") from exc
            if not self._z.eof:
                break
            frames.append(self._finish())
            data = self._z.unused_data
            self._reset()
        return frames


class ScreencapStreamDecoder:
    """Frames out of one screencap stream, whatever format the device sends.

    The format is recognised from the first bytes (gzip, PNG or a raw header),
    after any leading shell noise. :meth:`feed` returns the NEWEST complete
    frame of the data so far (:class:`RawFrame` or :class:`PngFrame`) or None;
    older frames completed by the same call are only counted in
    :attr:`dropped`. Empty captures count in :attr:`empty_frames`. When the
    device loop gives up, the text after :data:`ERROR_MARKER` collects in
    :attr:`device_error`.
    """

    SNIFF_LIMIT = 1 << 20

    def __init__(self):
        self.kind = None
        self.device_error = None
        self.dropped = 0
        self.empty_frames = 0
        self.frames = 0
        self._splitter = None
        self._sniff = bytearray()
        self._tail = b""

    def feed(self, data):
        data = bytes(data or b"")
        if self.device_error is not None:
            self._add_error_text(data)
            return None
        marker = ERROR_MARKER
        index = data.find(marker)
        if index < 0:
            index = None
            if self._tail:
                found = (self._tail + data[: len(marker) - 1]).find(marker)
                if found >= 0:
                    index = found - len(self._tail)  # it began in the previous read
        self._tail = data[-(len(marker) - 1):]
        if index is None:
            return self._feed_frames(data)
        newest = self._feed_frames(data[:index]) if index > 0 else None
        self.device_error = ""
        self._add_error_text(data[index + len(marker):])
        return newest

    def _add_error_text(self, data) -> None:
        if len(self.device_error) < 2000:
            text = data.decode("utf-8", "replace")
            self.device_error = (self.device_error + text)[:2000]

    def _feed_frames(self, data):
        if self._splitter is None:
            self._sniff += data
            kind, start = self._detect(self._sniff)
            if kind is None:
                if len(self._sniff) > self.SNIFF_LIMIT:
                    raise StreamFormatError("the device did not send screen captures")
                return None
            self.kind = kind
            self._splitter = {
                "gzip": GzipStreamSplitter,
                "png": PngStreamSplitter,
                "raw": RawStreamParser,
            }[kind]()
            data, self._sniff = bytes(self._sniff[start:]), bytearray()
        newest = None
        for item in self._splitter.feed(data):
            if self.kind == "gzip" and not item:
                self.empty_frames += 1
                continue
            if newest is not None:
                self.dropped += 1
            newest = item
            self.frames += 1
            self.empty_frames = 0
        if newest is None:
            return None
        if self.kind == "gzip":
            return raw_frame_from_block(newest)
        if self.kind == "png":
            return PngFrame(newest)
        return newest

    @staticmethod
    def _detect(buf):
        """``(kind, start)`` once the stream's format is recognisable."""
        if len(buf) < 16:
            return None, 0
        positions = []
        for kind, magic in (("gzip", GZIP_MAGIC), ("png", PNG_SIGNATURE)):
            index = buf.find(magic)
            if index >= 0:
                positions.append((index, kind))
        index = _find_raw_header(buf)
        if index is not None:
            positions.append((index, "raw"))
        if not positions:
            return None, 0
        start, kind = min(positions)
        if len(buf) - start < 16:
            return None, 0
        return kind, start


def png_size(data):
    """``(width, height)`` from a PNG's IHDR chunk, or None."""
    if len(data) >= 24 and data[:8] == PNG_SIGNATURE and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


# --------------------------------------------------------------------------- #
# Geometry and input mapping (no Qt)
# --------------------------------------------------------------------------- #
def fit_rect(frame_w, frame_h, area_w, area_h):
    """``(x, y, w, h)`` of a *frame_w* x *frame_h* image fitted into an
    *area_w* x *area_h* area with its aspect kept, centred (letterboxed)."""
    if frame_w <= 0 or frame_h <= 0 or area_w <= 0 or area_h <= 0:
        return 0.0, 0.0, 0.0, 0.0
    scale = min(area_w / float(frame_w), area_h / float(frame_h))
    width, height = frame_w * scale, frame_h * scale
    return (area_w - width) / 2.0, (area_h - height) / 2.0, width, height


def input_size(frame_w, frame_h, display_size=None):
    """The coordinate space ``input tap`` uses for a *frame_w* x *frame_h*
    capture: the display's logical size, turned like the capture (a device may
    capture at another resolution than apps and input use), else the capture
    size itself."""
    if display_size:
        width, height = display_size
        if width > 0 and height > 0:
            if (width >= height) != (frame_w >= frame_h):
                width, height = height, width
            return width, height
    return frame_w, frame_h


def map_to_device(x, y, area_w, area_h, frame_w, frame_h, display_size=None, *, clamp=False):
    """Device input coordinates of widget point (*x*, *y*), or None on the
    letterbox — unless *clamp*, which pulls the point onto the picture (the
    end of a swipe that left it)."""
    left, top, width, height = fit_rect(frame_w, frame_h, area_w, area_h)
    if width <= 0 or height <= 0:
        return None
    u, v = (x - left) / width, (y - top) / height
    if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
        if not clamp:
            return None
        u, v = min(1.0, max(0.0, u)), min(1.0, max(0.0, v))
    in_w, in_h = input_size(frame_w, frame_h, display_size)
    return min(in_w - 1, int(u * in_w)), min(in_h - 1, int(v * in_h))


def gesture_for(press, release, held_ms, *, moved, long_press_ms=500):
    """What one mouse press + release does on the device.

    ``("tap", x, y)``; a long press (*held_ms* >= *long_press_ms* without
    moving) is a swipe on the same point for that long; a drag is
    ``("swipe", x1, y1, x2, y2, ms)``. *press* / *release* are device points."""
    (x1, y1), (x2, y2) = press, release
    held = max(0, int(held_ms))
    if not moved:
        if held >= long_press_ms:
            return ("swipe", x1, y1, x1, y1, min(10000, held))
        return ("tap", x1, y1)
    return ("swipe", x1, y1, x2, y2, min(3000, max(80, held)))


def wheel_swipe(point, delta_x, delta_y, size, *, fraction=0.18, ms=150):
    """A short swipe for one mouse-wheel step at device *point*, or None.

    Wheel up reveals the content above, as a finger dragging down does."""
    if not delta_x and not delta_y:
        return None
    x, y = point
    width, height = size
    if abs(delta_y) >= abs(delta_x):
        step = int(height * fraction) * (1 if delta_y > 0 else -1)
        x2, y2 = x, min(height - 1, max(0, y + step))
    else:
        step = int(width * fraction) * (1 if delta_x > 0 else -1)
        x2, y2 = min(width - 1, max(0, x + step)), y
    if (x2, y2) == (x, y):
        return None
    return ("swipe", x, y, x2, y2, ms)


# --------------------------------------------------------------------------- #
# Worker thread
# --------------------------------------------------------------------------- #
_QIMAGE_FORMATS = {
    1: QImage.Format_RGBX8888,  # screencap alpha is often 0; a screen is opaque
    2: QImage.Format_RGBX8888,
    3: QImage.Format_RGB888,
    4: QImage.Format_RGB16,
    5: QImage.Format_RGB32,
}


def _voidptr(buffer):
    try:
        from PyQt5 import sip
    except ImportError:  # pragma: no cover - very old PyQt5
        import sip
    return sip.voidptr(buffer)


def frame_to_image(frame, target=None):
    """A detached ``Format_RGB32`` QImage of *frame*, smoothly scaled down to
    fit *target* ``(w, h)`` device pixels. Safe off the UI thread."""
    if isinstance(frame, PngFrame):
        image = QImage.fromData(frame.data, "PNG")
        if image.isNull():
            raise StreamFormatError("a PNG capture could not be decoded")
    else:
        bpp = RAW_BYTES_PER_PIXEL[frame.pixel_format]
        pixels = frame.pixels
        if len(pixels) < frame.width * frame.height * bpp:
            raise StreamFormatError("a raw capture is shorter than its header says")
        if not isinstance(pixels, bytes):
            pixels = _voidptr(pixels)  # a view into the capture buffer: no copy
        image = QImage(
            pixels, frame.width, frame.height, frame.width * bpp,
            _QIMAGE_FORMATS[frame.pixel_format],
        )
    if target and target[0] > 0 and target[1] > 0 and (
        image.width() > target[0] or image.height() > target[1]
    ):
        image = image.scaled(target[0], target[1], Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if image.format() == QImage.Format_RGB32:
        return image.copy()  # never keep pointing into the Python buffer
    return image.convertToFormat(QImage.Format_RGB32)


def _kill(proc, wait=True) -> None:
    """End a capture process (a finished one is left alone)."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    if wait:
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass


class ScreencapWorker(QThread):
    """Runs one capture stream: reads, splits and converts the newest frame.

    ``frame_ready`` is emitted only once the UI has taken the previous frame,
    so a busy UI never builds a queue and :meth:`take_frame` always returns the
    newest. :meth:`stop` kills the adb process, which unblocks the read; it is
    safe from any thread. ``ended(message, failed)`` reports the end.
    """

    frame_ready = pyqtSignal()
    note = pyqtSignal(str)
    ended = pyqtSignal(str, bool)

    READ_SIZE = 1 << 20
    # Empty gzip members are failed captures. The device loop prints its error
    # text after three in a row and exits, so reading goes on to that text; a
    # shell without pipefail never gets there, so give up after this many.
    EMPTY_CAPTURE_LIMIT = 10

    def __init__(self, handler, *, display=None, max_fps=5.0, fmt="auto"):
        super().__init__()
        self.handler = handler
        self.display = dict(display or {})
        self.max_fps = max(0.2, float(max_fps or 5.0))
        self.fmt = fmt
        self.kind = None  # the format actually streamed
        self.frames = 0
        self.last_frame_at = 0.0
        self.target = None
        self._lock = threading.Lock()
        self._latest = None
        self._proc = None
        self._stopping = False
        self._resume = threading.Event()
        self._resume.set()

    # ---- from the UI thread ----
    def set_target_size(self, width, height) -> None:
        self.target = (int(width), int(height))

    def set_paused(self, paused) -> None:
        if paused:
            self._resume.clear()
        else:
            self._resume.set()

    def take_frame(self):
        with self._lock:
            latest, self._latest = self._latest, None
        return latest

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            proc = self._proc
        self._resume.set()
        _kill(proc, wait=False)

    @property
    def stopping(self) -> bool:
        return self._stopping

    # ---- the thread ----
    def run(self):
        try:
            capture_id = self._capture_id()
        except Exception as exc:
            if not self._stopping:
                self.ended.emit(
                    f"could not look up display {self.display.get('id')}: {exc}", True
                )
            return
        if self._stopping:  # stopped while the display was being looked up
            self.ended.emit("", False)
            return
        fmt = self.fmt
        while True:
            outcome, message = self._stream(capture_id, fmt)
            if outcome == "fallback" and fmt != "png" and not self._stopping:
                self.note.emit(
                    f"[INFO] screencap: raw captures could not be read ({message}); "
                    "switching to PNG captures"
                )
                fmt = "png"
                continue
            self.ended.emit("" if self._stopping else message, outcome == "failed")
            return

    def _capture_id(self):
        display = self.display
        if int(display.get("id", 0) or 0) == 0:
            return None
        if "physical_id" not in display and "unique_id" not in display:
            # not from a scan that knows physical ids: ask Android now
            listing = getattr(self.handler, "list_displays", None)
            if listing is not None:
                found = listing(method="adb", safe=True)
                if isinstance(found, OperationResult):
                    found = found.value if found.success else []
                for entry in found or []:
                    if int(entry.get("id", -1)) == int(display["id"]):
                        display.update(entry)
                        break
        return ADBHandler.screencap_display_arg(display)

    def _stream(self, capture_id, fmt):
        if self._stopping:  # never start adb for a view that has already stopped
            return "stopped", ""
        opened = self.handler.open_screencap_stream(
            capture_id=capture_id, max_fps=self.max_fps, fmt=fmt, safe=True
        )
        if isinstance(opened, OperationResult):
            if not opened.success:
                return "failed", f"adb could not start the capture: {opened.error}"
            opened = opened.value
        proc = opened
        with self._lock:
            self._proc = proc
            stopping = self._stopping
        if stopping:
            _kill(proc)
            return "stopped", ""
        decoder = ScreencapStreamDecoder()
        try:
            while not self._stopping:
                while not self._resume.wait(0.25):
                    pass  # paused: not reading makes the device loop wait too
                if self._stopping:
                    break
                chunk = proc.stdout.read(self.READ_SIZE)
                if not chunk:
                    break
                try:
                    frame = decoder.feed(chunk)
                    self.kind = decoder.kind
                    if decoder.device_error is not None:
                        continue  # the loop is ending: read its error text to the end
                    if decoder.empty_frames >= self.EMPTY_CAPTURE_LIMIT:
                        return "failed", self._hint("the device returned empty captures")
                    if frame is None:
                        continue
                    image = frame_to_image(frame, self.target)
                except StreamFormatError as exc:
                    if fmt == "png" or decoder.kind == "png":
                        return "failed", f"the screen captures could not be read: {exc}"
                    return "fallback", str(exc)
                if isinstance(frame, RawFrame):
                    size = (frame.width, frame.height)
                else:
                    size = png_size(frame.data) or (image.width(), image.height())
                frame = None  # release the full-size capture before the next one
                self._publish(image, size)
                image = None
        finally:
            _kill(proc)
        if self._stopping:
            _close_pipes(proc)
            return "stopped", ""
        if decoder.device_error is not None:
            _close_pipes(proc)
            detail = " ".join(decoder.device_error.split())[:300] or "screencap failed"
            return "failed", self._hint(f"screencap failed on the device: {detail}")
        errors = b""
        try:
            errors = proc.stderr.read() or b""
        except Exception:
            pass
        _close_pipes(proc)
        detail = " ".join(errors.decode("utf-8", "replace").split())[:300]
        return "failed", detail or f"the capture ended (adb exit code {proc.poll()})"

    def _hint(self, message) -> str:
        unique = str(self.display.get("unique_id") or "")
        if unique.startswith("virtual:"):
            return (
                f"{message}. Display {self.display.get('id')} is a virtual display; "
                "adb screencap captures only physical displays on most builds, so "
                "show it with scrcpy instead"
            )
        return message

    def _publish(self, image, size) -> None:
        with self._lock:
            fresh = self._latest is None
            self._latest = (image, tuple(size))
            self.frames += 1
            self.last_frame_at = time.monotonic()
        if fresh:
            self.frame_ready.emit()


def _close_pipes(proc) -> None:
    for name in ("stdout", "stderr"):
        pipe = getattr(proc, name, None)
        try:
            if pipe is not None:
                pipe.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# The view
# --------------------------------------------------------------------------- #
class ScreencapView(QWidget):
    """A live device screen drawn from screencap frames, with touch and keys.

    *display* is an entry from ``list_displays`` (``{"id": 0}`` is the default
    display): input goes to its logical id, the capture uses its physical id.
    Keys go through *key_sink* ``(kind, payload, auto_repeat=)`` and
    *key_release* ``(code)`` when given (the owning MirrorPanel's batcher),
    else through a batcher of its own. Commands run in order on *dispatcher*.
    """

    log = pyqtSignal(str)
    first_frame = pyqtSignal()
    frame_shape_changed = pyqtSignal()
    ended = pyqtSignal(str, bool)
    # The measured frame rate changed (see renderer_text): the owner shows it
    # beside the screen, never painted over the device picture.
    stats_changed = pyqtSignal()

    DEFAULT_MAX_FPS = 5
    STALL_TIMEOUT_S = 30.0  # no capture for this long (while shown) = failed
    TAP_SLOP = 8  # widget pixels a press may move and still be a tap
    LONG_PRESS_MS = 500
    WHEEL_SWIPE_MS = 150
    _KEY_BACK = 4
    _KEY_HOME = 3

    def __init__(
        self,
        handler,
        display=None,
        *,
        dispatcher=None,
        max_fps=DEFAULT_MAX_FPS,
        fmt="auto",
        key_sink=None,
        key_release=None,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("screencapView")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.handler = handler
        self.display = dict(display or {"id": 0})
        self.max_fps = max_fps
        self.fmt = fmt
        self._owns_dispatcher = dispatcher is None
        if dispatcher is None:
            from .device_commands import DeviceCommandDispatcher

            dispatcher = DeviceCommandDispatcher()
        self._dispatcher = dispatcher
        self._key_sink = key_sink
        self._key_release = key_release
        self._batcher = None
        self._worker = None
        self._image = None
        self._frame_size = None
        self._message = "Screen capture is off."
        self._press = None
        self._wheel_busy = False
        self._first = False
        self._paused = False
        self._fps = 0.0
        self._fps_mark = (0.0, 0)
        self._live_since = 0.0
        self._closed = False
        self._retired = []  # stopped workers that may still be finishing
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._check_stream)

    # ---- state ----
    @property
    def display_id(self) -> int:
        return int(self.display.get("id", 0) or 0)

    @property
    def input_display_id(self):
        """The ``input -d`` id: None for the default display."""
        return self.display_id or None

    def frame_size(self):
        """``(width, height)`` of the device's captures, once one arrived."""
        return self._frame_size

    def aspect(self):
        size = self._frame_size
        return size[0] / float(size[1]) if size and size[1] else None

    def is_running(self) -> bool:
        return self._worker is not None

    def fps(self) -> float:
        """Captures shown per second, measured over the last couple of seconds."""
        return float(self._fps)

    def renderer_text(self) -> str:
        """``"screencap · 4.5 fps"`` once a rate is measured, else ``""``."""
        return f"screencap · {self._fps:.1f} fps" if self._fps > 0 else ""

    @property
    def streamed_format(self):
        worker = self._worker
        return getattr(worker, "kind", None) if worker is not None else None

    # ---- lifecycle ----
    def start(self) -> None:
        if self._closed:
            return
        self.stop()
        self._message = "Starting screen capture…"
        self._first = True
        if self._fps:
            self._fps = 0.0
            self.stats_changed.emit()
        worker = ScreencapWorker(
            self.handler, display=self.display, max_fps=self.max_fps, fmt=self.fmt
        )
        self._worker = worker
        self._sync_target()
        worker.frame_ready.connect(self._on_frame)
        worker.note.connect(self.log)
        worker.ended.connect(lambda message, failed, w=worker: self._on_ended(w, message, failed))
        # show/hide events only reach a view whose parents are shown: one
        # started inside a hidden tab must begin paused
        self._paused = not self.isVisible()
        worker.set_paused(self._paused)
        now = time.monotonic()
        self._live_since = now
        self._fps_mark = (now, 0)
        worker.start()
        self._watchdog.start()
        self.update()

    def stop(self, wait_ms: int = 0) -> None:
        """Kill the capture process and let the worker finish on its own.

        Never blocks the UI thread by default: the worker may be inside a
        display lookup that cannot be interrupted, and it opens no adb process
        once stopped. It stays parked (and listed by :meth:`running_threads`)
        until it has finished."""
        self._watchdog.stop()
        worker, self._worker = self._worker, None
        if worker is None:
            return
        for signal in (worker.frame_ready, worker.note, worker.ended):
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
        worker.stop()
        try:
            if wait_ms and worker.wait(int(wait_ms)):
                return
        except RuntimeError:
            return
        park_thread(worker)
        self._retired.append(worker)
        self._image = None

    def running_threads(self) -> list:
        """Capture workers still running, including stopped ones finishing up."""
        alive = []
        for worker in [self._worker] + self._retired:
            try:
                if worker is not None and worker.isRunning():
                    alive.append(worker)
            except RuntimeError:  # finished and already deleted
                pass
        self._retired = [worker for worker in alive if worker is not self._worker]
        return alive

    def close_view(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop()
        if self._batcher is not None:
            self._batcher.stop()
        if self._owns_dispatcher:
            self._dispatcher.stop()
            park_thread(self._dispatcher)

    def set_paused(self, paused: bool) -> None:
        """A hidden view stops reading (the device loop then waits) and lets
        its image go; shown again, it resumes with the next capture."""
        paused = bool(paused)
        was, self._paused = self._paused, paused
        worker = self._worker
        if worker is not None:
            worker.set_paused(paused)
            if paused:
                worker.take_frame()
        if paused:
            self._image = None
        elif was:
            self._live_since = time.monotonic()

    def showEvent(self, event):
        super().showEvent(event)
        self.set_paused(False)
        self._sync_target()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.set_paused(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_target()

    def _sync_target(self) -> None:
        worker = self._worker
        if worker is None:
            return
        ratio = float(self.devicePixelRatioF() or 1.0)
        worker.set_target_size(
            max(1, int(round(self.width() * ratio))), max(1, int(round(self.height() * ratio)))
        )

    # ---- frames ----
    def _on_frame(self) -> None:
        worker = self._worker
        if worker is None:
            return
        latest = worker.take_frame()
        if latest is None:
            return
        image, size = latest
        # never keep an image nobody sees (the shape and "first frame" still count)
        self._image = image if (self.isVisible() and not self._paused) else None
        if size != self._frame_size:
            self._frame_size = size
            self.frame_shape_changed.emit()
        now = time.monotonic()
        start, count = self._fps_mark
        if now - start >= 2.0:
            fps = (worker.frames - count) / (now - start)
            self._fps_mark = (now, worker.frames)
            if round(fps, 1) != round(self._fps, 1):
                self._fps = fps
                self.stats_changed.emit()
        if self._first:
            self._first = False
            self._message = ""
            kind = {"gzip": "compressed raw", "raw": "raw", "png": "PNG"}.get(worker.kind, "")
            self.log.emit(
                f"[OK] screencap: display {self.display_id} is live"
                + (f" ({kind} captures, up to {self.max_fps:g} fps)" if kind else "")
            )
            self.first_frame.emit()
        self.update()

    def _check_stream(self) -> None:
        worker = self._worker
        if worker is None:
            self._watchdog.stop()
            return
        if self._paused:
            return
        last = max(worker.last_frame_at, self._live_since)
        if time.monotonic() - last > self.STALL_TIMEOUT_S:
            waited = int(self.STALL_TIMEOUT_S)
            message = (
                f"no screen capture arrived within {waited} s"
                if worker.frames == 0
                else f"the device sent no new capture for {waited} s"
            )
            self.stop(wait_ms=0)
            self._finish(message, True)

    def _on_ended(self, worker, message, failed) -> None:
        if worker is not self._worker:
            return  # a stream that was already replaced or stopped
        self._watchdog.stop()
        self._worker = None
        park_thread(worker)
        self._retired.append(worker)  # it returns from run() a moment later
        self._finish(message, failed)

    def _finish(self, message, failed) -> None:
        self._image = None
        self._message = message if failed else "Screen capture stopped."
        self.update()
        if not self._closed:
            self.ended.emit(message, bool(failed))

    # ---- painting ----
    def paintEvent(self, _event):
        from . import theme

        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.TERM_BG))
        image = self._image
        if image is not None and not image.isNull():
            left, top, width, height = fit_rect(
                image.width(), image.height(), self.width(), self.height()
            )
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            # Only the device picture: the frame-rate indicator lives in the
            # owner's toolbar (renderer_text), where it can't cover the screen.
            painter.drawImage(QRectF(left, top, width, height), image)
        elif self._message:
            painter.setPen(QColor(theme.TERM_FG))
            painter.drawText(
                self.rect().adjusted(16, 16, -16, -16),
                Qt.AlignCenter | Qt.TextWordWrap,
                self._message,
            )
        painter.end()

    # ---- touch ----
    def _display_size(self):
        from .mirror_panel import _parse_display_size

        return _parse_display_size(self.display.get("size"))

    def device_point(self, x, y, clamp=False):
        """Device input coordinates of a widget point, or None."""
        size = self._frame_size
        if not size:
            return None
        return map_to_device(
            x, y, self.width(), self.height(), size[0], size[1],
            self._display_size(), clamp=clamp,
        )

    def mousePressEvent(self, event):
        self.setFocus(Qt.MouseFocusReason)
        button = event.button()
        if button == Qt.RightButton:  # as in scrcpy: right click = Back
            self._key(self._KEY_BACK, "back")
            return
        if button == Qt.MiddleButton:  # middle click = Home
            self._key(self._KEY_HOME, "home")
            return
        if button != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        point = self.device_point(event.x(), event.y())
        self._press = None if point is None else (event.pos(), point, time.monotonic())

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._press is None:
            super().mouseReleaseEvent(event)
            return
        start_pos, start, pressed_at = self._press
        self._press = None
        moved = (event.pos() - start_pos).manhattanLength() > self.TAP_SLOP
        end = self.device_point(event.x(), event.y(), clamp=True) or start
        held_ms = (time.monotonic() - pressed_at) * 1000.0
        self.send_gesture(
            gesture_for(start, end, held_ms, moved=moved, long_press_ms=self.LONG_PRESS_MS)
        )

    def wheelEvent(self, event):
        event.accept()
        size = self._frame_size
        point = self.device_point(event.x(), event.y())
        if point is None or not size or self._wheel_busy:
            return  # one wheel swipe at a time: the device is slower than a wheel
        delta = event.angleDelta()
        gesture = wheel_swipe(
            point, delta.x(), delta.y(), input_size(size[0], size[1], self._display_size()),
            ms=self.WHEEL_SWIPE_MS,
        )
        if gesture is not None:
            self._wheel_busy = True
            self.send_gesture(gesture, wheel=True)

    def send_gesture(self, gesture, wheel=False) -> None:
        """Queue a :func:`gesture_for` / :func:`wheel_swipe` result."""
        handler = self.handler
        extra = {"display_id": self.input_display_id} if self.input_display_id else {}
        if gesture[0] == "tap":
            _kind, x, y = gesture
            label = f"tap {x},{y}"

            def run():
                return handler.tap(x, y, safe=True, **extra)

        else:
            _kind, x1, y1, x2, y2, ms = gesture
            label = f"swipe {x1},{y1} → {x2},{y2}"

            def run():
                return handler.swipe(x1, y1, x2, y2, ms, safe=True, **extra)

        self._submit(label, run, wheel=wheel)

    def _key(self, code, label) -> None:
        handler = self.handler
        extra = {"display_id": self.input_display_id} if self.input_display_id else {}
        self._submit(label, lambda: handler.keyevent(code, safe=True, **extra))

    def _submit(self, label, run, wheel=False) -> None:
        def done(result):
            if wheel:
                self._wheel_busy = False
            if self._closed:
                return
            if isinstance(result, OperationResult) and not result.success:
                self.log.emit(f"[WARNING] screen input ({label}): {result.error}")
            elif result is False or (
                isinstance(result, OperationResult) and result.value is False
            ):
                self.log.emit(f"[WARNING] screen input ({label}): the device rejected it")

        def fail(message):
            if wheel:
                self._wheel_busy = False
            if not self._closed:
                self.log.emit(f"[WARNING] screen input ({label}): {message}")

        accepted = self._dispatcher.submit(run, on_done=done, on_fail=fail, priority=0)
        if not accepted and wheel:
            self._wheel_busy = False

    # ---- keyboard ----
    def _keys(self):
        if self._key_sink is None:
            from .mirror_panel import _KeyBatcher

            self._batcher = _KeyBatcher(
                self.handler, self._dispatcher, self, display_id=self.input_display_id
            )
            self._batcher.note.connect(self.log)
            self._key_sink, self._key_release = self._batcher.push, self._batcher.release
        return self._key_sink

    def keyPressEvent(self, event):
        from .mirror_panel import _forward_device_key

        if not _forward_device_key(event, self._keys(), with_repeat=True):
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        from .mirror_panel import _forward_device_key_release

        self._keys()
        release = self._key_release or (lambda _code: None)
        if not _forward_device_key_release(event, release):
            super().keyReleaseEvent(event)

    def focusNextPrevChild(self, next_child):
        # Tab is a device key while the screen has the keyboard
        return False if self.hasFocus() else super().focusNextPrevChild(next_child)

    def event(self, event):
        if event.type() == QEvent.ShortcutOverride and self.hasFocus():
            event.accept()  # device keystrokes (Ctrl+W…) are not app shortcuts
            return True
        return super().event(event)
