"""Self-update: check GitHub Releases for a newer TurboADB and install its wheel
with pip in place, then refresh adb/scrcpy and relaunch — so the user always runs
the latest version without touching a terminal."""

from __future__ import annotations

import atexit
import os
import re
import sys
import json
import subprocess
import urllib.request

from .config import user_path
from .tools import NO_WINDOW, parse_version, windowless_python

REPO_URL = "https://github.com/NVNKENNEDY/turboadb"
# Where each release is published: the standalone executable, the wheel and the
# source package.  This address redirects to the newest release's own page.
RELEASES_PAGE = f"{REPO_URL}/releases/latest"
# A version that can go in a download address.
_PLAIN_VERSION = re.compile(r"[0-9][0-9A-Za-z.!+_-]*")

# The folder of the turboadb package this process imported.
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def current_version() -> str:
    """The running TurboADB version (the single ``__version__`` lookup)."""
    try:
        from . import __version__

        return __version__
    except Exception:
        return "0"


def _version_of_release(url: str | None) -> str | None:
    """The version in a release page's address (``…/releases/tag/v6.0.0``)."""
    found = re.search(r"/releases/tag/v?([0-9][0-9A-Za-z.!+_-]*)/?$", url or "")
    return found.group(1) if found else None


def latest_release(timeout: float = 6.0) -> str | None:
    """The newest version on GitHub Releases, or None if it cannot be reached.

    Read from where :data:`RELEASES_PAGE` redirects to: GitHub's API allows 60
    requests an hour from one address, which a shared office address spends."""
    try:
        req = urllib.request.Request(
            RELEASES_PAGE, headers={"User-Agent": "TurboADB"}, method="HEAD"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _version_of_release(resp.geturl())
    except Exception:
        return None


def wheel_url(version: str) -> str:
    """Where the wheel of *version* is published."""
    return f"{REPO_URL}/releases/download/v{version}/turboadb-{version}-py3-none-any.whl"


def is_newer(latest: str | None, current: str | None = None) -> bool:
    if not latest:
        return False
    return parse_version(latest) > parse_version(current or current_version())


def _neutral_cwd() -> str | None:
    """The folder a fresh interpreter is started in: the interpreter's own, as
    the shortcuts use.  ``python -c`` and ``-m`` put the current folder first
    on ``sys.path``, so a child started from a source checkout imported the
    checkout instead of the copy pip had just upgraded."""
    folder = os.path.dirname(sys.executable or "")
    return folder if os.path.isdir(folder) else None


_WHERE_MARK = "TURBOADB-INSTALL-JSON:"

# Runs in a FRESH interpreter, started in a neutral folder: which turboadb this
# Python imports there, and what pip installed.  That is the copy a shortcut, a
# restart after an update and pip itself all work on, while this process may be
# running a source checkout (python main.py).  find_spec only locates the
# package; nothing of it is imported.
_WHERE_CODE = (
    "import json, os\n"
    "import importlib.util\n"
    "out = {}\n"
    "try:\n"
    "    spec = importlib.util.find_spec('turboadb')\n"
    "    out['file'] = spec.origin if spec else None\n"
    "except Exception:\n"
    "    out['file'] = None\n"
    "try:\n"
    "    import importlib.metadata as meta\n"
    "    dist = meta.distribution('turboadb')\n"
    "    out['version'] = dist.version\n"
    "    out['dist_file'] = str(dist.locate_file('turboadb/__init__.py'))\n"
    "    base = str(dist.locate_file(''))\n"
    "    url = json.loads(dist.read_text('direct_url.json') or '{}')\n"
    "    out['editable'] = bool((url.get('dir_info') or {}).get('editable')) or any(\n"
    "        os.path.isfile(os.path.join(base, name)) for name in ('pyproject.toml', 'setup.py'))\n"
    "except Exception:\n"
    "    pass\n"
    "print(MARK + json.dumps(out))\n"
)


def _probe_installed(timeout: float = 20.0) -> dict | None:
    """The report of ``_WHERE_CODE`` for this interpreter, or None when it
    could not be run or read."""
    code = "MARK = %r\n%s" % (_WHERE_MARK, _WHERE_CODE)
    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_neutral_cwd(),
            creationflags=NO_WINDOW,
        )
    except Exception:
        return None
    for line in (r.stdout or "").splitlines():
        if line.startswith(_WHERE_MARK):
            try:
                found = json.loads(line[len(_WHERE_MARK) :])
            except ValueError:
                return None
            return found if isinstance(found, dict) else None
    return None


