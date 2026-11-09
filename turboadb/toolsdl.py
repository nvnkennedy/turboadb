"""
Getting the external tools TurboADB drives.  TurboADB ships neither of them.

On Windows the first run installs both through winget (``Google.PlatformTools``
and ``Genymobile.scrcpy``), and winget keeps them up to date.  Where there is no
winget, or it fails, the tools come from their publishers into a per-user cache
(``~/.turboadb/tools/``):

  * ``adb``    — Google's official platform-tools zip.
  * ``scrcpy`` — Genymobile's official GitHub release. Prebuilt archives exist
                 for Windows; on macOS/Linux use brew/apt.

Nothing here runs at install time (wheels don't run code on install); it runs
on the first launch or command, or when you ask: ``turboadb fetch-tools``, the
GUI's download prompt, or ``turboadb.fetch_tools()``. The automatic :func:`ensure_tools` only installs a
tool that is MISSING; upgrades happen only through :func:`upgrade_tools`
(``turboadb upgrade-tools`` / self-update / the GUI Upgrade button).
"""

from __future__ import annotations

import os
import re
import sys
import json
import stat
import time
import shutil
import logging
import socket
import zipfile
import tempfile
import platform
import threading
import subprocess
import http.client
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

from .exceptions import ADBError, ADBNotFoundError
from .tools import (  # noqa: F401  (_exe re-exported for existing importers)
    ADB_DOWNLOAD,
    ADB_WINDOWS_FILES,
    NO_WINDOW,
    SCRCPY_DOWNLOAD,
    WINGET_PACKAGES,
    _adb_version_output,
    _exe,
    _is_absolute,
    _on_path,
    clear_tools_cache,
    find_adb,
    find_scrcpy,
    managed_tools_dir,
    parse_version,
    winget_copy,
)

PLATFORM_TOOLS_REPO_XML = "https://dl.google.com/android/repository/repository2-3.xml"

PLATFORM_TOOLS_URLS = {
    "windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
    "linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
}
PLATFORM_TOOLS_BASE = "https://dl.google.com/android/repository/"
SCRCPY_RELEASES_API = "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"
_UA = {"User-Agent": "turboadb"}

_log = logging.getLogger(__name__)

# The <host-os> value Google's manifest uses for each of our platforms.
_MANIFEST_HOST_OS = {"windows": "windows", "darwin": "macosx", "linux": "linux"}


def _os_key() -> str:
    s = sys.platform
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "darwin"
    return "linux"


def scrcpy_download_supported() -> bool:
    """True where a prebuilt scrcpy can be downloaded (Windows only)."""
    return _os_key() == "windows"


def adb_download_supported() -> bool:
    """True where Google builds platform-tools that this machine can run:
    Windows, macOS, and Linux on x86-64 (its one Linux build).  An ARM board
    keeps the adb of its own system."""
    if _os_key() != "linux":
        return True
    # _os_key calls every system that is neither Windows nor macOS "linux"
    return platform.system() == "Linux" and platform.machine().lower() in ("x86_64", "amd64")


def tools_dir() -> str:
    """The managed cache directory for downloaded tools (not created on read)."""
    return managed_tools_dir()


def _ensure_tools_dir() -> str:
    d = tools_dir()
    os.makedirs(d, exist_ok=True)
    return d


# A download is fetched and unpacked in a folder of this name inside the tools
# folder, never under %TEMP%: on another volume the final move became a copy,
# and a copy that failed halfway (a full disk) left platform-tools half there
# with no adb.  Neither lookup of the tools (managed_adb, tools.find_adb) ever
# looks inside it.
_STAGING_PREFIX = ".download-"
# A staging folder older than this is a leftover of an update that was killed.
_STALE_STAGING_S = 3600


def _staging_dir():
    """A temporary folder for one download, on the tools folder's volume.
    Leftovers of killed updates are removed first."""
    parent = _ensure_tools_dir()
    try:
        names = os.listdir(parent)
    except OSError:
        names = []
    now = time.time()
    for name in names:
        path = os.path.join(parent, name)
        try:
            stale = name.startswith(_STAGING_PREFIX) and now - os.path.getmtime(path) > _STALE_STAGING_S
        except OSError:
            stale = False
        if stale:
            shutil.rmtree(path, ignore_errors=True)
    return tempfile.TemporaryDirectory(prefix=_STAGING_PREFIX, dir=parent)


def adb_dir() -> str:
    return os.path.join(tools_dir(), "platform-tools")


def scrcpy_dir() -> str:
    return os.path.join(tools_dir(), "scrcpy")


def managed_adb() -> str | None:
    """The adb TurboADB looks after: winget's copy, else the one in the cache."""
    p = os.path.join(adb_dir(), _exe("adb"))
    return winget_copy("adb") or (p if os.path.isfile(p) else None)


def managed_scrcpy() -> str | None:
    """The scrcpy TurboADB looks after: winget's copy, else the one in the cache."""
    p = os.path.join(scrcpy_dir(), _exe("scrcpy"))
    return winget_copy("scrcpy") or (p if os.path.isfile(p) else None)


# --------------------------------------------------------------------------- #
# download + extract helpers
# --------------------------------------------------------------------------- #
# A server's bad moment: worth asking again (GitHub answers a release file with
# a 504 now and then, one file at a time, and the same file a minute later).
_RETRY_HTTP = frozenset((408, 425, 429, 500, 502, 503, 504))
_RETRY_ERRORS = (
    urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
    http.client.IncompleteRead, http.client.RemoteDisconnected,
)
_DOWNLOAD_TRIES = 4
_RETRY_WAITS_S = (1, 3, 6, 10, 15, 20)


class ServerDidNotAnswer(ADBError):
    """A download failed because of the server or the network, not the file."""


