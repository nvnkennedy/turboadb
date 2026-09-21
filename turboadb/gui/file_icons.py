"""Native file-type icons for the Files tab.

Where the platform has a real icon source - Windows Explorer's shell icons,
macOS Finder, a Linux desktop's icon theme - a row shows what the system's own
file manager shows: its folder icon, one icon per file type, and for files on
this PC each program's own icon and the special folders (Downloads, Desktop).
The device's files don't exist here, so they get the icon of their type,
looked up by extension.

Where there is no real source (a bare X server, the headless platform the
tests run on) Qt would fall back to generic style icons, which say less than
TurboADB's own glyphs, so those stay.

Lookups are cheap: Qt's icon engines are lazy (the shell is asked only when a
row is painted, i.e. for the rows on screen) and cache per type; this module
adds one shared QIcon per extension on top.
"""

from __future__ import annotations

import os
import tempfile
from typing import Callable, Optional

from PyQt5.QtCore import QFileInfo, QPoint, QRect, QRectF, Qt
from PyQt5.QtGui import (QColor, QGuiApplication, QIcon, QIconEngine, QPainter, QPen,
                         QPixmap)
from PyQt5.QtWidgets import QFileIconProvider

from . import theme
from .icons import icon_in
from .qtutil import cached_icon

# Android packages: a PC rarely has an application for them, so they get the
# system's blank file icon with an Android-green badge instead of a plain one.
ANDROID_PACKAGES = frozenset((".apk", ".apks", ".xapk", ".apkm", ".aab"))

# Qt platform plugins with real file icons; X11/Wayland have them only through
# a desktop icon theme.
_NATIVE_PLATFORMS = ("windows", "cocoa")
_THEMED_PLATFORMS = ("xcb", "wayland")


def native_icons_available() -> bool:
    """True where the system can supply real file icons."""
    platform = (QGuiApplication.platformName() or "").lower()
    if platform in _NATIVE_PLATFORMS:
        return True
    if platform.startswith(_THEMED_PLATFORMS):
        return QIcon.themeName() not in ("", "hicolor")
    return False


# A badge is drawn on a small white plate, as Explorer draws its shortcut
# arrow: a bare glyph at that size vanished into the icon beneath it. The
# glyph uses the light-background variant of its hue in every theme, since
# the plate is always white.
_PLATE = "#ffffff"
_PLATE_EDGE = QColor(0, 0, 0, 110)
# An APK's badge is a filled green plate with a white mark: at eight pixels
# an outlined green glyph on white blurred into a smudge.
_ANDROID_PLATE = "#2c9a5a"
_ANDROID_EDGE = QColor(0, 0, 0, 90)