def _same_file(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


# running_copy()'s answer: it cannot change while this process runs.
_RUNNING_COPY: dict = {}


def running_copy() -> dict:
    """Which TurboADB this process runs, next to the one this Python has
    installed — the copy that shortcuts, a restart after an update and pip
    itself all use.

    Returns ``{"version", "path", "installed", "installed_path", "same",
    "editable", "pip_managed", "note"}``.  *path* is the running package's
    folder; *installed* and *installed_path* are the version and folder a
    fresh interpreter imports (None when this Python has no TurboADB).  *same*
    says whether that is this copy, *editable* whether pip installed a source
    checkout, and *pip_managed* whether ``pip install --upgrade`` would replace
    the running files; each is None when this Python could not be asked,
    which callers treat as yes.  *note* is one sentence about a copy that
    differs, else None.  Asked once per process; a frozen executable is
    always its own copy."""
    if not _RUNNING_COPY:
        info = {
            "version": current_version(),
            "path": _PACKAGE_DIR,
            "installed": None,
            "installed_path": None,
            "same": True,
            "editable": False,
            "pip_managed": False,
            "note": None,
        }
        found = None if getattr(sys, "frozen", False) else _probe_installed()
        if found is None:
            if not getattr(sys, "frozen", False):
                info.update(same=None, editable=None, pip_managed=None)
        else:
            here = os.path.join(_PACKAGE_DIR, "__init__.py")
            file = found.get("file")
            info["installed"] = found.get("version")
            info["installed_path"] = os.path.dirname(file) if file else None
            info["same"] = _same_file(file, here)
            info["editable"] = bool(found.get("editable"))
            info["pip_managed"] = (
                info["same"] and not info["editable"] and _same_file(found.get("dist_file"), here)
            )
            if not info["same"]:
                if file:
                    version = info["installed"] or "(unknown version)"
                    other = f"TurboADB {version} from {info['installed_path']}"
                else:
                    other = "no TurboADB"
                info["note"] = (
                    f"TurboADB {info['version']} runs from {_PACKAGE_DIR}, but this Python "
                    f"({sys.executable}) has {other} installed"
                )
        _RUNNING_COPY.update(info)
    return dict(_RUNNING_COPY)


def self_update_blocker() -> str | None:
    """Why pip cannot upgrade the running TurboADB in place, or None when it can.

    pip upgrades the copy this Python has installed.  The standalone executable
    has no pip (and its own file is locked while it runs).  A source checkout
    is updated with git: pip would upgrade another copy (or nothing this
    process imports), and for a checkout installed in editable mode it would
    replace that install with a release."""
    if getattr(sys, "frozen", False):
        return f"running the standalone executable — download the new one from {RELEASES_PAGE}"
    copy = running_copy()
    if copy["pip_managed"] is not False:
        return None
    if copy["same"] is False:
        return f"{copy['note']}, and that is the copy pip upgrades — update this one with git pull"
    if copy["editable"]:
        return (
            f"TurboADB runs from a source checkout ({os.path.dirname(copy['path'])}) installed "
            "in editable mode — update it with git pull (pip would replace it with a release)"
        )
    return (
        f"TurboADB {copy['version']} runs from {copy['path']}, which pip does not manage — "
        "update it with git pull"
    )


def can_self_update() -> bool:
    """Whether pip can upgrade the running TurboADB in place: a normal pip
    install, not the frozen one-file exe or a source checkout (see
    :func:`self_update_blocker`)."""
    return self_update_blocker() is None


def check() -> str | None:
    """Return the latest version string IF it is newer than the one running;
    otherwise None.

    Whether pip can install it here is a separate question
    (:func:`can_self_update`): the standalone executable and a source checkout
    are told about a newer release too, and :func:`self_update_blocker` says
    how they get it."""
    latest = latest_release()
    return latest if is_newer(latest) else None


_CHECK_CACHE = user_path("update-check.json")
_DEFAULT_CHECK_CACHE = _CHECK_CACHE


def check_cache_path() -> str:
    """Where the daily release check is cached, resolved on call through
    :func:`turboadb.config.user_dir` (the module-level ``_CHECK_CACHE`` was
    frozen at import, so a process whose HOME changed afterwards wrote it beside
    a different settings.json). An explicit override of ``_CHECK_CACHE`` still
    wins, for tests and embedders that set it."""
    if _CHECK_CACHE != _DEFAULT_CHECK_CACHE:
        return _CHECK_CACHE
    return user_path("update-check.json")


def check_cached(max_age: float = 86400) -> str | None:
    """Like :func:`check`, but asks GitHub at most once per *max_age* seconds
    (default: daily) — for the quiet launch check, so starting the app several
    times a day doesn't mean several network round-trips."""
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
    latest = latest_release()
    if latest:  # cache successes only — a network blip
        try:  # shouldn't silence the check for a day
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "latest": latest}, fh)
        except Exception:
            pass
    return latest if is_newer(latest) else None


