"""Persisted app settings (theme, fonts, defaults, tool paths) at
~/.turboadb/settings.json."""

from __future__ import annotations

import os
import json

_DIR = os.path.join(os.path.expanduser("~"), ".turboadb")
_FILE = os.path.join(_DIR, "settings.json")

DEFAULTS = {
    "theme": "dark",                 # "dark" | "light"
    "term_font": "Consolas",
    "term_font_size": 10,
    "adb_path": "",                  # blank = auto-detect
    "scrcpy_path": "",               # blank = auto-detect
    "ffmpeg_path": "",               # blank = auto (cache/PATH); for the Webcam tab
    "scrcpy_max_size": 0,            # 0 = native
    "scrcpy_bit_rate": "8M",
    "scrcpy_video_codec": "",        # "" = auto; h264 is most IVI-compatible
    "scrcpy_turn_screen_off": False,
    "scrcpy_stay_awake": True,
    "logcat_format": "threadtime",
    "open_docs_first_run": True,
    "make_shortcut_first_run": True,
    "auto_update": True,             # check PyPI for a newer TurboADB at launch
    "recent_network_hosts": [],
    "recent_remote_hosts": [],
}


def add_recent(key: str, value: str, cap: int = 10) -> None:
    """Push *value* to the front of a recent-list setting (de-duped, capped)."""
    if not value:
        return
    data = load()
    lst = [x for x in (data.get(key) or []) if x != value]
    lst.insert(0, value)
    data[key] = lst[:cap]
    save(data)


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(_FILE, "r", encoding="utf-8") as fh:
            data.update(json.load(fh) or {})
    except Exception:
        pass
    return data


def save(data: dict) -> None:
    try:
        os.makedirs(_DIR, exist_ok=True)
        merged = dict(DEFAULTS)
        merged.update(data)
        with open(_FILE, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
    except Exception:
        pass


def get(key):
    return load().get(key, DEFAULTS.get(key))
