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
    # ribbon density: icons-only (True, default — fits without maximizing) vs
    # icons + text (False, "standard"). Toggle with the 🗜 ribbon button / View menu.
    "compact_ribbon": True,
    "open_docs_first_run": True,
    "make_shortcut_first_run": True,
    "auto_update": True,             # check PyPI for a newer TurboADB at launch
    "recent_network_hosts": [],
    "recent_remote_hosts": [],
    # remembered Remote-webcam connection (host/user/domain only — never the password)
    "webcam_remote_host": "",
    "webcam_remote_user": "",
    "webcam_remote_domain": "",
}


# The Remote-webcam password is kept in the OS credential vault (Windows
# Credential Manager) via keyring — never in settings.json — exactly like TurboSSH's
# jump password. Degrades gracefully (no-op / "") if keyring isn't available.
_KR_WEBCAM = ("turboadb-webcam", "::remote-default")


def webcam_remote_password() -> str:
    try:
        import keyring
        return keyring.get_password(*_KR_WEBCAM) or ""
    except Exception:
        return ""


def set_webcam_remote_password(value: str) -> None:
    try:
        import keyring
        if value:
            keyring.set_password(_KR_WEBCAM[0], _KR_WEBCAM[1], value)
        else:
            try:
                keyring.delete_password(_KR_WEBCAM[0], _KR_WEBCAM[1])
            except Exception:
                pass
    except Exception:
        pass


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
