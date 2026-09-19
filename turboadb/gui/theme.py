"""TurboADB colour palettes and the application-wide Qt stylesheet.

Themes come in dark/light pairs that share one set of tokens and one
stylesheet. The default pair is Graphite / Porcelain (layered, neither near-black
nor near-white); Black / White, Slate / Mist, Night / Paper and Mocha / Latte are
extra pairs selectable in Settings. The ribbon toggle reads as "light mode / dark
mode": it switches to the most recently chosen theme of the other kind (see
:func:`toggle_target`). A retired theme name still found in a settings file
resolves to the default of its kind (see :func:`resolve_name`).
The stylesheet is deliberately "soft": surfaces are separated by fill and
rounded corners rather than outlines, buttons are filled, and only text fields
keep a hairline edge.

Every palette is tuned for long sessions: no pure black or pure white surface,
body text at roughly 9-13:1 on dark pages and 10-14:1 on light ones (brighter
halates, dimmer strains), and muted accents instead of large saturated fills.

Terminal-like surfaces (shell, logcat, the log dock) use the terminal colours
below in both themes, so their ANSI and log-level colours are tuned for one
dark background. Every colour the GUI paints should come from this module.
"""

from __future__ import annotations

import os
import tempfile
from functools import partial

# Text tokens (text, dim, accent_text, section_text, sel_text) keep at least
# 4.5:1 contrast on win/panel/raised/input/ribbon, ``text`` also on ``button``,
# and ``frame`` (hovered field edge, checkbox outline) at least 3:1 on
# win/input; tests/test_gui.py enforces all of it. ``border`` is the quiet
# hairline used for field edges, menus and header rules.
# Layers, darkest to lightest (dark) / most to least recessed (light):
# chrome (top bar, sidebar, status bar) → win (page canvas) → panel (cards, tables)
# → raised (hover, elevated) ; ``border`` is the hairline that makes those layers read.
# ``accent`` is a FILL (primary buttons, checked boxes, text selection) under
# ``on_accent`` text and icons; focus rings, hovered outlines and the tab
# underline use ``accent_text``, so a theme may choose a deep, low-glare fill.
_DARK = dict(  # Graphite
    chrome="#1b1c1f", win="#202124", panel="#26272b", raised="#2d2e33",
    button="#35373c", button_hover="#3e4046", input="#1c1d20", ribbon="#1b1c1f",
    border="#3b3d44", frame="#6a6d75", line="#6e7179",
    text="#e3e4e8", dim="#a7a9b0", placeholder="#83858d", sel="#2c3e57", sel_text="#f1f3f6",
    accent="#6ea4e7", accent_hover="#89b6ee", accent_text="#8cb8ef", on_accent="#10131a",
    section_text="#c9cbd1", danger="#b8454c", danger_text="#f28b90", on_danger="#fff6f6",
    warn_text="#ecbb4c", ok_text="#8fc77c",
)
# Light palettes are deliberately dimmed (page luminance about 0.6 instead of
# 0.85): full-brightness light surfaces were reported as painful to look at.
_LIGHT = dict(  # Porcelain
    chrome="#cdcfd3", win="#d4d6d9", panel="#dbdcdd", raised="#dfe0e1",
    button="#cacdd1", button_hover="#c2c5ca", input="#e2e2e3", ribbon="#cdcfd3",
    border="#b5b9c1", frame="#61656c", line="#656a70",
    text="#1d2026", dim="#4d535b", placeholder="#666c74", sel="#bfcbdc", sel_text="#121d2a",
    accent="#295b99", accent_hover="#214d84", accent_text="#25548f", on_accent="#f7f9fc",
    section_text="#30353d", danger="#a53028", danger_text="#a53028", on_danger="#fff8f7",
    warn_text="#704b00", ok_text="#2b6230",
)
# Black / White stay monochrome in character but soft. Pure #000000 behind
# 17:1 text made every edge glare, so Black is a near-black page with gently
# stepped greys and a deep, barely-blue steel primary under light text (the
# light-grey fill outshone every button beside it). White is paper, not #ffffff.
_BLACK = dict(
    chrome="#0b0b0c", win="#0f0f10", panel="#161617", raised="#1d1d1f",
    button="#252527", button_hover="#2e2e31", input="#0a0a0b", ribbon="#0b0b0c",
    border="#2b2b2e", frame="#68686c", line="#6c6c70",
    text="#cbcbce", dim="#949498", placeholder="#7a7a7e", sel="#2f343c", sel_text="#ececee",
    accent="#3b424d", accent_hover="#474f5c", accent_text="#b1bac6", on_accent="#eef0f3",
    section_text="#b4b4b8", danger="#a3403b", danger_text="#e98c86", on_danger="#fff4f3",
    warn_text="#e6b54e", ok_text="#8dbd7c",
)
# Cards sit a shade below the paper page and fields a shade above it, so a
# page full of cards never reads as one white sheet.
_WHITE = dict(
    chrome="#e9e9e6", win="#f5f5f3", panel="#efefec", raised="#f9f9f7",
    button="#e3e3e0", button_hover="#d9d9d6", input="#fbfbf9", ribbon="#e9e9e6",
    border="#d9d9d6", frame="#858588", line="#7f7f82",
    text="#2e2e30", dim="#5f5f62", placeholder="#707073", sel="#dadbde", sel_text="#1b1b1d",
    accent="#41464e", accent_hover="#353940", accent_text="#3a3e45", on_accent="#f7f7f5",
    section_text="#3c3c3f", danger="#a8352e", danger_text="#a8352e", on_danger="#fff8f7",
    warn_text="#6e4a00", ok_text="#2d6532",
)
# Slate / Mist: cool blue-greys with low saturation, in the spirit of Nord.
_SLATE = dict(
    chrome="#232831", win="#292f39", panel="#2f3642", raised="#363e4b",
    button="#3d4655", button_hover="#465062", input="#242933", ribbon="#232831",
    border="#3f4858", frame="#7a8597", line="#7e899b",
    text="#e1e6ee", dim="#aab4c2", placeholder="#838ea0", sel="#3a4c64", sel_text="#eef2f7",
    accent="#466782", accent_hover="#51728f", accent_text="#8fbad2", on_accent="#f1f5f9",
    section_text="#b9c5d5", danger="#a54f58", danger_text="#e6979e", on_danger="#fdf4f5",
    warn_text="#ecc462", ok_text="#a6c48d",
)
_MIST = dict(
    chrome="#cdd4dd", win="#d6dce4", panel="#dde2e9", raised="#e2e6ec",
    button="#c9d1db", button_hover="#bfc8d4", input="#e6eaef", ribbon="#cdd4dd",
    border="#b4bfcc", frame="#5d6979", line="#606c7c",
    text="#212833", dim="#495465", placeholder="#616d7e", sel="#b7c7da", sel_text="#141e2b",
    accent="#3b6183", accent_hover="#325371", accent_text="#325677", on_accent="#f5f8fb",
    section_text="#2e3b4d", danger="#a03a3a", danger_text="#a03a3a", on_danger="#fff7f7",
    warn_text="#694900", ok_text="#2c6033",
)
# Night / Paper: a warm pair for long reading sessions. Night is a warm
# charcoal (greyer than the Mocha espresso) with a deep amber fill; Paper is a
# sepia-leaning cream (lighter and less tan than Latte) with an amber-brown accent.
_NIGHT = dict(
    chrome="#1a1917", win="#1f1e1c", panel="#262422", raised="#2d2b28",
    button="#35322e", button_hover="#3e3b36", input="#1b1a18", ribbon="#1a1917",
    border="#38352f", frame="#7b756b", line="#80796f",
    text="#ddd5c7", dim="#a99f90", placeholder="#8b8375", sel="#473c2c", sel_text="#f5eee2",
    accent="#7a5b2c", accent_hover="#876633", accent_text="#d8b06d", on_accent="#fcf6eb",
    section_text="#cdbfa8", danger="#a34a3f", danger_text="#e99585", on_danger="#fff6f0",
    warn_text="#eab853", ok_text="#a4c184",
)
_PAPER = dict(
    chrome="#dfd7c7", win="#e8e1d3", panel="#ede7dc", raised="#f1ece3",
    button="#dcd3c1", button_hover="#d3c8b3", input="#f4f0e8", ribbon="#dfd7c7",
    border="#cec4b1", frame="#74685a", line="#716559",
    text="#34291e", dim="#5d4f40", placeholder="#796b5b", sel="#d8c6a6", sel_text="#291e14",
    accent="#7a5a2b", accent_hover="#6b4e25", accent_text="#6c4e24", on_accent="#fdf8ef",
    section_text="#4e3c29", danger="#9b392c", danger_text="#9b392c", on_danger="#fff8f2",
    warn_text="#694700", ok_text="#395e27",
)
_MOCHA = dict(
    chrome="#221c18", win="#1c1714", panel="#241e1a", raised="#2d2520",
    button="#3a302a", button_hover="#463a32", input="#17120f", ribbon="#221c18",
    border="#3b322c", frame="#8a786a", line="#8a786a",
    text="#e4dacd", dim="#bfae9c", placeholder="#9a8a7b", sel="#4a3a2e", sel_text="#fff5ea",
    accent="#d9a066", accent_hover="#e5b27f", accent_text="#e8b884", on_accent="#1c1714",
    section_text="#e6cdb0", danger="#b84a3e", danger_text="#f08a7c", on_danger="#fff5ea",
    warn_text="#eeb84f", ok_text="#9cc27a",
)
_LATTE = dict(
    chrome="#c9bcaa", win="#d4c9ba", panel="#cabdab", raised="#d7cfc2",
    button="#c3b49e", button_hover="#b8a68c", input="#dcd6cc", ribbon="#c9bcaa",
    border="#b7a68e", frame="#6d5d4c", line="#695949",
    text="#261a11", dim="#534337", placeholder="#736251", sel="#c6af94", sel_text="#271a11",
    accent="#7a461b", accent_hover="#683a16", accent_text="#6f3e18", on_accent="#fff8ef",
    section_text="#4e3523", danger="#963227", danger_text="#963227", on_danger="#fff8ef",
    warn_text="#6b4600", ok_text="#345c25",
)

