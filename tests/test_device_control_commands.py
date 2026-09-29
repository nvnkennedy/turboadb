"""Device Control's commands: which queue runs them, what the log says about
them, and what the Keyboard box keeps to itself."""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PyQt5")

from turboadb.results import OperationResult  # noqa: E402


class _Recorder:
    """A dispatcher that runs each command at once and remembers its label."""

    def __init__(self, name, log):
        self.name = name
        self.log = log

    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        self.log.append(self.name)
        result = fn()
        if on_done:
            on_done(result)
        return True

    def stop(self):
        pass


class _Handler:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            value = self.results.get(name, True)
            return value if isinstance(value, OperationResult) else OperationResult(True, name, value=value)

        return call


def _panel(handler, **kwargs):
    from turboadb.gui.controls_panel import ControlsPanel

    queues = []
    panel = ControlsPanel(handler, compact=True, dispatcher=_Recorder("input", queues),
                          slow_dispatcher=_Recorder("slow", queues), **kwargs)
    logs = []
    panel.log.connect(logs.append)
    return panel, queues, logs


def _button(panel, name):
    from PyQt5.QtWidgets import QToolButton

    return next(b for b in panel.findChildren(QToolButton) if b.accessibleName() == name)


def test_launchers_and_reports_never_queue_behind_screen_input(qapp):
    handler = _Handler()
    panel, queues, _logs = _panel(handler)
    reports = []
    panel._show_info = lambda title, text: reports.append(title)  # no modal dialog
    try:
        for name in ("YouTube", "Gallery", "Camera", "Battery"):
            _button(panel, name).click()
        panel.url.setText("example.com")
        panel._open_url()
        panel._search()
        assert queues == ["slow"] * 6
        assert reports == ["Battery"]
        queues.clear()
        for name in ("Back", "Home", "Volume up", "Settings", "Notifications"):
            _button(panel, name).click()
        panel.quick_tiles["wifi"].btn_on.click()
        panel.text.setText("hi")
        panel._send_text()
        assert queues == ["input"] * 7  # keys, text and controls keep one order
    finally:
        panel.close_panel()
        panel.deleteLater()


def test_a_slow_launch_does_not_hold_up_a_key(qapp):
    from turboadb.gui.controls_panel import ControlsPanel

    release = threading.Event()
    keys = []

    class Handler:
        def open_url(self, url, safe=None):
            release.wait(10)  # an app missing on a head unit: many seconds
            return True

        def keyevent(self, key, safe=None, **_kwargs):
            keys.append(key)
            return True

    panel = ControlsPanel(Handler(), compact=True)
    try:
        _button(panel, "YouTube").click()
        _button(panel, "Back").click()
        deadline = time.monotonic() + 5
        while not keys and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert keys == ["back"] and not release.is_set()
        release.set()
    finally:
        release.set()
        running = panel.shutdown_threads()
        assert len(running) == 2  # both of the panel's own queues were busy
        for dispatcher in running:
            dispatcher.wait(5000)
        panel.deleteLater()


def test_an_unused_panel_starts_and_parks_no_thread(qapp):
    from turboadb.gui import qtutil
    from turboadb.gui.controls_panel import ControlsPanel

    panel = ControlsPanel(None, compact=True)
    try:
        assert panel._slow_dispatcher is None  # started only when first needed
        assert panel.shutdown_threads() == []
        assert panel._dispatcher not in qtutil._parked
    finally:
        panel.deleteLater()


def test_a_refused_key_or_text_is_not_reported_as_a_missing_app(qapp):
    from turboadb.gui.controls_panel import ControlsPanel

    msg = ControlsPanel._result_msg
    assert msg("YouTube", False).startswith("[WARNING] YouTube: nothing happened")
    handler = _Handler({"keyevent": False, "input_text": False, "media": False,
                        "expand_notifications": False, "screen_off": False})
    panel, _queues, logs = _panel(handler)
    try:
        for name in ("Home", "Play / pause", "Notifications"):
            _button(panel, name).click()
        panel.quick_tiles["screen"].btn_off.click()
        panel.text.setText("hello")
        panel._send_text()
        assert logs == [
            f"[ERROR] {label}: the device did not accept the command "
            "(see the log for adb's output)"
            for label in ("Home", "Play / pause", "Notifications", "Screen off", "type 5 characters")
        ]
        assert not [line for line in logs if "installed" in line]
    finally:
        panel.close_panel()
        panel.deleteLater()


def test_typed_text_never_reaches_the_log(qapp):
    handler = _Handler()
    panel, _queues, logs = _panel(handler)
    try:
        panel.text.setText("s3cret pin")
        panel._send_text()
        assert handler.calls == [("input_text", ("s3cret pin",), {"safe": True})]
        assert logs == ["[OK] type 10 characters"]
        assert "s3cret" not in "".join(logs)
        assert panel.text.text() == ""  # typed: the box is ready for more
        panel.text.setText("x")
        panel._send_text()
        assert logs[-1] == "[OK] type 1 character"
    finally:
        panel.close_panel()
        panel.deleteLater()


def test_text_the_device_did_not_take_comes_back_to_the_box(qapp):
    from turboadb.gui.controls_panel import ControlsPanel

    class Full:
        def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
            on_fail("device command queue is full; wait for the device to catch up")
            return False

        def stop(self):
            pass

    panel = ControlsPanel(_Handler(), compact=True, dispatcher=Full())
    logs = []
    panel.log.connect(logs.append)
    try:
        panel.text.setText("1234")
        panel._send_text()
        assert panel.text.text() == "1234"  # not lost when the queue is full
        assert logs == ["[ERROR] type 4 characters: device command queue is full; "
                        "wait for the device to catch up"]
    finally:
        panel.close_panel()
        panel.deleteLater()

    handler = _Handler({"input_text": OperationResult(False, "input_text", error="device offline")})
    panel, _queues, logs = _panel(handler)
    try:
        panel.text.setText("abcd")
        panel._send_text()
        assert panel.text.text() == "abcd"
        assert logs == ["[ERROR] type 4 characters: device offline"]
    finally:
        panel.close_panel()
        panel.deleteLater()


def test_restored_text_never_replaces_what_was_typed_since(qapp):
    from turboadb.gui.controls_panel import ControlsPanel

    held = []

    class Later:
        def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
            held.append((fn, on_done))
            return True

        def stop(self):
            pass

    panel = ControlsPanel(_Handler({"input_text": False}), compact=True, dispatcher=Later())
    try:
        panel.text.setText("first")
        panel._send_text()
        panel.text.setText("second")  # typed while the first was on its way
        fn, on_done = held.pop()
        on_done(fn())
        assert panel.text.text() == "second"
    finally:
        panel.close_panel()
        panel.deleteLater()
