"""Theme-aware vector icons for the GUI.

Every icon is a small 24x24 outline drawing rendered with QtSvg at paint time in
a colour from the active theme, so one ``QIcon`` follows live theme switches.
Pass ``tone`` to colour-code an icon: ``"blue"``, ``"green"``, ``"amber"``,
``"red"``, ``"purple"``, ``"teal"``, ``"orange"``, ``"pink"`` or the palette
roles ``"accent"``, ``"text"``, ``"dim"``, ``"ok"``, ``"warn"``, ``"danger"``
(see :func:`theme.hue`). Disabled icons use the theme's muted frame colour.

The drawings are original, deliberately simple shapes in the common outline
icon style, so they stay legible at 16-20 px.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from . import theme

_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="{colour}" stroke-width="{width}" stroke-linecap="round" '
    'stroke-linejoin="round">{body}</svg>'
)

_HANDSET = (
    '<path d="M21 16.4v3a2 2 0 0 1-2.2 2 18.7 18.7 0 0 1-8.1-2.9 18.3 18.3 0 0 1-5.6-5.6'
    'A18.7 18.7 0 0 1 2.2 4.8 2 2 0 0 1 4.2 2.6h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.6'
    'a2 2 0 0 1-.5 2.1L8.2 10.2a15 15 0 0 0 5.6 5.6l1.2-1.2a2 2 0 0 1 2.1-.5'
    'c.8.3 1.7.6 2.6.7a2 2 0 0 1 1.3 1.6z"/>'
)
_FOLDER = (
    '<path d="M3 7.5A2 2 0 0 1 5 5.5h4l2 2h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5'
    'a2 2 0 0 1-2-2z"/>'
)
_FILE = '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'
_SPEAKER = '<path d="M11 5 6.5 9H3v6h3.5L11 19z"/>'
_WIFI = (
    '<path d="M2 8.8a15 15 0 0 1 20 0"/><path d="M5.2 12.4a10 10 0 0 1 13.6 0"/>'
    '<path d="M8.6 15.9a5 5 0 0 1 6.8 0"/><path d="M12 19.5h.01"/>'
)


def _dots(points, radius=1.5):
    return "".join(
        f'<circle cx="{x}" cy="{y}" r="{radius}" fill="currentColor" stroke="none"/>'
        for x, y in points
    )


ICONS: Dict[str, str] = {
    # ---- app shell and navigation ----
    "terminal": '<rect x="2.5" y="4" width="19" height="16" rx="2.5"/><path d="m7 9.5 3 2.5-3 2.5"/><path d="M12.5 15h4.5"/>',
    "logcat": '<path d="M4 6h16M4 10h16M4 14h11M4 18h7"/><circle cx="18.5" cy="17.5" r="2.5"/>',
    "folder": _FOLDER,
    "folder-plus": _FOLDER + '<path d="M12 10.5v5M9.5 13h5"/>',
    "file": _FILE,
    "file-plus": _FILE + '<path d="M12 11.5v5M9.5 14h5"/>',
    "sliders": (
        '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/>'
        '<path d="M1.5 14h5M9.5 8h5M17.5 16h5"/>'
    ),
    "apps": (
        '<rect x="3" y="3" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="2"/>'
        '<rect x="3" y="13.5" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="2"/>'
    ),
    "video": '<rect x="2" y="6" width="13.5" height="12" rx="2.5"/><path d="m15.5 10.5 6-3.5v10l-6-3.5z"/>',
    "phone": _HANDSET,
    "phone-incoming": _HANDSET + '<path d="M15.5 3v5.5H21"/><path d="m21.5 2.5-6 6"/>',
    "phone-outgoing": _HANDSET + '<path d="M16 2.5h5.5V8"/><path d="m15 9 6.5-6.5"/>',
    "phone-missed": _HANDSET + '<path d="m16 3 5 5M21 3l-5 5"/>',
    "phone-off": _HANDSET + '<path d="M2.5 2.5l19 19"/>',
    "dialpad": _dots([(6, 4.5), (12, 4.5), (18, 4.5), (6, 10.5), (12, 10.5), (18, 10.5),
                      (6, 16.5), (12, 16.5), (18, 16.5), (12, 21.5)]),
    "message": '<path d="M21 14.5a2 2 0 0 1-2 2H8l-5 4.5V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "send": '<path d="M21.5 2.5 10.5 13.5"/><path d="m21.5 2.5-7 19-4-8-8-4z"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "clock": '<circle cx="12" cy="12" r="9.5"/><path d="M12 7v5l3.5 2"/>',
    "monitor": '<rect x="2" y="3.5" width="20" height="13.5" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "smartphone": '<rect x="6" y="2" width="12" height="20" rx="2.5"/><path d="M11 18h2"/>',
    "car": (
        '<path d="M3 17v-4l2.2-5.6A2 2 0 0 1 7.1 6h9.8a2 2 0 0 1 1.9 1.4L21 13v4'
        'a1 1 0 0 1-1 1h-1.5a1 1 0 0 1-1-1v-1h-11v1a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>'
        '<path d="M3 13h18"/>' + _dots([(7, 15), (17, 15)], 1)
    ),
    "displays": (
        '<rect x="2" y="3" width="9" height="7.5" rx="1.5"/><rect x="13" y="3" width="9" height="7.5" rx="1.5"/>'
        '<rect x="2" y="13.5" width="9" height="7.5" rx="1.5"/><rect x="13" y="13.5" width="9" height="7.5" rx="1.5"/>'
    ),
    "usb": (
        '<path d="M12 2.5v15"/><path d="m9 5.5 3-3 3 3"/><path d="M12 14 7 11V8.5"/>'
        '<path d="m12 12 5-3V7"/><circle cx="12" cy="19.5" r="2"/><circle cx="7" cy="7.5" r="1"/>'
        '<rect x="16" y="5" width="2" height="2"/>'
    ),
    "plug": '<path d="M9 2.5v4.5M15 2.5v4.5"/><path d="M6 7h12v4a6 6 0 0 1-12 0z"/><path d="M12 17v4.5"/>',
    "server": (
        '<rect x="3" y="3" width="18" height="7.5" rx="2"/><rect x="3" y="13.5" width="18" height="7.5" rx="2"/>'
        '<path d="M7 6.8h.01M7 17.2h.01"/>'
    ),
    "wrench": (
        '<path d="M14.5 3.3a5 5 0 0 0-5.8 6.5L3 15.5V21h5.5l5.7-5.7a5 5 0 0 0 6.5-5.8'
        'l-3.1 3.1-3.2-.6-.6-3.2z"/>'
    ),
    "sun": (
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2'
        'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'
    ),
    "moon": '<path d="M20.5 13A8.5 8.5 0 1 1 11 3.5a6.5 6.5 0 0 0 9.5 9.5z"/>',
    "panel-bottom": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 14.5h18"/>',
    "sidebar": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9.5 4v16"/><path d="M5.5 8h2M5.5 11h2"/>',
    "chevron-left": '<path d="m15 5-7 7 7 7"/>',
    "chevron-right": '<path d="m9 5 7 7-7 7"/>',
    "bookmark": '<path d="M6.5 3h11a1 1 0 0 1 1 1v17l-6.5-4-6.5 4V4a1 1 0 0 1 1-1z"/>',
    # a phone with live-connection waves: devices attached right now
    "phone-link": (
        '<rect x="3" y="3" width="10.5" height="18" rx="2.2"/><path d="M7 17.5h2.5"/>'
        '<path d="M16.8 9.2a4 4 0 0 1 0 5.6"/><path d="M19.6 6.4a8 8 0 0 1 0 11.2"/>'
    ),
    # the device screen with a pointer: view and operate the device
    "screen-control": (
        '<rect x="2.5" y="2" width="12" height="19" rx="2.2"/><path d="M7 17.5h3"/>'
        '<path d="M13.2 9.8 21.3 12.6l-3.4 1.4 2.6 2.9-1.7 1.5-2.6-2.9-1.4 3.4z" '
        'fill="currentColor"/>'
    ),
    "settings": (
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3'
        ' 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1'
        'a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1'
        'a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3'
        'H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1'
        'a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1'
        'a1.7 1.7 0 0 0-1.5 1z"/>'
    ),
    "help": '<circle cx="12" cy="12" r="9.5"/><path d="M9.2 9a3 3 0 0 1 5.8 1c0 2-3 2.6-3 4.6"/><path d="M12 17.5h.01"/>',
    "info": '<circle cx="12" cy="12" r="9.5"/><path d="M12 11v5.5M12 7.5h.01"/>',
    "more": _dots([(5, 12), (12, 12), (19, 12)], 1.6),
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "alert": (
        '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'
        '<path d="M12 9v4M12 17h.01"/>'
    ),
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4.5-4.5"/>',
    "filter": '<path d="M22 3H2l8 9.5V19l4 2v-8.5z"/>',
    "link": (
        '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7L11.8 5.2"/>'
        '<path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>'
    ),
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "rename": '<rect x="2" y="7" width="13" height="10" rx="2"/><path d="M15.5 4h4M15.5 20h4M17.5 4v16"/>',
    "refresh": '<path d="M20.5 12a8.5 8.5 0 1 1-2.5-6"/><path d="M20.5 3.5V9H15"/>',
    "power": '<path d="M18.4 6.6a9 9 0 1 1-12.8 0"/><path d="M12 2v10"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "heart": (
        '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1'
        'L12 21l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8z"/>'
    ),
    "chip": (
        '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/>'
        '<path d="M9 1.5V5M15 1.5V5M9 19v3.5M15 19v3.5M1.5 9H5M1.5 15H5M19 9h3.5M19 15h3.5"/>'
    ),
    "bug": (
        '<rect x="8" y="6" width="8" height="14" rx="4"/>'
        '<path d="M19 7.5l-3 2M5 7.5l3 2M19 18.5l-3-1.5M5 18.5l3-1.5M20.5 13H16M3.5 13H8M10 3.5l1 2.5M14 3.5l-1 2.5"/>'
    ),
    "columns": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M12 3v18"/>',
    "maximize": '<path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/>',
    "external": (
        '<path d="M18 13.5V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5.5"/>'
        '<path d="M15 3h6v6M10 14 21 3"/>'
    ),
    # ---- device keys and media ----
    "back": '<path d="M17 4.5v15L5.5 12z"/>',
    "home": '<circle cx="12" cy="12" r="7.5"/>',
    "recents": '<rect x="5" y="5" width="14" height="14" rx="2.5"/>',
    "bell": '<path d="M18 8.5a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
    "house": '<path d="M3 11 12 3.5 21 11"/><path d="M5.5 9.5V20.5h4.5v-6h4v6h4.5V9.5"/>',
    "volume-down": _SPEAKER + '<path d="M15.5 9a4.5 4.5 0 0 1 0 6"/>',
    "volume-up": _SPEAKER + '<path d="M15.5 9a4.5 4.5 0 0 1 0 6"/><path d="M18.5 6a8.5 8.5 0 0 1 0 12"/>',
    "mute": _SPEAKER + '<path d="m16 9.5 5 5M21 9.5l-5 5"/>',
    "play": '<path d="M7 4.5v15l12-7.5z"/>',
    "pause": '<rect x="6" y="4.5" width="4" height="15" rx="1"/><rect x="14" y="4.5" width="4" height="15" rx="1"/>',
    "play-pause": '<path d="M3.5 5v14l9-7z"/><path d="M16 5.5v13M20.5 5.5v13"/>',
    "stop": '<rect x="5.5" y="5.5" width="13" height="13" rx="2"/>',
    "skip-back": '<path d="M19 19.5 9 12l10-7.5z"/><path d="M5 5v14"/>',
    "skip-forward": '<path d="M5 4.5 15 12 5 19.5z"/><path d="M19 5v14"/>',
    "record": '<circle cx="12" cy="12" r="9.5"/><circle cx="12" cy="12" r="5" fill="currentColor" stroke="none"/>',
    "camera": (
        '<path d="M3 8.5a2 2 0 0 1 2-2h2.4l1.6-2.5h6l1.6 2.5H19a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5'
        'a2 2 0 0 1-2-2z"/><circle cx="12" cy="13" r="3.5"/>'
    ),
    "battery": (
        '<rect x="2" y="7" width="17" height="10" rx="2"/><path d="M22 11v2"/>'
        '<rect x="5" y="10" width="8" height="4" rx="1" fill="currentColor" stroke="none"/>'
    ),
    "wifi": _WIFI,
    "wifi-off": _WIFI + '<path d="M2.5 2.5l19 19"/>',
    "bluetooth": '<path d="m6.5 7 11 10L12 22V2l5.5 5-11 10"/>',
    "signal": '<path d="M4 20v-3.5M9.3 20v-7M14.6 20V9M20 20V4.5"/>',
    "airplane": (
        '<path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1'
        ' 3.5 1v-1.5L13 19v-5.5z"/>'
    ),
    "hotspot": (
        '<circle cx="12" cy="12" r="2"/><path d="M16.2 7.8a6 6 0 0 1 0 8.4M7.8 16.2a6 6 0 0 1 0-8.4'
        'M19.1 4.9a10 10 0 0 1 0 14.2M4.9 19.1a10 10 0 0 1 0-14.2"/>'
    ),
    "screen-on": '<rect x="6" y="2" width="12" height="20" rx="2.5"/><path d="M9.5 8h5M9.5 11.5h5M9.5 15h3"/>',
    "screen-off": '<rect x="6" y="2" width="12" height="20" rx="2.5"/><path d="M3 3l18 18"/>',
    "globe": (
        '<circle cx="12" cy="12" r="9.5"/><path d="M2.5 12h19"/>'
        '<path d="M12 2.5a14.5 14.5 0 0 1 0 19 14.5 14.5 0 0 1 0-19z"/>'
    ),
    "youtube": '<rect x="2" y="5" width="20" height="14" rx="4"/><path d="M10 9v6l5-3z" fill="currentColor"/>',
    "music": '<path d="M9 18V5.5l12-2.5v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    "map": '<path d="M1.5 6v15.5l7-3.5 7 3.5 7-3.5V2.5l-7 3.5-7-3.5z"/><path d="M8.5 2.5V18M15.5 6v15.5"/>',
    "store": (
        '<path d="M6 2.5 3 6.5v13a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-13l-3-4z"/>'
        '<path d="M3 6.5h18"/><path d="M16 10a4 4 0 0 1-8 0"/>'
    ),
    "image": '<rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/>',
    "calculator": (
        '<rect x="4" y="2" width="16" height="20" rx="2.5"/><path d="M8 6.5h8"/>'
        + _dots([(8, 11.5), (12, 11.5), (16, 11.5), (8, 15), (12, 15), (16, 15), (8, 18.5), (12, 18.5), (16, 18.5)], 1)
    ),
    "keyboard": (
        '<rect x="2" y="5" width="20" height="14" rx="2.5"/><path d="M7 15.5h10"/>'
        + _dots([(6, 9), (10, 9), (14, 9), (18, 9), (6, 12.2), (10, 12.2), (14, 12.2), (18, 12.2)], 0.9)
    ),
    "enter": '<path d="M9 10 4 15l5 5"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/>',
    "backspace": (
        '<path d="M21 4H8l-7 8 7 8h13a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2z"/><path d="m18 9-6 6M12 9l6 6"/>'
    ),
    "space": '<path d="M3 10v4a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-4"/>',
    "tab": '<path d="M3 12h13"/><path d="m12 8 4 4-4 4"/><path d="M20.5 6v12"/>',
    # ---- files, clipboard and output ----
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "paste": '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>',
    "save": (
        '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>'
        '<path d="M17 21v-8H7v8M7 3v5h8"/>'
    ),
    "trash": (
        '<path d="M3 6h18"/><path d="m19 6-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
        '<path d="M10 11v6M14 11v6M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>'
    ),
    "eraser": (
        '<path d="M20 20H8.5l-5-5a2 2 0 0 1 0-2.8l9.2-9.2a2 2 0 0 1 2.8 0l5 5a2 2 0 0 1 0 2.8L12 19.3"/>'
        '<path d="m7 9.5 7.5 7.5"/>'
    ),
    "arrow-down": '<path d="M12 4v16M5 13l7 7 7-7"/>',
    "arrow-up": '<path d="M12 20V4M5 11l7-7 7 7"/>',
    "arrow-left": '<path d="M20 12H4M11 5l-7 7 7 7"/>',
    "arrow-right": '<path d="M4 12h16M13 5l7 7-7 7"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5M12 15V3"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5M12 3v12"/>',
    "zap": '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
    "flip": '<path d="M12 3v18"/><path d="M8 7 3 12l5 5z"/><path d="m16 7 5 5-5 5z"/>',
    "rotate": '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/><path d="M3.5 3.5V9H9"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
}

# Alternative names used by callers.
ALIASES: Dict[str, str] = {
    "screen": "monitor",
    "screenshot": "camera",
    "reboot": "refresh",
    "webcam": "video",
    "device-control": "screen-control",
    "controls": "sliders",
    "files": "folder",
    "shell": "terminal",
    "health": "heart",
    "build": "chip",
    "root": "shield",
    "split": "columns",
    "connect": "plug",
    "tools": "wrench",
    "log": "panel-bottom",
    "notifications": "bell",
    "mobile-data": "signal",
    "browser": "globe",
    "gallery": "image",
    "clear": "eraser",
    "delete": "trash",
    "close": "x",
    "call": "phone",
    "end-call": "phone-off",
    "sms": "message",
    "ivi": "car",
}

_RENDERERS: Dict[Tuple[str, str, float], object] = {}
_ENGINE = None


def has_icon(name: str) -> bool:
    return ALIASES.get(name, name) in ICONS


def icon_names():
    return tuple(sorted(ICONS))


def svg(name: str, colour: str, width: float = 1.9) -> str:
    """The SVG markup of *name* drawn in *colour* (an empty string if unknown)."""
    body = ICONS.get(ALIASES.get(name, name))
    if body is None:
        return ""
    return _SVG.format(colour=colour, width=width, body=body.replace("currentColor", colour))


def _renderer(name: str, colour: str, width: float):
    key = (name, colour, width)
    cached = _RENDERERS.get(key)
    if cached is not None:
        return cached
    try:
        from PyQt5.QtCore import QByteArray
        from PyQt5.QtSvg import QSvgRenderer
    except Exception:  # QtSvg missing: callers fall back to a plain dot
        return None
    markup = svg(name, colour, width)
    if not markup:
        return None
    renderer = QSvgRenderer(QByteArray(markup.encode("utf-8")))
    if not renderer.isValid():
        return None
    _RENDERERS[key] = renderer
    return renderer


def _engine_class():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    from PyQt5.QtCore import QRect, QRectF, Qt
    from PyQt5.QtGui import QColor, QIcon, QIconEngine, QPainter, QPixmap

    class _VectorIconEngine(QIconEngine):
        def __init__(self, name, tone=None, width=1.9):
            super().__init__()
            self.name = name
            self.tone = tone
            self.width = width

        def colour(self, mode):
            if mode == QIcon.Disabled:
                return theme.palette()["frame"]
            return theme.hue(self.tone)

        def paint(self, painter, rect, mode, state):
            colour = self.colour(mode)
            side = min(rect.width(), rect.height())
            target = QRectF(
                rect.x() + (rect.width() - side) / 2.0,
                rect.y() + (rect.height() - side) / 2.0,
                side,
                side,
            )
            renderer = _renderer(self.name, colour, self.width)
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            if renderer is not None:
                renderer.render(painter, target)
            else:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(colour))
                painter.drawEllipse(target.center(), side / 6.0, side / 6.0)
            painter.restore()

        def pixmap(self, size, mode, state):
            pm = QPixmap(size)
            pm.fill(Qt.transparent)
            painter = QPainter(pm)
            self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
            painter.end()
            return pm

        def clone(self):
            return _VectorIconEngine(self.name, self.tone, self.width)

    _ENGINE = _VectorIconEngine
    return _ENGINE


def icon(name: str, tone: Optional[str] = None, *, width: float = 1.9):
    """A theme-following vector icon; *tone* colour-codes it (default: muted)."""
    from PyQt5.QtGui import QIcon

    return QIcon(_engine_class()(ALIASES.get(name, name), tone, width))


def pixmap(name: str, size: int, tone: Optional[str] = None):
    """A one-off pixmap (for QLabel). It does not follow later theme switches,
    so call it again from the widget's theme refresh if it must."""
    from PyQt5.QtCore import QSize

    return icon(name, tone).pixmap(QSize(size, size))
