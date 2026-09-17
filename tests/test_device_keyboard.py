"""Typing on the embedded device screen: the native scrcpy keyboard route and
the fast, never-stale ADB fallback.  Headless (Qt offscreen platform); no
device, adb or network is used, and Win32 is a fake ``user32``."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytest.importorskip("PyQt5")


class _Dispatcher:
    """Queues commands like DeviceCommandDispatcher; the test decides when each
    one starts (``start_next``) and when its result arrives."""

    def __init__(self):
        self.queue = []
        self.submitted = 0

    def submit(self, fn, *, on_done=None, on_fail=None, priority=10):
        self.queue.append((fn, on_done))
        self.submitted += 1
        return True

    def start_next(self):
        """Run the oldest command; returns a callable delivering its result."""
        fn, on_done = self.queue.pop(0)
        result = fn()
        return lambda: on_done(result) if on_done else None

    def run_all(self):
        while self.queue:
            self.start_next()()

    def stop(self):
        pass

    def isRunning(self):
        return False


class _Handler:
    serial = "mock"
    config = None

    def __init__(self):
        self.calls = []

    def input_text(self, text, safe=None):
        self.calls.append(("text", text))
        return True

    def keyevent(self, code, safe=None):
        self.calls.append(("keys", [code]))
        return True

    def keyevents(self, codes, safe=None):
        self.calls.append(("keys", list(codes)))
        return True


def _pump():
    from turboadb.gui.mirror_panel import _KeyBatcher

    handler, dispatcher = _Handler(), _Dispatcher()
    return _KeyBatcher(handler, dispatcher), handler, dispatcher


# --------------------------------------------------------------------------- #
# the ADB fallback: fast and never stale
# --------------------------------------------------------------------------- #
def test_auto_repeat_is_dropped_while_that_key_is_queued_or_running(app):
    pump, handler, dispatcher = _pump()
    pump.push("key", 67)  # Backspace pressed
    finish = dispatcher.start_next()  # its adb call is running
    for _ in range(30):  # a second of Windows auto-repeat
        pump.push("key", 67, auto_repeat=True)
    assert dispatcher.queue == [] and pump.pending == 1
    finish()
    pump.push("key", 67, auto_repeat=True)  # the device caught up: one more
    pump.push("key", 67, auto_repeat=True)  # ...but only one may wait
    assert len(dispatcher.queue) == 1
    dispatcher.run_all()
    assert handler.calls == [("keys", [67]), ("keys", [67])]

    # a held character key behaves the same way
    pump.push("text", "a")
    pump.push("text", "a", auto_repeat=True)  # still inside the batch window
    pump.flush()
    finish = dispatcher.start_next()
    pump.push("text", "a", auto_repeat=True)  # the text is still being typed
    pump.flush()
    finish()
    assert handler.calls[-1] == ("text", "a") and dispatcher.queue == []
    pump.stop()


def test_identical_keys_are_coalesced_into_one_input_call(app):
    pump, handler, dispatcher = _pump()
    pump.push("key", 22)
    finish = dispatcher.start_next()  # the device is busy
    for _ in range(5):
        pump.push("key", 67)
    pump.push("key", 66)
    assert len(dispatcher.queue) == 2
    finish()
    dispatcher.run_all()
    assert handler.calls == [("keys", [22]), ("keys", [67] * 5), ("keys", [66])]

    # never merged across a command queued by someone else (a tap), and a
    # single call carries at most MAX_KEYS_PER_CALL keys
    handler.calls.clear()
    pump.push("key", 21)
    dispatcher.submit(lambda: handler.calls.append(("tap",)))
    pump.push("key", 21)
    for _ in range(pump.MAX_KEYS_PER_CALL):
        pump.push("key", 21)
    dispatcher.run_all()
    assert handler.calls == [
        ("keys", [21]),
        ("tap",),
        ("keys", [21] * pump.MAX_KEYS_PER_CALL),
        ("keys", [21]),
    ]
    pump.stop()


def test_release_discards_queued_repeats_that_have_not_started(app):
    pump, handler, dispatcher = _pump()
    pump.push("key", 67)
    dispatcher.start_next()()  # the first Backspace is done
    dispatcher.submit(lambda: handler.calls.append(("tap",)))  # e.g. a Home tap
    finish_tap = dispatcher.start_next()
    pump.push("key", 67, auto_repeat=True)  # one repeat waits behind the tap
    assert pump.pending == 1
    pump.release(67)
    finish_tap()
    dispatcher.run_all()
    assert handler.calls == [("keys", [67]), ("tap",)]
    assert pump.pending == 0

    # a real press is never discarded by its release
    handler.calls.clear()
    pump.push("key", 20)
    finish = dispatcher.start_next()
    pump.push("key", 67)
    pump.release(67)
    finish()
    dispatcher.run_all()
    assert handler.calls == [("keys", [20]), ("keys", [67])]
    pump.stop()


def test_text_and_keys_keep_their_order(app):
    pump, handler, dispatcher = _pump()
    pump.push("key", 22)
    finish = dispatcher.start_next()
    pump.push("text", "a")
    pump.push("text", "b")
    pump.push("key", 67)  # flushes "ab" first
    pump.push("text", "c")
    pump.flush()
    pump.push("text", "d")
    pump.flush()  # joins the "c" still waiting
    pump.push("key", 66)
    finish()
    dispatcher.run_all()
    assert handler.calls == [
        ("keys", [22]),
        ("text", "ab"),
        ("keys", [67]),
        ("text", "cd"),
        ("keys", [66]),
    ]
    pump.stop()


def test_pending_commands_are_capped_and_stop_discards_them(app):
    pump, handler, dispatcher = _pump()
    notes = []
    pump.note.connect(notes.append)
    pump.push("key", 22)
    dispatcher.start_next()  # never answers
    for code in range(100, 100 + pump.MAX_PENDING + 5):
        pump.push("key", code)  # distinct keys: nothing to coalesce
    assert len(dispatcher.queue) == pump.MAX_PENDING
    assert len(notes) == 1 and "dropped" in notes[0]
    pump.stop()
    dispatcher.run_all()
    assert handler.calls == [("keys", [22])]


def test_embed_container_reports_auto_repeat_and_real_releases(app):
    from PyQt5.QtCore import QEvent, Qt
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtWidgets import QApplication
    from turboadb.gui.mirror_panel import _EmbedContainer

    sent, released = [], []
    container = _EmbedContainer(
        lambda kind, payload, auto_repeat=None: sent.append((kind, payload, auto_repeat)),
        on_key_release=released.append,
    )
    try:
        def key(kind, auto_repeat, text="\b"):
            event = QKeyEvent(kind, Qt.Key_Backspace, Qt.NoModifier, text, auto_repeat)
            QApplication.sendEvent(container, event)

        key(QEvent.KeyPress, False)
        key(QEvent.KeyRelease, True)  # Windows pairs each repeat with a release
        key(QEvent.KeyPress, True)
        key(QEvent.KeyRelease, False)
        assert sent == [("key", 67, False), ("key", 67, True)]
        assert released == [67]
        QApplication.sendEvent(
            container, QKeyEvent(QEvent.KeyPress, Qt.Key_X, Qt.NoModifier, "x", True)
        )
        assert sent[-1] == ("text", "x", True)
    finally:
        container.close()


# --------------------------------------------------------------------------- #
# the native route: Win32 focus on scrcpy's SDL child window
# --------------------------------------------------------------------------- #
class _User32:
    """Just enough Win32 focus behaviour for the embedding helpers."""

    ROOT, SDL_CHILD, SDL_THREAD = 0x1000, 0x2002, 4321

    def __init__(self, parent, allow=True):
        self.parent = parent
        self.allow = allow  # whether Windows lets the SDL child take focus
        self.focus = parent  # the container forwarded keys over ADB so far
        self.attach = []
        self.set_focus = []

    def IsChild(self, parent, child):
        return parent == self.parent and child == self.SDL_CHILD

    def GetFocus(self):
        return self.focus

    def GetWindowThreadProcessId(self, hwnd, _pid):
        return self.SDL_THREAD if hwnd == self.SDL_CHILD else 0

    def GetAncestor(self, _hwnd, _flag):
        return self.ROOT

    def AttachThreadInput(self, _mine, theirs, attach):
        self.attach.append((theirs, bool(attach)))
        return 1

    def SetFocus(self, hwnd):
        self.set_focus.append(hwnd)
        if hwnd == self.SDL_CHILD and not self.allow:
            return 0
        previous, self.focus = self.focus, hwnd
        return previous


def test_focus_helper_attaches_parks_on_the_top_level_and_verifies(monkeypatch):
    import turboadb.gui.mirror_panel as mp

    fake = _User32(parent=0x1001)
    monkeypatch.setattr(mp, "_IS_WIN", True)
    monkeypatch.setattr(mp, "_win_api", lambda: (None, fake))
    assert mp._focus_embedded_child(fake.SDL_CHILD, fake.parent) is True
    # parked on the top-level first (Qt would otherwise deactivate the app)
    assert fake.set_focus == [fake.ROOT, fake.SDL_CHILD]
    assert fake.attach == [(fake.SDL_THREAD, True), (fake.SDL_THREAD, False)]
    assert fake.focus == fake.SDL_CHILD
    assert mp._focus_embedded_child(fake.SDL_CHILD, fake.parent) is True  # no churn
    assert len(fake.set_focus) == 2
    assert mp._focus_embedded_child(0x9999, fake.parent) is False  # not our child

    refused = _User32(parent=0x1001, allow=False)
    monkeypatch.setattr(mp, "_win_api", lambda: (None, refused))
    assert mp._focus_embedded_child(refused.SDL_CHILD, refused.parent) is False
    assert refused.attach[-1] == (refused.SDL_THREAD, False)  # always detached

    # handing the keyboard back only acts while the SDL child still holds it
    monkeypatch.setattr(mp, "_win_api", lambda: (None, fake))
    assert mp._release_child_focus(fake.SDL_CHILD, 0x3003) is True
    assert fake.focus == 0x3003
    assert mp._release_child_focus(fake.SDL_CHILD, 0x4004) is False
    fake.focus = 0  # the child was destroyed while focused
    assert mp._release_child_focus(fake.SDL_CHILD, 0x4004) is False
    assert mp._release_child_focus(fake.SDL_CHILD, 0x4004, if_lost=True) is True


@pytest.fixture
def native_panel(app, monkeypatch):
    """A shown MirrorPanel with a (fake) embedded scrcpy child window."""
    import turboadb.gui.mirror_panel as mp
    from turboadb.gui.mirror_panel import MirrorPanel

    dispatcher = _Dispatcher()
    panel = MirrorPanel(_Handler(), {"name": "mock"}, dispatcher=dispatcher)
    panel._fit = lambda: None  # the fake child HWND must never reach MoveWindow
    panel.resize(640, 480)
    panel.show()
    app.processEvents()
    claimed = []
    fake = _User32(parent=int(panel.container.winId()))
    monkeypatch.setattr(mp, "_IS_WIN", True)
    monkeypatch.setattr(mp, "_win_api", lambda: (None, fake))
    monkeypatch.setattr(mp, "_claim_native_focus", lambda hwnd: claimed.append(hwnd) or True)
    panel._child_hwnd = fake.SDL_CHILD
    try:
        yield panel, fake, claimed, dispatcher
    finally:
        panel._child_hwnd = None
        panel._scrcpy = None
        panel.close_panel()
        panel.close()


def test_click_gives_the_keyboard_to_scrcpy_when_windows_allows_it(native_panel):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QLineEdit

    panel, fake, claimed, _dispatcher = native_panel
    logs = []
    panel.log.connect(logs.append)
    assert panel._focus_embedded(Qt.MouseFocusReason) is True
    assert panel._native_keyboard
    assert fake.focus == fake.SDL_CHILD
    assert claimed == []  # the container did not pull the keyboard back
    assert any("straight to scrcpy" in line for line in logs)

    # choosing another TurboADB widget takes the keyboard back from scrcpy
    edit = QLineEdit(panel)  # not created yet: it has no effective window id
    panel._on_app_focus_changed(panel.container, edit)
    assert not panel._native_keyboard
    assert fake.focus == int(panel.window().winId())

    # re-activating TurboADB with the screen still focused hands it back
    assert panel._focus_embedded(Qt.MouseFocusReason) and panel._native_keyboard
    fake.focus = fake.ROOT  # Windows focuses the top-level on activation
    panel._on_app_focus_changed(None, panel.container)
    assert panel._native_kb_timer.isActive()

    # leaving the page releases it
    panel.yield_keyboard()
    assert not panel._native_keyboard and not panel._native_kb_timer.isActive()


def test_click_falls_back_to_the_adb_route_when_windows_refuses(native_panel):
    from PyQt5.QtCore import QEvent, Qt
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtWidgets import QApplication

    panel, fake, claimed, dispatcher = native_panel
    fake.allow = False
    logs = []
    panel.log.connect(logs.append)
    assert panel._focus_embedded(Qt.MouseFocusReason) is True
    assert not panel._native_keyboard
    assert claimed == [int(panel.container.winId())]
    assert any("sent over adb" in line for line in logs)

    def backspace(auto_repeat):
        QApplication.sendEvent(
            panel.container,
            QKeyEvent(QEvent.KeyPress, Qt.Key_Backspace, Qt.NoModifier, "\b", auto_repeat),
        )

    backspace(False)
    finish = dispatcher.start_next()
    for _ in range(10):
        backspace(True)
    assert dispatcher.queue == []  # the repeat flood never queued
    finish()
    assert panel.handler.calls == [("keys", [67])]


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


def test_embedded_sdk_keyboard_launch_sends_every_key_as_a_key_event(app, monkeypatch):
    """SDL never gives an embedded child keyboard focus and drops text events
    without it, so the default SDK keyboard must not rely on text events."""
    import turboadb.gui.mirror_panel as mp
    from turboadb.gui.mirror_panel import MirrorPanel

    launches = []

    class Launch:
        def __init__(self, handler, opts, compat, log_path):
            self.opts = opts
            self.done, self.fail = _Signal(), _Signal()
            launches.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

        def isRunning(self):
            return False

        def isFinished(self):
            return True

    monkeypatch.setattr(mp, "_MirrorLaunchThread", Launch)
    monkeypatch.setattr(mp, "park_thread", lambda _thread: None)
    monkeypatch.setattr(mp, "_IS_WIN", True)  # embedding is Windows-only
    panel = MirrorPanel(_Handler(), {"name": "mock"}, dispatcher=_Dispatcher())
    try:
        def launched_args(**spec):
            panel._launch_pending = False
            panel.start(**spec)
            return launches[-1].opts.to_args()

        assert panel._kb_mode() is None  # default: scrcpy's own SDK keyboard
        args = launched_args(embed=True)
        assert "--raw-key-events" in args
        assert not any(a.startswith("--keyboard") for a in args)
        # scrcpy's own window does get SDL focus: keep its normal text input
        assert "--raw-key-events" not in launched_args(embed=False)
        panel.act_kb_uhid.setChecked(True)
        args = launched_args(embed=True)
        assert "--keyboard=uhid" in args and "--raw-key-events" not in args
        # scrcpy 4.1 exits with "--raw-key-events is specific to --keyboard=sdk"
        # when control (and so the keyboard) is disabled
        panel.act_kb_uhid.setChecked(False)
        panel.act_kb_sdk.setChecked(True)
        real_tune = panel._tune

        def view_only(opts):
            opts = real_tune(opts)
            opts.no_control = True
            return opts

        panel._tune = view_only
        args = launched_args(embed=True)
        assert "--no-control" in args and "--raw-key-events" not in args
    finally:
        panel._launch_pending = False
        panel.close_panel()
        panel.close()
