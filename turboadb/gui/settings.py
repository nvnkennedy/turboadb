"""Persisted app settings (theme, fonts, defaults, tool paths) at
~/.turboadb/settings.json."""

from __future__ import annotations

import os
import json
import tempfile
import threading
import time
import warnings

_DIR = os.path.join(os.path.expanduser("~"), ".turboadb")
_FILE = os.path.join(_DIR, "settings.json")

# One zoom range for every terminal/log view and the Settings dialog.
FONT_SIZE_MIN = 6
FONT_SIZE_MAX = 40

DEFAULTS = {
    # Bumped when a stored value needs a one-time upgrade (see _load_cached).
    "settings_version": 3,
    "theme": "dark",  # key from turboadb.gui.theme.THEMES
    "term_font": "Consolas",
    "term_font_size": 10,
    "adb_path": "",  # blank = auto-detect
    "scrcpy_path": "",  # blank = auto-detect
    "ffmpeg_path": "",  # blank = auto (cache/PATH); for the Webcam tab
    "scrcpy_max_size": 0,  # 0 = native
    "scrcpy_bit_rate": "16M",
    "scrcpy_video_codec": "",  # "" = auto; h264 is most IVI-compatible
    "scrcpy_audio": True,
    "scrcpy_audio_source": "output",  # whole device output → PC speakers
    "scrcpy_audio_codec": "opus",
    "scrcpy_audio_bit_rate": "128K",
    "scrcpy_audio_buffer": 50,
    "scrcpy_audio_output_buffer": 10,
    "scrcpy_audio_dup": False,
    "scrcpy_turn_screen_off": False,
    "scrcpy_stay_awake": True,
    "logcat_format": "threadtime",
    # ribbon density: icons-only (True, default — fits without maximizing) vs
    # icons + text (False, "standard"). Toggle with the 🗜 ribbon button / View menu.
    "compact_ribbon": True,
    "make_shortcut_first_run": True,
    "auto_update": True,  # check PyPI for a newer TurboADB at launch
    # Ask whether a second open of the same device should create a terminal-only
    # tab.  The terminal session owns independent Android Shell/CMD/PowerShell
    # processes but does not duplicate the full device workspace.
    "duplicate_device_action": "ask",  # ask | terminal | focus
    "recent_network_hosts": [],
    "recent_remote_hosts": [],
    # remembered Remote-webcam connection (host/user/domain only — never the password)
    "webcam_remote_host": "",
    "webcam_remote_user": "",
    "webcam_remote_domain": "",
    # remembered admin login for the remote 'serve' deploy (ADB Server button).
    # The user is stored here; the PASSWORD goes in the OS credential vault.
    "deploy_user": "",
    "deploy_remember": True,
    "mute_popups_with_log": True,
}

# Serialises every read-modify-write so concurrent set()/save() calls (UI
# thread, workers, debounced writes) never lose each other's updates.
_LOCK = threading.RLock()
# (path, (mtime_ns, size), data) of the last parsed/written file.
_cache = None


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


# The remote-deploy (ADB Server) admin password lives in the OS credential
# vault too — retyping it on every deploy was a constant annoyance.
_KR_DEPLOY = ("turboadb-deploy", "::default")


def deploy_password() -> str:
    try:
        import keyring

        return keyring.get_password(*_KR_DEPLOY) or ""
    except Exception:
        return ""


def set_deploy_password(value: str) -> None:
    try:
        import keyring

        if value:
            keyring.set_password(_KR_DEPLOY[0], _KR_DEPLOY[1], value)
        else:
            try:
                keyring.delete_password(_KR_DEPLOY[0], _KR_DEPLOY[1])
            except Exception:
                pass
    except Exception:
        pass


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
    with _LOCK:
        data = load()
        lst = [x for x in (data.get(key) or []) if x != value]
        lst.insert(0, value)
        data[key] = lst[:cap]
        save(data)


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _coerce(key: str, value):
    """Return *value* converted to the type of ``DEFAULTS[key]``.

    settings.json is hand-editable; a string "false" or a float font size must
    not reach ``bool()``/``QFont`` as the wrong type.  Unconvertible values fall
    back to the default (with a warning)."""
    default = DEFAULTS[key]
    try:
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
                return value.strip().lower() in _TRUE
        elif isinstance(default, int):
            if isinstance(value, bool):
                raise ValueError("boolean is not a number")
            if isinstance(value, (int, float)) and float(value).is_integer():
                return int(value)
            if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                return int(value.strip())
        elif isinstance(default, str):
            if isinstance(value, str):
                return value
            if value is None:
                return default
        elif isinstance(default, list):
            if isinstance(value, list):
                return [x for x in value if isinstance(x, str)]
        else:  # pragma: no cover - every default is one of the types above
            return value
    except (TypeError, ValueError, OverflowError):
        pass
    warnings.warn(
        f"TurboADB ignored invalid setting {key}={value!r}; using {default!r}.",
        RuntimeWarning,
        stacklevel=3,
    )
    return _copy_value(default)


