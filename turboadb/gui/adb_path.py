"""Resolve the one ADB executable used by every GUI worker.

Keeping this in a small dependency-free module avoids accidental fallbacks to a
different ``adb.exe`` in background dialogs.  Two ADB versions taking turns on
port 5037 is a common cause of device disconnects on Windows.
"""

from __future__ import annotations

import os
import threading

from . import settings as settings_mod

# The daemon thread of the one early server start (see prewarm_adb_server).
_prewarm_thread = None
_prewarm_lock = threading.Lock()
# How long the early start waits for the daemon.  It only waits: the launcher
# it starts is shared with every later caller, which wait on it in turn.
PREWARM_TIMEOUT_S = 20.0


def gui_adb_path() -> str | None:
    """Return the ADB the GUI pins: ``TURBOADB_ADB``, else the Settings path,
    else TurboADB's managed Platform-Tools.

    That is :func:`turboadb.tools.find_adb`'s order, so the GUI and a CLI
    started from the same environment use the same adb.  The GUI used to
    ignore ``TURBOADB_ADB`` once the managed copy existed, and the two adbs
    then kept restarting each other's server."""
    env = (os.environ.get("TURBOADB_ADB") or "").strip()
    if env:
        try:
            from ..tools import _path_candidate

            found = _path_candidate("adb", env)
        except Exception:
            found = None
        if found:
            return found
    configured = (settings_mod.get("adb_path") or "").strip()
    if configured:
        return configured
    try:
        from ..toolsdl import managed_adb

        return managed_adb() or None
    except Exception:
        return None


def prewarm_adb_server() -> None:
    """Start the GUI's adb server on a daemon thread, once per process.

    The Windows daemon's first USB scan is the slow part of a cold launch, so
    the ``turboadb-gui`` launcher calls this before importing PyQt, and
    ``gui.app.main`` again for every other entry point; only the first call
    starts anything.  :func:`turboadb.tools.ensure_adb_server` shares its
    launcher with the main window's startup check and the device tabs, so one
    ``adb start-server`` runs however many of them ask while the daemon scans.

    Nothing starts before the GUI has its own adb (Settings path or managed
    copy): a system or PATH adb must not win this race.
    """
    global _prewarm_thread
    with _prewarm_lock:
        if _prewarm_thread is not None:
            return
        adb_path = gui_adb_path()
        if not adb_path:
            return

        def run() -> None:
            try:
                from ..tools import ensure_adb_server

                ensure_adb_server(adb_path, timeout=PREWARM_TIMEOUT_S)
            except Exception:
                # The main window reports an actionable message once it
                # exists; this early start is only a latency optimisation.
                pass

        _prewarm_thread = threading.Thread(
            target=run, name="TurboADB-early-server-start", daemon=True
        )
        _prewarm_thread.start()