# (key, label, description, palette, pair) in the order shown in Settings.
# Each description must fit on its Settings -> Themes card (about 36
# characters; longer ones were cut off with "..." at the default dialog size).
_THEME_TABLE = (
    ("dark", "Graphite", "Layered graphite, the default dark.", _DARK, "light"),
    ("light", "Porcelain", "Soft neutral grey, the default light.", _LIGHT, "dark"),
    ("mono-dark", "Black", "Soft near-black with calm grey text.", _BLACK, "mono-light"),
    ("mono-light", "White", "Paper white with soft dark text.", _WHITE, "mono-dark"),
    ("slate-dark", "Slate", "Cool blue-grey with a frost accent.", _SLATE, "slate-light"),
    ("slate-light", "Mist", "Pale blue-grey with a steel accent.", _MIST, "slate-dark"),
    ("night-dark", "Night", "Warm charcoal, soft amber accent.", _NIGHT, "night-light"),
    ("night-light", "Paper", "Sepia cream paper for easy reading.", _PAPER, "night-dark"),
    ("mocha-dark", "Mocha", "Espresso dark with a caramel accent.", _MOCHA, "mocha-light"),
    ("mocha-light", "Latte", "Warm tan light with a toffee accent.", _LATTE, "mocha-dark"),
)
THEMES = {key: pal for key, _label, _desc, pal, _pair in _THEME_TABLE}
THEME_LABELS = {key: label for key, label, _desc, _pal, _pair in _THEME_TABLE}
THEME_DESCRIPTIONS = {key: desc for key, _label, desc, _pal, _pair in _THEME_TABLE}
_COUNTERPARTS = {key: pair for key, _label, _desc, _pal, pair in _THEME_TABLE}
# Removed themes, mapped to the default of their kind: a settings file may
# still name one as ``theme`` or as the remembered dark/light choice.
_RETIRED = {
    "forest-dark": "dark", "forest-light": "light",
    "plum-dark": "dark", "plum-light": "light",
    "teal-dark": "dark", "teal-light": "light",
}

# ---- terminal-like surfaces (identical in both themes) --------------------
TERM_BG = "#0e0e10"
TERM_FG = "#d4d6da"
TERM_PROMPT = "#7aa7db"
TERM_SELECTION = "#34465c"
TERM_SELECTION_TEXT = "#eceef1"
TERM_BORDER = "#2e3034"

# ANSI SGR colours, muted to sit comfortably on TERM_BG.
ANSI_FG = {
    30: "#5c6370", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b",
    34: "#61afef", 35: "#c678dd", 36: "#56b6c2", 37: "#abb2bf",
    90: "#7f848e", 91: "#ef7a82", 92: "#a9d18e", 93: "#f2d38c",
    94: "#7ab8f0", 95: "#d292e6", 96: "#6cc4cf", 97: "#d6d8dc",
}
ANSI_BG = {
    40: "#2b2d30", 41: "#9e3b43", 42: "#4f7a3a", 43: "#8a6a2f",
    44: "#2f5f8f", 45: "#7a4a8f", 46: "#2f7a82", 47: "#9aa0a8",
    100: "#4a4d52", 101: "#b8505a", 102: "#62934a", 103: "#a8843d",
    104: "#3f76ad", 105: "#935cab", 106: "#3d939c", 107: "#c8cbd0",
}

# Messages TurboADB itself echoes into a terminal.
ECHO_ERROR = "#e06c75"
ECHO_WARN = "#e9b44c"

# Log dock: level -> (badge colour, message colour).
LOG_LEVEL_STYLE = {
    "DEBUG": ("#8b9099", "#9ba0a8"),
    "INFO": ("#8fb3d9", "#c3cfdd"),
    "OK": ("#8fbf73", "#b7d3a6"),
    "WARNING": ("#e9b44c", "#eed293"),
    "ERROR": ("#e06c75", "#e8a3a8"),
}
LOG_TIMESTAMP = "#7c8189"
LOG_TEXT = "#c3c7cd"

# Logcat priority letters, plus the default line colour and match highlight.
LOGCAT_LEVELS = {
    "E": "#e06c75",
    "F": "#d292e6",
    "W": "#e9b44c",
    "I": "#8fbf73",
    "D": "#8fb3d9",
    "V": "#8b9099",
}
LOGCAT_DEFAULT = LOG_TEXT
HIGHLIGHT_BG = "#f0c948"
HIGHLIGHT_FG = "#1a1a1a"

# Backwards-compatible names used across the GUI.
ACCENT = _DARK["accent"]
ACCENT_2 = _DARK["accent_hover"]
ACCENT_DARK = _LIGHT["accent"]
DANGER = "#c75c62"  # icon tint readable on both ribbons
WARN = ECHO_WARN
LOG_COLORS = {
    "ERROR": LOG_LEVEL_STYLE["ERROR"][0],
    "WARNING": LOG_LEVEL_STYLE["WARNING"][0],
    "stderr": "#ef9b5f",
    "OK": LOG_LEVEL_STYLE["OK"][0],
    "INFO": LOG_LEVEL_STYLE["INFO"][0],
    **{f" {level} ": colour for level, colour in LOGCAT_LEVELS.items()},
}


def theme_names() -> tuple[str, ...]:
    """Return palette keys in the order presented to the user."""
    return tuple(THEME_LABELS)


def resolve_name(name: str | None, fallback: str | None = "dark") -> str | None:
    """The theme key to use for a stored or requested *name*.

    A current key is returned as is; a retired one (Forest, Plum, Deep teal and
    their light halves) becomes the default of its kind, so a settings file
    naming Rose opens in Porcelain, not Graphite; anything else is *fallback*.
    Every lookup in this module goes through here.
    """
    if not isinstance(name, str):
        return fallback
    if name in THEMES:
        return name
    return _RETIRED.get(name, fallback)


def theme_label(name: str) -> str:
    return THEME_LABELS[resolve_name(name)]


def theme_description(name: str) -> str:
    return THEME_DESCRIPTIONS.get(name, "")


def current_name() -> str:
    """The saved theme name (retired or unknown values resolved, see :func:`resolve_name`)."""
    try:
        from . import settings as settings_mod

        name = settings_mod.get("theme")
    except Exception:
        name = None
    return resolve_name(name)


_ACTIVE_NAME = None  # the theme last applied to the app (live previews included)


def palette(name: str | None = None) -> dict:
    """Colour tokens for *name* (default: the theme currently applied, else the saved one)."""
    return THEMES[resolve_name(name or _ACTIVE_NAME or current_name())]