def _has_gui() -> bool:
    """Whether this Python has the window's toolkit (the ``gui`` extra)."""
    try:
        import importlib.util

        return importlib.util.find_spec("PyQt5") is not None
    except (ImportError, ValueError):
        return False


def _pip_upgrade_cmd(version: str):
    """``pip install --upgrade`` of the wheel published for *version*.

    Where the window is installed, the wheel is asked for with its ``gui``
    extra: a release that adds a package the window needs then brings it,
    where the bare wheel left the new window unable to start."""
    url = wheel_url(version)
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-input",
        "--disable-pip-version-check",
        f"turboadb[gui] @ {url}" if _has_gui() else url,
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
    "        for tool in up.get('unknown') or []:\n"
    "            out['errors'].setdefault(tool, 'could not find out whether a newer version exists')\n"
    "        out['manual'] = {t: toolsdl.manual_update_steps(t) for t in ('adb', 'scrcpy') if t in out['errors']}\n"
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


# What the adb/scrcpy refresh may take: winget asked twice for each tool, up to
# 240 s a time, then the publisher's download.
_TOOLS_REPORT_TIMEOUT_S = 1800.0


def _fresh_report(upgrade_tools: bool = True, timeout: float = _TOOLS_REPORT_TIMEOUT_S) -> dict:
    """Ask a fresh interpreter for the installed version AND (optionally) run the
    adb/scrcpy refresh there, returning its report dict.

    Our own process still holds the OLD turboadb in memory, so importing it here
    would lie about the version and would run the previous release's downloader.
    It starts in a neutral folder (see :func:`_neutral_cwd`), so a source
    checkout in the current folder can't stand in for the upgraded package.
    When there is no report, the dict holds only ``_failure``: why not."""
    code = _report_source(upgrade_tools)
    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=_neutral_cwd(),
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"_failure": f"it did not finish within {timeout:g} s"}
    except Exception as exc:
        return {"_failure": f"it could not be started: {exc}"}
    for line in (r.stdout or "").splitlines():
        if line.startswith(_REPORT_MARK):
            try:
                found = json.loads(line[len(_REPORT_MARK) :])
            except ValueError:
                break
            if isinstance(found, dict):
                return found
            break
    said = (r.stderr or "").strip().splitlines()
    return {"_failure": f"it ended with exit code {r.returncode} and no report"
                        + (f": {said[-1][:300]}" if said else "")}


def _manual_steps() -> dict:
    """How the user updates adb and scrcpy by hand (text only: nothing of the
    old package's downloader runs)."""
    try:
        from . import toolsdl

        return {tool: toolsdl.manual_update_steps(tool) for tool in ("adb", "scrcpy")}
    except Exception:
        return {}


def _installed_version_fresh() -> str | None:
    """The just-upgraded version, read in a subprocess (see :func:`_fresh_report`)."""
    return _fresh_report(upgrade_tools=False, timeout=30).get("version")


