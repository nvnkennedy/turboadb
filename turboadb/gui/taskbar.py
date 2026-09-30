"""TurboADB on the Windows taskbar: its name, its icon and what a pin starts,
and an icon that follows the taskbar's light or dark mode.

The main window carries TurboADB's app ID and what a pin made of it shows and
runs (:func:`turboadb.winshell.set_window_identity`): TurboADB.exe when the
GUI runs as that (:mod:`turboadb.launcher`), else what the shortcuts start.
So a pin shows TurboADB and starts TurboADB, never a bare Python.

When Windows switches between light and dark mode, the window's icon follows
at once, and so do the icons of TurboADB's pins, Start-menu and Desktop
shortcuts (their files, written off the UI thread).  The shell is only ever
touched on the real Windows platform: the test suite's offscreen windows
leave the user's shortcuts alone.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from typing import Optional

from PyQt5.QtCore import QAbstractNativeEventFilter, QObject, QTimer
from PyQt5.QtGui import QGuiApplication, QIcon

from .. import winshell

_WM_SETTINGCHANGE = 0x001A
_DEBOUNCE_MS = 400  # Windows broadcasts a mode change several times


def is_mode_change(msg_address: int) -> bool:
    """Whether the Windows MSG at *msg_address* announces a light/dark mode
    change: WM_SETTINGCHANGE for "ImmersiveColorSet"."""
    from ctypes import wintypes

    msg = wintypes.MSG.from_address(msg_address)
    return (msg.message == _WM_SETTINGCHANGE and bool(msg.lParam)
            and ctypes.wstring_at(msg.lParam) == "ImmersiveColorSet")


class _ModeChanges(QAbstractNativeEventFilter):
    """Calls *changed* for each mode-change broadcast Qt's windows receive."""

    def __init__(self, changed):
        super().__init__()
        self._changed = changed

    def nativeEventFilter(self, event_type, message):
        try:
            if bytes(event_type) == b"windows_generic_MSG" and is_mode_change(int(message)):
                self._changed()
        except Exception:
            pass
        return False, 0


def mode_icon(light: bool, ico: str) -> QIcon:
    """The window icon of a light or dark taskbar: the ``.ico`` file's sizes,
    and its large PNG for Qt builds without an ICO reader."""
    icon = QIcon(ico)
    png = winshell.asset("icon-light.png" if light else "icon.png")
    if os.path.isfile(png):
        icon.addFile(png)
    return icon


def relaunch_command() -> str:
    """The command line a pin starts TurboADB with (never builds anything:
    this runs on the UI thread)."""
    from ..cli import _resolve_gui_target

    target, args, _folder = _resolve_gui_target(build=False)
    return winshell.command_line(target, args)


def _sync_shortcut_icons(icon: str, everywhere: bool) -> None:
    """Point TurboADB's pins (with *everywhere* also its Start-menu and Desktop
    shortcuts, when shortcut upkeep is on) at *icon*."""
    try:
        paths = winshell.pinned_shortcuts()
        if everywhere:
            from ..cli import _shortcut_paths
            from . import settings as settings_mod

            if settings_mod.get("make_shortcut_first_run"):
                paths += [path for _loc, path, _maker in _shortcut_paths()]
        winshell.sync_shortcut_icons(icon, paths)
    except Exception:
        pass


class Taskbar(QObject):
    """Keeps *window*'s taskbar icon and identity in step with Windows' mode."""

    def __init__(self, app, window):
        super().__init__(window)
        self._app, self._window = app, window
        self._light: Optional[bool] = None
        self._shell = QGuiApplication.platformName() == "windows"
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DEBOUNCE_MS)
        self._timer.timeout.connect(self.refresh)
        self._filter = _ModeChanges(self._timer.start)
        app.installNativeEventFilter(self._filter)
        self.refresh()

    @property
    def light(self) -> Optional[bool]:
        return self._light

    def refresh(self) -> None:
        """Show the icon of the taskbar's current mode, if it changed."""
        light = winshell.taskbar_is_light()
        if light == self._light:
            return
        first = self._light is None
        self._light = light
        path = winshell.icon_file(light)
        icon = mode_icon(light, path)
        self._app.setWindowIcon(icon)
        self._window.setWindowIcon(icon)
        if not self._shell:
            return
        try:
            winshell.set_window_identity(int(self._window.winId()), relaunch_command(), path)
        except Exception:
            pass
        # at start only the pins: the shortcut upkeep writes the others with
        # this icon already; a change of mode reaches all of them
        threading.Thread(target=_sync_shortcut_icons, args=(path, not first), daemon=True,
                         name="turboadb-taskbar-icons").start()

    def close(self) -> None:
        self._app.removeNativeEventFilter(self._filter)


def install(app, window) -> Optional[Taskbar]:
    """TurboADB's taskbar identity and mode-following icon for the main
    *window* (Windows; None elsewhere).  Keep the result referenced."""
    if sys.platform != "win32":
        return None
    try:
        return Taskbar(app, window)
    except Exception:
        return None