def _copy_value(value):
    return list(value) if isinstance(value, list) else value


def _copy(data: dict) -> dict:
    return {k: _copy_value(v) for k, v in data.items()}


def _signature(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load_cached() -> dict:
    """The parsed settings (shared, do not mutate) — re-read only when the
    file's mtime/size changed, so frequent get() calls don't hit the disk."""
    global _cache
    path = _FILE
    sig = _signature(path)
    cached = _cache
    if cached is not None and cached[0] == path and cached[1] == sig:
        return cached[2]
    data = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if saved is not None:
            if not isinstance(saved, dict):
                raise ValueError("settings file must contain a JSON object")
            for key, value in saved.items():
                data[key] = _coerce(key, value) if key in DEFAULTS else value
            version = saved.get("settings_version")
            if not isinstance(version, int) or version < 3:
                # Older files store every default, so these values are the old
                # defaults rather than choices; move them to the current ones.
                # The terminal font is never touched: 10 pt is the default and
                # any other size is the user's own choice.
                if str(data.get("scrcpy_bit_rate") or "").upper() == "8M":
                    data["scrcpy_bit_rate"] = DEFAULTS["scrcpy_bit_rate"]
                # Record the upgrade so the next save stores the current version
                # and a value chosen afterwards is never reset again.
                data["settings_version"] = DEFAULTS["settings_version"]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        warnings.warn(
            f"TurboADB could not load settings from {path}: {exc}. Using defaults.",
            RuntimeWarning,
            stacklevel=3,
        )
    _cache = (path, sig, data)
    return data


def load() -> dict:
    """A fresh, mutable copy of all settings (defaults filled in)."""
    with _LOCK:
        return _copy(_load_cached())


def _replace_with_retry(src: str, dst: str, attempts: int = 6) -> None:
    """``os.replace`` that rides out Windows sharing violations.

    Antivirus scanners, indexers and a second TurboADB process briefly open
    settings.json; on Windows that makes the rename fail with PermissionError
    even though a moment later it succeeds."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _atomic_write_json(path: str, value) -> None:
    """Durably replace *path* without leaving a half-written JSON file behind."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".turboadb-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save(data: dict) -> None:
    global _cache
    if not isinstance(data, dict):
        raise ValueError("settings must be a dictionary")
    merged = dict(DEFAULTS)
    for key, value in data.items():
        merged[key] = _coerce(key, value) if key in DEFAULTS else value
    with _LOCK:
        path = _FILE
        _atomic_write_json(path, merged)
        _cache = (path, _signature(path), _copy(merged))


def get(key: str, default=None):
    with _LOCK:
        loaded = _load_cached()
        if key in loaded:
            return _copy_value(loaded[key])
    if default is not None:
        return default
    return _copy_value(DEFAULTS.get(key))


def update(changes: dict) -> None:
    """Persist several settings at once, merged into the CURRENT file under the
    lock (never a stale snapshot taken when a dialog opened)."""
    if not changes:
        return
    with _LOCK:
        data = load()
        data.update(changes)
        save(data)


def set(key: str, value) -> None:
    """Persist a single setting value."""
    update({key: value})


# ---- debounced writes (e.g. Ctrl+wheel zoom: many notches, one fsync) ----
_pending: dict = {}
_pending_timer = None
_PENDING_DELAY_S = 0.6


def set_later(key: str, value, delay: float = _PENDING_DELAY_S) -> None:
    """Persist *key* after *delay* seconds of quiet, off the calling thread.

    Repeated calls coalesce into a single write, so zooming a terminal with
    the mouse wheel no longer fsyncs settings.json on the UI thread per notch.
    """
    global _pending_timer
    with _LOCK:
        _pending[key] = value
        if _pending_timer is not None:
            _pending_timer.cancel()
        _pending_timer = threading.Timer(delay, flush_pending)
        _pending_timer.daemon = True
        _pending_timer.start()


def flush_pending() -> None:
    """Write any debounced settings now (also runs at interpreter exit)."""
    global _pending_timer
    with _LOCK:
        if _pending_timer is not None:
            _pending_timer.cancel()
            _pending_timer = None
        changes = dict(_pending)
        _pending.clear()
    if not changes:
        return
    try:
        update(changes)
    except (OSError, ValueError) as exc:
        warnings.warn(f"TurboADB could not save settings: {exc}", RuntimeWarning, stacklevel=2)


import atexit  # noqa: E402  (registered after flush_pending exists)

atexit.register(flush_pending)
