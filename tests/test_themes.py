"""Theme switching, the theme set and its palettes, and the tab-row chrome.

The ribbon's sun/moon toggle used to jump to the current theme's table pair
(Slate -> Mist, Porcelain -> Graphite) while users read it as "light mode / dark
mode". It now returns to the most recently chosen theme of the other kind.
Black and White were softened (a pure #000000 / #ffffff page glared), purple,
forest and teal pairs gave way to Slate / Mist and Night / Paper, and a saved
retired theme opens as the default of its kind. The device section tabs lost a
native white rule above their corner buttons and gained header partitions;
overflowing tab rows show arrows on their scroll buttons, progress labels stay
readable over chunk and track, links follow the theme, and theme descriptions
fit their Settings cards.
Headless (Qt offscreen platform); settings live in a per-test file.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    from turboadb.gui import settings

    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "_FILE", str(path))
    monkeypatch.setattr(settings, "_cache", None)
    return path


@pytest.fixture
def app_theme(qapp):
    """Put the shared QApplication's stylesheet, palette and active theme back."""
    from PyQt5.QtGui import QPalette
    from turboadb.gui import theme

    sheet, pal, active = qapp.styleSheet(), QPalette(qapp.palette()), theme._ACTIVE_NAME
    yield qapp
    qapp.setStyleSheet(sheet)
    qapp.setPalette(pal)  # also drops the QTabBar class palette apply_to_app() sets
    theme._ACTIVE_NAME = active


def _window_class():
    from PyQt5.QtWidgets import QMainWindow, QMenu, QToolBar
    from turboadb.gui.main_window import MainWindow

    class Window(QMainWindow):
        """MainWindow's theme plumbing only: no devices, adb, timers or sidebar."""

        _apply_theme = MainWindow._apply_theme
        _add_theme_choices = MainWindow._add_theme_choices
        _build_theme_toggle = MainWindow._build_theme_toggle
        _sync_theme_action = MainWindow._sync_theme_action
        _sync_theme_menu = MainWindow._sync_theme_menu
        show_settings = MainWindow.show_settings

        def __init__(self):
            super().__init__()
            self.toggles = 0
            self.logged = []
            self.ribbon = QToolBar("TurboADB")
            self.ribbon.setObjectName("ribbon")
            self.addToolBar(self.ribbon)
            self._build_theme_toggle(self.ribbon)
            self.themes_menu = QMenu("&Themes", self)
            self._theme_menu_actions = self._add_theme_choices(self.themes_menu)

        def toggle_theme(self):
            self.toggles += 1
            MainWindow.toggle_theme(self)

        def refresh_sessions(self):
            pass

        def _log(self, text):
            self.logged.append(text)

        def _offer_adb_restart_for_new_binary(self):
            pass

        @property
        def theme_button(self):
            return self.ribbon.widgetForAction(self.act_theme)

    return Window


@pytest.fixture
def window(qapp, settings_file, app_theme):
    win = _window_class()()
    yield win
    win.close()
    win.deleteLater()


def _saved(*keys):
    from turboadb.gui import settings

    return tuple(settings.get(key) for key in keys)


