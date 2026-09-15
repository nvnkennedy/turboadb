"""Resolve the one ADB executable used by every GUI worker.

Keeping this in a small dependency-free module avoids accidental fallbacks to a
different ``adb.exe`` in background dialogs.  Two ADB versions taking turns on
port 5037 is a common cause of device disconnects on Windows.
"""

from __future__ import annotations

from . import settings as settings_mod


def gui_adb_path() -> str | None:
    """Return the configured ADB path, or TurboADB's managed Platform-Tools."""
    configured = (settings_mod.get("adb_path") or "").strip()
    if configured:
        return configured
    try:
        from ..toolsdl import managed_adb

        return managed_adb() or None
    except Exception:
        return None
