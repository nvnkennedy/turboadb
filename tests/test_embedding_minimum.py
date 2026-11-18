"""TurboADB inside an area smaller than its window's minimum size.

A page that needs more room (Device Control: about 720 px) than the host's
area gives it is clamped by Qt to its minimum.  The Embedder asked for the
area's size anyway, every 120 ms for ever, since the clamped window never
equalled it; and the host could not know how much room TurboADB needed.  Now
the Embedder asks for what Qt can set, the control API reports the minimum,
and the Qt widget can follow it.  Qt's offscreen platform; no host process.
"""
import time
import types

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QRect, QSize  # noqa: E402
from PyQt5.QtWidgets import QWidget  # noqa: E402

from turboadb.gui import embedding  # noqa: E402


class _Window(QWidget):
    """TurboADB's window: counts what the Embedder asks of it."""

    def __init__(self):
        super().__init__()
        self.asked = []

    def setGeometry(self, *args):  # noqa: N802 (Qt's name)
        self.asked.append(QRect(*args) if len(args) == 4 else QRect(args[0]))
        super().setGeometry(*args)


class _OffByOne(_Window):
    """A window Qt never makes quite the size asked for (DPI rounding)."""

    def setGeometry(self, rect):  # noqa: N802
        self.asked.append(QRect(rect))
        QWidget.setGeometry(self, QRect(rect.topLeft(), rect.size() - QSize(1, 0)))


@pytest.fixture
def host(monkeypatch):
    """The host's area: its size in device pixels, changed by the test."""
    area = types.SimpleNamespace(size=(648, 500))
    monkeypatch.setattr(embedding, "host_alive", lambda hwnd: True)
    monkeypatch.setattr(embedding, "host_client_size", lambda hwnd: area.size)
    monkeypatch.setattr(embedding, "keep_to_itself", lambda hwnd: False)
    return area


def _embedder(window):
    return embedding.Embedder(window, parent=0x1234)


def test_a_window_clamped_to_its_minimum_is_not_resized_again(qapp, host):
    window = _Window()
    window.setMinimumSize(720, 300)
    follow = _embedder(window)
    try:
        for _ in range(25):  # three seconds of polls
            follow._follow()
        assert len(window.asked) == 1  # (6.3.0: 25, one every 120 ms)
        assert window.geometry().size() == QSize(720, 500)  # its minimum, the area's height
    finally:
        window.deleteLater()
        qapp.processEvents()


def test_the_window_follows_the_area_again_once_it_fits(qapp, host):
    window = _Window()
    window.setMinimumSize(720, 300)
    follow = _embedder(window)
    try:
        follow._follow()
        window.setMinimumSize(600, 300)  # another page: it fits the area now
        follow._follow()
        follow._follow()
        assert window.geometry().size() == QSize(648, 500) and len(window.asked) == 2
        host.size = (1000, 640)  # the host's window grew
        follow._follow()
        follow._follow()
        assert window.geometry().size() == QSize(1000, 640) and len(window.asked) == 3
    finally:
        window.deleteLater()
        qapp.processEvents()


def test_a_size_qt_does_not_take_is_asked_once_per_change(qapp, host):
    window = _OffByOne()
    follow = _embedder(window)
    try:
        for _ in range(10):
            follow._follow()
        assert len(window.asked) == 1 and window.geometry().size() == QSize(647, 500)
        host.size = (700, 500)
        for _ in range(10):
            follow._follow()
        assert len(window.asked) == 2
    finally:
        window.deleteLater()
        qapp.processEvents()


def test_the_size_qt_can_set_keeps_to_the_minimum_and_maximum(qapp):
    window = QWidget()
    try:
        window.setMinimumSize(720, 300)
        window.setMaximumSize(2000, 800)
        assert embedding.settable_size(window, QSize(648, 500)) == QSize(720, 500)
        assert embedding.settable_size(window, QSize(3000, 900)) == QSize(2000, 800)
        assert embedding.settable_size(window, QSize(900, 600)) == QSize(900, 600)
    finally:
        window.deleteLater()


class _Sized:
    def __init__(self, width, height, ratio):
        self._size, self._ratio = QSize(width, height), ratio

    def minimumSize(self):  # noqa: N802
        return self._size

    def devicePixelRatioF(self):  # noqa: N802
        return self._ratio


@pytest.mark.parametrize("width, height, ratio, px", [
    (720, 348, 1.0, (720, 348)),
    (720, 348, 1.25, (900, 435)),
    (721, 349, 1.25, (902, 437)),  # rounded up: an area that big is never too small
    (440, 398, 1.5, (660, 597)),
    (0, 0, 2.0, (0, 0)),
])
def test_the_minimum_is_reported_in_logical_and_device_pixels(width, height, ratio, px):
    report = embedding.minimum_report(_Sized(width, height, ratio))
    assert (report["min_width"], report["min_height"]) == (width, height)
    assert (report["min_width_px"], report["min_height_px"]) == px
    assert report["pixel_ratio"] == ratio