# Colour-coding hues for icons and tinted tiles: (dark themes, light themes).
# Dark-theme values are light enough to read on panels; light-theme values are
# deep enough for the same on porcelain. Contrast is checked in tests.
_HUES = {
    "blue": ("#7ab0f0", "#2a62a8"),
    "green": ("#7fcf8e", "#297539"),
    "amber": ("#ecbb4c", "#8f5d00"),
    "red": ("#f08c8c", "#b3342c"),
    "purple": ("#b9a2f3", "#6b46ba"),
    "teal": ("#62cfc5", "#1c736c"),
    "orange": ("#ff9f5c", "#a84e0e"),
    "pink": ("#ef93c3", "#a8386f"),
}
TONES = tuple(_HUES)
_ROLE_TONES = {
    "dim": "dim",
    "text": "text",
    "accent": "accent_text",
    "ok": "ok_text",
    "warn": "warn_text",
    "danger": "danger_text",
    "on-accent": "on_accent",
    "on-danger": "on_danger",
}


def _mix(base: str, over: str, amount: float) -> str:
    """Blend hex colour *over* onto *base* by *amount* (0..1)."""
    b = [int(base.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    o = [int(over.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(b, o))


def hue(tone: str | None = None, name: str | None = None) -> str:
    """The colour for an icon *tone* in theme *name* (default: the active theme).

    Tones are the colour hues in ``TONES`` or a palette role (``dim``, ``text``,
    ``accent``, ``ok``, ``warn``, ``danger``); unknown tones fall back to ``dim``.
    """
    c = palette(name)
    if tone in _HUES:
        dark_value, light_value = _HUES[tone]
        return light_value if _luminance(c["win"]) > 0.4 else dark_value
    return c[_ROLE_TONES.get(tone or "dim", "dim")]


def tint(tone: str, name: str | None = None, extra: float = 0.0) -> str:
    """A soft background wash of *tone* over the panel colour."""
    c = palette(name)
    amount = (0.13 if _luminance(c["win"]) > 0.4 else 0.17) + extra
    return _mix(c["panel"], hue(tone, name), amount)


def _tone_rules(name: str) -> str:
    """Tinted buttons: ``setProperty("tone", "green")`` on a QPushButton/QToolButton."""
    c = THEMES[resolve_name(name)]
    rules = []
    for tone in TONES:
        sel = f'QPushButton[tone="{tone}"], QToolButton[tone="{tone}"]'
        rules.append(
            f"{sel} {{ background: {tint(tone, name)}; color: {c['text']};"
            f" border: 1px solid {tint(tone, name, 0.12)}; }}"
        )
        rules.append(
            f'QPushButton[tone="{tone}"]:hover, QToolButton[tone="{tone}"]:hover'
            f" {{ background: {tint(tone, name, 0.09)}; }}"
        )
        rules.append(
            f'QPushButton[tone="{tone}"]:pressed, QToolButton[tone="{tone}"]:pressed,'
            f' QPushButton[tone="{tone}"]:checked, QToolButton[tone="{tone}"]:checked'
            f" {{ background: {tint(tone, name, 0.18)}; border-color: {hue(tone, name)}; }}"
        )
        rules.append(f'QLabel[tone="{tone}"] {{ color: {hue(tone, name)}; }}')
    rules.append(
        "QPushButton[tone]:disabled, QToolButton[tone]:disabled"
        f" {{ background: {c['button']}; color: {c['placeholder']}; border-color: transparent; }}"
    )
    return "\n    ".join(rules)


def _luminance(colour: str) -> float:
    channels = [int(colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(fg: str, bg: str) -> float:
    """WCAG contrast ratio of two hex colours (1 to 21)."""
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _readable_fill(track: str, fill: str, text: str, minimum: float = 4.6) -> str:
    """*fill*, blended toward *track* only as far as *text* needs to stay readable on it.

    For a surface that one text colour crosses, such as a progress bar whose
    label sits over both the chunk and the empty track: Graphite's light-blue
    and White's near-black accents left that label unreadable over the chunk.
    """
    for step in range(20, -1, -1):
        colour = _mix(track, fill, step / 20)
        if _contrast(text, colour) >= minimum:
            return colour
    return track


def is_light(name: str | None = None) -> bool:
    """True when the palette's window colour is light (any palette, not a name)."""
    return _luminance(palette(name)["win"]) > 0.4


def counterpart(name: str | None = None) -> str:
    """The other half of *name*'s dark/light pair in ``_THEME_TABLE``.

    Not what the ribbon toggle switches to: that is :func:`toggle_target`."""
    return _COUNTERPARTS.get(resolve_name(name or current_name(), None), "dark")


# Settings keys holding the user's most recent theme of each kind, and the
# defaults used until one is chosen.
LAST_DARK_KEY = "theme_last_dark"
LAST_LIGHT_KEY = "theme_last_light"


def last_theme_key(name: str) -> str:
    """The settings key that remembers the most recent theme of *name*'s kind."""
    return LAST_LIGHT_KEY if is_light(name) else LAST_DARK_KEY


def theme_choice(name: str, previous: str | None = None) -> dict:
    """Settings changes for choosing theme *name* (menu, Settings or toggle).

    Besides ``theme`` itself, *name* becomes the remembered theme of its kind.
    *previous*, the theme being left, is recorded under its kind first: it was
    the latest of that kind, and a settings file older than these keys names
    it only as ``theme``.
    """
    changes = {}
    for key in (previous, name):
        key = resolve_name(key, None)
        if key is not None:
            changes[last_theme_key(key)] = key
    changes["theme"] = resolve_name(name)
    return changes


def toggle_target(name: str | None = None) -> str:
    """The theme the dark/light toggle switches to from *name* (default: the saved one).

    Users read the toggle as "light mode / dark mode", so it goes to the most
    recently chosen theme of the OTHER kind (Slate -> Porcelain -> Slate), not to
    *name*'s pair; Graphite / Porcelain until a theme of that kind was chosen.
    """
    name = resolve_name(name, None) or current_name()
    to_light = not is_light(name)
    key, fallback = (LAST_LIGHT_KEY, "light") if to_light else (LAST_DARK_KEY, "dark")
    try:
        from . import settings as settings_mod

        target = settings_mod.get(key)
    except Exception:
        target = None
    # A hand-edited file may name an unknown theme, or one of the wrong kind;
    # a retired one stands for the default of its kind.
    target = resolve_name(target, fallback)
    if is_light(target) != to_light:
        target = fallback
    return target


# Settings files already checked for retired theme names (by path).
_RETIRED_CHECKED = set()


def _forget_retired_themes() -> None:
    """Rewrite saved theme names that were retired, once, to their kind's default.

    Lookups here resolve them anyway, but the Settings dialog preselects (and
    its OK re-applies) the raw saved name; left in place, a Rose user would be
    shown Graphite there and switched to it. Attempted once per settings file:
    a read-only file would otherwise make every theme switch wait out the
    write retries.
    """
    try:
        from . import settings as settings_mod

        path = settings_mod.settings_file()
        if path in _RETIRED_CHECKED:
            return
        _RETIRED_CHECKED.add(path)

        saved = {key: settings_mod.get(key) for key in ("theme", LAST_DARK_KEY, LAST_LIGHT_KEY)}
        changes = {key: _RETIRED[value] for key, value in saved.items()
                   if isinstance(value, str) and value in _RETIRED}
        if changes:
            settings_mod.update(changes)
    except Exception:  # an unreadable or read-only settings file: resolve per lookup
        pass


def accent_text(name: str = "dark") -> str:
    """Accent colour which remains legible when used for text."""
    return THEMES[resolve_name(name)]["accent_text"]


def section_text(name: str = "dark") -> str:
    """Sidebar section-heading colour for the active theme."""
    return THEMES[resolve_name(name)]["section_text"]


def status_colors(name: str | None = None) -> dict:
    """(foreground, background) pairs for status pills such as the webcam state."""
    c = palette(name)
    return {
        "idle": (c["dim"], "transparent"),
        "info": (c["on_accent"], c["accent"]),
        "ok": (c["on_danger"], "#3f6b35"),
        "rec": (c["on_danger"], c["danger"]),
        "warn": ("#1e1f22", "#e2ab3c"),
        "error": (c["on_danger"], c["danger"]),
    }


def report_palette(name: str | None = None) -> dict:
    """Colours for exported HTML/PNG reports, matching the app theme."""
    c = palette(name)
    light = is_light(name)
    return {
        "background": c["input"] if light else c["win"],
        "surface": c["raised"] if light else c["panel"],
        "header": c["panel"] if light else c["raised"],
        "foreground": c["text"],
        "muted": c["dim"],
        "border": c["border"],
        "accent": c["accent_text"],
    }


def apply_to_app(app, name: str | None = None) -> None:
    """Apply the stylesheet plus the palette roles QSS can't reach.

    Placeholder text colour is only settable through ``QPalette``, so it is
    applied here to follow live theme switches too. So are link colours (rich
    text links, such as the file link in a "Saved ..." toast, were Qt's default
    dark blue, unreadable on dark themes) and the bevel roles (light, midlight,
    mid, dark, shadow) that native controls still draw with: their Windows
    defaults are white and light grey.

    One such bevel was the bright rule above the device actions: in document
    mode a QTabWidget has the native style paint a tab-bar base line behind
    each corner widget (``setDrawBase(False)`` doesn't reach it), in the tab
    bar's light/dark roles. Tab bars get those roles in the page colour, which
    is what shows behind a corner widget, so the line disappears.

    A retired theme name (see :func:`resolve_name`) is also rewritten in the
    settings file here, which runs before any window opens.
    """
    global _ACTIVE_NAME
    _forget_retired_themes()
    name = resolve_name(name, None) or current_name()
    _ACTIVE_NAME = name  # glyph icons repaint in this palette on their next paint
    css = stylesheet(name)
    if app.styleSheet():
        # Swapping one large stylesheet for another makes Qt resolve every
        # widget against both; clearing first more than halves a live switch
        # (about 830 ms to 370 ms with a device open). Nothing paints between.
        app.setStyleSheet("")
    app.setStyleSheet(css)
    try:
        from PyQt5.QtGui import QColor, QPalette

        c = THEMES[name]
        pal = app.palette()
        pal.setColor(QPalette.PlaceholderText, QColor(c["placeholder"]))
        pal.setColor(QPalette.Link, QColor(c["accent_text"]))
        pal.setColor(QPalette.LinkVisited, QColor(c["accent_text"]))
        bevels =(QPalette.Light, QPalette.Midlight, QPalette.Mid, QPalette.Dark)
        for role in bevels:
            pal.setColor(role, QColor(c["border"]))
        pal.setColor(QPalette.Shadow, QColor(c["chrome"]))
        app.setPalette(pal)  # (this also drops the class palette below)
        tab_bars = QPalette(pal)
        for role in bevels + (QPalette.Shadow,):
            tab_bars.setColor(role, QColor(c["win"]))
        app.setPalette(tab_bars, "QTabBar")
    except Exception:
        pass
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QApplication, QLabel

        # A rich-text label takes its link colour from the application palette
        # when its text is parsed, so a label already showing a link kept the
        # previous theme's colour through a live switch. Setting the same text
        # again is a no-op in QLabel, hence the empty string first.
        for widget in QApplication.allWidgets():
            if isinstance(widget, QLabel) and widget.textFormat() != Qt.PlainText:
                text = widget.text()
                if "<a " in text.lower():
                    widget.setText("")
                    widget.setText(text)
    except Exception:
        pass


_LOG_BOX_DECL = f"background:{TERM_BG};color:{LOG_TEXT};border:none;border-radius:6px;"


def log_box_css() -> str:
    """Stylesheet for read-only log boxes (terminal colours, no outline).

    In-app log boxes use ``setObjectName("logBox")`` instead, which the
    application stylesheet styles the same way without a per-widget stylesheet.
    """
    return f"QPlainTextEdit{{{_LOG_BOX_DECL}}}"


def _camera_status_rules(name: str) -> str:
    """The webcam status chip: ``setProperty("state", kind)`` on QLabel#cameraStatus."""
    rules = []
    for state, (fg, bg) in status_colors(name).items():
        sel = f'QLabel#cameraStatus[state="{state}"]'
        if bg == "transparent":
            rules.append(f"{sel} {{ color: {fg}; padding: 2px 4px; font-weight: normal; }}")
        else:
            rules.append(
                f"{sel} {{ color: {fg}; background: {bg}; padding: 2px 10px;"
                f" border-radius: 9px; font-weight: 600; }}"
            )
    return "\n    ".join(rules)


# ---- small generated images for QSS sub-controls ---------------------------
_PNG_CACHE = {}


def _cached_png(kind: str, color: str, width: int, height: int, paint) -> str:
    """Paint (once) a small transparent PNG and return a QSS-safe path, or ""."""
    key = (kind, color)
    cached = _PNG_CACHE.get(key)
    if cached and os.path.exists(cached):
        return cached
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap, QPainter

    pm = QPixmap(width, height)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    paint(p, color)
    p.end()
    path = os.path.join(tempfile.gettempdir(), f"turboadb-{kind}-{color.lstrip('#')}.png")
    if not pm.save(path):
        return ""
    path = path.replace("\\", "/")
    _PNG_CACHE[key] = path
    return path


def _pen(color: str, width: float):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QColor, QPen

    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _checkmark_png(color: str) -> str:
    """A ✓ for ticked checkboxes / menu items (QSS can't draw one natively)."""

    def paint(p, c):
        p.setPen(_pen(c, 2.4))
        p.drawLine(4, 9, 8, 13)
        p.drawLine(8, 13, 14, 5)

    return _cached_png("check", color, 18, 18, paint)


def _dot_png(color: str) -> str:
    """The inner marker of a checked radio button."""

    def paint(p, c):
        from PyQt5.QtCore import Qt
        from PyQt5.QtGui import QColor

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(c))
        p.drawEllipse(6, 6, 8, 8)

    return _cached_png("dot", color, 20, 20, paint)


# Chevron image size and its three stroke points, per direction.
_CHEVRONS = {
    "down": ((14, 9), ((3, 3), (7, 7), (11, 3))),
    "up": ((14, 9), ((3, 6), (7, 2), (11, 6))),
    "right": ((9, 14), ((3, 3), (7, 7), (3, 11))),
    "left": ((9, 14), ((6, 3), (2, 7), (6, 11))),
}


def _arrow_png(color: str, direction: str = "down") -> str:
    """A chevron for QSS arrow sub-controls (a CSS triangle renders as a dot):
    the combo box drop-down, the theme toggle and tab bar scroll buttons."""
    (width, height), (start, tip, end) = _CHEVRONS[direction]

    def paint(p, c):
        p.setPen(_pen(c, 1.8))
        p.drawLine(*start, *tip)
        p.drawLine(*tip, *end)

    kind = "arrow" if direction == "down" else f"arrow-{direction}"
    return _cached_png(kind, color, width, height, paint)


def _close_png(color: str) -> str:
    """A thin × for closable tabs (Qt's stylesheet fallback is a red box)."""

    def paint(p, c):
        p.setPen(_pen(c, 1.6))
        p.drawLine(4, 4, 10, 10)
        p.drawLine(10, 4, 4, 10)

    return _cached_png("close", color, 14, 14, paint)


def _tab_separator_png(color: str) -> str:
    """A short vertical hairline drawn between adjacent tab headers."""

    def paint(p, c):
        from PyQt5.QtGui import QColor

        p.fillRect(0, 0, 1, 16, QColor(c))

    return _cached_png("tabsep", color, 1, 16, paint)


def _image(fn, color: str) -> str:
    try:
        return fn(color)
    except Exception:  # no QGuiApplication yet (e.g. imported by a CLI tool)
        return ""


def stylesheet(name: str = "dark") -> str:
    c = THEMES[resolve_name(name)]
    check = _image(_checkmark_png, c["on_accent"])
    menu_check = _image(_checkmark_png, c["text"])
    dot = _image(_dot_png, c["on_accent"])
    arrow = _image(_arrow_png, c["dim"])
    close = _image(_close_png, c["dim"])
    # A missing PNG (no QGuiApplication, or an unwritable cache) must drop the
    # image property entirely: "image: url()" is invalid and Qt then paints its
    # own fallback arrow/cross on top of the rule.
    close_image = f"image: url({close});" if close else ""
    arrow_image = f"image: url({arrow});" if arrow else ""
    # Device and section tab headers are partitioned by a short hairline at
    # each header's right edge, a little firmer than ``border`` so it reads on
    # the tab row. The background shorthands of the :last, :next-selected,
    # :hover and :selected rules drop it again: nothing trails the last header,
    # and the selected or hovered header is never cut by a line of its own.
    tab_sep = _image(_tab_separator_png, _mix(c["border"], c["frame"], 0.3))
    tab_sep_image = (
        f"background-image: url({tab_sep}); background-position: right center;"
        " background-repeat: no-repeat;"
    ) if tab_sep else ""
    # Chevrons for the scroll buttons of an overflowing tab bar, enabled and disabled.
    scroll_arrows = []
    for direction in ("left", "right", "up", "down"):
        size = "width: 9px; height: 14px;" if direction in ("left", "right") else (
            "width: 14px; height: 9px;")
        for state, colour in (("", c["text"]), (":disabled", c["frame"])):
            image = _image(partial(_arrow_png, direction=direction), colour)
            scroll_arrows.append(
                f"QTabBar QToolButton::{direction}-arrow{state} {{ "
                + (f"image: url({image}); " if image else "") + f"{size} }}"
            )
    scroll_arrow_rules = "\n    ".join(scroll_arrows)
    # A progress bar's label crosses the chunk and the track in one colour.
    progress_chunk = _readable_fill(c["button"], c["accent"], c["text"])
    return f"""
    QWidget {{
        background: {c["win"]}; color: {c["text"]};
        font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
        font-size: 9pt;
    }}
    QMainWindow::separator {{ background: {c["win"]}; width: 6px; height: 6px; }}
    QDialog {{ background: {c["win"]}; }}
    QLabel {{ background: transparent; color: {c["dim"]}; font-size: 9pt; }}
    QScrollArea {{ border: none; background: transparent; }}
    QToolTip {{ background: {c["raised"]}; color: {c["text"]}; border: 1px solid {c["border"]};
        border-radius: 6px; padding: 5px 8px; }}

    QMenuBar {{ background: {c["ribbon"]}; color: {c["text"]}; border: none; padding: 2px 6px; }}
    QMenuBar::item {{ background: transparent; color: {c["text"]}; padding: 5px 10px;
        border-radius: 6px; }}
    QMenuBar::item:selected {{ background: {c["button"]}; color: {c["text"]}; }}
    QMenuBar::item:pressed {{ background: {c["sel"]}; color: {c["sel_text"]}; }}
    QMenu {{
        background: {c["raised"]}; color: {c["text"]}; border: 1px solid {c["border"]};
        border-radius: 8px; padding: 6px; font-size: 9pt;
    }}
    QMenu::item {{ background: transparent; color: {c["text"]}; padding: 6px 28px 6px 12px;
        border-radius: 6px; }}
    QMenu::item:selected {{ background: {c["sel"]}; color: {c["sel_text"]}; }}
    QMenu::item:disabled {{ color: {c["dim"]}; }}
    QMenu::separator {{ height: 1px; background: {c["border"]}; margin: 5px 10px; }}
    QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
    QMenu::indicator:checked {{ image: url({menu_check}); }}

    QToolBar {{ background: {c["chrome"]}; border: none; border-bottom: 1px solid {c["border"]};
        spacing: 3px; padding: 4px 6px; }}
    QDockWidget {{ color: {c["dim"]}; }}
    QDockWidget::title {{ background: {c["chrome"]}; padding: 6px 10px; font-weight: 600;
        font-size: 9pt; border-bottom: 1px solid {c["border"]}; }}
    QStatusBar {{ background: {c["chrome"]}; color: {c["dim"]}; border: none;
        border-top: 1px solid {c["border"]}; font-size: 8.5pt; }}
    QStatusBar::item {{ border: none; }}
    QStatusBar QLabel#statusPill {{
        background: {c["button"]}; color: {c["text"]}; border: none;
        border-radius: 9px; padding: 2px 9px; margin: 3px; font-size: 8.5pt;
    }}

    /* Soft filled buttons: separated from the surface by fill, never outlines. */
    QPushButton, QToolButton {{
        background: {c["button"]}; color: {c["text"]}; border: none;
        border-radius: 6px; padding: 5px 12px; font-weight: 600; font-size: 9pt;
    }}
    QPushButton:hover, QToolButton:hover {{ background: {c["button_hover"]}; }}
    QPushButton:pressed, QToolButton:pressed {{ background: {c["sel"]}; color: {c["sel_text"]}; }}
    QPushButton:disabled, QToolButton:disabled {{ background: {c["panel"]}; color: {c["dim"]}; }}
    QPushButton[role="ok"], QToolButton[role="ok"] {{ background: {c["accent"]};
        color: {c["on_accent"]}; font-weight: 700; }}
    QPushButton[role="ok"]:hover, QToolButton[role="ok"]:hover,
    QPushButton[role="ok"]:pressed, QToolButton[role="ok"]:pressed {{
        background: {c["accent_hover"]}; color: {c["on_accent"]}; }}
    QPushButton[role="danger"], QToolButton[role="danger"] {{ background: {c["danger"]};
        color: {c["on_danger"]}; }}
    QPushButton[role="ghost"], QToolButton[role="ghost"] {{
        background: transparent; color: {c["text"]}; font-weight: 500;
    }}
    QPushButton[role="ghost"]:hover, QToolButton[role="ghost"]:hover {{
        background: {c["button"]}; }}
    QPushButton[role="ghost"]:pressed, QToolButton[role="ghost"]:pressed {{
        background: {c["sel"]}; color: {c["sel_text"]}; }}
    QPushButton[role="ok"]:disabled, QToolButton[role="ok"]:disabled,
    QPushButton[role="danger"]:disabled, QToolButton[role="danger"]:disabled {{
        background: {c["button"]}; color: {c["dim"]}; }}
    QPushButton[role="ghost"]:disabled, QToolButton[role="ghost"]:disabled {{
        background: transparent; color: {c["dim"]}; }}
    QPushButton#latestFollowButton:checked {{
        background: {c["accent"]}; color: {c["on_accent"]}; font-weight: 700;
    }}
    QPushButton#latestFollowButton:checked:hover {{ background: {c["accent_hover"]}; }}
    QToolButton::menu-button {{ border: none; width: 0px; }}
    QToolButton::menu-arrow, QToolButton::menu-indicator {{ image: none; width: 0px; height: 0px; }}
    QToolBar QToolButton {{ background: transparent; border: none; border-radius: 6px;
        padding: 4px 8px; }}
    QToolBar QToolButton:hover {{ background: {c["button"]}; }}
    QToolBar QToolButton:pressed {{ background: {c["sel"]}; color: {c["sel_text"]}; }}
    #ribbon QToolButton {{ font-size: 8.5pt; font-weight: 500; color: {c["text"]}; }}

    QLineEdit, QSpinBox, QComboBox {{
        background: {c["input"]}; border: 1px solid {c["border"]}; border-radius: 6px;
        padding: 4px 8px; color: {c["text"]}; selection-background-color: {c["accent"]};
        selection-color: {c["on_accent"]}; font-size: 9pt;
    }}
    QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{ border-color: {c["frame"]}; }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {c["accent_text"]}; }}
    QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
        background: {c["panel"]}; color: {c["dim"]}; border-color: {c["panel"]};
    }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
        border: none; width: 22px; }}
    QComboBox::down-arrow, QComboBox::down-arrow:disabled {{ {arrow_image} width: 12px;
        height: 8px; margin-right: 6px; }}
    QComboBox QAbstractItemView {{
        background: {c["raised"]}; color: {c["text"]}; border: 1px solid {c["border"]};
        selection-background-color: {c["sel"]}; selection-color: {c["sel_text"]}; outline: 0;
        padding: 4px; font-size: 9pt;
    }}
    QComboBox QAbstractItemView::item {{ min-height: 22px; padding: 2px 6px; color: {c["text"]}; }}
    QPlainTextEdit, QTextEdit {{
        font-family: 'Consolas', 'Cascadia Code', monospace;
        background: {c["input"]}; border: none; border-radius: 6px;
        selection-background-color: {c["accent"]}; selection-color: {c["on_accent"]};
    }}

    QListWidget, QTreeWidget {{
        background: transparent; border: none; outline: 0; font-size: 9pt;
    }}
    QListWidget::item {{ padding: 5px 8px; border-radius: 6px; }}
    QListWidget::item:hover, QTreeWidget::item:hover, QTreeView::item:hover {{
        background: {c["panel"]}; }}
    QListWidget::item:selected, QTreeWidget::item:selected, QTreeView::item:selected {{
        background: {c["sel"]}; color: {c["sel_text"]}; }}
    QListWidget::indicator, QTreeWidget::indicator, QTreeView::indicator,
    QListView::indicator {{
        width: 14px; height: 14px; border: 1px solid {c["line"]};
        border-radius: 4px; background: {c["input"]}; margin-right: 4px;
    }}
    QListWidget::indicator:hover, QTreeWidget::indicator:hover,
    QListView::indicator:hover {{ border-color: {c["accent_text"]}; }}
    QListWidget::indicator:checked, QTreeWidget::indicator:checked,
    QListView::indicator:checked {{
        background: {c["accent"]}; border-color: {c["accent"]}; image: url({check}); }}
    /* Tables: rows separated by shade only — no cell grid, no outer frame. */
    QTableWidget, QTableView {{
        background: {c["panel"]}; alternate-background-color: {c["raised"]};
        color: {c["text"]}; gridline-color: {c["panel"]};
        border: 1px solid {c["border"]}; border-radius: 8px; outline: 0; font-size: 9pt;
        selection-background-color: {c["sel"]}; selection-color: {c["sel_text"]};
    }}
    QTableWidget::item, QTableView::item {{ padding: 4px 6px; border: none; }}
    QTableWidget::item:hover, QTableView::item:hover {{ background: {c["button"]};
        color: {c["text"]}; }}
    QTableWidget::item:selected, QTableView::item:selected {{
        background: {c["sel"]}; color: {c["sel_text"]}; }}
    QHeaderView {{ background: transparent; border: none; }}
    QHeaderView::section {{
        background: {c["panel"]}; color: {c["dim"]}; padding: 6px 8px; border: none;
        border-bottom: 1px solid {c["border"]}; font-weight: 600; font-size: 8.5pt;
    }}
    QHeaderView::section:hover {{ color: {c["text"]}; }}
    QHeaderView::section:pressed {{ background: {c["sel"]}; color: {c["sel_text"]}; }}
    QTableCornerButton::section {{ background: {c["panel"]}; border: none;
        border-bottom: 1px solid {c["border"]}; }}

    /* Group boxes are rounded cards: the title sits above, the fill does the rest. */
    QGroupBox {{
        background: {c["panel"]}; border: 1px solid {c["border"]}; border-radius: 8px;
        margin-top: 22px; padding: 10px 8px 8px 8px; font-weight: 600; font-size: 9pt;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 4px;
        top: 3px; padding: 0 4px; color: {c["section_text"]}; }}
    /* A pane must not draw a generic frame: the nested terminal/device tab
       bars otherwise create a double divider between tab rows. */
    QTabWidget::pane {{ border: 0; background: {c["panel"]}; }}
    /* Qt measures tab labels with the QTabBar's font and ignores font rules on
       ::tab, so the weight lives on the bar and never changes per state;
       otherwise labels were clipped ("VI head uni"). */
    QTabBar {{ font-weight: 600; background: transparent; }}
    QTabBar::tab {{
        background: transparent; color: {c["dim"]}; padding: 7px 14px; margin: 0 1px;
        border: none; border-bottom: 2px solid transparent;
        border-top-left-radius: 6px; border-top-right-radius: 6px;
    }}
    QTabBar::tab:hover {{ background: {c["button"]}; color: {c["text"]}; }}
    QTabBar::tab:selected {{ color: {c["accent_text"]}; border-bottom-color: {c["accent_text"]}; }}
    /* Primary workspace tabs read as folder tabs merged into the page; device
       and terminal tabs rely on the animated underline. */
    QTabWidget#mainTabs::pane {{ border: 0; background: {c["win"]}; }}
    QTabWidget#mainTabs QTabBar {{ background: {c["chrome"]}; }}
    /* The strip beside the tab bar (behind the + corner) is the widget's own
       fill; it only paints with Qt.WA_StyledBackground set on the tab widget. */
    QTabWidget#mainTabs {{ background: {c["chrome"]}; }}
    QFrame#animatedTabIndicator {{ background: {c["accent_text"]}; border: none; border-radius: 1px; }}
    QTabWidget#mainTabs QTabBar::tab {{
        background: transparent; border: none; border-bottom: 3px solid transparent;
        margin: 4px 1px 0 1px; padding: 7px 14px; color: {c["dim"]};
        border-top-left-radius: 8px; border-top-right-radius: 8px; {tab_sep_image}
    }}
    /* No hairline after the last header or on either side of the selected one. */
    QTabWidget#mainTabs QTabBar::tab:last, QTabWidget#mainTabs QTabBar::tab:next-selected {{
        background: transparent; }}
    QTabWidget#mainTabs QTabBar::tab:hover {{ background: {c["button"]}; color: {c["text"]}; }}
    QTabWidget#mainTabs QTabBar::tab:selected {{
        background: {c["win"]}; color: {c["accent_text"]}; border-bottom-color: transparent;
    }}
    QTabWidget#deviceTabs::pane {{ border: 0; background: {c["win"]}; }}
    QTabWidget#deviceTabs QTabBar {{ background: {c["win"]}; }}
    QTabWidget#deviceTabs QTabBar::tab {{
        background: transparent; border: none; border-bottom: 3px solid transparent;
        margin: 2px 1px 0 1px; padding: 7px 12px; color: {c["dim"]}; {tab_sep_image}
    }}
    QTabWidget#deviceTabs QTabBar::tab:last, QTabWidget#deviceTabs QTabBar::tab:next-selected {{
        background: transparent; }}
    QTabWidget#deviceTabs QTabBar::tab:hover {{ background: {c["button"]}; color: {c["text"]}; }}
    QTabWidget#deviceTabs QTabBar::tab:selected {{
        background: transparent; color: {c["accent_text"]}; border-bottom-color: transparent;
    }}
    /* The terminal's shell choices are a segmented control, not a second row of
       document tabs; its ::tab rules live with the rest of the shell below. */
    QTabWidget#terminalTabs::pane {{ border: none; border-radius: 0; background: {c["input"]}; }}

    QCheckBox {{ background: transparent; color: {c["text"]}; spacing: 6px; font-size: 9pt; }}
    QCheckBox::indicator, QGroupBox::indicator {{ width: 15px; height: 15px;
        border: 1px solid {c["line"]}; border-radius: 4px; background: {c["input"]}; }}
    QCheckBox::indicator:hover {{ border-color: {c["accent_text"]}; }}
    QCheckBox::indicator:checked, QGroupBox::indicator:checked {{
        background: {c["accent"]}; border-color: {c["accent"]}; image: url({check}); }}
    QCheckBox::indicator:disabled {{ border-color: {c["border"]}; }}
    QRadioButton {{ background: transparent; color: {c["text"]}; spacing: 6px; font-size: 9pt; }}
    QRadioButton::indicator {{ width: 15px; height: 15px;
        border: 1px solid {c["line"]}; border-radius: 8px; background: {c["input"]}; }}
    QRadioButton::indicator:hover {{ border-color: {c["accent_text"]}; }}
    QRadioButton::indicator:checked {{ background: {c["accent"]}; border-color: {c["accent"]};
        image: url({dot}); }}
    QRadioButton::indicator:disabled {{ border-color: {c["border"]}; }}

    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:horizontal {{ width: 6px; }}
    QSplitter::handle:vertical {{ height: 6px; }}
    QSplitter::handle:hover {{ background: {c["button"]}; }}
    QProgressBar {{ border: none; border-radius: 6px; background: {c["button"]};
        text-align: center; color: {c["text"]}; height: 12px; font-size: 8pt; }}
    QProgressBar::chunk {{ background: {progress_chunk}; border-radius: 6px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c["button_hover"]}; border-radius: 4px;
        min-height: 28px; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {c["button_hover"]}; border-radius: 4px;
        min-width: 28px; }}
    QScrollBar::handle:hover {{ background: {c["line"]}; }}
    QScrollBar::handle:pressed {{ background: {c["accent"]}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    #welcomeCard {{ background: {c["panel"]}; border: 1px solid {c["border"]}; border-radius: 12px; }}
    #welcomeLogo {{ color: {c["accent_text"]}; font-size: 20pt; font-weight: 800;
        letter-spacing: 0.5px; }}
    #welcomeTag {{ color: {c["dim"]}; font-size: 9.5pt; }}
    #welcomeSection {{ color: {c["section_text"]}; font-size: 8.5pt; font-weight: 800;
        letter-spacing: 1px; padding-top: 4px; }}
    #welcomeHint {{ color: {c["text"]}; font-size: 9pt; }}
    #welcomeFoot {{ color: {c["dim"]}; font-size: 8.5pt; }}
    #welcomeTileSub {{ color: {c["dim"]}; font-size: 8.5pt; background: transparent; padding: 0px; }}
    QLabel#welcomeTileTitle {{ color: {c["text"]}; font-weight: 700; font-size: 9.5pt;
        background: transparent; padding: 0px; }}
    QToolButton#welcomeBadge {{ padding: 0px; border-radius: 10px; }}
    QToolButton#iconButton[flush="true"] {{ padding: 0px; }}
    QPushButton#welcomeTile, QToolButton#welcomeTile {{ background: {c["raised"]};
        color: {c["text"]}; border: 1px solid {c["border"]}; border-radius: 10px; text-align: left; }}
    QPushButton#welcomeTile:hover, QToolButton#welcomeTile:hover {{
        background: {c["button"]}; border-color: {c["accent_text"]}; }}
    QPushButton#welcomeTile:pressed, QToolButton#welcomeTile:pressed {{
        background: {c["sel"]}; }}
    QFrame#deviceControlCard {{ background: {c["panel"]}; border: 1px solid {c["border"]};
        border-radius: 8px; }}
    QFrame#iviPreviewTile {{ background: {c["panel"]}; border: 1px solid {c["border"]};
        border-radius: 8px; }}
    QLabel#deviceControlCardTitle, QLabel#splitPaneTitle {{
        background: transparent; color: {c["section_text"]}; border: none;
        border-bottom: 1px solid {c["border"]}; padding: 8px 10px; font-weight: 700;
    }}
    QWidget#splitWorkspace {{ background: {c["panel"]}; }}
    QFrame#splitPane {{ background: {c["panel"]}; border: none; }}
    QDialog#notificationToast {{
        background: {c["raised"]}; border: 1px solid {c["accent_text"]}; border-radius: 10px;
    }}
    QLabel#notificationToastTitle {{ color: {c["text"]}; font-weight: 700; font-size: 9.5pt; }}
    QLabel#notificationToastBody {{ color: {c["dim"]}; }}
    QLabel#notificationToastBody a {{ color: {c["accent_text"]}; font-weight: 700;
        text-decoration: none; }}
    QLabel#iviPreviewTitle {{ color: {c["text"]}; font-weight: 700; font-size: 10pt; }}
    QLabel#iviPreviewStatus {{ color: {c["dim"]}; font-size: 8.5pt; }}
    QLabel#settingsPageTitle {{ color: {c["accent_text"]}; font-size: 14pt; font-weight: 700; }}
    QLabel#settingsHint, QLabel#mutedHint {{ color: {c["dim"]}; font-size: 8.5pt; }}
    /* Settings pages scroll inside the dialog: no frame, and the viewport and
       body show the dialog's own fill instead of painting a second slab. */
    QScrollArea#settingsPageScroll, QScrollArea#settingsPageScroll > QWidget,
    QWidget#settingsPageBody {{ background: transparent; border: none; }}
    QLabel#sidebarSection {{
        color: {c["section_text"]}; font-weight: 800; font-size: 9.5pt;
        padding: 7px 2px 2px 2px; letter-spacing: 1px;
    }}

    /* ---- production shell: app bar, sidebar, page header, toolbars, cards ---- */
    QWidget#appBar {{ background: {c["chrome"]}; border-bottom: 1px solid {c["border"]}; }}
    QLabel#appTitle {{ color: {c["text"]}; font-size: 10.5pt; font-weight: 700;
        padding: 0 10px 0 4px; }}
    QFrame#barSeparator {{ background: {c["border"]}; min-width: 1px; max-width: 1px;
        margin: 7px 6px; }}
    QToolButton#iconButton {{ background: transparent; border: none; border-radius: 6px;
        padding: 5px; }}
    QToolButton#iconButton:hover {{ background: {c["button_hover"]}; }}
    QToolButton#iconButton:checked {{ background: {c["sel"]}; }}
    QWidget#sidebarPanel {{ background: {c["chrome"]}; border-right: 1px solid {c["border"]}; }}
    QWidget#sidebarPanel QListWidget {{ background: transparent; border: none; }}
    QLabel#sidebarTitle {{ color: {c["text"]}; font-size: 10pt; font-weight: 700; }}
    QLabel#countBadge {{ background: {c["button"]}; color: {c["dim"]}; border-radius: 8px;
        padding: 1px 7px; font-size: 8pt; font-weight: 700; }}
    QLabel#emptyState {{ color: {c["dim"]}; font-size: 8.5pt; padding: 6px 8px; }}
    QWidget#pageHeader {{ background: {c["win"]}; border-bottom: 1px solid {c["border"]}; }}
    QLabel#pageTitle {{ color: {c["text"]}; font-size: 12.5pt; font-weight: 700; }}
    QLabel#deviceChip {{ background: {c["panel"]}; color: {c["dim"]}; border: 1px solid {c["border"]};
        border-radius: 9px; padding: 1px 8px; font-size: 8.5pt; }}
    QLabel#statePill {{ background: {c["panel"]}; color: {c["dim"]}; border: 1px solid {c["border"]};
        border-radius: 9px; padding: 1px 9px; font-size: 8.5pt; font-weight: 600; }}
    QLabel#statePill[state="ok"] {{ color: {c["ok_text"]}; }}
    QLabel#statePill[state="warn"] {{ color: {c["warn_text"]}; }}
    QLabel#statePill[state="error"] {{ color: {c["danger_text"]}; }}
    QWidget#pageToolbar {{ background: {c["win"]}; border-bottom: 1px solid {c["border"]}; }}
    QFrame#card {{ background: {c["panel"]}; border: 1px solid {c["border"]}; border-radius: 8px; }}
    QWidget#cardHeader {{ background: transparent; border-bottom: 1px solid {c["border"]}; }}
    QLabel#cardTitle {{ color: {c["section_text"]}; font-size: 9pt; font-weight: 700; }}
    QFrame#videoWell {{ background: #000; border: 1px solid {c["border"]}; border-radius: 8px; }}
    QWidget#dialogFooter {{ background: {c["chrome"]}; border-top: 1px solid {c["border"]}; }}
    QToolBar#ribbon {{ padding: 5px 10px; spacing: 6px; }}
    #ribbon QToolButton[role="ok"] {{ background: {c["accent"]}; color: {c["on_accent"]};
        font-weight: 700; padding: 5px 12px; }}
    #ribbon QToolButton[role="ok"]:hover {{ background: {c["accent_hover"]}; }}
    QWidget#sidebarPanel QListWidget::item {{ padding: 6px 8px; }}
    QGroupBox#logPanel {{ background: {c["chrome"]}; border: none; border-radius: 0;
        margin-top: 0; padding: 0; }}
    QWidget#sidePanel, QFrame#mirrorView {{ background: {c["panel"]};
        border: 1px solid {c["border"]}; border-radius: 8px; }}
    QWidget#sidePanel QLabel#cardTitle {{ padding: 10px 12px 6px 12px; }}
    QWidget#barSpacer {{ background: transparent; }}
    /* Plain container widgets (exact QWidget, not subclasses) inside a card
       show the card's fill instead of painting a darker page-coloured slab. */
    QFrame#card .QWidget, QFrame#sidePanel .QWidget, QFrame#mirrorView .QWidget,
    QWidget#pageHeader .QWidget, QWidget#pageToolbar .QWidget {{ background: transparent; }}
    QPushButton#terminalFontButton {{ padding: 4px 8px; min-width: 0; }}
    /* Terminal shells are a segmented control, one level below the section tabs. */
    QTabWidget#terminalTabs QTabBar::tab {{
        background: transparent; color: {c["dim"]}; border: 1px solid transparent;
        border-radius: 6px; margin: 6px 2px 4px 2px; padding: 3px 12px; font-size: 8.5pt;
    }}
    QTabWidget#terminalTabs QTabBar::tab:hover {{ background: {c["button"]}; color: {c["text"]}; }}
    QTabWidget#terminalTabs QTabBar::tab:selected {{
        background: {c["raised"]}; color: {c["text"]}; border: 1px solid {c["border"]};
    }}
    QTabWidget#terminalTabs QTabBar::tab:first {{ margin-left: 10px; }}
    QTabBar::close-button {{ {close_image} subcontrol-position: right; border-radius: 4px;
        margin: 2px; padding: 1px; }}
    QTabBar::close-button:hover {{ background: {c["button_hover"]}; }}
    /* Scroll buttons of a tab bar whose headers overflow (a 1366 px screen
       with a device open). The generic button padding left them as blank
       boxes; they are opaque in the tab row's colour, since scrolled headers
       paint underneath them, and carry a chevron. The scroller width covers
       both buttons. */
    QTabBar::scroller {{ width: 44px; }}
    QTabBar QToolButton, QTabBar QToolButton:disabled {{ background: {c["win"]}; border: none;
        border-radius: 0px; padding: 0px; }}
    /* Child combinators: the device section tabs sit inside a main tab page. */
    QTabWidget#mainTabs > QTabBar > QToolButton,
    QTabWidget#mainTabs > QTabBar > QToolButton:disabled {{ background: {c["chrome"]}; }}
    QTabBar QToolButton:hover, QTabWidget#mainTabs > QTabBar > QToolButton:hover {{
        background: {c["button_hover"]}; border-radius: 6px; }}
    QTabBar QToolButton:pressed, QTabWidget#mainTabs > QTabBar > QToolButton:pressed {{
        background: {c["sel"]}; border-radius: 6px; }}
    {scroll_arrow_rules}
    QFrame#sidebarCard {{ background: {c["panel"]}; border: 1px solid {c["border"]};
        border-radius: 8px; }}
    QFrame#sidebarCard QLabel#sidebarSection {{ padding: 0; color: {c["section_text"]};
        font-size: 8.5pt; }}
    QWidget#sidebarCardHeader {{ background: transparent; border: none;
        border-bottom: 1px solid {c["border"]}; }}
    QWidget#sidebarHandle {{ background: {c["chrome"]}; border: none;
        border-right: 1px solid {c["border"]}; }}
    QWidget#sidebarHandle:hover {{ background: {c["button"]}; }}
    QWidget#deviceActions {{ background: transparent; }}
    /* As a corner widget the device actions paint their tab row's colour over
       the native tab-bar base line (see apply_to_app), which Fusion still
       draws from the window colour; in split view they stay transparent. */
    QTabWidget#deviceTabs > QWidget#deviceActions {{ background: {c["win"]}; }}
    QToolButton#sidebarHandleArrow {{ background: {c["sel"]}; border: 1px solid {c["border"]};
        border-radius: 6px; padding: 0; }}
    QToolButton#sidebarHandleArrow:hover {{ background: {c["button_hover"]}; }}
    QFrame#activityToast {{ background: {c["raised"]}; border: 1px solid {c["frame"]};
        border-radius: 10px; }}
    QFrame#activityToast QToolButton {{ background: transparent; border: none; }}
    QLabel#activityToastText {{ color: {c["text"]}; font-size: 9.5pt; }}
    QFrame#errorToast {{ background: {c["danger"]}; border: 1px solid {_mix(c["danger"], "#000000", 0.3)};
        border-radius: 12px; }}
    QFrame#errorToast QLabel {{ color: {c["on_danger"]}; background: transparent; }}
    QFrame#errorToast QToolButton {{ background: transparent; border: none; }}
    QLabel#errorToastTitle {{ font-size: 11.5pt; font-weight: 700; }}
    QLabel#errorToastText {{ font-size: 10pt; }}
    QFrame#errorToast QPushButton {{ background: {_mix(c["danger"], c["on_danger"], 0.2)};
        color: {c["on_danger"]}; border: none; border-radius: 6px; padding: 5px 14px; font-weight: 600; }}
    QFrame#errorToast QPushButton:hover {{ background: {_mix(c["danger"], c["on_danger"], 0.32)}; }}
    {_tone_rules(name)}
    QToolButton#shellSwitch {{ background: transparent; color: {c["dim"]}; border: 1px solid transparent;
        border-radius: 6px; padding: 3px 9px; font-weight: 600; }}
    QToolButton#shellSwitch:hover {{ background: {c["button"]}; color: {c["text"]}; }}
    QToolButton#shellSwitch:checked {{ background: {c["raised"]}; color: {c["text"]};
        border: 1px solid {c["border"]}; }}
    QToolBar#ribbon QToolButton {{ padding: 5px 9px; }}
    /* The ribbon theme toggle is a split button: the icon switches light/dark,
       the arrow lists every theme. Other menu buttons hide their arrow. */
    QToolBar#ribbon QToolButton#themeToggle {{ padding-right: 18px; }}
    QToolButton#themeToggle::menu-button {{ border: none; width: 16px;
        border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}
    QToolButton#themeToggle::menu-button:hover {{ background: {c["button_hover"]}; }}
    QToolButton#themeToggle::menu-arrow {{ {arrow_image} width: 10px; height: 7px; }}

    /* ---- page details that used to be widget-level stylesheets ----
       Qt keeps a separate style engine for every widget with its own
       stylesheet (and its whole subtree) and rebuilds each one on a theme
       switch, so page styling lives here, keyed by object name or property. */
    /* Device controls: geometry only; fills come from the tone rules. */
    QToolButton#ctlNav {{ border-radius: 14px; padding: 4px; }}
    QToolButton#ctlTile {{ border-radius: 10px; padding: 6px 2px 5px 2px; font-size: 8.5pt; }}
    QToolButton#ctlKey {{ border-radius: 8px; padding: 4px 8px; }}
    QToolButton#ctlMedia {{ border-radius: 10px; padding: 4px; }}
    QToolButton#ctlSeg {{ border-radius: 7px; padding: 2px 6px; font-size: 8.5pt; }}
    QToolButton#ctlAction {{ border-radius: 8px; padding: 4px 10px; }}
    /* Section titles align with their buttons. Same specificity as the
       #sidePanel caption rule above, so this one must stay after it. */
    QWidget#controlsPanel QLabel#cardTitle {{ padding: 0px; }}
    /* Screen options popover: the menu's raised surface, fields included. */
    QWidget#screenOptions {{ background: {c["raised"]}; }}
    QWidget#screenOptions QComboBox {{ background: {c["raised"]}; }}
    QWidget#screenOptions QLabel#mutedHint {{ padding-left: 22px; }}
    QLabel#screenOptionsTitle {{ color: {c["accent_text"]}; font-weight: 700; font-size: 11pt; }}
    QLabel#screenOptionsSection {{ color: {c["accent_text"]}; font-weight: 700; font-size: 8pt;
        letter-spacing: 1px; padding-top: 10px; }}
    QLabel#iviPreviewImage {{ background: #000; border-radius: 4px; }}
    QStackedWidget#phoneStack {{ background: transparent; }}
    QLineEdit#phoneNumber {{ font-size: 16pt; font-weight: 600; padding: 6px 10px; }}
    QToolButton#paneTitle {{ padding: 2px 4px 2px 0px; font-weight: 700; }}
    QPushButton#paneOp {{ padding: 4px 6px; }}
    QLabel#cameraView {{ background: {TERM_BG}; color: {LOG_TIMESTAMP}; border-radius: 6px; }}
    {_camera_status_rules(name)}
    QPlainTextEdit#logcatView {{ background: {TERM_BG}; border: none; }}
    /* Its scroll bars and their (plain QWidget) containers too, or the bars'
       2px margins show the page colour around the terminal-dark tracks. */
    QPlainTextEdit#logcatView QScrollBar, QPlainTextEdit#logcatView .QWidget {{
        background: {TERM_BG}; }}
    QLabel#logcatEmptyHint {{ color: {LOG_TIMESTAMP}; font-size: 10pt; }}
    QPlainTextEdit#logBox {{ {_LOG_BOX_DECL} }}
    QLineEdit[invalid="true"] {{ color: {c["danger_text"]}; }}
    """


_GLYPH_ENGINE = None


def _glyph_engine_class():
    """Build (once) the QIconEngine that draws a glyph in the live theme colour."""
    global _GLYPH_ENGINE
    if _GLYPH_ENGINE is not None:
        return _GLYPH_ENGINE
    from PyQt5.QtCore import QRect, Qt
    from PyQt5.QtGui import QColor, QFont, QIcon, QIconEngine, QPainter, QPixmap

    class _GlyphIconEngine(QIconEngine):
        def __init__(self, glyph, color=None):
            super().__init__()
            self.glyph = glyph
            self.color = color

        def paint(self, painter, rect, mode, state):
            tokens = palette()
            colour = self.color or (tokens["frame"] if mode == QIcon.Disabled else tokens["dim"])
            # Segoe UI Symbol carries monochrome versions of most emoji, so the
            # pen colour applies instead of the colour-emoji font's own palette.
            font = QFont("Segoe UI Symbol")
            font.setPixelSize(max(8, int(min(rect.width(), rect.height()) * 0.9)))
            painter.save()
            painter.setRenderHint(QPainter.TextAntialiasing)
            painter.setPen(QColor(colour))
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignCenter, self.glyph)
            painter.restore()

        def pixmap(self, size, mode, state):
            pm = QPixmap(size)
            pm.fill(Qt.transparent)
            painter = QPainter(pm)
            self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
            painter.end()
            return pm

        def clone(self):
            return _GlyphIconEngine(self.glyph, self.color)

    _GLYPH_ENGINE = _GlyphIconEngine
    return _GLYPH_ENGINE


def emoji_icon(ch: str, color: str | None = None):
    """A monochrome glyph icon for toolbars, menus and tabs.

    Without *color* the glyph is painted in the active theme's icon colour each
    time it is drawn, so a live theme switch restyles every icon; pass *color*
    for a fixed semantic tint (for example ``DANGER``). Multicolour emoji made
    the chrome look like a toy, so emoji are drawn as single-colour glyphs.
    """
    from PyQt5.QtGui import QIcon

    return QIcon(_glyph_engine_class()(ch, color))