class _BadgeEngine(QIconEngine):
    """A native icon with a small plated badge in a lower corner: bottom-left
    for a link (where Explorer marks a shortcut), bottom-right for a type."""

    def __init__(self, base: QIcon, badge: QIcon, corner: str = "left",
                 plate: str = _PLATE, edge: QColor = _PLATE_EDGE):
        super().__init__()
        self._base, self._badge, self._corner = base, badge, corner
        self._plate, self._edge = plate, edge

    def paint(self, painter, rect, mode, state):
        self._base.paint(painter, rect, Qt.AlignCenter, mode, state)
        side = max(8, int(round(min(rect.width(), rect.height()) * 0.56)))
        left = rect.left() if self._corner == "left" else rect.right() - side + 1
        plate = QRectF(left, rect.bottom() - side + 1, side, side)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(self._edge, max(1.0, side / 12.0)))
        painter.setBrush(QColor(self._plate))
        radius = side * 0.22
        painter.drawRoundedRect(plate.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.restore()
        inset = max(1, int(side * 0.14))
        glyph = plate.toRect().adjusted(inset, inset, -inset, -inset)
        self._badge.paint(painter, glyph, Qt.AlignCenter, mode, state)

    def pixmap(self, size, mode, state):
        pm = QPixmap(size)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        self.paint(painter, QRect(QPoint(0, 0), size), mode, state)
        painter.end()
        return pm

    def clone(self):
        return _BadgeEngine(self._base, self._badge, self._corner, self._plate, self._edge)


def _badge_glyph(name: str, tone: str) -> QIcon:
    """*name* in the light-background variant of *tone*, stroked a little
    heavier so it survives being drawn eight pixels wide."""
    return icon_in(name, theme.hue(tone, "light"), width=2.6)


def _same_picture(a: QIcon, b: QIcon, side: int = 16) -> bool:
    return a.pixmap(side, side).toImage() == b.pixmap(side, side).toImage()


class FileIcons:
    """The Files tab's row icons: native where the system has them."""

    def __init__(self, *, native: Optional[bool] = None,
                 provider: Optional[Callable[[], QFileIconProvider]] = None):
        self.native = native_icons_available() if native is None else bool(native)
        self._provider = (provider or QFileIconProvider)() if self.native else None
        self._by_type = {}  # extension -> QIcon
        self._badged = {}  # (kind, extension) -> QIcon
        self._folder = None
        self._generic = None
        # a folder that never exists: a device file's name is looked up here,
        # so only its extension counts (never a same-named file on this PC)
        self._nowhere = os.path.join(tempfile.gettempdir(),
                                     f"turboadb-no-such-folder-{os.getpid()}")

    # ---- lookups -----------------------------------------------------------
    def folder(self) -> QIcon:
        """The system's folder icon."""
        if self._folder is None:
            self._folder = self._provider.icon(QFileIconProvider.Folder)
        return self._folder

    def for_type(self, name: str) -> QIcon:
        """The icon of *name*'s file type (by extension), shared per type."""
        ext = os.path.splitext(name)[1].lower()
        icon = self._by_type.get(ext)
        if icon is None:
            icon = self._provider.icon(QFileInfo(os.path.join(self._nowhere, "x" + ext)))
            if ext in ANDROID_PACKAGES and self._is_generic(icon):
                # the system's own blank file, marked as an Android package
                icon = QIcon(_BadgeEngine(
                    icon, icon_in("apps", "#ffffff", width=2.8), "right",
                    plate=_ANDROID_PLATE, edge=_ANDROID_EDGE))
            self._by_type[ext] = icon
        return icon

    def for_path(self, path: str) -> QIcon:
        """The icon of an item on this PC, exactly as the system shows it: a
        program's own icon, a shortcut's target, a special folder's icon."""
        return self._provider.icon(QFileInfo(path))

    def with_link_badge(self, name: str, is_dir: bool) -> QIcon:
        """A device symbolic link: its target kind's icon plus a link badge."""
        key = ("dir", "") if is_dir else ("file", os.path.splitext(name)[1].lower())
        icon = self._badged.get(key)
        if icon is None:
            base = self.folder() if is_dir else self.for_type(name)
            icon = QIcon(_BadgeEngine(base, _badge_glyph("link", "blue"), "left"))
            self._badged[key] = icon
        return icon

    def _is_generic(self, icon: QIcon) -> bool:
        """True when *icon* is the system's "no application" icon."""
        if self._generic is None:
            self._generic = self._provider.icon(
                QFileInfo(os.path.join(self._nowhere, "x.turboadb-unknown-type")))
        return _same_picture(icon, self._generic)

    # ---- one call for a listing row ----------------------------------------
    def row_icon(self, name: str, is_dir: bool, ftype: str, glyph: str, tone: str,
                 local_dir: str = "") -> QIcon:
        """The icon for one row of a listing.

        *glyph*/*tone* is the row's themed icon, used where there is no native
        one: without a system source, and for the ``..`` row and device-only
        entries (pipes, sockets, device nodes) that no PC file manager draws.
        *local_dir* is the PC folder the row is in; empty for the device."""
        if not self.native or glyph in ("arrow-up", "chip"):
            return cached_icon(glyph, tone)
        if local_dir:
            return self.for_path(os.path.join(local_dir, name))
        if ftype.endswith("Link"):
            return self.with_link_badge(name, is_dir or ftype == "Folder Link")
        return self.folder() if is_dir else self.for_type(name)


_shared: Optional[FileIcons] = None


def file_icons() -> FileIcons:
    """The one FileIcons every Files tab shares (created on first use, once a
    QApplication exists)."""
    global _shared
    if _shared is None:
        _shared = FileIcons()
    return _shared
