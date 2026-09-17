"""Self-update: check PyPI for a newer TurboADB and ``pip install --upgrade`` it
in place, then refresh adb/scrcpy and relaunch — so the user always runs the
latest version without touching a terminal."""

from __future__ import annotations

import atexit
import os
import sys
import json
import subprocess
import urllib.request

from .config import user_path
from .tools import NO_WINDOW, parse_version, windowless_python

PYPI_JSON = "https://pypi.org/pypi/turboadb/json"


def current_version() -> str:
    """The running TurboADB version (the single ``__version__`` lookup)."""
    try:
        from . import __version__

        return __version__
    except Exception:
        return "0"


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
    return parse_version(latest) > parse_version(current or current_version())


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


_CHECK_CACHE = user_path("update-check.json")
_DEFAULT_CHECK_CACHE = _CHECK_CACHE


def check_cache_path() -> str:
    """Where the daily PyPI check is cached, resolved on call through
    :func:`turboadb.config.user_dir` (the module-level ``_CHECK_CACHE`` was
    frozen at import, so a process whose HOME changed afterwards wrote it beside
    a different settings.json). An explicit override of ``_CHECK_CACHE`` still
    wins, for tests and embedders that set it."""
    if _CHECK_CACHE != _DEFAULT_CHECK_CACHE:
        return _CHECK_CACHE
    return user_path("update-check.json")


def check_cached(max_age: float = 86400) -> str | None:
    """Like :func:`check`, but hits PyPI at most once per *max_age* seconds
    (default: daily) — for the quiet launch check, so starting the app several
    times a day doesn't mean several network round-trips."""
    if not can_self_update():
        return None
    import time

    cache = check_cache_path()
    try:
        with open(cache, encoding="utf-8") as fh:
            c = json.load(fh)
        if time.time() - float(c.get("ts", 0)) < max_age:
            latest = c.get("latest")
            return latest if is_newer(latest) else None
    except Exception:
        pass
    latest = pypi_latest()
    if latest:  # cache successes only — a network blip
        try:  # shouldn't silence the check for a day
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "latest": latest}, fh)
        except Exception:
            pass
    return latest if is_newer(latest) else None


def _pip_upgrade_cmd():
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-input",
        "--disable-pip-version-check",
        "turboadb",
    ]


_REPORT_MARK = "TURBOADB-UPGRADE-JSON:"

# Runs in a FRESH interpreter after the pip upgrade: it imports the code that
# was just written to disk, so the tool refresh and the versions it reports come
# from the NEW turboadb. Doing this in-process re-used whatever turboadb was
# already imported — the old package driving the new one's upgrade.
_REPORT_CODE = (
    "import json\n"
    "import importlib.metadata as meta\n"
    "out = {'version': None, 'adb': None, 'scrcpy': None, 'errors': {}}\n"
    "try:\n"
    "    out['version'] = meta.version('turboadb')\n"
    "except Exception as exc:\n"
    "    out['errors']['turboadb'] = str(exc)\n"
    "try:\n"
    "    from turboadb import toolsdl\n"
    "    if UPGRADE:\n"
    "        up = toolsdl.upgrade_tools() or {}\n"
    "        out['errors'].update({k: str(v) for k, v in (up.get('errors') or {}).items()})\n"
    "        out['updated'] = sorted(up.get('updated') or {})\n"
    "    out['adb'] = toolsdl.installed_adb_version()\n"
    "    out['scrcpy'] = toolsdl.installed_scrcpy_version()\n"
    "except Exception as exc:\n"
    "    out['errors']['tools'] = str(exc)\n"
    "print(MARK + json.dumps(out))\n"
)


def _report_source(upgrade_tools: bool) -> str:
    """The script above with its two inputs bound (no str.format: the code is
    full of dict literals whose braces it would try to substitute)."""
    return "MARK = %r\nUPGRADE = %r\n%s" % (_REPORT_MARK, bool(upgrade_tools), _REPORT_CODE)


def _fresh_report(upgrade_tools: bool = True, timeout: float = 900.0) -> dict:
    """Ask a fresh interpreter for the installed version AND (optionally) run the
    adb/scrcpy refresh there, returning its report dict.

    Our own process still holds the OLD turboadb in memory, so importing it here
    would lie about the version and would run the previous release's downloader.
    Returns ``{}`` when the subprocess could not be run or parsed."""
    code = _report_source(upgrade_tools)
    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except Exception:
        return {}
    for line in (r.stdout or "").splitlines():
        if line.startswith(_REPORT_MARK):
            try:
                return json.loads(line[len(_REPORT_MARK) :])
            except ValueError:
                return {}
    return {}


