"""Native file-type icons in the Files tab.

The headless test platform has no shell icons, so the provider is faked: the
tests check which icon each row asks the system for, the caching, the badges
and the fallback to TurboADB's own glyphs."""

import os

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QFileInfo  # noqa: E402
from PyQt5.QtGui import QColor, QIcon, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QFileIconProvider  # noqa: E402

from turboadb.gui import file_icons as fi  # noqa: E402
from turboadb.gui.qtutil import cached_icon  # noqa: E402


def _solid(colour):
    pm = QPixmap(16, 16)
    pm.fill(QColor(colour))
    return QIcon(pm)


class FakeProvider(QFileIconProvider):
    """Answers like the shell: one colour per type, per program, per folder."""

    GENERIC = "#808080"  # the "no application" icon

    def __init__(self, known=(".png", ".mp4", ".zip", ".pdf")):
        super().__init__()
        self.asked = []
        self.known = set(known)

    def icon(self, what):
        if isinstance(what, QFileInfo):
            path = what.filePath()
            self.asked.append(path)
            ext = os.path.splitext(path)[1].lower()
            if ext == ".exe":
                return _solid("#%06x" % (hash(path) & 0xFFFFFF))  # each program its own
            if ext in self.known:
                return _solid({".png": "#3366ff", ".mp4": "#aa00aa", ".zip": "#ccaa00",
                               ".pdf": "#dd2222"}.get(ext, "#00cc88"))
            return _solid(self.GENERIC)
        self.asked.append(what)
        return _solid("#f0c040")  # the folder


@pytest.fixture
def themed(qapp, monkeypatch):
    """Apply a theme for the test and put the previous stylesheet back after."""
    from turboadb.gui import theme

    monkeypatch.setattr(theme, "_ACTIVE_NAME", theme._ACTIVE_NAME)
    previous = qapp.styleSheet()
    yield lambda name: theme.apply_to_app(qapp, name)
    qapp.setStyleSheet(previous)


def _icons(**kwargs):
    provider = FakeProvider(**kwargs)
    return fi.FileIcons(native=True, provider=lambda: provider), provider


def _colour(icon, x=8, y=8):
    return icon.pixmap(16, 16).toImage().pixelColor(x, y).name()


def _corner_has(icon, colour, corner):
    """True when *colour* appears in a lower corner of the 16 px icon - the
    badge's plate (its glyph covers some of the plate's pixels)."""
    image = icon.pixmap(16, 16).toImage()
    xs = range(0, 8) if corner == "left" else range(8, 16)
    return any(image.pixelColor(x, y).name() == colour for x in xs for y in range(8, 16))


# --------------------------------------------------------------------------- #
# where native icons are used at all
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("platform, theme_name, expected", [
    ("windows", "", True),
    ("cocoa", "", True),
    ("xcb", "breeze", True),
    ("wayland-egl", "Adwaita", True),
    ("xcb", "", False),          # a bare X server: Qt's generic icons are worse
    ("xcb", "hicolor", False),   # the fallback theme is not a desktop theme
    ("offscreen", "", False),    # the headless platform the tests run on
])
def test_native_icons_only_where_the_system_has_them(monkeypatch, platform, theme_name,
                                                      expected):
    monkeypatch.setattr(fi.QGuiApplication, "platformName", staticmethod(lambda: platform))
    monkeypatch.setattr(fi.QIcon, "themeName", staticmethod(lambda: theme_name))
    assert fi.native_icons_available() is expected


def test_without_native_icons_the_glyphs_stay(qapp):
    icons = fi.FileIcons(native=False)
    assert icons.row_icon("a.png", False, "File", "image", "purple") is \
        cached_icon("image", "purple")
    assert icons.row_icon("DCIM", True, "Folder", "folder", "amber") is \
        cached_icon("folder", "amber")


# --------------------------------------------------------------------------- #
# the device pane: by type
# --------------------------------------------------------------------------- #
def test_device_files_get_their_types_icon_looked_up_by_extension_only(qapp):
    icons, provider = _icons()
    png = icons.row_icon("holiday.png", False, "File", "image", "purple")
    assert _colour(png) == "#3366ff"
    [path] = provider.asked
    assert os.path.basename(path) == "x.png"  # never the real name
    assert not os.path.exists(os.path.dirname(path))  # nor a folder that exists


def test_one_lookup_per_type(qapp):
    icons, provider = _icons()
    first = icons.row_icon("a.PNG", False, "File", "image", "purple")
    second = icons.row_icon("b.png", False, "File", "image", "purple")
    assert first is second and len(provider.asked) == 1


def test_device_folders_get_the_systems_folder_icon_once(qapp):
    icons, provider = _icons()
    a = icons.row_icon("DCIM", True, "Folder", "folder", "amber")
    b = icons.row_icon("Music", True, "Folder", "folder", "amber")
    assert a is b and _colour(a) == "#f0c040"
    assert provider.asked == [QFileIconProvider.Folder]


def test_the_parent_row_and_device_nodes_keep_their_glyphs(qapp):
    icons, provider = _icons()
    assert icons.row_icon("..", True, "Folder", "arrow-up", "accent") is \
        cached_icon("arrow-up", "accent")
    assert icons.row_icon("null", False, "Character Device", "chip", "dim") is \
        cached_icon("chip", "dim")
    assert provider.asked == []


