"""Self-update: check PyPI for a newer TurboADB and ``pip install --upgrade`` it
in place, then refresh adb/scrcpy and relaunch — so the user always runs the
latest version without touching a terminal."""

from __future__ import annotations

import os
import sys
import json
import subprocess
import urllib.request

from .tools import NO_WINDOW

PYPI_JSON = "https://pypi.org/pypi/turboadb/json"


def current_version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "0"


def _vtuple(v: str):
    """A lenient version tuple so '0.9.27' > '0.9.9' compares correctly."""
    out = []
    for part in str(v).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def pypi_latest(timeout: float = 6.0) -> str | None:
    """The newest version on PyPI, or None if the network/PyPI is unavailable."""
    try:
        req = urllib.request.Request(PYPI_JSON, headers={"User-Agent": "TurboADB"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        return (data.get("info") or {}).get("version")
    except Exception:
        return None


def is_newer(latest: str | None, current: str | None = None) -> bool:
    if not latest:
        return False
    return _vtuple(latest) > _vtuple(current or current_version())


def can_self_update() -> bool:
    """We can only pip-upgrade a normal pip install — not the frozen one-file exe
    (which has no pip and whose own file is locked while running)."""
    return not getattr(sys, "frozen", False)


def check() -> str | None:
    """Return the latest version string IF it is newer than what's installed and
    we're able to self-update; otherwise None."""
    if not can_self_update():
        return None
    latest = pypi_latest()
    return latest if is_newer(latest) else None


def _pip_upgrade_cmd():
    return [sys.executable, "-m", "pip", "install", "--upgrade",
            "--no-input", "--disable-pip-version-check", "turboadb"]


def _installed_version_fresh() -> str | None:
    """Read the just-upgraded version from disk in a SUBPROCESS — our own process
    still holds the OLD turboadb in memory, so importing it here would lie."""
    try:
        code = ("import importlib.metadata as m;"
                "print(m.version('turboadb'))")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=30, creationflags=NO_WINDOW)
        return (r.stdout or "").strip() or None
    except Exception:
        return None


def run_upgrade(notify=None) -> dict:
    """pip-upgrade TurboADB, then refresh adb + scrcpy. Returns a result dict:
    {ok, old, new, adb, scrcpy, error}."""
    def say(msg):
        if notify:
            try:
                notify(msg)
            except Exception:
                pass

    res = {"ok": False, "old": current_version(), "new": None,
           "adb": None, "scrcpy": None, "error": None}
    if not can_self_update():
        res["error"] = "running the bundled exe — upgrade with: pip install -U turboadb"
        return res
    try:
        say("Upgrading TurboADB from PyPI (pip install --upgrade)…")
        p = subprocess.run(_pip_upgrade_cmd(), capture_output=True, text=True,
                           timeout=600, creationflags=NO_WINDOW)
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "pip failed").strip()
            res["error"] = tail[-600:]
            return res
        res["ok"] = True
        res["new"] = _installed_version_fresh() or res["old"]
        say(f"TurboADB upgraded to {res['new']}.")
    except Exception as exc:
        res["error"] = str(exc)
        return res
    # bring adb + scrcpy up to date too, and report their versions
    try:
        say("Updating adb + scrcpy…")
        from . import toolsdl
        toolsdl.upgrade_tools(notify=say)
        res["adb"] = toolsdl.installed_adb_version()
        res["scrcpy"] = toolsdl.installed_scrcpy_version()
    except Exception as exc:
        say(f"adb/scrcpy refresh skipped: {exc}")
    return res


def _relaunch_cmd():
    """The best windowless way to start a FRESH GUI process (which will import the
    upgraded code)."""
    import shutil
    launcher = shutil.which("turboadb-gui")
    if launcher:
        return [launcher]
    pydir = os.path.dirname(sys.executable)
    pyw = os.path.join(pydir, "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return [exe, "-m", "turboadb", "gui"]


def relaunch() -> bool:
    """Start a new GUI process detached from this one. Returns True on success."""
    try:
        flags = 0
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200      # DETACHED | NEW_PROCESS_GROUP
        subprocess.Popen(_relaunch_cmd(), creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        return True
    except Exception:
        return False