def test_a_reported_minimum_area_holds_the_window_at_its_minimum():
    """The Embedder's rounding: an area of min_*_px device pixels is never
    rounded below the minimum."""
    for ratio in (1.0, 1.25, 1.5, 1.75, 2.0, 2.25):
        for width in range(300, 1300, 7):
            report = embedding.minimum_report(_Sized(width, 10, ratio))
            assert round(report["min_width_px"] / ratio) >= width


# ---- the Qt widget ------------------------------------------------------------------------
def _pump(qapp, until, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not until():
        qapp.processEvents()
        time.sleep(0.005)
    return until()


def _report(width, height):
    return {"min_width": width, "min_height": height, "min_width_px": width,
            "min_height_px": height, "pixel_ratio": 1.0}


@pytest.mark.parametrize("follow", [False, True])
def test_the_widget_learns_the_minimum_and_follows_it_when_asked(qapp, follow):
    from turboadb import embed_qt

    view = embed_qt.TurboADBWidget(autostart=False, follow_minimum=follow)
    seen = []
    view.minimum_changed.connect(lambda w, h: seen.append((w, h)))
    try:
        ratio = view.devicePixelRatioF()
        view._take_minimum({**_report(900, 435), "min_width_px": round(900 * ratio),
                            "min_height_px": round(435 * ratio)})
        assert _pump(qapp, lambda: seen)
        assert seen == [(900, 435)] and view.turboadb_minimum == (900, 435)
        expected = QSize(900, 435) if follow else QSize(320, 240)
        assert view.minimumSize() == expected  # without follow_minimum: the host's own
        view._take_minimum({**_report(100, 100), "min_width_px": 100, "min_height_px": 100})
        assert _pump(qapp, lambda: len(seen) == 2)
        if follow:
            assert view.minimumSize() == QSize(320, 240)  # never below the widget's own
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_a_minimum_that_jumps_for_a_moment_is_not_followed(qapp):
    """TurboADB's minimum jumps for a moment as it starts: a layout following
    every jump would jump with it."""
    from turboadb import embed_qt

    view = embed_qt.TurboADBWidget(autostart=False, follow_minimum=True)
    seen = []
    view.minimum_changed.connect(lambda w, h: seen.append((w, h)))
    try:
        ratio = view.devicePixelRatioF()
        for height in (398, 620, 398):
            view._take_minimum({**_report(440, height), "min_width_px": round(440 * ratio),
                                "min_height_px": round(height * ratio)})
            qapp.processEvents()
        assert _pump(qapp, lambda: seen)
        _pump(qapp, lambda: False, timeout=(view.SETTLE_MS * 2) / 1000)
        assert seen == [(440, 398)]
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_an_older_turboadb_without_a_minimum_changes_nothing(qapp):
    from turboadb import embed_qt

    view = embed_qt.TurboADBWidget(autostart=False, follow_minimum=True)
    seen = []
    view.minimum_changed.connect(lambda w, h: seen.append((w, h)))
    try:
        view._take_minimum({"menubar": True, "theme": "dark"})  # 6.3.0's /v1/ui
        _pump(qapp, lambda: False, timeout=(view.SETTLE_MS * 2) / 1000)
        assert seen == [] and view.minimumSize() == QSize(320, 240)
    finally:
        view.deleteLater()
        qapp.processEvents()


class _Pretend:
    """A TurboADB that only pretends: its minimum now, then a change."""

    pid, window = 4242, 99

    def __init__(self):
        self.asked_after = []

    def info(self):
        return {"events": 7}

    def ui(self):
        return _report(440, 398)

    def events(self, after=None):
        self.asked_after.append(after)
        yield {"id": 8, "type": "min_size", **_report(720, 348)}
        yield {"id": 9, "type": "closing"}

    def close(self, timeout=15.0):
        pass


def test_the_widget_reads_the_minimum_then_follows_its_events(qapp, monkeypatch):
    from turboadb import embed_qt

    pretend = _Pretend()
    monkeypatch.setattr(embed_qt, "launch", lambda parent, **kw: pretend)
    view = embed_qt.TurboADBWidget(autostart=False)
    seen, kinds = [], []
    view.minimum_changed.connect(lambda w, h: seen.append((w, h)))
    view.event_received.connect(lambda e: kinds.append(e["type"]))
    view.finished.connect(lambda: kinds.append("finished"))
    try:
        if view.devicePixelRatioF() != 1.0:
            pytest.skip("the pretend reports device pixels at 100 %")
        view.start()
        assert _pump(qapp, lambda: "finished" in kinds and seen)
        # (the events from the one that was newest when the minimum was read)
        assert pretend.asked_after == [7]
        assert kinds == ["min_size", "closing", "finished"]
        assert seen[-1] == (720, 348)
    finally:
        view.deleteLater()
        qapp.processEvents()