# --------------------------------------------------------------------------- #
# the PC pane: by real path
# --------------------------------------------------------------------------- #
def test_pc_rows_are_looked_up_by_their_real_path(qapp, tmp_path):
    icons, provider = _icons()
    a = icons.row_icon("Setup.exe", False, "File", "file", "dim", local_dir=str(tmp_path))
    b = icons.row_icon("Other.exe", False, "File", "file", "dim", local_dir=str(tmp_path))
    assert [os.path.normcase(p) for p in provider.asked] == [
        os.path.normcase(os.path.join(str(tmp_path), "Setup.exe")),
        os.path.normcase(os.path.join(str(tmp_path), "Other.exe")),
    ]
    assert _colour(a) != _colour(b)  # each program shows its own icon


def test_pc_folders_use_their_own_path_too(qapp, tmp_path):
    icons, provider = _icons()
    icons.row_icon("Downloads", True, "Folder", "folder", "amber", local_dir=str(tmp_path))
    assert provider.asked[0].endswith("Downloads")  # special folders keep their icon


# --------------------------------------------------------------------------- #
# badges
# --------------------------------------------------------------------------- #
def test_a_device_link_shows_its_target_kind_with_a_link_badge(qapp):
    icons, _provider = _icons()
    folder = icons.folder()
    link = icons.row_icon("sdcard", True, "Folder Link", "link", "teal")
    assert link is not folder
    assert _colour(link, 12, 3) == _colour(folder, 12, 3)  # the folder shows through
    assert _corner_has(link, "#ffffff", "left")  # the plate, bottom-left
    assert not _corner_has(folder, "#ffffff", "left")
    assert icons.row_icon("other", True, "Folder Link", "link", "teal") is link  # cached
    file_link = icons.row_icon("clip.mp4", False, "File Link", "link", "teal")
    assert _colour(file_link, 12, 3) == "#aa00aa"  # the target type shows through


def test_an_apk_without_an_application_gets_an_android_badge(qapp):
    icons, _provider = _icons()
    apk = icons.row_icon("app-release.apk", False, "File", "apps", "green")
    assert _colour(apk, 3, 3) == FakeProvider.GENERIC  # the system's blank file
    assert _corner_has(apk, fi._ANDROID_PLATE, "right")  # the green plate, bottom-right
    assert icons.row_icon("other.apk", False, "File", "apps", "green") is apk


def test_an_apk_the_pc_knows_keeps_its_own_icon(qapp):
    icons, _provider = _icons(known=(".apk",))
    icons._provider.known.add(".apk")
    apk = icons.row_icon("app.apk", False, "File", "apps", "green")
    assert not _corner_has(apk, fi._ANDROID_PLATE, "right")


def test_badges_paint_at_every_size(qapp):
    icons, _provider = _icons()
    link = icons.row_icon("x", True, "Folder Link", "link", "teal")
    for side in (8, 16, 24, 32, 48):
        assert not link.pixmap(side, side).isNull()


def test_icon_in_ignores_the_theme(qapp, themed):
    from turboadb.gui.icons import icon, icon_in

    themed("dark")
    fixed_dark = icon_in("folder", "#123456").pixmap(24, 24).toImage()
    themed("light")
    fixed_light = icon_in("folder", "#123456").pixmap(24, 24).toImage()
    assert fixed_dark == fixed_light
    themed("dark")
    tone_dark = icon("folder", "blue").pixmap(24, 24).toImage()
    themed("light")
    assert icon("folder", "blue").pixmap(24, 24).toImage() != tone_dark  # still themed


# --------------------------------------------------------------------------- #
# in the Files tab
# --------------------------------------------------------------------------- #
def test_files_rows_use_native_icons_and_keep_their_kind(qapp, monkeypatch, tmp_path):
    from turboadb.gui.file_browser import ICON_ROLE, FileBrowser

    icons, provider = _icons()
    monkeypatch.setattr(fi, "_shared", icons)
    browser = FileBrowser(None)
    try:
        remote = browser.remote_table
        remote.base_dir = "/sdcard"
        browser._populate(remote, [
            ("DCIM", 0, "<DIR>", "Folder", "", "", "", True),
            ("a.png", 1, "1 B", "File", "", "", "", False),
        ], parent_row=True)
        rows = {remote.item(r, 0).text(): remote.item(r, 0) for r in range(remote.rowCount())}
        assert _colour(rows["a.png"].icon()) == "#3366ff"
        assert _colour(rows["DCIM"].icon()) == "#f0c040"
        assert {name: item.data(ICON_ROLE) for name, item in rows.items()} == {
            "..": "arrow-up", "DCIM": "folder", "a.png": "image"}

        local = browser.local_table
        local.base_dir = str(tmp_path)
        provider.asked.clear()
        browser._populate(local, [("tool.exe", 1, "1 B", "File", "", "", "", False)],
                          parent_row=False)
        assert os.path.normcase(provider.asked[0]) == \
            os.path.normcase(os.path.join(str(tmp_path), "tool.exe"))
    finally:
        browser.close_panel()