def _installed_version_fresh() -> str | None:
    """The just-upgraded version, read in a subprocess (see :func:`_fresh_report`)."""
    return _fresh_report(upgrade_tools=False, timeout=30).get("version")


def run_upgrade(notify=None) -> dict:
    """pip-upgrade TurboADB, then refresh adb + scrcpy. Returns a result dict:
    {ok, old, new, adb, scrcpy, error}."""

    def say(msg):
        if notify:
            try:
                notify(msg)
            except Exception:
                pass

    res = {
        "ok": False,
        "old": current_version(),
        "new": None,
        "adb": None,
        "scrcpy": None,
        "error": None,
    }
    if not can_self_update():
        res["error"] = "running the standalone executable — upgrade with: pip install -U turboadb"
        return res
    try:
        say("Upgrading TurboADB from PyPI (pip install --upgrade)…")
        p = subprocess.run(
            _pip_upgrade_cmd(), capture_output=True, text=True, timeout=600, creationflags=NO_WINDOW
        )
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "pip failed").strip()
            res["error"] = tail[-600:]
            return res
        res["ok"] = True
    except Exception as exc:
        res["error"] = str(exc)
        return res
    # The version report AND the adb/scrcpy refresh both run in the SAME fresh
    # interpreter, so they come from the code pip just wrote — this process is
    # still running the old package and must not drive the new one's downloader.
    say("Updating adb + scrcpy…")
    report = _fresh_report()
    res["new"] = report.get("version") or res["old"]
    res["adb"], res["scrcpy"] = report.get("adb"), report.get("scrcpy")
    say(f"TurboADB upgraded to {res['new']}.")
    terr = report.get("errors") or {}
    if terr:
        # surface failures instead of silently reporting the OLD versions
        res["tools_errors"] = terr
        for tool, err in terr.items():
            say(f"[WARNING] {tool} update failed: {err}")
    return res


def _relaunch_cmd():
    """The best windowless way to start a FRESH GUI process (which will import the
    upgraded code). Always the interpreter that is running now — a
    ``turboadb-gui`` found on PATH may belong to a different environment that
    was never upgraded."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [windowless_python(), "-m", "turboadb", "gui"]


def _flush_user_state() -> None:
    """Write anything this process still owes to ``~/.turboadb`` before a
    replacement process starts reading (and writing) the same files."""
    try:
        from .gui import settings as settings_mod

        settings_mod.flush_pending()
    except Exception:
        pass


def _spawn() -> bool:
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    subprocess.Popen(
        _relaunch_cmd(),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return True


def relaunch() -> bool:
    """Start a new GUI process detached from this one. Returns True on success.

    **The caller must be finished with this instance before calling it.** Two
    TurboADB processes running at once both write ``settings.json`` and the last
    one to exit wins, so a modal dialog between the spawn and the quit is a bug;
    use :func:`relaunch_on_exit` instead, which cannot get that order wrong.
    Debounced settings are flushed here so the new process at least reads this
    one's final values."""
    try:
        _flush_user_state()
        return _spawn()
    except Exception:
        return False


_relaunch_scheduled = False


def _relaunch_at_exit() -> None:
    try:
        _flush_user_state()
        _spawn()
    except Exception:
        pass


def relaunch_on_exit() -> bool:
    """Schedule the replacement GUI to start when THIS process exits.

    The safe contract for "update, then restart": nothing is spawned while this
    instance is still alive, so the two can never overlap — show the dialogs,
    then quit, in whatever order suits the UI. Returns True if the restart was
    scheduled; call :func:`cancel_relaunch` to undo it."""
    global _relaunch_scheduled
    if _relaunch_scheduled:
        return True
    try:
        atexit.register(_relaunch_at_exit)
        _relaunch_scheduled = True
        return True
    except Exception:
        return False


def cancel_relaunch() -> None:
    """Undo a :func:`relaunch_on_exit` (the user cancelled the restart)."""
    global _relaunch_scheduled
    if _relaunch_scheduled:
        atexit.unregister(_relaunch_at_exit)
        _relaunch_scheduled = False