def run_upgrade(notify=None, *, expected: str | None = None, allow_unchanged: bool = False) -> dict:
    """Install the newer release's wheel with pip, then refresh adb + scrcpy.
    Returns a result dict: {ok, old, new, unchanged, adb, scrcpy, error}.

    *expected* is the version that was offered, and pip installs exactly that
    one (without it the newest release is asked for here).  The installed
    version is read back first: an upgrade that leaves it where it was fails,
    before adb and scrcpy are touched — otherwise the GUI reported the old
    version as new, restarted, and offered the same update again.
    *allow_unchanged* is for a run that only means to refresh adb and scrcpy
    (no release is newer): pip is not run at all."""

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
        "unchanged": None,
        "adb": None,
        "scrcpy": None,
        "error": None,
    }
    if not can_self_update():
        res["error"] = self_update_blocker() or "pip cannot upgrade this copy of TurboADB"
        return res
    version = expected if expected and _PLAIN_VERSION.fullmatch(expected) else None
    if version is None and not allow_unchanged:
        latest = latest_release()
        if not latest:
            res["error"] = f"could not read the newest release from {RELEASES_PAGE}"
            return res
        if is_newer(latest):
            version = latest
        else:
            allow_unchanged = True  # nothing newer: only adb and scrcpy are left
    if version and not allow_unchanged:
        try:
            say(f"Installing TurboADB {version} from GitHub…")
            p = subprocess.run(
                _pip_upgrade_cmd(version),
                capture_output=True,
                text=True,
                timeout=600,
                creationflags=NO_WINDOW,
            )
            if p.returncode != 0:
                tail = (p.stderr or p.stdout or "pip failed").strip()
                res["error"] = tail[-600:]
                return res
        except Exception as exc:
            res["error"] = str(exc)
            return res
    installed = None
    if version and not allow_unchanged:
        installed = _installed_version_fresh()
        if installed and (
            parse_version(installed) <= parse_version(res["old"])
            or parse_version(installed) < parse_version(version)
        ):
            res["new"] = installed
            res["unchanged"] = installed == res["old"]
            res["error"] = (
                f"pip finished, but TurboADB {installed} is still what is installed "
                f"(not {version}). Nothing else was changed; try again later."
            )
            return res
    res["ok"] = True
    # The version report AND the adb/scrcpy refresh both run in the SAME fresh
    # interpreter, so they come from the code pip just wrote — this process is
    # still running the old package and must not drive the new one's downloader.
    say("Updating adb + scrcpy…")
    report = _fresh_report()
    # pip installed exactly *version* (it said so, and its exit code was 0):
    # with no report to read that back from, that is still the new version
    pip_ran = bool(version and not allow_unchanged)
    res["new"] = report.get("version") or installed or (version if pip_ran else None) or res["old"]
    res["unchanged"] = res["new"] == res["old"]
    res["adb"], res["scrcpy"] = report.get("adb"), report.get("scrcpy")
    if res["unchanged"]:
        say(f"TurboADB {res['new']} is up to date.")
    else:
        say(f"TurboADB upgraded to {res['new']}.")
    terr = dict(report.get("errors") or {})
    manual = dict(report.get("manual") or {})
    if report.get("_failure"):
        # The refresh gave no report at all: whether adb and scrcpy were
        # updated is unknown, and the user is told so, with how to do it.
        terr["tools"] = f"the adb and scrcpy update did not report back ({report['_failure']})"
        manual = {**_manual_steps(), **manual}
    if terr:
        # TurboADB itself is updated either way: surface what was not, and how
        # to do it by hand, instead of silently reporting the OLD versions
        res["tools_errors"] = terr
        res["tools_manual"] = manual
        for tool, err in terr.items():
            say(f"[WARNING] {tool} update failed: {err}")
            if manual.get(tool):
                say(f"  Update {tool} yourself: {manual[tool]}")
        for tool, steps in manual.items():
            if tool not in terr:  # (the refresh as a whole went unreported)
                say(f"  Update {tool} yourself: {steps}")
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
        # ``-m turboadb`` imports from the current folder first: started from a
        # source checkout, the "upgraded" GUI would have been the checkout
        cwd=_neutral_cwd(),
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