def _contrast(fg, bg):
    from turboadb.gui import theme

    hi, lo = sorted((theme._luminance(fg), theme._luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _channels(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


# --------------------------------------------------------------------------- #
# the theme set
# --------------------------------------------------------------------------- #
THEME_SET = (
    ("dark", "Graphite"), ("light", "Porcelain"),
    ("mono-dark", "Black"), ("mono-light", "White"),
    ("slate-dark", "Slate"), ("slate-light", "Mist"),
    ("night-dark", "Night"), ("night-light", "Paper"),
    ("mocha-dark", "Mocha"), ("mocha-light", "Latte"),
)
RETIRED = {
    "forest-dark": "dark", "forest-light": "light",
    "plum-dark": "dark", "plum-light": "light",
    "teal-dark": "dark", "teal-light": "light",
}


def test_theme_set_is_five_dark_light_pairs_in_order():
    from turboadb.gui import theme

    assert theme.theme_names() == tuple(key for key, _label in THEME_SET)
    for index, (key, label) in enumerate(THEME_SET):
        assert theme.theme_label(key) == label
        assert theme.theme_description(key)
        # each dark theme is directly followed by its light pair
        pair = THEME_SET[index + 1 if index % 2 == 0 else index - 1][0]
        assert theme.counterpart(key) == pair
        assert theme.is_light(key) == (index % 2 == 1)
    for gone in RETIRED:
        assert gone not in theme.THEMES and gone not in theme.theme_names()
    labels = [label for _key, label in THEME_SET]
    assert not {"Plum", "Rose", "Forest", "Sage", "Deep teal", "Mint"} & set(labels)


def test_every_palette_defines_every_token():
    import re

    from turboadb.gui import theme

    tokens = set(theme.THEMES["dark"])
    assert {"chrome", "win", "panel", "raised", "input", "text", "accent", "on_accent"} <= tokens
    for name in theme.theme_names():
        palette = theme.THEMES[name]
        assert set(palette) == tokens, (name, set(palette) ^ tokens)
        for token, colour in palette.items():
            assert re.fullmatch(r"#[0-9a-f]{6}", colour), (name, token, colour)


def test_every_palette_is_easy_on_the_eyes():
    """No pure black or white surface, body text in a comfortable contrast band
    (brighter halates on dark pages, dimmer strains on light ones), and no
    light slab of a primary button glaring out of a dark theme."""
    from turboadb.gui import theme

    for name in theme.theme_names():
        c = theme.THEMES[name]
        light = theme.is_light(name)
        for surface in ("chrome", "win", "panel", "raised", "input", "button"):
            assert c[surface] not in ("#000000", "#ffffff"), (name, surface)
        low, high = (10, 14) if light else (9, 13)
        assert low <= _contrast(c["text"], c["win"]) <= high, (name, _contrast(c["text"], c["win"]))
        assert _contrast(c["input"], c["win"]) >= 1.03, name
        if not light:
            assert theme._luminance(c["accent"]) < 0.45, (name, c["accent"])
        # focus rings, hovered outlines and the tab underline use accent_text
        for surface in ("win", "panel", "input"):
            assert _contrast(c["accent_text"], c[surface]) >= 3.0, (name, surface)
        # hover and selection stay visible against the surfaces they sit on
        assert c["button_hover"] != c["button"] and c["sel"] not in (c["panel"], c["win"]), name
        assert _contrast(c["on_accent"], c["accent_hover"]) >= 4.5, name


def test_black_and_white_are_soft_but_still_black_and_white(qapp):
    from turboadb.gui import theme

    black, white = theme.THEMES["mono-dark"], theme.THEMES["mono-light"]
    assert theme.counterpart("mono-dark") == "mono-light"
    assert not theme.is_light("mono-dark") and theme.is_light("mono-light")
    # still clearly black and white: near-black and paper pages...
    assert theme._luminance(black["win"]) < 0.01 and theme._luminance(white["win"]) > 0.85
    for c in (black, white):
        # ...with neutral layered greys (at most a hint of tint)
        for key in ("chrome", "win", "panel", "raised", "input", "button", "border", "text"):
            r, g, b = _channels(c[key])
            assert max(r, g, b) - min(r, g, b) <= 3, (key, c[key])
        # errors still read as errors
        r, g, b = _channels(c["danger_text"])
        assert r > g + 60 and r > b + 60
    # softened text: about 11-12:1, where it used to be 17:1
    assert 10 <= _contrast(black["text"], black["win"]) <= 12.5
    assert 11 <= _contrast(white["text"], white["win"]) <= 13
    # Black's primary fill is a deep, barely-tinted steel under light text,
    # not the light-grey slab that outshone every button beside it
    assert theme._luminance(black["accent"]) < 0.08
    assert theme._luminance(black["on_accent"]) > theme._luminance(black["accent"])
    assert max(_channels(black["accent"])) - min(_channels(black["accent"])) <= 24
    assert theme._luminance(white["accent"]) < 0.08  # a calm dark accent on paper
    css = theme.stylesheet("mono-dark")
    assert "background: #000000" not in css and "#ffffff" not in css
    assert f"color: {black['text']}" in css


def test_black_and_white_icon_hues_follow_their_kind():
    from turboadb.gui import theme

    for tone in theme.TONES:
        dark_value, light_value = theme._HUES[tone]
        assert theme.hue(tone, "mono-dark") == dark_value
        assert theme.hue(tone, "mono-light") == light_value
        assert theme.hue(tone, "night-light") == light_value
        assert theme.hue(tone, "slate-dark") == dark_value
    assert theme.report_palette("mono-light")["background"] == theme.THEMES["mono-light"]["input"]


def test_every_palette_keeps_fields_distinct_from_the_page():
    """Borderless editors (deploy hosts, the file editor) paint ``input`` straight
    onto ``win``; White once had both at #ffffff and they vanished."""
    from turboadb.gui import theme

    for name, c in theme.THEMES.items():
        assert c["input"].lower() != c["win"].lower(), name
        # at least as distinct as the subtlest existing palette (about 1.03)
        assert _contrast(c["input"], c["win"]) >= 1.03, name
        assert _contrast(c["text"], c["input"]) >= 7, name
    white = theme.THEMES["mono-light"]
    assert _contrast(white["placeholder"], white["input"]) >= 4.5


def test_new_pairs_have_their_own_character():
    from turboadb.gui import theme

    def hue_and_saturation(colour):
        import colorsys

        r, g, b = (value / 255 for value in _channels(colour))
        h, lightness, s = colorsys.rgb_to_hls(r, g, b)
        return h * 360, s

    # Slate / Mist: cool blue-grey, low saturation
    for name in ("slate-dark", "slate-light"):
        h, s = hue_and_saturation(theme.THEMES[name]["win"])
        assert 190 <= h <= 230 and s < 0.3, (name, h, s)
    # Night / Paper: warm, and apart from Mocha / Latte
    for name in ("night-dark", "night-light"):
        h, s = hue_and_saturation(theme.THEMES[name]["win"])
        assert 20 <= h <= 50 and s < 0.35, (name, h, s)
    night, mocha = theme.THEMES["night-dark"], theme.THEMES["mocha-dark"]
    paper, latte = theme.THEMES["night-light"], theme.THEMES["mocha-light"]
    assert hue_and_saturation(night["win"])[1] < hue_and_saturation(mocha["win"])[1]  # greyer
    assert theme._luminance(paper["win"]) > theme._luminance(latte["win"]) + 0.1  # lighter
    assert night["accent"] != mocha["accent"] and paper["accent"] != latte["accent"]


# --------------------------------------------------------------------------- #
# retired themes
# --------------------------------------------------------------------------- #
def test_retired_themes_resolve_to_the_default_of_their_kind(qapp):
    from turboadb.gui import theme

    for gone, default in RETIRED.items():
        assert theme.resolve_name(gone) == default
        assert theme.palette(gone) is theme.THEMES[default]
        assert theme.is_light(gone) == theme.is_light(default)
        assert theme.theme_label(gone) == theme.theme_label(default)
        assert theme.counterpart(gone) == theme.counterpart(default)
        assert theme.accent_text(gone) == theme.accent_text(default)
        assert theme.section_text(gone) == theme.section_text(default)
        assert theme.stylesheet(gone) == theme.stylesheet(default)
    for key in theme.theme_names():
        assert theme.resolve_name(key) == key
    assert theme.resolve_name("no-such-theme") == "dark"
    assert theme.resolve_name("no-such-theme", "light") == "light"
    assert theme.resolve_name(None, None) is None and theme.resolve_name(["x"]) == "dark"
    # choosing a theme while leaving a retired one records the retired kind's default
    assert theme.theme_choice("slate-light", previous="plum-dark") == {
        "theme": "slate-light", "theme_last_light": "slate-light", "theme_last_dark": "dark",
    }
    assert theme.theme_choice("teal-light") == {"theme": "light", "theme_last_light": "light"}


def test_saved_retired_themes_toggle_and_apply_as_their_kind_default(settings_file, app_theme):
    from turboadb.gui import settings, theme

    settings.update({
        "theme": "plum-light", "theme_last_dark": "forest-dark", "theme_last_light": "teal-light",
    })
    assert theme.current_name() == "light"
    assert theme.toggle_target() == "dark"  # Forest, the last dark, is Graphite now
    assert theme.toggle_target("night-dark") == "light"  # Mint, the last light, is Porcelain
    assert theme.toggle_target("plum-dark") == "light"
    # a retired memory of the wrong kind still falls back to the default
    settings.update({"theme_last_dark": "plum-light", "theme_last_light": "forest-dark"})
    assert theme.toggle_target("slate-light") == "dark"
    assert theme.toggle_target("slate-dark") == "light"

    # startup: app.py applies the raw saved name, which also rewrites the file,
    # so code reading the settings directly (Settings dialog) sees a real theme
    settings.update({"theme_last_dark": "forest-dark", "theme_last_light": "teal-light"})
    theme.apply_to_app(app_theme, settings.get("theme"))
    assert theme._ACTIVE_NAME == "light"
    assert app_theme.styleSheet() == theme.stylesheet("light")
    assert _saved("theme", "theme_last_dark", "theme_last_light") == ("light", "dark", "light")
    # a file without retired names is left alone
    settings.update({"theme": "night-light", "theme_last_dark": "slate-dark"})
    before = settings_file.read_text("utf-8")
    theme.apply_to_app(app_theme, "night-light")
    assert settings_file.read_text("utf-8") == before


def test_menus_and_settings_dialog_show_a_retired_theme_as_its_kind_default(
    qapp, settings_file, app_theme
):
    from turboadb.gui import settings, theme
    from turboadb.gui.settings_dialog import SettingsDialog

    settings.update({"theme": "forest-light", "theme_last_dark": "teal-dark"})
    theme.apply_to_app(qapp, settings.get("theme"))  # what app.py does at startup
    win = _window_class()()
    dlg = SettingsDialog()
    try:
        win._sync_theme_menu()
        checked = [key for key, a in win._theme_menu_actions.items() if a.isChecked()]
        assert checked == ["light"]
        assert [k for k, a in win._theme_button_actions.items() if a.isChecked()] == ["light"]
        assert win.act_theme.text() == "Graphite"
        assert dlg._orig_theme == "light" and dlg._theme_buttons["light"].isChecked()
        assert dlg.changed_settings() == {}
    finally:
        dlg.reject()
        dlg.deleteLater()
        win.deleteLater()


# --------------------------------------------------------------------------- #
# the toggle rule
# --------------------------------------------------------------------------- #
def test_toggle_target_defaults_to_graphite_and_porcelain(settings_file):
    from turboadb.gui import settings, theme

    assert settings.DEFAULTS["theme_last_dark"] == "dark"
    assert settings.DEFAULTS["theme_last_light"] == "light"
    for name in theme.theme_names():
        assert theme.toggle_target(name) == ("dark" if theme.is_light(name) else "light")
    # no argument: from the saved theme
    settings.set("theme", "slate-dark")
    assert theme.toggle_target() == "light"


def test_toggle_target_ignores_unknown_or_wrong_kind_memories(settings_file):
    from turboadb.gui import settings, theme

    settings.update({"theme_last_dark": "no-such-theme", "theme_last_light": "night-dark"})
    assert theme.toggle_target("slate-light") == "dark"
    assert theme.toggle_target("slate-dark") == "light"
    settings.update({"theme_last_dark": "night-dark", "theme_last_light": "mono-light"})
    assert theme.toggle_target("slate-light") == "night-dark"
    assert theme.toggle_target("slate-dark") == "mono-light"


def test_theme_choice_records_the_chosen_and_the_replaced_theme_per_kind():
    from turboadb.gui import theme

    assert theme.theme_choice("slate-dark") == {
        "theme": "slate-dark", "theme_last_dark": "slate-dark",
    }
    assert theme.theme_choice("slate-light", previous="slate-dark") == {
        "theme": "slate-light", "theme_last_light": "slate-light", "theme_last_dark": "slate-dark",
    }
    # same kind: the new choice wins; unknown names are never remembered
    assert theme.theme_choice("night-dark", previous="slate-dark") == {
        "theme": "night-dark", "theme_last_dark": "night-dark",
    }
    assert theme.theme_choice("light", previous="bogus") == {
        "theme": "light", "theme_last_light": "light",
    }
    # the table pairing is unchanged for existing callers
    assert theme.counterpart("slate-dark") == "slate-light"


def test_toggle_goes_slate_porcelain_slate_and_back_to_slate_after_mist(window):
    from turboadb.gui import settings

    window._theme_menu_actions["slate-dark"].trigger()
    assert settings.get("theme") == "slate-dark"
    window.act_theme.trigger()
    assert settings.get("theme") == "light"  # Porcelain, the default light
    window.act_theme.trigger()
    assert settings.get("theme") == "slate-dark"  # not Graphite
    window._theme_menu_actions["slate-light"].trigger()  # Mist from the menu
    window.act_theme.trigger()
    assert settings.get("theme") == "slate-dark"
    window.act_theme.trigger()
    assert settings.get("theme") == "slate-light"  # Mist is now the light choice
    assert window.toggles == 4


def test_toggle_uses_defaults_when_nothing_was_chosen(window):
    from turboadb.gui import settings

    assert window.act_theme.text() == "Porcelain"
    window.act_theme.trigger()
    assert settings.get("theme") == "light"
    window.act_theme.trigger()
    assert settings.get("theme") == "dark"


def test_toggle_returns_to_the_theme_of_a_file_older_than_the_memory(qapp, settings_file, app_theme):
    """A settings file that only names ``theme`` must still toggle back to it."""
    import json

    from turboadb.gui import settings

    settings_file.write_text(json.dumps({"settings_version": 3, "theme": "slate-dark"}), "utf-8")
    win = _window_class()()
    try:
        assert win.act_theme.text() == "Porcelain"
        win.act_theme.trigger()
        assert settings.get("theme") == "light"
        win.act_theme.trigger()
        assert settings.get("theme") == "slate-dark"
    finally:
        win.deleteLater()


# --------------------------------------------------------------------------- #
# every selection path records the choice per kind
# --------------------------------------------------------------------------- #
def test_every_theme_selection_path_remembers_its_kind(window, monkeypatch):
    import turboadb.gui.main_window as mw_mod
    from turboadb.gui.settings_dialog import SettingsDialog

    keys = ("theme", "theme_last_dark", "theme_last_light")
    window._theme_menu_actions["night-dark"].trigger()  # Themes menu
    assert _saved(*keys) == ("night-dark", "night-dark", "light")
    window._theme_button_actions["mono-light"].trigger()  # ribbon dropdown
    assert _saved(*keys) == ("mono-light", "night-dark", "mono-light")
    window.act_theme.trigger()  # the toggle
    assert _saved(*keys) == ("night-dark", "night-dark", "mono-light")

    class Chooser(SettingsDialog):
        def exec_(self):
            self._select_theme("slate-light")
            self.accept()
            return self.result()

    monkeypatch.setattr(mw_mod, "SettingsDialog", Chooser)
    window.show_settings()  # Settings dialog
    assert _saved(*keys) == ("slate-light", "night-dark", "slate-light")
    window.act_theme.trigger()
    assert _saved("theme") == ("night-dark",)


def test_settings_dialog_adds_the_memory_only_when_the_theme_changed(qapp, settings_file, app_theme):
    from turboadb.gui import settings
    from turboadb.gui.settings_dialog import SettingsDialog

    settings.set("theme", "slate-dark")
    dlg = SettingsDialog()
    try:
        assert dlg.changed_settings() == {}
        dlg._select_theme("slate-light")
        assert dlg.changed_settings() == {
            "theme": "slate-light", "theme_last_light": "slate-light", "theme_last_dark": "slate-dark",
        }
        dlg._select_theme("slate-dark")  # back to where it started: nothing to save
        assert dlg.changed_settings() == {}
    finally:
        dlg.reject()
        dlg.deleteLater()


# --------------------------------------------------------------------------- #
# the ribbon split button
# --------------------------------------------------------------------------- #
def test_ribbon_dropdown_lists_every_theme_in_dark_and_light_groups(window):
    from PyQt5.QtWidgets import QToolButton
    from turboadb.gui import theme

    button = window.theme_button
    assert button is not None and button.objectName() == "themeToggle"
    assert button.popupMode() == QToolButton.MenuButtonPopup
    menu = window.act_theme.menu()
    assert menu is not None
    labels = [(a.text(), a.isEnabled()) for a in menu.actions() if not a.isSeparator()]
    dark = [theme.theme_label(n) for n in theme.theme_names() if not theme.is_light(n)]
    light = [theme.theme_label(n) for n in theme.theme_names() if theme.is_light(n)]
    assert dark == ["Graphite", "Black", "Slate", "Night", "Mocha"]
    assert light == ["Porcelain", "White", "Mist", "Paper", "Latte"]
    assert labels == (
        [("Dark themes", False)] + [(label, True) for label in dark]
        + [("Light themes", False)] + [(label, True) for label in light]
    )
    assert set(window._theme_button_actions) == set(theme.theme_names())
    checked = [key for key, a in window._theme_button_actions.items() if a.isChecked()]
    assert checked == ["dark"]
    # the stylesheet gives this one button a visible, clickable arrow
    css = theme.stylesheet("dark")
    assert "QToolButton#themeToggle::menu-button" in css
    assert "QToolBar#ribbon QToolButton#themeToggle { padding-right: 18px; }" in css


def test_choosing_a_dropdown_theme_applies_it_without_toggling(window):
    from turboadb.gui import settings, theme

    window._theme_menu_actions["slate-dark"].trigger()
    button = window.theme_button
    mist = window._theme_button_actions["slate-light"]
    mist.trigger()
    # A QToolButton re-emits its menu's items as triggered(QAction), and the
    # toolbar forwards that as actionTriggered: neither may toggle.
    button.triggered.emit(mist)
    window.ribbon.actionTriggered.emit(mist)
    assert window.toggles == 0
    assert settings.get("theme") == "slate-light"
    assert theme._ACTIVE_NAME == "slate-light"
    assert window._theme_button_actions["slate-light"].isChecked()
    assert window._theme_menu_actions["slate-light"].isChecked()
    assert not window._theme_button_actions["slate-dark"].isChecked()
    # clicking the button itself still toggles, exactly once
    button.click()
    assert window.toggles == 1
    assert settings.get("theme") == "slate-dark"
    assert window._theme_button_actions["slate-dark"].isChecked()


def test_ribbon_label_and_tooltip_name_the_target_theme(window):
    from turboadb.gui import icons, theme

    def shows(label):
        assert window.act_theme.text() == label
        assert window.act_theme.toolTip().startswith(f"Switch to the {label} theme")
        assert window.theme_button.toolTip() == window.act_theme.toolTip()

    shows("Porcelain")
    window._theme_menu_actions["slate-dark"].trigger()
    shows("Porcelain")
    window.act_theme.trigger()
    shows("Slate")
    window._theme_menu_actions["slate-light"].trigger()
    shows("Slate")
    window._theme_menu_actions["mono-dark"].trigger()
    shows("Mist")
    sun = icons.icon("sun", "amber").pixmap(20, 20).toImage()
    assert window.act_theme.icon().pixmap(20, 20).toImage() == sun
    assert theme.is_light("slate-light")


def _menu_row(menu, action, checked):
    """The pixels of *action*'s row in *menu*, drawn checked or unchecked."""
    action.setChecked(checked)
    menu.ensurePolished()
    menu.adjustSize()
    return menu.grab().copy(menu.actionGeometry(action)).toImage()


@pytest.mark.parametrize("name", ["mono-light", "mono-dark", "dark", "night-light"])
def test_theme_menus_visibly_mark_the_active_theme(qapp, window, name):
    """Under the stylesheet an item's icon replaces its check mark, so iconed
    theme items drew checked and unchecked rows pixel-identical."""
    from turboadb.gui import theme

    theme.apply_to_app(qapp, name)
    for menu, actions in (
        (window.act_theme.menu(), window._theme_button_actions),
        (window.themes_menu, window._theme_menu_actions),
    ):
        action = actions[name]
        assert _menu_row(menu, action, True) != _menu_row(menu, action, False)
        assert action.icon().isNull()  # the sun/moon lives on the group heading
    headings = [a for a in window.themes_menu.actions() if not a.isSeparator() and not a.isEnabled()]
    assert [a.text() for a in headings] == ["Dark themes", "Light themes"]
    assert all(not a.icon().isNull() for a in headings)


# --------------------------------------------------------------------------- #
# Settings OK / Cancel
# --------------------------------------------------------------------------- #
def _settings_that(monkeypatch, pick, accept):
    import turboadb.gui.main_window as mw_mod
    from turboadb.gui.settings_dialog import SettingsDialog

    class Chooser(SettingsDialog):
        def exec_(self):
            self._select_theme(pick)  # the live preview
            (self.accept if accept else self.reject)()
            return self.result()

    monkeypatch.setattr(mw_mod, "SettingsDialog", Chooser)


def test_settings_ok_applies_exactly_the_chosen_theme(window, monkeypatch, app_theme):
    from turboadb.gui import settings, theme

    window._theme_menu_actions["slate-dark"].trigger()
    applied = []
    real_apply = type(window)._apply_theme

    def spy(name, **kwargs):
        applied.append((name, kwargs))
        real_apply(window, name, **kwargs)

    window._apply_theme = spy
    _settings_that(monkeypatch, "mono-light", accept=True)
    window.show_settings()
    assert applied == [("mono-light", {"persist": False, "announce": False})]
    assert settings.get("theme") == "mono-light"
    assert theme._ACTIVE_NAME == "mono-light"
    assert app_theme.styleSheet() == theme.stylesheet("mono-light")
    assert window._theme_button_actions["mono-light"].isChecked()
    assert window.act_theme.text() == "Slate"
    assert window.logged[-1] == "[OK] Settings saved — theme: White"


def test_settings_cancel_reverts_the_preview(window, monkeypatch, app_theme):
    from turboadb.gui import settings, theme

    window._theme_menu_actions["slate-dark"].trigger()
    before = _saved("theme", "theme_last_dark", "theme_last_light")
    applied = []
    window._apply_theme = lambda name, **kwargs: applied.append(name)
    _settings_that(monkeypatch, "mono-light", accept=False)
    window.show_settings()
    assert applied == []
    assert _saved("theme", "theme_last_dark", "theme_last_light") == before
    assert settings.get("theme") == "slate-dark"
    assert theme._ACTIVE_NAME == "slate-dark"
    assert app_theme.styleSheet() == theme.stylesheet("slate-dark")


# --------------------------------------------------------------------------- #
# tab rows: header partitions and the corner of the device section tabs
# --------------------------------------------------------------------------- #
def test_tab_rows_have_partitions_and_a_clean_corner_in_every_theme(qapp):
    from turboadb.gui import theme

    for name in theme.theme_names():
        c = theme.THEMES[name]
        css = theme.stylesheet(name)
        rule = theme._mix(c["border"], c["frame"], 0.3).lstrip("#")
        for tabs in ("mainTabs", "deviceTabs"):
            body = css.split(f"QTabWidget#{tabs} QTabBar::tab {{", 1)[1].split("}", 1)[0]
            assert f"turboadb-tabsep-{rule}.png" in body, (name, tabs)
            assert "background-position: right center;" in body
            assert "background-repeat: no-repeat;" in body
            # no stray partition after the last header or beside the selected one
            assert (f"QTabWidget#{tabs} QTabBar::tab:last, "
                    f"QTabWidget#{tabs} QTabBar::tab:next-selected {{") in css
        assert f"QTabWidget#deviceTabs > QWidget#deviceActions {{ background: {c['win']}; }}" in css
        # rings and the tab underline use the text-safe accent; fills keep accent
        assert f"QFrame#animatedTabIndicator {{ background: {c['accent_text']};" in css
        assert (f"QLineEdit:focus, QSpinBox:focus, QComboBox:focus "
                f"{{ border: 1px solid {c['accent_text']}; }}") in css
        assert f'QPushButton[role="ok"], QToolButton[role="ok"] {{ background: {c["accent"]};' in css
    # the device section tab font weight still lives on the bar, not on ::tab
    assert "QTabBar { font-weight: 600;" in theme.stylesheet("dark")


def test_apply_to_app_paints_native_bevels_in_theme_colours(app_theme):
    from PyQt5.QtGui import QPalette
    from PyQt5.QtWidgets import QApplication
    from turboadb.gui import theme

    for name in ("mono-dark", "night-light"):
        c = theme.THEMES[name]
        theme.apply_to_app(app_theme, name)
        app_palette, bars = QApplication.palette(), QApplication.palette("QTabBar")
        for role in (QPalette.Light, QPalette.Midlight, QPalette.Mid, QPalette.Dark):
            assert app_palette.color(role).name() == c["border"], (name, role)
            assert bars.color(role).name() == c["win"], (name, role)
        assert app_palette.color(QPalette.PlaceholderText).name() == c["placeholder"]


def _section_tabs(qapp):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QHBoxLayout, QTabWidget, QToolButton, QWidget

    tabs = QTabWidget()
    tabs.setObjectName("deviceTabs")
    tabs.setDocumentMode(True)
    tabs.tabBar().setDrawBase(False)
    tabs.tabBar().setExpanding(False)
    corner = QWidget()
    corner.setObjectName("deviceActions")
    row = QHBoxLayout(corner)
    row.setContentsMargins(0, 2, 8, 2)
    screen = QToolButton()
    screen.setText("Screen")
    screen.setProperty("role", "ok")
    row.addWidget(screen)
    shot = QToolButton()
    shot.setText("Screenshot")
    row.addWidget(shot)
    tabs.setCornerWidget(corner, Qt.TopRightCorner)
    for label in ("Terminal", "Logcat", "Files", "Apps"):
        tabs.addTab(QWidget(), label)
    tabs.resize(1000, 120)  # wide enough that no header scrolls under the corner
    tabs.show()
    qapp.processEvents()
    return tabs, corner


@pytest.mark.parametrize("name", ["mono-dark", "dark", "mono-light", "night-dark", "slate-light"])
def test_section_tab_row_renders_partitions_and_no_rule_above_the_corner(app_theme, name):
    """The native tab-bar base line (white under Fusion, light grey under the
    Windows style) ran across the top of the device actions; the headers had no
    partition. Rendered, as the user sees it."""
    from PyQt5.QtGui import QColor
    from turboadb.gui import theme

    theme.apply_to_app(app_theme, name)
    c = theme.THEMES[name]
    tabs, corner = _section_tabs(app_theme)
    try:
        image = tabs.grab().toImage()
        top = corner.geometry().top()
        for y in (top, top + 1):
            for x in range(corner.geometry().left(), corner.geometry().right() + 1, 7):
                assert QColor(image.pixel(x, y)).name() == c["win"], (name, x, y)
        bar = tabs.tabBar()
        assert bar.tabRect(bar.count() - 1).right() < corner.geometry().left()

        def partition_at(index):
            rect = bar.tabRect(index)
            y = rect.center().y()
            return any(
                QColor(image.pixel(x, y)).name() != c["win"]
                for x in range(rect.right() - 2, rect.right() + 1)
            )

        assert not partition_at(0)  # the selected header stands alone
        assert partition_at(1) and partition_at(2)
        assert not partition_at(3)  # nothing after the last header
    finally:
        tabs.close()
        tabs.deleteLater()


def _overflowing_tabs(qapp):
    """A main tab row and, on its first page, a device section row, both too
    narrow for their headers (a 1366 px laptop with the corner actions)."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QTabWidget, QVBoxLayout, QWidget

    def tab_widget(name, labels, closable=False):
        tabs = QTabWidget()
        tabs.setObjectName(name)
        tabs.setDocumentMode(True)
        tabs.setTabsClosable(closable)
        tabs.setElideMode(Qt.ElideNone)
        tabs.setUsesScrollButtons(True)
        tabs.tabBar().setExpanding(False)
        tabs.tabBar().setDrawBase(False)
        for label in labels:
            tabs.addTab(QWidget(), label)
        return tabs

    main = tab_widget("mainTabs", ["IVI head unit", "Pixel 7", "emulator-5554", "R-Car H3"], True)
    page = main.widget(0)
    inner = tab_widget("deviceTabs", ["Terminal", "Logcat", "Files", "Device Control", "Apps",
                                      "Phone", "Webcam"])
    QVBoxLayout(page).addWidget(inner)
    main.resize(360, 160)
    main.show()
    qapp.processEvents()
    return main, inner


@pytest.mark.parametrize("name", [key for key, _label in THEME_SET])
def test_overflowing_tab_rows_show_scroll_arrows_in_every_theme(app_theme, name):
    """The scroll buttons of an overflowing tab bar were blank boxes: the
    generic button padding left no room for Qt's arrow."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QColor
    from PyQt5.QtWidgets import QToolButton
    from turboadb.gui import theme

    theme.apply_to_app(app_theme, name)
    c = theme.THEMES[name]
    main, inner = _overflowing_tabs(app_theme)
    try:
        image = main.grab().toImage()
        for tabs, row in ((main, c["chrome"]), (inner, c["win"])):
            bar = tabs.tabBar()
            buttons = {b.arrowType(): b for b in bar.findChildren(QToolButton) if b.isVisible()}
            assert set(buttons) == {Qt.LeftArrow, Qt.RightArrow}, (name, tabs.objectName())
            # scrolled to the start: left is disabled, right is enabled
            assert not buttons[Qt.LeftArrow].isEnabled() and buttons[Qt.RightArrow].isEnabled()
            for arrow, button in buttons.items():
                assert button.width() >= 18, (name, button.width())
                rect = button.geometry().translated(bar.mapTo(main, bar.rect().topLeft()))
                pixels = [
                    (x, y, QColor(image.pixel(x, y)).name())
                    for x in range(rect.left(), rect.right() + 1)
                    for y in range(rect.top(), rect.bottom() + 1)
                ]
                # opaque in the row's colour, so scrolled headers don't show through
                colours = [colour for _x, _y, colour in pixels]
                assert max(set(colours), key=colours.count) == row, (name, tabs.objectName())
                wanted = 3.0 if button.isEnabled() else 1.8
                mark = [(x, y) for x, y, colour in pixels if _contrast(colour, row) >= wanted]
                xs, ys = [x for x, _y in mark], [y for _x, y in mark]
                assert len(mark) >= 6, (name, tabs.objectName(), arrow, len(mark))
                assert max(xs) - min(xs) >= 3 and max(ys) - min(ys) >= 6, (name, arrow)
                strongest = max(_contrast(colour, row) for _x, _y, colour in pixels)
                if button.isEnabled():
                    assert strongest >= _contrast(c["text"], row) * 0.8, (name, strongest)
                else:  # visibly dimmer than an enabled arrow
                    assert strongest < _contrast(c["text"], row) * 0.8, (name, strongest)
    finally:
        main.close()
        main.deleteLater()


def test_scroll_arrow_rules_cover_every_direction_and_state(qapp):
    from turboadb.gui import theme

    for name in theme.theme_names():
        c = theme.THEMES[name]
        css = theme.stylesheet(name)
        assert "QTabBar::scroller { width: 44px; }" in css
        for direction in ("left", "right", "up", "down"):
            kind = "arrow" if direction == "down" else f"arrow-{direction}"
            for state, colour in (("", c["text"]), (":disabled", c["frame"])):
                rule = css.split(f"QTabBar QToolButton::{direction}-arrow{state} {{", 1)[1]
                assert f"turboadb-{kind}-{colour.lstrip('#')}.png" in rule.split("}", 1)[0]
        for selector in ("QTabBar QToolButton:hover", "QTabBar QToolButton:pressed",
                         "QTabBar QToolButton, QTabBar QToolButton:disabled"):
            assert selector in css, (name, selector)


def _rule(css, selector):
    """The declarations of the first rule that starts with exactly *selector*."""
    return css.split(f"\n    {selector} {{", 1)[1].split("}", 1)[0]


def _declared(declarations, prop):
    import re

    return re.search(rf"(?<![-\w]){prop}:\s*(#[0-9a-f]{{6}})", declarations).group(1)


@pytest.mark.parametrize("name", [key for key, _label in THEME_SET])
def test_progress_bar_label_reads_over_chunk_and_track(app_theme, name):
    """One label colour crosses both the chunk and the empty track: Graphite's
    light-blue and White's near-black chunks left it unreadable."""
    from PyQt5.QtGui import QColor
    from PyQt5.QtWidgets import QProgressBar
    from turboadb.gui import theme

    css = theme.stylesheet(name)
    bar_rule = _rule(css, "QProgressBar")
    track, label = _declared(bar_rule, "background"), _declared(bar_rule, "color")
    chunk = _declared(_rule(css, "QProgressBar::chunk"), "background")
    assert _contrast(label, track) >= 4.5, (name, label, track)
    assert _contrast(label, chunk) >= 4.5, (name, label, chunk)
    assert _contrast(chunk, track) >= 1.4, (name, chunk, track)  # progress still shows

    theme.apply_to_app(app_theme, name)
    bar = QProgressBar()
    bar.resize(300, 16)
    bar.setValue(50)
    bar.show()
    app_theme.processEvents()
    try:
        image = bar.grab().toImage()
        middle = bar.height() // 2
        assert QColor(image.pixel(30, middle)).name() == chunk
        assert QColor(image.pixel(270, middle)).name() == track
    finally:
        bar.close()
        bar.deleteLater()


def test_first_and_last_section_tabs_stay_whole_beside_the_scroll_buttons(app_theme):
    """The wider scroll buttons must not clip the first or last header: a
    selected header is scrolled fully into the room left of them (the device
    section row on a 1366 px laptop with the device list open)."""
    from PyQt5.QtWidgets import QToolButton
    from turboadb.gui import theme

    theme.apply_to_app(app_theme, "dark")
    tabs, _corner = _section_tabs(app_theme)
    try:
        for label in ("Device Control", "Phone", "Webcam"):
            tabs.addTab(type(tabs.widget(0))(), label)
        bar = tabs.tabBar()
        for width in (1116, 900, 700):
            tabs.resize(width, 120)
            app_theme.processEvents()
            buttons = [b for b in bar.findChildren(QToolButton) if b.isVisible()]
            room = min((b.x() for b in buttons), default=bar.width())
            if width == 700:
                assert len(buttons) == 2  # the row really overflows
            for index in (0, bar.count() - 1, 0):
                tabs.setCurrentIndex(index)
                app_theme.processEvents()
                rect = bar.tabRect(index)
                assert rect.left() >= 0 and rect.right() < room, (width, index, rect, room)
    finally:
        tabs.close()
        tabs.deleteLater()


def test_links_are_readable_and_follow_a_live_theme_switch(app_theme):
    """Rich-text links (the file link in a "Saved ..." toast) drew in Qt's
    default dark blue on every theme, and a label kept the colour it was
    created with."""
    from PyQt5.QtGui import QColor, QPalette
    from PyQt5.QtWidgets import QApplication, QLabel
    from turboadb.gui import theme

    label = QLabel('Saved to <a href="file:///tmp">the Pictures folder</a>')
    label.resize(320, 30)
    label.show()
    try:
        for name in theme.theme_names():  # one label, switched through every theme
            c = theme.THEMES[name]
            theme.apply_to_app(app_theme, name)
            app_theme.processEvents()
            palette = QApplication.palette()
            for role in (QPalette.Link, QPalette.LinkVisited):
                assert palette.color(role).name() == c["accent_text"], (name, role)
            for surface in ("win", "panel", "raised", "input", "chrome"):
                assert _contrast(c["accent_text"], c[surface]) >= 4.5, (name, surface)
            image = label.grab().toImage()
            drawn = {
                QColor(image.pixel(x, y)).name()
                for x in range(image.width()) for y in range(image.height())
            }
            assert c["accent_text"] in drawn, name  # the underline, in this theme's colour
    finally:
        label.close()
        label.deleteLater()


def test_theme_descriptions_are_short_enough_for_their_settings_cards():
    from turboadb.gui import theme

    for name in theme.theme_names():
        description = theme.theme_description(name)
        assert 20 <= len(description) <= 38, (name, len(description))
        assert description.endswith(".") and "—" not in description


_CARD_WIDTHS = """
import json
from PyQt5.QtGui import QFontInfo
from PyQt5.QtWidgets import QApplication
app = QApplication([])
from turboadb.gui import theme
from turboadb.gui.settings_dialog import SettingsDialog
theme.apply_to_app(app, "dark")
dlg = SettingsDialog()
dlg.show()
titles = [dlg.nav.item(i).text() for i in range(dlg.nav.count())]
dlg.nav.setCurrentRow(titles.index("Themes"))
app.processEvents()
print(json.dumps({
    name: [QFontInfo(card.font()).family(), card.sizeHint().width(), card.width()]
    for name, card in dlg._theme_buttons.items()
}))
"""


@pytest.mark.skipif(
    not os.path.exists(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf")),
    reason="needs Segoe UI, the font the stylesheet asks for",
)
def test_theme_descriptions_fit_their_settings_cards():
    """Descriptions longer than a card were cut off with "..." at the Settings
    dialog's default size. Measured in a child process with the system fonts:
    the shared offscreen test application has no font directory, so its text
    widths mean nothing, and giving it one would change every other GUI test."""
    import json
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=root,
               QT_QPA_FONTDIR=os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
    done = subprocess.run([sys.executable, "-c", _CARD_WIDTHS], cwd=root, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    cards = json.loads(done.stdout.strip().splitlines()[-1])
    assert len(cards) == 10
    for name, (family, needed, width) in cards.items():
        assert family == "Segoe UI", (name, family)
        assert needed <= width, (name, needed, width)


# --------------------------------------------------------------------------- #
# Settings: screen renderer
# --------------------------------------------------------------------------- #
def test_screen_renderer_choice_defaults_to_scrcpy_and_round_trips(qapp, settings_file, app_theme):
    from turboadb.gui import settings
    from turboadb.gui.settings_dialog import SettingsDialog

    assert settings.DEFAULTS["screen_backend"] == "scrcpy"
    assert settings.SCREEN_BACKENDS == ("scrcpy", "screencap")
    assert settings.get("screen_backend") == "scrcpy"

    dlg = SettingsDialog()
    try:
        combo = dlg.screen_backend
        assert [combo.itemData(i) for i in range(combo.count())] == ["scrcpy", "screencap"]
        assert combo.currentData() == "scrcpy"
        assert "recommended" in combo.itemText(0) and "screencap" in combo.itemText(1)
        combo.setCurrentIndex(combo.findData("screencap"))
        assert dlg.changed_settings() == {"screen_backend": "screencap"}
        dlg.accept()  # OK persists it
    finally:
        dlg.deleteLater()
    assert settings.get("screen_backend") == "screencap"

    dlg = SettingsDialog()
    try:
        assert dlg.screen_backend.currentData() == "screencap"
        dlg.screen_backend.setCurrentIndex(0)
        dlg.reject()  # Cancel discards the change
    finally:
        dlg.deleteLater()
    assert settings.get("screen_backend") == "screencap"

    settings.set("screen_backend", "something-else")  # a hand-edited file
    dlg = SettingsDialog()
    try:
        assert dlg.screen_backend.currentData() == "scrcpy"
    finally:
        dlg.reject()
        dlg.deleteLater()


# --------------------------------------------------------------------------- #
# Settings: small screens
# --------------------------------------------------------------------------- #
class _Screen:
    def __init__(self, height):
        self.height = height

    def availableGeometry(self):
        from PyQt5.QtCore import QRect

        return QRect(0, 0, 1280, self.height)


def _settings_on_screen(monkeypatch, height):
    import types

    from PyQt5.QtWidgets import QApplication
    from turboadb.gui import settings_dialog as sd

    monkeypatch.setattr(
        sd,
        "QApplication",
        types.SimpleNamespace(
            instance=QApplication.instance, primaryScreen=lambda: _Screen(height)
        ),
    )
    return sd.SettingsDialog()


def test_settings_pages_scroll_so_ok_and_cancel_fit_small_screens(
    qapp, settings_file, app_theme, monkeypatch
):
    from PyQt5.QtWidgets import QDialogButtonBox, QFrame, QScrollArea
    from turboadb.gui import theme

    theme.apply_to_app(qapp, "dark")
    dlg = _settings_on_screen(monkeypatch, 680)  # 1280x720, or 1366x768 at 125%
    try:
        assert dlg.pages.count() == dlg.nav.count() == 6
        for index in range(dlg.pages.count()):
            scroll = dlg.pages.widget(index).findChild(QScrollArea, "settingsPageScroll")
            assert scroll is not None, dlg.nav.item(index).text()
            assert scroll.widgetResizable() and scroll.frameShape() == QFrame.NoFrame
            assert scroll.widget().objectName() == "settingsPageBody"
        # OK / Cancel live in the footer, outside every scrolling page
        parent = dlg.findChild(QDialogButtonBox).parentWidget()
        while parent is not None and parent is not dlg:
            assert not isinstance(parent, QScrollArea)
            parent = parent.parentWidget()
        # the scrcpy and Themes pages once forced a 654 px minimum (590 before
        # the renderer row); scrolling pages leave only the chrome
        assert dlg.minimumSizeHint().height() <= 590
        assert dlg.height() <= 680 - 60
        assert "QScrollArea#settingsPageScroll" in theme.stylesheet("dark")
    finally:
        dlg.reject()
        dlg.deleteLater()


def test_settings_opens_tall_enough_for_every_page_on_a_large_screen(
    qapp, settings_file, app_theme, monkeypatch
):
    from PyQt5.QtWidgets import QScrollArea

    dlg = _settings_on_screen(monkeypatch, 1400)
    try:
        assert 560 <= dlg.height() <= 1400 - 60
        dlg.show()
        for index in range(dlg.pages.count()):
            dlg.nav.setCurrentRow(index)
            qapp.processEvents()
            scroll = dlg.pages.widget(index).findChild(QScrollArea, "settingsPageScroll")
            # nothing to scroll: the whole page shows at the opening size
            assert scroll.verticalScrollBar().maximum() == 0, dlg.nav.item(index).text()
    finally:
        dlg.reject()
        dlg.deleteLater()


def test_settings_renderer_change_reaches_open_screens(window, monkeypatch, app_theme):
    """OK with a new Screen renderer tells every open screen panel, passing the
    renderer that was in force before the dialog; an unchanged renderer
    notifies nobody."""
    import turboadb.gui.main_window as mw_mod
    from turboadb.gui import settings
    from turboadb.gui.mirror_panel import MirrorPanel
    from turboadb.gui.settings_dialog import SettingsDialog

    calls = []
    monkeypatch.setattr(MirrorPanel, "apply_default_backend",
                        classmethod(lambda cls, root, old: calls.append((root, old))))
    settings.set("screen_backend", "scrcpy")

    def choose(backend):
        class Chooser(SettingsDialog):
            def exec_(self):
                self.screen_backend.setCurrentIndex(self.screen_backend.findData(backend))
                self.accept()
                return self.result()

        monkeypatch.setattr(mw_mod, "SettingsDialog", Chooser)

    choose("screencap")
    window.show_settings()
    assert calls == [(window, "scrcpy")]
    assert settings.get("screen_backend") == "screencap"

    choose("screencap")  # unchanged this time
    window.show_settings()
    assert calls == [(window, "scrcpy")]


def test_dim_text_is_readable_on_buttons_in_every_theme():
    """Small bold dim text on a button (the sidebar's device-count badge) keeps
    4.5:1 in every theme."""
    from turboadb.gui import theme

    for name in theme.theme_names():
        c = theme.THEMES[name]
        assert _contrast(c["dim"], c["button"]) >= 4.5, name
        assert _contrast(c["dim"], c["win"]) >= 4.5, name


def test_retired_theme_rewrite_is_tried_once_per_settings_file(monkeypatch):
    """A read-only settings file must not make every theme apply retry the
    write; a different settings file (another HOME) is still checked."""
    from turboadb.gui import settings, theme

    calls = []
    path = {"now": "/home/a/settings.json"}

    def failing_update(changes):
        calls.append((path["now"], dict(changes)))
        raise PermissionError("read-only settings file")

    monkeypatch.setattr(theme, "_RETIRED_CHECKED", set())
    monkeypatch.setattr(settings, "settings_file", lambda: path["now"])
    monkeypatch.setattr(settings, "get",
                        lambda key, default=None: "plum-light" if key == "theme" else None)
    monkeypatch.setattr(settings, "update", failing_update)
    for _ in range(4):
        theme._forget_retired_themes()
    path["now"] = "/home/b/settings.json"
    theme._forget_retired_themes()
    theme._forget_retired_themes()
    assert calls == [("/home/a/settings.json", {"theme": "light"}),
                     ("/home/b/settings.json", {"theme": "light"})]

