"""The taskbar icon follows Windows' light or dark mode, live, and a pin
starts TurboADB.

The icon was one dark tile whatever the taskbar looked like, and a pin of the
running window started a bare Python (turboadb.gui.taskbar)."""

import ctypes
import os
import sys

import pytest

pytest.importorskip("PyQt5")

from turboadb import winshell  # noqa: E402


@pytest.fixture
def window(qapp):
    from PyQt5.QtWidgets import QWidget

    w = QWidget()
    yield w
    w.deleteLater()


@pytest.fixture
def mode(monkeypatch):
    """The taskbar's mode as the test sets it; the shell must not be touched
    by an offscreen window."""
    from turboadb.gui import taskbar

    state = {"light": False}
    monkeypatch.setattr(winshell, "taskbar_is_light", lambda: state["light"])
    monkeypatch.setattr(winshell, "set_window_identity", lambda *a, **k: pytest.fail("the shell was touched"))
    monkeypatch.setattr(taskbar, "_sync_shortcut_icons", lambda *a: pytest.fail("shortcuts were touched"))
    return state


def _lightness(icon):
    """How light the tile is, below the gauge's open bottom."""
    return icon.pixmap(64, 64).toImage().pixelColor(32, 60).lightness()


def test_the_window_icon_follows_the_taskbar_mode(qapp, window, mode):
    from turboadb.gui import taskbar

    bar = taskbar.Taskbar(qapp, window)
    try:
        assert bar.light is False
        assert _lightness(window.windowIcon()) < 60 and _lightness(qapp.windowIcon()) < 60
        mode["light"] = True
        bar.refresh()
        assert bar.light is True
        assert _lightness(window.windowIcon()) > 200 and _lightness(qapp.windowIcon()) > 200
        mode["light"] = False
        bar.refresh()
        assert _lightness(window.windowIcon()) < 60
    finally:
        bar.close()


def test_the_icon_is_only_set_again_when_the_mode_changed(qapp, window, mode):
    from turboadb.gui import taskbar

    bar = taskbar.Taskbar(qapp, window)
    try:
        key = window.windowIcon().cacheKey()
        bar.refresh()  # a broadcast that changed nothing
        assert window.windowIcon().cacheKey() == key
    finally:
        bar.close()


def test_a_pin_starts_what_the_shortcuts_start(monkeypatch):
    from turboadb import cli
    from turboadb.gui import taskbar

    asked = []

    def target(build=True):
        asked.append(build)
        return "C:\\Program Files\\T\\bin\\TurboADB.exe", "-m turboadb_launch", "C:\\Program Files\\T"

    monkeypatch.setattr(cli, "_resolve_gui_target", target)
    assert taskbar.relaunch_command() == '"C:\\Program Files\\T\\bin\\TurboADB.exe" -m turboadb_launch'
    assert asked == [False]  # the UI thread never builds TurboADB.exe


def test_nothing_is_installed_off_windows(qapp, window, monkeypatch):
    from turboadb.gui import taskbar

    monkeypatch.setattr(sys, "platform", "linux")
    assert taskbar.install(qapp, window) is None


def _msg(message, text):
    from ctypes import wintypes

    buf = ctypes.create_unicode_buffer(text) if text is not None else None
    msg = wintypes.MSG(None, message, 0, ctypes.addressof(buf) if buf is not None else 0)
    return msg, buf


@pytest.mark.skipif(os.name != "nt", reason="Windows messages")
def test_a_mode_change_broadcast_is_recognised():
    from turboadb.gui import taskbar

    for message, text, expected in ((0x001A, "ImmersiveColorSet", True), (0x001A, "Environment", False),
                                    (0x0010, "ImmersiveColorSet", False), (0x001A, None, False)):
        msg, _buf = _msg(message, text)
        assert taskbar.is_mode_change(ctypes.addressof(msg)) is expected, (message, text)


@pytest.mark.skipif(os.name != "nt", reason="Windows messages")
def test_a_mode_change_broadcast_refreshes_the_icon_after_a_pause(qapp, window, mode):
    from PyQt5 import sip
    from PyQt5.QtCore import QByteArray
    from PyQt5.QtTest import QTest

    from turboadb.gui import taskbar

    bar = taskbar.Taskbar(qapp, window)
    try:
        mode["light"] = True
        msg, _buf = _msg(0x001A, "ImmersiveColorSet")
        handled = bar._filter.nativeEventFilter(QByteArray(b"windows_generic_MSG"),
                                                sip.voidptr(ctypes.addressof(msg)))
        assert handled == (False, 0)  # Qt still gets it
        assert _lightness(window.windowIcon()) < 60  # not at once: Windows sends several
        QTest.qWait(taskbar._DEBOUNCE_MS + 300)
        assert bar.light is True and _lightness(window.windowIcon()) > 200
    finally:
        bar.close()