def _worth_retrying(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRY_HTTP
    if isinstance(getattr(exc, "reason", None), socket.gaierror):
        return False  # no name lookup: this PC is offline, asking again won't help
    return isinstance(exc, _RETRY_ERRORS)


def _with_retries(action, tries: int = _DOWNLOAD_TRIES):
    """``action()``, asked again after a pause while a server has a bad moment."""
    for attempt in range(max(1, tries)):
        try:
            return action()
        except Exception as exc:
            if attempt + 1 >= tries or not _worth_retrying(exc):
                raise
            time.sleep(_RETRY_WAITS_S[min(attempt, len(_RETRY_WAITS_S) - 1)])


def _download(url: str, dest: str, on_progress=None, tries: int = _DOWNLOAD_TRIES) -> None:
    try:
        _with_retries(lambda: _download_once(url, dest, on_progress), tries)
    except Exception as exc:
        if not _worth_retrying(exc):
            raise
        host = urllib.parse.urlparse(url).hostname or "the download server"
        raise ServerDidNotAnswer(
            f"{host} did not answer ({exc}), after {max(1, tries)} tries. Nothing was "
            "changed, and a copy you already have still works. Try again in a while."
        ) from exc


def _download_once(url: str, dest: str, on_progress=None) -> None:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if on_progress and total:
                    try:
                        on_progress(int(done * 100 / total))
                    except Exception:
                        pass
    if on_progress:
        try:
            on_progress(100)
        except Exception:
            pass


def _extract_zip(zip_path: str, dest_parent: str, *, strip_top_to: str | None = None):
    """Extract *zip_path*. If *strip_top_to* is given, the archive's single
    top-level folder is flattened into that exact directory. Member paths are
    sanitized ('..' / absolute / backslash tricks dropped) so a crafted archive
    can never write outside the target directory (zip-slip).

    On Linux and macOS each file keeps the permission bits the archive stores
    for it: written with a plain ``open()`` every file came out 0644, so
    fastboot and the other platform-tools besides adb could not run.  Write
    access for group and others is never granted."""
    with zipfile.ZipFile(zip_path) as z:
        # ZipInfo entries, not names: of two entries with one name, each keeps
        # its own permissions
        members = z.infolist()
        names = [m.filename.replace("\\", "/") for m in members]
        top = None
        if strip_top_to:
            tops = {n.split("/")[0] for n in names if n.strip("/")}
            top = next(iter(tops)) if len(tops) == 1 else None
        target_dir = strip_top_to or dest_parent
        os.makedirs(target_dir, exist_ok=True)
        root = os.path.realpath(target_dir)
        for member, name in zip(members, names):
            if name.endswith("/"):
                continue
            rel = name[len(top) + 1 :] if top and name.startswith(top + "/") else name
            parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
            if not parts:
                continue
            target = os.path.join(root, *parts)
            if not os.path.realpath(target).startswith(os.path.join(root, "")):
                continue  # zip-slip attempt — skip it
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            _keep_mode(target, member.external_attr >> 16)


def _keep_mode(path: str, mode: int) -> None:
    """Give an extracted file the Unix permissions its zip entry stores (none
    in an archive made on Windows); owner read/write always, nobody else's
    write, no special bits."""
    if os.name == "nt" or not mode or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
        return
    try:
        os.chmod(path, (stat.S_IMODE(mode) & 0o755) | 0o600)
    except OSError:
        pass


def _kill_adb_server(adb_path: str | None = None) -> list:
    """Best-effort stop of the adb server. On Windows a running server LOCKS its
    own adb.exe, so replacing the managed platform-tools in place silently fails
    (rmtree with ignore_errors dropped the error) — which is exactly why
    'upgrade adb' sometimes did nothing. Always stop the server first.

    The stop waits until the server has let go of the port (and so of its
    exe).  The local server always stops, and so does a server shared on
    another port this account recorded (``turboadb serve --port N``, see
    :func:`turboadb.devices.recorded_shared_ports`): it runs the same adb.
    Returns the ports of the servers that were shared on the network, which
    the caller must bring back after the swap with
    :func:`_restore_shared_server`: other machines depend on them, and
    nothing else would restart them."""
    try:
        from . import devices, tools

        exe = adb_path or managed_adb() or find_adb()
        local = tools.local_adb_port()
        shared = []
        for port in [local] + [p for p in devices.recorded_shared_ports() if p != local]:
            if devices.server_is_shared(port):
                shared.append(port)
            elif port != local:
                continue  # nothing of ours there any more
            tools.kill_adb_server(exe, port)
        return shared
    except Exception:
        return []


# Why a shared adb server stopped for an update is not back, for the result.
_SHARE_PROBLEM = ""
# While updates run together (see _one_restart), the ports of the shared
# servers they stopped, brought back once at the end.
_BATCH = threading.local()


@contextmanager
def _one_restart():
    """Within the block, a shared server stopped for an update comes back once,
    at the end: upgrading adb and scrcpy together restarted it twice, and each
    restart dropped every other machine's shells, logcats and mirrors."""
    if getattr(_BATCH, "ports", None) is not None:
        yield  # an outer batch brings them back
        return
    _BATCH.ports = []
    try:
        yield
    finally:
        ports, _BATCH.ports = _BATCH.ports, None
        _restore_shared_server(ports)


def _restore_shared_server(ports) -> None:
    """Share this PC's devices again on *ports* after the update (see
    :func:`_kill_adb_server`); never raises.  A failure is reported through
    :func:`_take_share_problem`.  Within :func:`_one_restart` the ports wait
    for the end of the batch."""
    global _SHARE_PROBLEM
    ports = [p for p in (ports or []) if p]
    waiting = getattr(_BATCH, "ports", None)
    if waiting is not None:
        waiting.extend(p for p in ports if p not in waiting)
        return
    for port in ports:
        try:
            from .devices import restart_shared_server

            _log.info(restart_shared_server(port, managed_adb()))
        except Exception as exc:
            again = "turboadb serve" + ("" if port == 5037 else f" --port {port}")
            problem = (
                f"the shared adb server on port {port} could not be restarted after the "
                f"update ({exc}); share this PC's devices again ({again})"
            )
            _SHARE_PROBLEM = f"{_SHARE_PROBLEM}; {problem}" if _SHARE_PROBLEM else problem
            _log.warning(problem)


def _take_share_problem(errors: dict) -> None:
    global _SHARE_PROBLEM
    if _SHARE_PROBLEM:
        errors["sharing"] = _SHARE_PROBLEM
        _SHARE_PROBLEM = ""


def _aside(dest: str) -> str:
    """Where the current *dest* goes while it is replaced: ``dest.old``, emptied
    first, or a fresh ``dest.old-N`` when an earlier copy there cannot be
    removed (a terminal's working folder is inside it, a scanner holds a
    file).  That copy used to make the update fail, blaming a running adb."""
    old = dest + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if not os.path.lexists(old):
        return old
    n = 1
    while os.path.lexists(f"{old}-{n}"):
        n += 1
    return f"{old}-{n}"


def _clear_old_copies(dest: str) -> None:
    """Remove the ``dest.old-N`` copies earlier updates could not put at
    ``dest.old`` (best-effort: one still in use stays for the next time)."""
    parent, base = os.path.split(dest)
    try:
        names = os.listdir(parent or ".")
    except OSError:
        return
    for name in names:
        if name.startswith(base + ".old-"):
            shutil.rmtree(os.path.join(parent, name), ignore_errors=True)


def _swap_dir(new_dir: str, dest: str) -> None:
    """Replace *dest* with *new_dir* atomically-ish: move the old directory
    aside (kept as ``dest.old`` for a manual rollback), then move the new one
    in. If a file in the old dir is still locked (a running adb server / scrcpy
    session), the rename fails LOUDLY instead of silently leaving a
    half-updated mix of old and new files.

    *new_dir* belongs on the same volume as *dest* (see :func:`_staging_dir`),
    so both moves are renames.  A move that failed halfway all the same (a
    copy between volumes) is cleared away before the old version goes back."""
    old = None
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _clear_old_copies(dest)
    if os.path.isdir(dest):
        old = _aside(dest)
        try:
            os.rename(dest, old)
        except OSError as exc:
            raise ADBError(
                f"cannot replace {dest} — a file in it is still in use "
                f"(a running adb server or scrcpy/mirror session locks its exe, or a "
                f"terminal's working folder is inside it; close those and retry): {exc}"
            ) from exc
    try:
        shutil.move(new_dir, dest)
    except Exception:
        if old:
            if os.path.lexists(dest):  # part of the new version
                shutil.rmtree(dest, ignore_errors=True)
            try:
                os.rename(old, dest)  # roll the old version back
            except OSError:
                pass
        raise
    # keep the previous version as *.old — a one-step offline rollback if a new
    # platform-tools/scrcpy release turns out to be broken (it's replaced on the
    # next swap, so at most one extra copy sits on disk)


def _chmod_x(path: str) -> None:
    if os.name != "nt" and os.path.isfile(path):
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass


_USE_SYSTEM_ADB = (
    "Install adb with the system's package manager instead, or set TURBOADB_ADB to one."
)


def _require_runnable(adb: str) -> None:
    """Raise ADBError unless the downloaded *adb* runs on this machine.

    Google's Linux build is for x86-64 with glibc.  Where it cannot execute
    (musl, NixOS, a 32-bit system) it was installed all the same, and the
    managed copy comes before the system's adb: every command then failed with
    "Exec format error" beside a working adb, and each update check, which
    could read no version from it, downloaded it again."""
    try:
        said = _adb_version_output(adb, timeout=30.0)
    except subprocess.TimeoutExpired:
        problem = "'adb version' timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        problem = str(exc)
    else:
        if "Android Debug Bridge" in said:
            return
        problem = " ".join(said.split())[:200] or "'adb version' printed nothing"
    raise ADBError(
        f"the platform-tools download does not run on this machine ({problem}); "
        f"nothing was installed. {_USE_SYSTEM_ADB}"
    )


# --------------------------------------------------------------------------- #
# winget
# --------------------------------------------------------------------------- #
# How winget ends, as an unsigned 32-bit code.  It is done when the package
# is already at its newest.
_WINGET_DONE = frozenset((0, 0x8A15002B, 0x8A150061))  # updated, UPDATE_NOT_APPLICABLE, ALREADY_INSTALLED
# Asking again changes none of these: bad arguments, no installer for this PC,
# a hash mismatch, no such package, and a 404: winget lists a file its publisher
# has taken down (Google withdrew platform-tools 37.0.2 while winget offered it).
_WINGET_FINAL = frozenset((0x8A150002, 0x8A150010, 0x8A150011, 0x8A150014, 0x80190194))
_WINGET_TRIES = 2
_WINGET_TIMEOUT_S = 240.0
# winget prints about this many lines for one package, and no percentages when
# a program reads its output: the progress bar follows the lines, in any language.
_WINGET_LINES = 11
# The line of `winget show` with the version; its label depends on the language.
_WINGET_VERSION = re.compile(r"^[^:：\r\n]+[:：]\s*v?(\d+(?:\.\d+)+)\s*$", re.M)


def winget_exe() -> str | None:
    """winget by its full path, or None where this PC has none.  Never one
    lying in the current folder (see :func:`tools._on_path`)."""
    if os.name != "nt":
        return None
    found = _on_path("winget")
    if found:
        return found
    # a program started from a shortcut may have no WindowsApps on its PATH
    local = os.environ.get("LOCALAPPDATA", "")
    alias = os.path.join(local, "Microsoft", "WindowsApps", "winget.exe")
    return alias if _is_absolute(alias) and os.path.lexists(alias) else None


def _tell(on_progress, percent: int) -> None:
    if on_progress:
        try:
            on_progress(percent)
        except Exception:
            pass


def _run_winget(cmd: list, on_progress=None, timeout: float = _WINGET_TIMEOUT_S) -> tuple:
    """Run one winget command with no window: ``(code, output lines)``.  *code*
    is None when it did not finish within *timeout* seconds and was ended."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    expired = threading.Event()

    def expire():
        expired.set()
        proc.kill()

    watchdog = threading.Timer(timeout, expire)
    watchdog.daemon = True
    watchdog.start()
    lines = []
    try:
        for line in proc.stdout:
            if line.strip():
                lines.append(line.strip())
                _tell(on_progress, min(95, len(lines) * 100 // _WINGET_LINES))
        code = proc.wait()
    finally:
        watchdog.cancel()
        proc.stdout.close()
    return (None if expired.is_set() else code & 0xFFFFFFFF), lines


def _winget_problem(winget: str, tool: str, force: bool, on_progress) -> str | None:
    """Have winget install or update *tool*.  None when it did, else why not.
    Asked twice: winget's downloads come from the same servers as ours.

    ``--force`` takes the installed copy away first and can fail after that, so
    only a reinstall the user asked for uses it, once winget has said there is
    nothing newer to install."""
    cmd = [
        winget, "install", "--id", WINGET_PACKAGES[tool], "--exact", "--source", "winget",
        "--scope", "user", "--silent", "--accept-package-agreements",
        "--accept-source-agreements", "--disable-interactivity",
    ]
    problem = ""
    for attempt in range(_WINGET_TRIES):
        if attempt:
            time.sleep(_RETRY_WAITS_S[attempt])
        try:
            code, lines = _run_winget(cmd, on_progress)
            if force and code in _WINGET_DONE and code != 0 and "--force" not in cmd:
                cmd = cmd + ["--force"]
                code, lines = _run_winget(cmd, on_progress)
        except (OSError, subprocess.SubprocessError) as exc:
            return str(exc)
        if code in _WINGET_DONE:
            _tell(on_progress, 100)
            return None
        if code is None:
            problem = f"it did not finish within {_WINGET_TIMEOUT_S:.0f} s"
        else:
            said = lines[-1] if lines else "no output"
            problem = said if f"{code:08x}" in said.lower() else f"{said} (0x{code:08X})"
        if code in _WINGET_FINAL:
            break
    return problem


def _install_with_winget(tool: str, existing: str | None, *, force: bool = False,
                         on_progress=None) -> str | None:
    """*tool* installed or updated by winget, and the path to it.

    None leaves it to the publisher's download: this PC has no winget, or winget
    failed and holds no copy.  When it still holds one the failure is raised,
    because a download would go to a folder that is looked at only after
    winget's.  An adb server locks its adb.exe, so it stops first, and scrcpy's
    release has an adb of its own that may be the one running."""
    winget = winget_exe()
    if winget is None:
        return None
    shared = _kill_adb_server(existing if tool == "adb" else None) if existing else []
    try:
        problem = _winget_problem(winget, tool, force, on_progress)
    finally:
        clear_tools_cache()
        _restore_shared_server(shared)
    if problem and winget_copy(tool):  # asked now: a failed --force can leave none
        raise ADBError(
            f"winget could not update {tool}: {problem.rstrip('.')}. Nothing was changed, and the "
            "copy you have still works. Try again in a while."
        )
    if problem:
        _log.warning("winget could not install %s (%s); using its publisher's download", tool, problem)
        return None
    installed = winget_copy(tool)
    if not installed:
        _log.warning("winget installed %s somewhere unexpected; using its publisher's download", tool)
    return installed


def manual_update_steps(tool: str) -> str:
    """What someone runs to update *tool* themselves, or where to get it."""
    if winget_exe():
        return f"winget upgrade --id {WINGET_PACKAGES[tool]} --exact"
    return {"adb": ADB_DOWNLOAD, "scrcpy": SCRCPY_DOWNLOAD}[tool]


def winget_latest(tool: str) -> str | None:
    """The newest version of *tool* winget can install, or None if it cannot say."""
    winget = winget_exe()
    if winget is None:
        return None
    try:
        out = subprocess.run(
            [winget, "show", "--id", WINGET_PACKAGES[tool], "--exact", "--source", "winget",
             "--accept-source-agreements", "--disable-interactivity"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = _WINGET_VERSION.search(out.stdout or "") if out.returncode == 0 else None
    return found.group(1) if found else None


# --------------------------------------------------------------------------- #
# public install functions
# --------------------------------------------------------------------------- #
def download_platform_tools(*, force: bool = False, on_progress=None) -> str:
    """Install adb and return the path to it: through winget where there is
    one, else Google's platform-tools into the cache.

    The archive is extracted to a STAGING directory and swapped in atomically,
    with the adb server stopped first — extracting straight over a live adb.exe
    fails on Windows (the running server locks its own file), and with the old
    ``ignore_errors`` delete that failure was silent.

    The versioned archive named in Google's ``repository2-3.xml`` is preferred
    over the ``-latest-`` alias, because only that one comes with a published
    size and checksum to verify the download against before the swap.

    Nothing is downloaded where Google has no build for this machine
    (:func:`adb_download_supported`), and a download whose adb does not run
    here is not installed: the adb in use, and its server, are left alone."""
    existing = managed_adb()
    if existing and not force:
        return existing
    if not adb_download_supported():
        raise ADBNotFoundError(
            "Google builds no adb for this machine "
            f"({platform.system()} {platform.machine()}). {_USE_SYSTEM_ADB}"
        )
    installed = _install_with_winget("adb", existing, force=force, on_progress=on_progress)
    if installed:
        return installed
    archive = _archive_to_download()
    url = archive.get("url") or PLATFORM_TOOLS_URLS[_os_key()]
    if not archive.get("url"):
        archive = {}  # the alias isn't the artifact the manifest describes
        _log.warning(
            "Could not read Google's platform-tools manifest; downloading the "
            "'latest' alias with no published checksum to verify it against."
        )
    with _staging_dir() as tmp:
        zip_path = os.path.join(tmp, "platform-tools.zip")
        _download(url, zip_path, on_progress)
        _check_zip(zip_path)  # a truncated download must fail BEFORE the swap
        _verify_platform_tools(zip_path, archive)
        return _install_platform_tools(zip_path, existing)


def _install_platform_tools(zip_path: str, existing: str | None) -> str:
    """Replace the managed platform-tools with those in *zip_path*."""
    with _staging_dir() as tmp:
        # the zip contains a top-level "platform-tools/" folder
        staging = os.path.join(tmp, "new")
        _extract_zip(zip_path, staging)
        new_dir = os.path.join(staging, "platform-tools")
        staged = os.path.join(new_dir, _exe("adb"))
        if not os.path.isfile(staged):
            raise ADBNotFoundError(
                "platform-tools downloaded but adb was not "
                "found in the archive (unexpected layout)."
            )
        # before anything is stopped or replaced
        _chmod_x(staged)
        _require_runnable(staged)
        # unlock adb.exe before the swap
        shared = _kill_adb_server(existing) if existing else []
        try:
            _swap_dir(new_dir, adb_dir())
        except Exception:
            _restore_shared_server(shared)  # the previous adb is still in place
            raise
    # find_adb remembers its answer: a process that had found the system's adb
    # went on running that one beside the copy it had just installed
    clear_tools_cache()
    adb = managed_adb()
    if not adb:
        _restore_shared_server(shared)
        raise ADBNotFoundError(
            "platform-tools downloaded but adb was not found after install (unexpected layout)."
        )
    _chmod_x(adb)
    _sync_scrcpy_adb()
    _restore_shared_server(shared)  # other machines use it: back on the new adb
    return adb


def _release_cache_path() -> str:
    return os.path.join(tools_dir(), ".scrcpy-release.json")


def _github_release(timeout: float = 30) -> tuple:
    """``(release_json, authoritative)`` for scrcpy's latest release.

    **ETag caching**: repeated checks send If-None-Match and reuse the cached
    body on 304, so the unauthenticated GitHub rate limit (60 req/h/IP) stops
    making update checks randomly fail.

    *authoritative* is True for a fresh 200 **and** for a 304 (GitHub confirmed
    the cached body is still current). It is False when the body is a STALE
    fallback used because the request failed — offline, rate-limited, 5xx.
    Conflating the two reported "up to date" for a check that never happened,
    and meant the `unknown` branch could never fire for scrcpy."""
    cache_path = _release_cache_path()
    cached = None
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cached = json.load(fh)
    except Exception:
        pass
    headers = dict(_UA)
    if cached and cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    req = urllib.request.Request(SCRCPY_RELEASES_API, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
            etag = resp.headers.get("ETag") or ""
        try:
            _ensure_tools_dir()
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump({"etag": etag, "data": data}, fh)
        except Exception:
            pass
        return data, True
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and cached and cached.get("data"):
            return cached["data"], True  # unchanged since last time
        if cached and cached.get("data"):
            return cached["data"], False  # rate-limited etc. — stale cache
        raise
    except Exception:
        if cached and cached.get("data"):
            return cached["data"], False
        raise


def _github_release_json(timeout: float = 30) -> dict:
    """The scrcpy latest-release JSON, cached copy included (see
    :func:`_github_release`). Use it where a stale body is still useful — for
    picking a download asset — never to answer "is this up to date?"."""
    return _github_release(timeout)[0]


def _scrcpy_assets() -> tuple:
    """(zip_url, zip_name, sums_url|None) for the right Windows scrcpy asset.
    *sums_url* points at the release's SHA256SUMS.txt when it publishes one."""
    if not scrcpy_download_supported():
        raise ADBNotFoundError(
            "Prebuilt scrcpy is downloaded only on Windows. On macOS use "
            "'brew install scrcpy'; on Linux use 'apt install scrcpy' (or your "
            "distro's package)."
        )
    is64 = platform.machine().endswith("64") or sys.maxsize > 2**32
    want = "win64" if is64 else "win32"
    data = _github_release_json()
    assets = data.get("assets", [])
    sums = next(
        (a["browser_download_url"] for a in assets if a["name"].upper().startswith("SHA256SUMS")),
        None,
    )
    for a in assets:
        if a["name"].endswith(".zip") and want in a["name"]:
            return a["browser_download_url"], a["name"], sums
    for a in assets:  # fall back to any windows zip
        if a["name"].endswith(".zip") and "win" in a["name"]:
            return a["browser_download_url"], a["name"], sums
    raise ADBNotFoundError("No suitable scrcpy Windows release asset was found.")


def _verify_sha256(path: str, name: str, sums_url: str) -> None:
    """Check *path* against the published SHA256SUMS.txt entry for *name*.
    Best-effort: if the sums file can't be fetched or has no entry, skip; a
    MISMATCH always raises (corrupt or tampered download)."""
    import hashlib

    def fetch():
        req = urllib.request.Request(sums_url, headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    try:
        sums = _with_retries(fetch)
    except Exception:
        return  # can't fetch the sums — skip check
    expected = None
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            expected = parts[0].lower()
            break
    if not expected:
        return
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest().lower() != expected:
        raise ADBError(
            f"SHA-256 mismatch for {name} — the download is corrupt "
            f"or tampered with; nothing was installed. Try again."
        )


def _file_digest(path: str, algorithm: str):
    import hashlib

    h = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _verify_platform_tools(path: str, archive: dict) -> None:
    """Check a downloaded platform-tools zip against Google's own manifest.

    adb used to be installed on a CRC check alone while scrcpy was verified
    against the release's SHA256SUMS, so a mirror or proxy serving different
    bytes was accepted. A MISMATCH always raises; when the manifest published no
    checksum the size is still checked and the gap is LOGGED rather than
    silently skipped."""
    size = archive.get("size")
    actual = os.path.getsize(path)
    if size and actual != size:
        raise ADBError(
            f"platform-tools download is {actual} bytes, but Google's manifest "
            f"publishes {size}; nothing was installed. Try again."
        )
    digest, kind = archive.get("checksum"), archive.get("checksum_type") or "sha1"
    if not digest:
        _log.warning(
            "Google published no checksum for platform-tools %s; verified the "
            "archive's size (%s bytes) and CRC only.",
            archive.get("version") or "?",
            actual,
        )
        return
    try:
        got = _file_digest(path, kind)
    except (ValueError, TypeError):
        _log.warning("Google published an unsupported %s checksum; size/CRC only.", kind)
        return
    if got != digest:
        raise ADBError(
            f"{kind.upper()} mismatch for the platform-tools download (expected "
            f"{digest}, got {got}) — it is corrupt or tampered with; nothing was "
            "installed. Try again."
        )


def _check_zip(path: str) -> None:
    """CRC-check the whole archive so a truncated download fails BEFORE any
    swap, not halfway through extraction."""
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
    except zipfile.BadZipFile as exc:
        raise ADBError(f"downloaded archive is not a valid zip: {exc}") from exc
    if bad:
        raise ADBError(
            f"downloaded archive is corrupt (bad CRC: {bad}); nothing was installed. Try again."
        )


# Held while scrcpy is downloaded and swapped in: the launch check, a device
# tab's Start and Tools → Download may all ask for it at once.
_SCRCPY_INSTALL_LOCK = threading.RLock()


def install_missing_scrcpy(*, on_progress=None) -> str:
    """The scrcpy to use, downloaded first if none can be found (waiting for a
    download already under way).  Unlike :func:`ensure_tools` it can be asked
    again in the same process: a screen started after a failed launch check,
    or before that check has finished, gets scrcpy there and then.

    Raises :class:`ADBNotFoundError` for a path set on purpose that names no
    scrcpy (a download would not change it) and where no prebuilt exists."""
    with _SCRCPY_INSTALL_LOCK:
        try:
            return find_scrcpy()
        except ADBNotFoundError as exc:
            if getattr(exc, "configured", None):
                raise
        return download_scrcpy(on_progress=on_progress)


def download_scrcpy(*, force: bool = False, on_progress=None) -> str:
    """Install scrcpy and return the path to it: through winget where there
    is one, else the latest Windows prebuilt into the cache."""
    with _SCRCPY_INSTALL_LOCK:
        return _install_scrcpy_release(force=force, on_progress=on_progress)


def _install_scrcpy_release(*, force: bool, on_progress) -> str:
    existing = managed_scrcpy()
    if existing and not force:
        return existing
    installed = _install_with_winget("scrcpy", existing, force=force, on_progress=on_progress)
    if installed:
        return installed
    url, asset_name, sums_url = _scrcpy_assets()
    with _staging_dir() as tmp:
        zip_path = os.path.join(tmp, "scrcpy.zip")
        _download(url, zip_path, on_progress)
        _check_zip(zip_path)
        if sums_url:
            _verify_sha256(zip_path, asset_name, sums_url)
        return _install_scrcpy(zip_path, existing)


def _install_scrcpy(zip_path: str, existing: str | None) -> str:
    """Replace the managed scrcpy with the one in *zip_path*."""
    with _staging_dir() as tmp:
        staging = os.path.join(tmp, "scrcpy-new")
        _extract_zip(zip_path, tmp, strip_top_to=staging)
        if not os.path.isfile(os.path.join(staging, _exe("scrcpy"))):
            raise ADBNotFoundError(
                "scrcpy downloaded but scrcpy.exe was not found in the archive (unexpected layout)."
            )
        # the scrcpy folder bundles its own adb.exe, which may be running as
        # the server — stop it so the swap isn't blocked by a file lock
        shared = _kill_adb_server() if existing else []
        try:
            _swap_dir(staging, scrcpy_dir())
        except Exception:
            _restore_shared_server(shared)
            raise
    clear_tools_cache()  # as for adb: the copy just installed is the one to find
    scr = managed_scrcpy()
    if not scr:
        _restore_shared_server(shared)
        raise ADBNotFoundError(
            "scrcpy downloaded but scrcpy.exe was not found after install (unexpected layout)."
        )
    _sync_scrcpy_adb()
    _restore_shared_server(shared)
    return scr


def _stamp_path() -> str:
    return os.path.join(tools_dir(), ".stamp")


def _read_stamp() -> str:
    """The TurboADB version whose tools were last ensured (see
    :func:`ensure_tools`, which skips its work when this matches)."""
    try:
        with open(_stamp_path(), encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def _write_stamp(version: str) -> None:
    try:
        _ensure_tools_dir()
        with open(_stamp_path(), "w", encoding="utf-8") as fh:
            fh.write(version)
    except Exception:
        pass


def _pkg_version() -> str:
    from .update import current_version

    return current_version()


def auto_fetch_enabled() -> bool:
    """Auto-download is ON by default; disable with TURBOADB_AUTO_FETCH=0
    (handy for offline / CI / locked-down environments)."""
    v = os.environ.get("TURBOADB_AUTO_FETCH")
    if v is None:
        return True
    return v.strip().lower() not in ("0", "false", "no", "off", "")


# The tools ensure_tools() has seen to in this process ("adb", "scrcpy"): a
# CLI command ensures adb alone, the GUI both, and one must not stand in for
# the other (a CLI run in the GUI's process kept the GUI from fetching scrcpy).
_ensured = set()
_ENSURE_LOCK = threading.Lock()


def ensure_tools(*, on_progress=None, notify=None, scrcpy: bool = True) -> dict:
    """Make sure the managed cache has adb and scrcpy, each where a prebuilt
    exists for this machine. Checks each tool at most once per process and
    downloads ONLY tools that are missing from the cache — it never upgrades
    an existing copy, because replacing platform-tools means stopping the
    running adb server (which drops every device session). Upgrades go
    through :func:`upgrade_tools`.

    Best-effort and never raises — if the network is down, detection simply
    falls back to whatever's already on PATH. Thread-safe: concurrent callers
    wait for the first one instead of racing the same download.

    A ``.stamp`` naming the TurboADB version whose tools were last ensured short-
    circuits the whole function when everything it would check is already in the
    cache — that is what :func:`_write_stamp` (also written by the GUI's startup
    tool check) exists for.

    Disable entirely with the ``TURBOADB_AUTO_FETCH=0`` environment variable.
    """
    def norm(note, errors=None):
        return {
            "note": note,
            "adb": managed_adb(),
            "scrcpy": managed_scrcpy(),
            "errors": errors or {},
        }

    with _ENSURE_LOCK:
        wanted = {"adb", "scrcpy"} if scrcpy else {"adb"}
        if wanted <= _ensured:
            return norm("already-ensured")
        check_adb = "adb" not in _ensured
        _ensured.update(wanted)
        if not auto_fetch_enabled():
            return norm("disabled")
        # The stamp only short-circuits when the tools it covers are still
        # present, so deleting one from the cache still re-fetches it.
        stamp_covers_scrcpy = bool(scrcpy) and scrcpy_download_supported()
        if (
            _read_stamp() == _pkg_version()
            and managed_adb()
            and (managed_scrcpy() if stamp_covers_scrcpy else True)
        ):
            return norm("stamped")
        errors = {}
        note = "up-to-date"
        try:
            # adb that an earlier call in this process saw to is not fetched
            # again, and none is where Google builds none for this machine
            want_adb = check_adb and adb_download_supported() and managed_adb() is None
            # Requesting scrcpy where no prebuilt exists always "failed", so the
            # stamp was never written and every command retried the network.
            want_scrcpy = bool(scrcpy) and scrcpy_download_supported() and managed_scrcpy() is None
            if want_adb or want_scrcpy:
                note = "installed"
                if notify:
                    what = " + ".join(
                        n for n, w in (("platform-tools", want_adb), ("scrcpy", want_scrcpy)) if w
                    )
                    notify(
                        f"Installing {what} (one-time; set TURBOADB_AUTO_FETCH=0 to skip)…"
                    )
                res = fetch_tools(
                    adb=want_adb, scrcpy=want_scrcpy, force=False, on_progress=on_progress
                )
                errors = dict(res.get("errors", {}))
            # only mark this TurboADB version when nothing FAILED
            if managed_adb() and not errors:
                _write_stamp(_pkg_version())
            _sync_scrcpy_adb()
        except Exception as exc:  # never let auto-fetch break a real command
            errors["ensure"] = str(exc)
        return norm(note, errors)


def _sync_scrcpy_adb() -> None:
    """Ensure scrcpy's bundled adb on Windows matches platform-tools adb to eliminate
    daemon killing conflicts. Called after every managed download, not only from
    :func:`ensure_tools`."""
    if os.name != "nt":
        return
    m_adb = managed_adb()
    s_dir = scrcpy_dir()
    if not m_adb or not os.path.isdir(s_dir):
        return
    pt_dir = os.path.dirname(m_adb)
    for name in ADB_WINDOWS_FILES:
        src = os.path.join(pt_dir, name)
        dst = os.path.join(s_dir, name)
        if os.path.exists(src):
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# version checks ("only download if there's a newer version")
# --------------------------------------------------------------------------- #
def installed_adb_version(adb_path: str | None = None) -> str | None:
    try:
        exe = adb_path or find_adb()
        m = re.search(r"Version\s+(\d+\.\d+\.\d+)", _adb_version_output(exe))
        return m.group(1) if m else None
    except Exception:
        return None


def _parse_platform_tools_manifest(xml: str, host_os: str) -> dict:
    """Pull everything we need out of Google's ``repository2-3.xml`` in ONE pass:
    ``{"version", "url", "size", "checksum", "checksum_type"}`` (keys absent when
    the manifest doesn't publish them) for the platform-tools archive matching
    *host_os*. Returns ``{}`` when the entry can't be found at all."""
    out: dict = {}
    pkg = re.search(
        r"<[\w:]*remotePackage[^>]*path=\"platform-tools\".*?</[\w:]*remotePackage>", xml, re.S
    )
    # Fall back to the old, looser revision-only match if the schema moves on:
    # losing the checksum is bad, losing the version check entirely is worse.
    block = pkg.group(0) if pkg else ""
    if not block:
        loose = re.search(r'path="platform-tools".*?</revision>', xml, re.S)
        block_for_rev = loose.group(0) if loose else ""
    else:
        block_for_rev = block
    rev = re.search(
        r"<major>(\d+)</major>\s*<minor>(\d+)</minor>\s*<micro>(\d+)</micro>", block_for_rev
    )
    if rev:
        out["version"] = ".".join(rev.groups())
    if not block:
        return out
    archives = re.findall(r"<[\w:]*archive>.*?</[\w:]*archive>", block, re.S)
    chosen = None
    for archive in archives:
        m = re.search(r"<[\w:]*host-os>([\w-]+)</[\w:]*host-os>", archive)
        if m and m.group(1).strip().lower() == host_os:
            chosen = archive
            break
    if chosen is None and len(archives) == 1:
        chosen = archives[0]  # a single, OS-independent archive
    if chosen is None:
        return out
    url = re.search(r"<[\w:]*url>([^<]+)</[\w:]*url>", chosen)
    size = re.search(r"<[\w:]*size>(\d+)</[\w:]*size>", chosen)
    checksum = re.search(r"<[\w:]*checksum(?:\s+type=\"([\w-]+)\")?\s*>([0-9a-fA-F]+)<", chosen)
    if url:
        href = url.group(1).strip()
        out["url"] = href if "://" in href else PLATFORM_TOOLS_BASE + href.lstrip("/")
    if size:
        out["size"] = int(size.group(1))
    if checksum:
        out["checksum_type"] = (checksum.group(1) or "sha1").lower()
        out["checksum"] = checksum.group(2).lower()
    return out


def latest_adb_archive() -> dict:
    """Google's published platform-tools archive for THIS OS: ``{"version",
    "url", "size", "checksum", "checksum_type"}``, or ``{}`` when the manifest
    is unreachable. The same fetch answers both "is there a newer adb?" and
    "what should the downloaded zip hash to?"."""
    try:
        req = urllib.request.Request(PLATFORM_TOOLS_REPO_XML, headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml = resp.read().decode("utf-8", "replace")
    except Exception:
        return {}
    try:
        return _parse_platform_tools_manifest(xml, _MANIFEST_HOST_OS[_os_key()])
    except Exception:
        return {}


# The archive the last version check read, with the monotonic time it was read:
# the download that follows the check uses it instead of fetching Google's
# manifest a second time.
_CHECKED_ARCHIVE = None
_CHECKED_ARCHIVE_TTL = 300.0


def latest_adb_version() -> str | None:
    global _CHECKED_ARCHIVE
    archive = latest_adb_archive()
    if archive.get("url"):
        _CHECKED_ARCHIVE = (time.monotonic(), archive)
    return archive.get("version")


def _archive_to_download() -> dict:
    """The platform-tools archive to download: the one a version check read in
    the last few minutes (taken once), else Google's manifest read now."""
    global _CHECKED_ARCHIVE
    checked, _CHECKED_ARCHIVE = _CHECKED_ARCHIVE, None
    if checked and time.monotonic() - checked[0] < _CHECKED_ARCHIVE_TTL:
        return checked[1]
    return latest_adb_archive()


def installed_scrcpy_version(scrcpy_path: str | None = None) -> str | None:
    try:
        exe = scrcpy_path or find_scrcpy()
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW
        )
        m = re.search(r"scrcpy\s+(\d+\.\d+(?:\.\d+)?)", out.stdout or out.stderr or "")
        return m.group(1) if m else None
    except Exception:
        return None


def latest_scrcpy_version() -> str | None:
    """The newest published scrcpy version, or None when it could NOT be
    determined (offline, GitHub rate limit). A stale cached release body is not
    an answer — reporting it as one turned a failed check into "up to date"."""
    try:
        data, authoritative = _github_release()  # ETag-cached, rate-limit friendly
    except Exception:
        return None
    if not authoritative:
        return None
    tag = (data.get("tag_name") or "").lstrip("vV")
    return tag or None


_vtuple = parse_version


def _decide(installed: str | None, latest: str | None) -> bool | None:
    """True = upgrade available; False = up to date; None = couldn't determine.
    Compares numerically so a formatting difference (or an installed version
    NEWER than the published one) never triggers a pointless re-download."""
    if installed is None:
        return True  # not installed -> "upgrade" means install
    if latest is None:
        return None  # can't reach the version source
    return _vtuple(latest) > _vtuple(installed)


def check_updates() -> dict:
    """Compare installed adb/scrcpy against the latest available. Returns
    ``{"adb": {"installed","latest","upgrade","path","system","supported"},
    "scrcpy": {...}}`` where ``upgrade`` is True / False / None (unknown) and
    ``supported`` says whether TurboADB can download that tool on this OS.
    Makes no changes.

    ``latest`` comes from where the copy came from.  For one winget installed it
    is the newest winget can deliver, so nothing is offered that it does not
    have yet: a publisher's release reaches winget some days later.  For one
    from the publisher it is the publisher's: winget can list a version the
    publisher has withdrawn, and the update it offered would never arrive.

    Only the MANAGED copy (the one an install replaces, and the one TurboADB
    prefers) is version-checked. ``installed`` is None when there is no managed
    copy — so the decision is "install", not "up to date". Passing the missing
    path straight to ``installed_*_version`` made it fall back to the PATH tool,
    which reported everything current while the managed cache stayed EMPTY and
    ``gui_adb_path()`` kept returning None forever. The PATH/system version is
    reported separately as ``system``."""
    a_path, s_path = managed_adb(), managed_scrcpy()
    ai = installed_adb_version(a_path) if a_path else None
    si = installed_scrcpy_version(s_path) if s_path else None
    al = (winget_copy("adb") and winget_latest("adb")) or latest_adb_version()
    sl = (winget_copy("scrcpy") and winget_latest("scrcpy")) or latest_scrcpy_version()
    return {
        "adb": {
            "installed": ai,
            "latest": al,
            "upgrade": _decide(ai, al),
            "path": a_path,
            # what a plain `adb` on this machine resolves to, for context only
            "system": None if a_path else installed_adb_version(),
            "supported": adb_download_supported(),
        },
        "scrcpy": {
            "installed": si,
            "latest": sl,
            "upgrade": _decide(si, sl),
            "path": s_path,
            "system": None if s_path else installed_scrcpy_version(),
            "supported": scrcpy_download_supported(),
        },
    }


def upgrade_tools(*, on_progress=None, notify=None, checks: dict | None = None) -> dict:
    """Check for newer adb/scrcpy and download **only** the ones that are
    outdated (or missing). If everything's current, downloads nothing. Returns a
    summary dict including the version check and what was updated.

    Pass *checks* (a :func:`check_updates` result) to reuse a check that was
    already made instead of querying the version sources twice. A tool is only
    considered where a prebuilt download exists for this machine (scrcpy on
    Windows; adb see :func:`adb_download_supported`)."""
    if checks is None:
        checks = check_updates()
    updated = {}
    errors = {}
    tools = [
        tool
        for tool, downloadable in (("adb", adb_download_supported()),
                                   ("scrcpy", scrcpy_download_supported()))
        if downloadable
    ]
    want_adb = "adb" in tools and checks["adb"]["upgrade"] is True
    want_scrcpy = "scrcpy" in tools and checks["scrcpy"]["upgrade"] is True
    unknown = [t for t in tools if checks[t]["upgrade"] is None]
    if not want_adb and not want_scrcpy:
        # 'unknown' (couldn't reach the version source / GitHub rate limit) is
        # NOT the same as up to date — reporting it as such hid failed checks
        if unknown and notify:
            notify(
                "couldn't determine the latest "
                + "/".join(unknown)
                + " version (network / rate limit) — nothing was changed; "
                "try again in a while"
            )
        return {
            "checks": checks,
            "updated": {},
            "errors": {},
            "up_to_date": not unknown,
            "unknown": unknown,
        }
    if notify:
        bits = []
        if want_adb:
            bits.append(f"adb {checks['adb']['installed']}→{checks['adb']['latest']}")
        if want_scrcpy:
            bits.append(f"scrcpy {checks['scrcpy']['installed']}→{checks['scrcpy']['latest']}")
        notify("Updating " + ", ".join(bits) + " …")
    with _one_restart():
        if want_adb:
            try:
                updated["adb"] = download_platform_tools(force=True, on_progress=on_progress)
            except Exception as exc:
                errors["adb"] = str(exc)
        if want_scrcpy:
            try:
                updated["scrcpy"] = download_scrcpy(force=True, on_progress=on_progress)
            except Exception as exc:
                errors["scrcpy"] = str(exc)
    _take_share_problem(errors)
    return {
        "checks": checks,
        "updated": updated,
        "errors": errors,
        "up_to_date": False,
        "unknown": unknown,
    }


def fetch_tools(
    *, adb: bool = True, scrcpy: bool = True, force: bool = False, on_progress=None, on_stage=None
) -> dict:
    """Install whatever's requested, through winget where there is one, else
    into the cache. Returns
    ``{"adb": path|None, "scrcpy": path|None, "errors": {...}}``.

    *on_stage* (optional) is called with a short label ("adb (1/2)", …) as each
    tool starts, so a progress UI can say WHAT is installing instead of its
    bar appearing to jump backwards between the two 0-100 runs.

    scrcpy's Windows release has its own adb; installing scrcpy alone is enough
    to get both, but platform-tools gives you the latest standalone adb.
    """

    def stage(label):
        if on_stage:
            try:
                on_stage(label)
            except Exception:
                pass

    total = int(adb) + int(scrcpy)
    result = {"adb": None, "scrcpy": None, "errors": {}}
    with _one_restart():
        if adb:
            stage(f"platform-tools / adb (1/{total})")
            try:
                result["adb"] = download_platform_tools(force=force, on_progress=on_progress)
            except Exception as exc:
                result["errors"]["adb"] = str(exc)
        if scrcpy:
            stage(f"scrcpy ({total}/{total})")
            try:
                result["scrcpy"] = download_scrcpy(force=force, on_progress=on_progress)
            except Exception as exc:
                result["errors"]["scrcpy"] = str(exc)
    _take_share_problem(result["errors"])
    return result
