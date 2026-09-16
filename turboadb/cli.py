# PYTHON_ARGCOMPLETE_OK
"""
TurboADB command-line interface (fully argument-driven).

    turboadb devices
    turboadb info        [-s SERIAL]
    turboadb shell       [-s SERIAL] -- getprop ro.build.version.release
    turboadb logcat      [-s SERIAL] --tag ActivityManager --match "ANR|FATAL" --save boot.log
    turboadb push        [-s SERIAL] local.apk /data/local/tmp/app.apk
    turboadb pull        [-s SERIAL] /sdcard/log.txt log.txt
    turboadb install     [-s SERIAL] app.apk --grant
    turboadb uninstall   [-s SERIAL] com.example.app
    turboadb packages    [-s SERIAL] --third-party
    turboadb screenshot  [-s SERIAL] shot.png
    turboadb record      [-s SERIAL] clip.mp4 --time-limit 20
    turboadb forward     [-s SERIAL] tcp:9222 localabstract:chrome_devtools_remote
    turboadb reverse     [-s SERIAL] tcp:8000 tcp:8000
    turboadb scrcpy      [-s SERIAL] --max-size 1280 --bit-rate 8M
    turboadb connect     192.168.1.50:5555
    turboadb devices     --adb-host 192.168.1.20    # devices on ANOTHER PC's adb server
    turboadb tcpip       [-s SERIAL] 5555
    turboadb pair        192.168.1.50:37123 482913
    turboadb reboot      [-s SERIAL] [recovery|bootloader|sideload]
    turboadb doctor

Network targets: pass ``-s host:port`` (or use ``connect`` first). USB: omit
``-s`` for the only device, or pass its serial. The shared flags (``-s``,
``--adb-path``, ``--adb-host``, ``--adb-port``, ``--timeout``, ``--json``) work
before or after the subcommand.
"""

from __future__ import annotations

import os
import sys
import json
import logging
import argparse
import subprocess

from .config import ADBConfig, ScrcpyOptions, parse_host_port
from .core import ADBHandler
from .devices import list_devices
from .results import CommandResult, TransferResult, StreamResult
from .exceptions import ADBError, ADBNotFoundError
from .tools import NO_WINDOW, adb_available, scrcpy_available, windowless_python

DOCS_URL = "https://pypi.org/project/turboadb/"


# --------------------------------------------------------------------------- #
# console-script helpers (entry points)
# --------------------------------------------------------------------------- #
def _here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _icon_path() -> str:
    return os.path.join(_here(), "assets", "icon.ico")


def _gui_exe_path() -> str:
    """The Windows GUI executable bundled in the wheel by scripts/release.py."""
    return os.path.join(_here(), "bin", "turboadb-gui.exe")


def _prewarm_gui_adb_server() -> None:
    """Start the GUI's managed ADB daemon before importing PyQt.

    The Windows daemon's first USB scan is the only expensive part of a cold
    launch.  Running it on a daemon thread here overlaps that scan with PyQt
    import and window construction.  ``gui.app`` performs the same guarded
    warm-up for alternate entry points; :func:`ensure_adb_server` serializes
    both callers so only one identical ADB client ever starts the daemon.
    """
    try:
        import threading

        from .gui.adb_path import gui_adb_path  # dependency-free (no PyQt import)
        from .tools import ensure_adb_server

        adb_path = gui_adb_path()
        if not adb_path:
            return
        threading.Thread(
            target=ensure_adb_server,
            args=(adb_path,),
            kwargs={"timeout": 12.0},
            name="TurboADB-launch-server-start",
            daemon=True,
        ).start()
    except Exception:
        # The GUI shows an actionable tool/server error after it has a window.
        # This early start is strictly a launch-latency optimisation.
        pass


def open_docs(argv=None) -> int:
    """`turboadb-docs`: open the rendered docs, falling back to the bundled README."""
    import webbrowser
    from pathlib import Path

    try:
        if webbrowser.open(DOCS_URL):
            print(f"Opened docs: {DOCS_URL}")
            return 0
    except Exception:
        pass
    readme = os.path.join(_here(), "README.md")
    if os.path.exists(readme):
        # a proper file URI (drive letter, backslashes, spaces encoded)
        webbrowser.open(Path(readme).resolve().as_uri())
        print(f"Opened bundled README: {readme}")
    else:
        print(f"Docs: {DOCS_URL}")
    return 0


def _resolve_gui_target():
    """The best way to launch the GUI from a shortcut, always from the CURRENT
    environment: the frozen exe itself, the windowless ``turboadb-gui`` launcher
    installed next to this interpreter, else ``pythonw -m turboadb gui``. (A
    ``turboadb-gui`` merely found on PATH may belong to another environment.)"""
    if getattr(sys, "frozen", False):
        # running the bundled one-file exe: the exe the user launched is the
        # stable launcher (NOT the ephemeral _MEIPASS copy)
        return sys.executable, "", os.path.dirname(sys.executable)
    pydir = os.path.dirname(sys.executable)
    for base in (os.path.join(pydir, "Scripts"), pydir):
        cand = os.path.join(base, "turboadb-gui.exe")
        if os.path.exists(cand):
            return cand, "", base
    exe = windowless_python()
    return exe, "-m turboadb gui", os.path.dirname(exe)


def _write_shortcut(folder_expr: str, name: str) -> bool:
    """Create ``<folder>/<name>.lnk`` pointing at the GUI, with our icon. The
    *folder_expr* is a PowerShell expression yielding the target directory (e.g.
    ``[Environment]::GetFolderPath('Desktop')``)."""
    if os.name != "nt":
        return False
    target, args, workdir = _resolve_gui_target()
    icon = _icon_path()

    def q(s):
        return str(s).replace("'", "''")  # for PS single-quoted strings —

    # a user name / path containing an apostrophe broke the whole script
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$dir = {folder_expr}; "
        "if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }; "
        f"$lnk = $ws.CreateShortcut([IO.Path]::Combine($dir, '{q(name)}.lnk')); "
        f"$lnk.TargetPath = '{q(target)}'; "
        + (f"$lnk.Arguments = '{q(args)}'; " if args else "")
        + f"$lnk.WorkingDirectory = '{q(workdir)}'; "
        + (f"$lnk.IconLocation = '{q(icon)}'; " if os.path.exists(icon) else "")
        + "$lnk.Description = 'TurboADB - Android ADB + scrcpy toolkit'; "
        "$lnk.Save()"
    )
    try:
        # CREATE_NO_WINDOW: never flash a console window (this runs at every
        # launch, so without it two PowerShell consoles blink on screen)
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ],
            check=True,
            capture_output=True,
            timeout=30,
            creationflags=NO_WINDOW,
        )
        return True
    except Exception:
        return False


def create_desktop_shortcut(name: str = "TurboADB") -> bool:
    """Create/refresh a Desktop shortcut to the GUI (Windows only)."""
    return _write_shortcut("[Environment]::GetFolderPath('Desktop')", name)


def create_start_menu_shortcut(name: str = "TurboADB") -> bool:
    """Create/refresh a Start-menu (Programs) shortcut so the GUI shows up in the
    Start menu and Windows search."""
    return _write_shortcut("[Environment]::GetFolderPath('Programs')", name)


def _windows_folder(csidl: int) -> str:
    """Resolve a Windows known folder by CSIDL (handles OneDrive-redirected
    Desktops), matching .NET's GetFolderPath so our existence-checks line up with
    where the shortcuts actually get written."""
    import ctypes

    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return buf.value


def _shortcut_paths(name: str = "TurboADB"):
    """The Desktop and Start-menu .lnk paths we manage (Windows only)."""
    if os.name != "nt":
        return []
    try:
        desktop = _windows_folder(0x10)  # CSIDL_DESKTOPDIRECTORY
    except Exception:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    try:
        programs = _windows_folder(0x02)  # CSIDL_PROGRAMS
    except Exception:
        appdata = os.environ.get("APPDATA", "")
        programs = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")
    return [
        ("desktop", os.path.join(desktop, f"{name}.lnk"), create_desktop_shortcut),
        ("start menu", os.path.join(programs, f"{name}.lnk"), create_start_menu_shortcut),
    ]


def ensure_shortcuts(name: str = "TurboADB", force: bool = False) -> dict:
    """Make sure the Desktop and Start-menu shortcuts exist. With *force* (used at
    every GUI launch) it re-creates them so they always point at the right
    launcher and reappear even if an antivirus or cleanup tool removed one; it
    only REPORTS a location when it was actually missing before (so a normal
    launch logs nothing). Returns {location: ok_bool} for created/repaired or
    failed locations only."""
    out = {}
    for loc, path, maker in _shortcut_paths(name):
        existed = os.path.exists(path)
        if not existed or force:
            maker(name)
            landed = os.path.exists(path)
            if not existed:
                out[loc] = landed  # newly created (True) or failed (False)
            elif not landed:
                out[loc] = False  # was present but the refresh lost it
    return out


def create_shortcut(argv=None) -> int:
    """`turboadb-shortcut`: (re)create the Desktop + Start-menu shortcuts."""
    if os.name != "nt":
        print("Shortcut creation is Windows-only.", file=sys.stderr)
        return 2
    res = ensure_shortcuts(force=True)
    # ensure_shortcuts only reports locations that were missing (or lost), so an
    # empty result after refreshing existing shortcuts is success, not failure
    present = [loc for loc, path, _maker in _shortcut_paths() if os.path.exists(path)]
    failed = [loc for loc, ok in res.items() if not ok]
    if present and not failed:
        print(f"Created/refreshed the 'TurboADB' shortcut ({' and '.join(present)}).")
        return 0
    if present:
        print(
            f"Shortcut ready in {' and '.join(present)}, but could not create it in "
            f"{' and '.join(failed)}.",
            file=sys.stderr,
        )
        return 1
    print("Could not create the shortcut.", file=sys.stderr)
    return 1


def _staged_exe(src: str) -> str:
    """Return a temp COPY of the bundled exe to actually run.

    A running .exe is locked by Windows, so running the installed one in place
    would make ``pip install --upgrade`` fail to replace it. The temp name
    includes the version and size, so a new build lands in a new file instead
    of clashing with a copy that may still be running."""
    import shutil
    import tempfile

    try:
        from . import __version__ as ver
    except Exception:
        ver = "x"
    try:
        size = os.path.getsize(src)
        dst = os.path.join(tempfile.gettempdir(), f"turboadb-gui-{ver}-{size}.exe")
        if not os.path.exists(dst) or os.path.getsize(dst) != size:
            shutil.copy2(src, dst)
        return dst
    except Exception:
        return src  # fall back to running in place


def launch_gui(argv=None) -> int:
    """`turboadb-gui`: launch the PyQt5 app.

    Runs the GUI from the installed Python package when PyQt5 is available, so
    ``pip install --upgrade turboadb`` always takes effect on the next launch.
    Without PyQt5 (e.g. a Python with no PyQt5 wheel) it starts the Windows
    executable bundled in the wheel, from a temp copy so upgrades never block."""
    try:
        import PyQt5  # noqa: F401
    except ImportError:
        exe = _gui_exe_path()
        if os.name == "nt" and os.path.isfile(exe):
            args = list(argv) if argv is not None else sys.argv[1:]
            return subprocess.call([_staged_exe(exe)] + args)
        print(
            "The GUI needs PyQt5 (or, on Windows, the executable bundled in the wheel). "
            'Install it with:  pip install "turboadb[gui]".',
            file=sys.stderr,
        )
        return 1
    _prewarm_gui_adb_server()
    # Any other ImportError inside the GUI is a real bug: let it surface with
    # its traceback instead of being mislabelled as "PyQt5 missing".
    from .gui.app import main as gui_main

    return gui_main()


# --------------------------------------------------------------------------- #
# argument plumbing
# --------------------------------------------------------------------------- #
def _add_target(p: argparse.ArgumentParser, suppress: bool = True) -> None:
    """Attach the shared target/connection flags.

    These flags are accepted both *before* the subcommand (e.g.
    ``turboadb -s SERIAL info``, the form used throughout the README) and
    *after* it (e.g. ``turboadb info -s SERIAL``). To make both orders work the
    flags live on the top-level parser AND on every subparser:

    * The top-level parser is added with ``suppress=False`` so it carries the
      real default values.
    * Each subparser is added with ``suppress=True`` (the default) so its copies
      use ``argparse.SUPPRESS`` defaults — present only when the user actually
      passes the flag after the subcommand, and so never clobbering a value
      already parsed at the top level. (argparse copies a subparser's parsed
      namespace, including its defaults, over the outer one.)

    Every subcommand must use this helper rather than declaring its own
    ``--adb-path``/``--adb-host``/``--json``: a subparser copy with a real
    default silently overwrote the value given before the subcommand.
    """
    _none = argparse.SUPPRESS if suppress else None
    _port = argparse.SUPPRESS if suppress else 5037
    _json = argparse.SUPPRESS if suppress else False
    p.add_argument(
        "-s", "--serial", default=_none, help="device serial, or host:port for a network device"
    )
    p.add_argument("--adb-path", default=_none, help="path to the adb executable")
    p.add_argument("--scrcpy-path", default=_none, help="path to the scrcpy executable")
    p.add_argument(
        "--adb-host",
        default=_none,
        help="remote adb server host — drive a device plugged into "
        "another machine (that machine: adb -a nodaemon server start)",
    )
    p.add_argument(
        "--adb-port", type=int, default=_port, help="adb server port (default 5037)"
    )
    p.add_argument("--timeout", type=float, default=_none, help="per-command timeout (seconds)")
    p.add_argument("--json", action="store_true", default=_json, help="emit machine-readable JSON")


def _config(args, **overrides) -> ADBConfig:
    """ADBConfig from the shared flags (every subcommand honours --adb-host).
    ``-s @NAME`` uses a saved target (see ``turboadb targets``)."""
    serial = getattr(args, "serial", None)
    kwargs = dict(
        serial=serial,
        adb_path=getattr(args, "adb_path", None),
        scrcpy_path=getattr(args, "scrcpy_path", None),
        adb_server_host=getattr(args, "adb_host", None),
        adb_server_port=getattr(args, "adb_port", 5037),
    )
    if isinstance(serial, str) and serial.startswith("@"):
        kwargs.update(_saved_target(serial[1:]))
    timeout = getattr(args, "timeout", None)
    if timeout is not None:
        # only when given: passing None would DISABLE the default timeouts.
        # --timeout covers push/pull too, not just short commands.
        kwargs["command_timeout"] = timeout
        kwargs["transfer_timeout"] = timeout
    kwargs.update(overrides)
    return ADBConfig(**kwargs)


def _saved_target(name: str) -> dict:
    """ADBConfig fields for the saved target *name* (``turboadb targets``)."""
    from .gui.sessions import SessionStore

    target = SessionStore().get(name)
    if target is None:
        raise ValueError(f"no saved target named {name!r} (see: turboadb targets list)")
    if target["type"] == "network":
        return {"serial": None, "host": target["host"], "port": target["port"]}
    if target["type"] == "remote":
        return {
            "serial": target.get("serial") or None,
            "adb_server_host": target["adb_host"],
            "adb_server_port": target["adb_port"],
        }
    return {"serial": target.get("serial") or None}


def _handler(args, **overrides) -> ADBHandler:
    return ADBHandler(_config(args, **overrides))


def _output(args, obj) -> None:
    if getattr(args, "json", False):
        if isinstance(obj, (CommandResult, TransferResult)):
            print(json.dumps(obj.as_dict(), default=str, indent=2))
        elif isinstance(obj, StreamResult):
            print(
                json.dumps(
                    {"lines": obj.lines, "matches": obj.matches, "saved_to": obj.saved_to},
                    default=str,
                    indent=2,
                )
            )
        else:
            print(json.dumps(obj, default=str, indent=2))
    else:
        print(obj)


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="turboadb", description="Android ADB + scrcpy device toolkit (automotive & general)."
    )
    parser.add_argument("-V", "--version", action="version", version=f"turboadb {__version__}")
    # Accept the shared target flags before the subcommand too, so the
    # documented `turboadb -s SERIAL <cmd>` order works. This holds the real
    # defaults; the per-subcommand copies (added via _add_target) use SUPPRESS
    # so they never overwrite a value parsed here.
    _add_target(parser, suppress=False)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def device_cmd(name, help_text):
        p = sub.add_parser(name, help=help_text)
        _add_target(p)
        return p

    def display_opt(p):
        p.add_argument(
            "--display", type=int, default=None, metavar="N",
            help="display id on multi-display devices (see: turboadb displays)",
        )

    def on_off(name, help_text):
        p = device_cmd(name, help_text)
        p.add_argument("state", choices=["on", "off"])
        return p

    # --- setup and tools ---
    sub.add_parser("doctor", help="report whether adb/scrcpy were found")

    p_fetch = sub.add_parser("fetch-tools", help="download adb (+scrcpy) into ~/.turboadb/tools")
    p_fetch.add_argument("--adb-only", action="store_true")
    p_fetch.add_argument("--scrcpy-only", action="store_true")
    p_fetch.add_argument("--force", action="store_true", help="re-download even if already cached")

    p_upg = sub.add_parser(
        "upgrade-tools", help="check for newer adb/scrcpy and update only if newer"
    )
    p_upg.add_argument("--check", action="store_true", help="only report versions; don't download")

    p_self = sub.add_parser(
        "self-update", help="upgrade TurboADB itself (pip) + adb/scrcpy to the latest"
    )
    p_self.add_argument(
        "--check", action="store_true", help="only report whether a newer TurboADB exists"
    )
    sub.add_parser("shortcut", help="create Desktop + Start-menu shortcuts to the GUI")
    sub.add_parser("gui", help="launch the desktop GUI")

    # --- devices and connection ---
    device_cmd("devices", "list devices on the local (or a remote) adb server")
    device_cmd("info", "connect and print device identity/build")
    p_conn = device_cmd("connect", "adb connect host:port")
    p_conn.add_argument("hostport")
    p_disc = device_cmd("disconnect", "adb disconnect host:port")
    p_disc.add_argument("hostport", nargs="?", default=None)
    p_pair = device_cmd("pair", "pair with an Android 11+ device")
    p_pair.add_argument("hostport", help="host:pairing_port")
    p_pair.add_argument("code", help="6-digit pairing code")
    p_tcp = device_cmd("tcpip", "restart adbd in TCP mode on a USB device")
    p_tcp.add_argument("port", type=int, nargs="?", default=5555)
    p_wless = device_cmd(
        "wireless",
        "one-shot USB -> Wi-Fi: read the device's IP, adb tcpip, then "
        "connect (afterwards the cable can be unplugged)",
    )
    p_wless.add_argument("port", type=int, nargs="?", default=5555)
    p_discover = device_cmd(
        "discover", "find Android 11+ Wireless-debugging devices on the LAN (adb mdns services)"
    )
    p_discover.add_argument(
        "--connect", action="store_true", help="also adb connect every device that is ready"
    )
    device_cmd(
        "restart-server",
        "kill + start the adb server (fixes 'device not visible' from adb version mismatches)",
    )
    p_wait = device_cmd("wait", "wait until the device is online")
    p_wait.add_argument(
        "seconds", type=float, nargs="?", default=None, help="give up after this many seconds"
    )
    device_cmd("state", "the device's adb state (device, offline, unauthorized, recovery, …)")
    device_cmd("serialno", "the device's serial number")
    device_cmd("ip", "the device's Wi-Fi / Ethernet IP address")
    p_tg = device_cmd("targets", "saved device targets: list, add, remove, export, import")
    targets = p_tg.add_subparsers(dest="targets_cmd", required=True)
    t_list = targets.add_parser("list", help="list the saved targets")
    t_list.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    t_add = targets.add_parser(
        "add", help="save a target: usb [SERIAL] | network HOST[:PORT] | remote ADBHOST[:PORT] [SERIAL]"
    )
    t_add.add_argument("name")
    t_add.add_argument("kind", choices=["usb", "network", "remote"])
    t_add.add_argument(
        "address", nargs="?", default=None,
        help="usb: the serial; network: HOST[:PORT]; remote: the adb server HOST[:PORT]",
    )
    t_add.add_argument(
        "remote_serial", nargs="?", default=None, metavar="SERIAL",
        help="remote: the device's serial on that server",
    )
    t_rm = targets.add_parser("remove", help="delete a saved target")
    t_rm.add_argument("name")
    t_exp = targets.add_parser("export", help="write every saved target to a JSON file")
    t_exp.add_argument("file")
    t_imp = targets.add_parser("import", help="merge saved targets from a JSON file")
    t_imp.add_argument("file")

    # --- shell, logs and reports ---
    p_shell = device_cmd(
        "shell", "run a device shell command; with no command, open an interactive shell"
    )
    p_shell.add_argument("--su", action="store_true", help="wrap in su -c (rooted)")
    p_shell.add_argument("--all", action="store_true", help="run the command on every online device")
    p_shell.add_argument(
        "--batch", metavar="FILE", default=None,
        help="run each line of FILE as a command, in order (# lines are skipped)",
    )
    p_shell.add_argument(
        "--keep-going", action="store_true", help="with --batch: continue after a failing command"
    )
    p_shell.add_argument("command", nargs=argparse.REMAINDER)
    p_adb = device_cmd("adb", "run any adb command for this device: turboadb -s S adb -- ARGS")
    p_adb.add_argument("adb_args", nargs=argparse.REMAINDER, metavar="ARGS")

    p_log = device_cmd("logcat", "stream logcat live, with filters, match + save")
    p_log.add_argument("--tag", default=None)
    p_log.add_argument("--priority", default=None, help="V/D/I/W/E/F")
    p_log.add_argument(
        "--filter", action="append", default=None, metavar="TAG:LEVEL",
        help="logcat filter spec, repeatable (e.g. ActivityManager:I); overrides --tag/--priority",
    )
    p_log.add_argument(
        "--buffer", action="append", default=None,
        help="logcat buffer (repeatable): main/system/crash/...",
    )
    p_log.add_argument("--format", default="threadtime", help="logcat -v format")
    p_log.add_argument("--match", default=None, help="regex to flag (count) matching lines")
    p_log.add_argument(
        "--grep", default=None, metavar="REGEX",
        help="print only lines matching REGEX (case-insensitive)",
    )
    p_log.add_argument(
        "--crashes", action="store_true",
        help="crashes and ANRs: the crash/main/system buffers at Error level",
    )
    p_log.add_argument("--save", default=None, help="tee output to this file")
    p_log.add_argument("--stop-on-match", action="store_true")
    p_log.add_argument("--clear", action="store_true", help="logcat -c first")
    p_log.add_argument("--dump", action="store_true", help="-d: dump then exit")
    p_log.add_argument(
        "--tail", type=int, default=None, metavar="N",
        help="only the last N lines: with --dump the last N buffered lines (-t N); "
        "otherwise start the live stream there (-T N), skipping the cached backlog",
    )
    device_cmd("logcat-clear", "clear logcat buffers (-c)")
    p_bug = device_cmd(
        "bugreport", "capture a full adb bugreport (slow — a few minutes; --timeout sets the limit)"
    )
    p_bug.add_argument(
        "path", nargs="?", default=None, help="output file (default: bugreport-<time>.zip)"
    )

    # --- files ---
    p_push = device_cmd("push", "upload a file/dir to the device")
    p_push.add_argument("local")
    p_push.add_argument("remote")
    p_pull = device_cmd("pull", "download a file/dir from the device")
    p_pull.add_argument("remote")
    p_pull.add_argument("local")
    p_ls = device_cmd("ls", "list a device folder")
    p_ls.add_argument("path", nargs="?", default="/sdcard")
    p_mkdir = device_cmd("mkdir", "create device folder(s), with any missing parents")
    p_mkdir.add_argument("paths", nargs="+", metavar="PATH")
    p_touch = device_cmd("touch", "create empty device file(s)")
    p_touch.add_argument("paths", nargs="+", metavar="PATH")
    p_rm = device_cmd("rm", "delete device files (and folders with -r)")
    p_rm.add_argument("paths", nargs="+", metavar="PATH")
    p_rm.add_argument(
        "-r", "--recursive", action="store_true", help="also delete folders and their contents"
    )
    p_mv = device_cmd("mv", "move or rename a device file or folder")
    p_mv.add_argument("src")
    p_mv.add_argument("dst")
    p_mv.add_argument("-f", "--force", action="store_true", help="replace an existing destination")
    p_cp = device_cmd("cp", "copy a device file or folder")
    p_cp.add_argument("src")
    p_cp.add_argument("dst")
    p_stat = device_cmd("stat", "size, permissions, type and real path of a device file")
    p_stat.add_argument("path")
    p_edit = device_cmd("edit", "edit a device text file in a local editor and save it back")
    p_edit.add_argument("path")
    p_edit.add_argument(
        "--editor", default=None,
        help="editor command (default: $VISUAL, then $EDITOR, else notepad / vi)",
    )

    # --- apps ---
    p_inst = device_cmd("install", "install one or more APKs (splits ok)")
    p_inst.add_argument("apks", nargs="+")
    p_inst.add_argument("--no-replace", action="store_true")
    p_inst.add_argument("--downgrade", action="store_true")
    p_inst.add_argument("--grant", action="store_true", help="grant all perms")
    p_inst.add_argument("--test", action="store_true", help="allow test-only APKs (-t)")
    p_uni = device_cmd("uninstall", "uninstall a package")
    p_uni.add_argument("package")
    p_uni.add_argument("--keep-data", action="store_true")
    p_pkg = device_cmd("packages", "list installed packages")
    p_pkg.add_argument("--third-party", action="store_true")
    p_pkg.add_argument("--system", action="store_true")
    p_pkg.add_argument("--disabled", action="store_true", help="only disabled packages")
    p_pkg.add_argument("--enabled", action="store_true", help="only enabled packages")
    p_pkg.add_argument("--path", action="store_true", help="include each package's APK path")
    p_pkg.add_argument("filter", nargs="?", default=None)
    p_clr = device_cmd("clear", "clear an app's data (pm clear)")
    p_clr.add_argument("package")
    p_start = device_cmd("start", "launch an app by package")
    p_start.add_argument("package")
    p_stop = device_cmd("stop", "force-stop an app")
    p_stop.add_argument("package")
    p_sact = device_cmd("start-activity", "start an activity by component (am start -n)")
    p_sact.add_argument("component", help="package/.Activity")
    p_sact.add_argument("--action", default=None, help="intent action")
    p_sact.add_argument("--data", default=None, help="intent data URI")
    for flag, kind in (("--es", "string"), ("--ei", "integer"), ("--ez", "boolean")):
        p_sact.add_argument(
            flag, nargs=2, action="append", default=None, metavar=("KEY", "VALUE"),
            help=f"{kind} extra (repeatable)",
        )
    device_cmd("activity", "the activity in the foreground")
    p_grant = device_cmd("grant", "grant a runtime permission")
    p_grant.add_argument("package")
    p_grant.add_argument("permission")
    p_revoke = device_cmd("revoke", "revoke a runtime permission")
    p_revoke.add_argument("package")
    p_revoke.add_argument("permission")
    device_cmd("close-apps", "close background apps")

    # --- screen, capture and mirroring ---
    p_shot = device_cmd("screenshot", "capture a PNG screenshot")
    p_shot.add_argument("path")
    display_opt(p_shot)
    p_rec = device_cmd("record", "record the screen, then pull it (Ctrl+C stops early)")
    p_rec.add_argument("path")
    p_rec.add_argument("--time-limit", type=int, default=30)
    p_rec.add_argument("--size", default=None, help="e.g. 1280x720")
    p_rec.add_argument("--bit-rate", default=None, help="e.g. 8M")
    display_opt(p_rec)
    p_rec.add_argument(
        "--continuous", action="store_true",
        help="keep recording past Android's 3-minute limit, in parts, until Ctrl+C",
    )
    p_disp = device_cmd("displays", "list the device's displays (id, size, name)")
    p_disp.add_argument(
        "--method", choices=["auto", "adb", "scrcpy"], default="auto",
        help="how to ask: adb (fast), scrcpy, or auto (adb, then scrcpy)",
    )

    p_scr = device_cmd("scrcpy", "launch scrcpy screen mirroring")
    p_scr.add_argument("--max-size", type=int, default=None)
    p_scr.add_argument("--bit-rate", default=None)
    p_scr.add_argument("--max-fps", type=int, default=None)
    p_scr.add_argument(
        "--display-id", type=int, default=None, help="which display (see: turboadb displays)"
    )
    p_scr.add_argument(
        "--compat", action="store_true",
        help="IVI / automotive compatibility profile (H.264, capped size and fps, no audio, forward tunnel)",
    )
    p_scr.add_argument("--video-codec", choices=["h264", "h265", "av1"], default=None)
    p_scr.add_argument(
        "--render-driver", default=None, help="SDL render driver, e.g. software (Remote Desktop)"
    )
    p_scr.add_argument("--crop", default=None, metavar="W:H:X:Y", help="crop the screen, e.g. 1920:720:0:0")
    p_scr.add_argument(
        "--keyboard", choices=["sdk", "uhid", "aoa"], default=None, help="keyboard injection mode"
    )
    p_scr.add_argument(
        "--force-adb-forward", action="store_true",
        help="use a forward tunnel (devices that block adb reverse)",
    )
    p_scr.add_argument("--no-audio", action="store_true", help="disable device-audio forwarding")
    p_scr.add_argument(
        "--audio-source",
        choices=["output", "playback", "mic", "voice-call-downlink", "voice-performance"],
        default=None,
        help="audio stream to forward (default: scrcpy's output source)",
    )
    p_scr.add_argument("--audio-codec", choices=["opus", "aac", "flac", "raw"], default=None)
    p_scr.add_argument("--audio-bit-rate", default=None, help="audio bitrate, for example 128K")
    p_scr.add_argument("--audio-buffer", type=int, default=None, help="capture latency in milliseconds")
    p_scr.add_argument(
        "--audio-output-buffer", type=int, default=None, help="PC playback buffer in milliseconds"
    )
    p_scr.add_argument(
        "--audio-dup", action="store_true", help="keep sound playing on device (playback source only)"
    )
    p_scr.add_argument("--record", default=None, help="record mirror to FILE")
    p_scr.add_argument("--record-format", choices=["mp4", "mkv"], default=None)
    p_scr.add_argument(
        "--no-playback", action="store_true", help="don't show the video (e.g. only --record)"
    )
    p_scr.add_argument("--turn-screen-off", action="store_true")
    p_scr.add_argument(
        "--no-stay-awake", action="store_true", help="let the device sleep while mirroring"
    )
    p_scr.add_argument("--show-touches", action="store_true", help="show touches on the device")
    p_scr.add_argument("--no-control", action="store_true")
    p_scr.add_argument("--fullscreen", action="store_true")
    p_scr.add_argument("--always-on-top", action="store_true")
    p_scr.add_argument("--window-borderless", action="store_true")
    p_scr.add_argument("--window-title", default=None)
    p_scr.add_argument("--log", default=None, metavar="FILE", help="write scrcpy's log to FILE")
    p_scr.add_argument(
        "--video-source",
        choices=["display", "camera"],
        default=None,
        help="mirror the device camera instead of the screen (scrcpy 2.2+, Android 12+)",
    )
    p_scr.add_argument(
        "--camera-facing",
        choices=["front", "back", "external"],
        default=None,
        help="which camera (with --video-source camera)",
    )
    p_scr.add_argument(
        "--camera-size", default=None, help="camera resolution WxH (with --video-source camera)"
    )
    p_scr.add_argument(
        "--wait", action="store_true", help="block until the scrcpy window is closed"
    )
    on_off("screen", "turn the device screen on|off")

    # --- keys and input ---
    p_key = device_cmd(
        "key", "send key(s) by name or code (home/back/recents/vol_up/play_pause/…)"
    )
    p_key.add_argument("keys", nargs="+", metavar="KEY")
    p_key.add_argument("--longpress", action="store_true", help="long-press (a single key)")
    display_opt(p_key)
    p_text = device_cmd("text", "type text into the focused field")
    display_opt(p_text)
    p_text.add_argument("words", nargs=argparse.REMAINDER)
    p_scroll = device_cmd("scroll", "scroll the screen by swipe")
    p_scroll.add_argument("direction", choices=["up", "down", "left", "right"])
    display_opt(p_scroll)
    p_tap = device_cmd("tap", "tap at X Y, or the centre of the screen")
    p_tap.add_argument("coords", nargs="*", type=int, metavar="XY", help="X and Y in screen pixels")
    display_opt(p_tap)
    p_swipe = device_cmd("swipe", "swipe from X1 Y1 to X2 Y2")
    for coord in ("x1", "y1", "x2", "y2"):
        p_swipe.add_argument(coord, type=int)
    p_swipe.add_argument("--ms", type=int, default=200, help="duration in milliseconds")
    display_opt(p_swipe)
    p_media = device_cmd("media", "media control (play-pause/next/…)")
    p_media.add_argument("action")
    p_bri = device_cmd("brightness", "set brightness 0.0-1.0 (live), or read / step it")
    p_bri.add_argument("fraction", type=float, nargs="?", default=None)
    p_bri.add_argument("--get", action="store_true", help="print the brightness setting (0-255)")
    p_bri.add_argument("--level", type=int, default=None, help="set the brightness setting 0-255")
    p_bri.add_argument("--step", type=int, default=None, help="change the setting by N (e.g. 20 or -20)")
    p_notif = device_cmd("notifications", "expand or collapse the notification shade")
    p_notif.add_argument("state", choices=["expand", "collapse"])

    # --- connectivity ---
    on_off("wifi", "wifi on|off")
    on_off("bluetooth", "bluetooth on|off")
    on_off("airplane", "airplane mode on|off")
    on_off("hotspot", "mobile hotspot on|off (best-effort)")
    on_off("mobile-data", "mobile data on|off")

    # --- open apps and the web ---
    device_cmd("settings", "open the Settings app")
    p_open = device_cmd(
        "open", "open a URL (VIEW intent) or a shortcut: browser, youtube, maps, spotify, play-store"
    )
    p_open.add_argument("url")
    p_search = device_cmd("search", "web-search in the browser")
    p_search.add_argument("query", nargs=argparse.REMAINDER)
    device_cmd("camera", "open the camera app")
    device_cmd("gallery", "open the gallery / photos app")
    device_cmd("calculator", "open the calculator app")

    # --- device status ---
    device_cmd("battery", "battery stats (dumpsys battery)")
    p_health = device_cmd("health", "one-shot health snapshot (battery/temp/memory/cpu/uptime)")
    p_health.add_argument("--full", action="store_true", help="the detailed report with raw dumps")
    p_health.add_argument("-o", "--output", default=None, help="save the --full report to a file")
    p_bld = device_cmd("build-info", "build / version properties")
    p_bld.add_argument("--full", action="store_true", help="the detailed report with every property")
    p_bld.add_argument("-o", "--output", default=None, help="save the --full report to a file")
    p_prop = device_cmd("getprop", "one Android property, or all of them")
    p_prop.add_argument("name", nargs="?", default=None)

    # --- phone and messages ---
    p_dial = device_cmd("dial", "open the dialler with a number")
    p_dial.add_argument("number")
    p_call = device_cmd("call", "place a call")
    p_call.add_argument("number")
    device_cmd("end-call", "end the current call")
    device_cmd("answer", "answer an incoming call")
    device_cmd("call-state", "the call state: idle, ringing or in call")
    p_clog = device_cmd("call-log", "recent calls")
    p_clog.add_argument("--limit", type=int, default=20)
    p_sms = device_cmd("sms", "recent SMS messages")
    p_sms.add_argument("--limit", type=int, default=20)
    p_ssms = device_cmd("send-sms", "compose an SMS (opens the app)")
    p_ssms.add_argument("number")
    p_ssms.add_argument("body", nargs=argparse.REMAINDER)
    device_cmd("phone-support", "telephony and the dialler / call / SMS apps the device has")

    # --- root and system ---
    device_cmd("root", "restart adbd as root")
    device_cmd("unroot", "restart adbd WITHOUT root")
    device_cmd("remount", "adb remount read-write")
    p_mrw = device_cmd("mount-rw", "remount a partition read-write (default /)")
    p_mrw.add_argument("path", nargs="?", default="/")
    for verity in ("disable-verity", "enable-verity"):
        p_ver = device_cmd(verity, f"adb {verity}")
        p_ver.add_argument(
            "--reboot", action="store_true", help="sync and reboot afterwards to apply it"
        )
    p_reb = device_cmd("reboot", "reboot the device")
    p_reb.add_argument(
        "mode", nargs="?", default=None,
        choices=[None, "recovery", "bootloader", "sideload", "fastboot"],
    )
    p_reb.add_argument(
        "--wait", action="store_true", help="wait until the device has booted again"
    )
    p_reb.add_argument(
        "--wait-timeout", type=float, default=180, metavar="SECONDS",
        help="how long --wait waits (default 180)",
    )

    # --- port forwarding ---
    p_fwd = device_cmd("forward", "adb forward LOCAL REMOTE (stays until you stop), or list/remove rules")
    p_fwd.add_argument("local", nargs="?", default=None)
    p_fwd.add_argument("remote", nargs="?", default=None)
    p_rev = device_cmd("reverse", "adb reverse REMOTE LOCAL (stays until you stop), or list/remove rules")
    p_rev.add_argument("remote", nargs="?", default=None)
    p_rev.add_argument("local", nargs="?", default=None)
    for p_rule in (p_fwd, p_rev):
        p_rule.add_argument("--list", action="store_true", help="list this device's rules")
        p_rule.add_argument("--remove", default=None, metavar="SPEC", help="remove one rule")
        p_rule.add_argument("--remove-all", action="store_true", help="remove this device's rules")
        p_rule.add_argument(
            "--no-wait", action="store_true", help="add the rule and exit, leaving it active"
        )

    # --- sharing devices with other PCs ---
    p_serve = device_cmd(
        "serve",
        "expose THIS PC's adb server to the network so other machines can "
        "drive its devices (auto 'adb -a nodaemon server start')",
    )
    p_serve.add_argument("--port", type=int, default=5037, help="adb server port (default 5037)")
    p_serve.add_argument(
        "--install-startup",
        action="store_true",
        help="also run automatically at every Windows login, so "
        "it never has to be started by hand again",
    )
    p_serve.add_argument(
        "--startup-task",
        action="store_true",
        help="register a SYSTEM startup Scheduled Task (headless, "
        "survives logoff — best for remote/RDP hosts) and "
        "start it now",
    )
    p_serve.add_argument(
        "--uninstall-startup",
        action="store_true",
        help="remove the login auto-start launcher + startup task",
    )
    p_serve.add_argument(
        "--stop", action="store_true", help="stop sharing: back to a local-only adb server"
    )
    p_serve.add_argument(
        "--status", action="store_true", help="report whether the adb server is shared"
    )

    p_deploy = sub.add_parser(
        "deploy-serve",
        help="install/start 'turboadb serve' on remote Windows host(s) over WinRM "
        "(NTLM) — the same as the GUI 'ADB Server' button",
    )
    p_deploy.add_argument("hosts", nargs="+", help="remote hostname(s) or IP(s)")
    p_deploy.add_argument(
        "-u", "--user", required=True, help="admin login, DOMAIN\\user (local admin on targets)"
    )
    p_deploy.add_argument(
        "-p", "--password", default=None, help="password (omit to be prompted securely)"
    )
    p_deploy.add_argument(
        "--port", type=int, default=5037, help="adb server port on the targets (default 5037)"
    )
    p_deploy.add_argument(
        "--winrm-port", type=int, default=None, help="WinRM port (default 5985, or 5986 with --ssl)"
    )
    p_deploy.add_argument("--ssl", action="store_true", help="WinRM over HTTPS")
    p_deploy.add_argument(
        "--no-update", action="store_true", help="don't pip-upgrade turboadb on the host first"
    )
    p_deploy.add_argument(
        "--test", action="store_true", help="only test WinRM + credentials; don't deploy"
    )
    return parser


# --------------------------------------------------------------------------- #
# command dispatch
# --------------------------------------------------------------------------- #
def _words(ws):
    """argparse keeps the '--' separator in a REMAINDER list, so the documented
    ``turboadb shell -- getprop …`` sent a literal '--' to the device — drop it."""
    ws = list(ws or [])
    if ws and ws[0] == "--":
        ws = ws[1:]
    return ws


def _progress_printer():
    """A stderr ``NN%`` progress callback (the one shared implementation)."""
    state = {"last": -1}

    def cb(pct):
        if pct == state["last"]:
            return
        state["last"] = pct
        sys.stderr.write(f"\r  {pct:3d}%")
        sys.stderr.flush()
        if pct >= 100:
            sys.stderr.write("\n")

    return cb


def _progress(args):
    return None if getattr(args, "json", False) else _progress_printer()


def _make_output_crashproof():
    """Never let a non-ASCII character (—, →, ·, …) crash the CLI on a legacy
    Windows console (cp1252): degrade unencodable chars to '?' instead of
    raising UnicodeEncodeError. Best-effort; only touches the CLI's own streams."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # Python 3.7+
        except Exception:
            pass


class _StderrHandler(logging.StreamHandler):
    """Always writes to the CURRENT sys.stderr (which may be replaced after the
    handler is installed, e.g. by a test runner or a redirect)."""

    _turboadb_cli = True

    def __init__(self):
        super().__init__(sys.stderr)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value):
        pass


def _configure_logging() -> None:
    """Show the engine's INFO+ events on stderr (the library itself attaches no
    handlers; raw ``$ adb …`` traces stay at DEBUG and hidden)."""
    logger = logging.getLogger("turboadb")
    if any(getattr(h, "_turboadb_cli", False) for h in logger.handlers):
        return
    handler = _StderrHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)


# --json for commands that print a plain result: set by main() for each call.
_JSON = {"on": False}


def _report(ok, ok_msg: str, fail_msg: str) -> int:
    """Print the outcome of a bool-returning action and give its exit code."""
    if _JSON["on"]:
        print(json.dumps({"ok": bool(ok), "message": (ok_msg if ok else fail_msg).strip()}))
        return 0 if ok else 1
    if ok:
        print(ok_msg)
        return 0
    print(fail_msg, file=sys.stderr)
    return 1


def _result(value) -> None:
    """Print an action's result (``{"ok": true, "result": …}`` with --json)."""
    if _JSON["on"]:
        print(json.dumps({"ok": True, "result": value}, default=str, indent=2))
    else:
        print(value)


def _saved(path) -> None:
    if _JSON["on"]:
        print(json.dumps({"ok": True, "path": path}))
    else:
        print(f"Saved {path}")


def _write_report(text: str, output) -> int:
    """Print a long report, or save it to *output*."""
    if not output:
        print(text)
        return 0
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    _saved(output)
    return 0


def _print_command(res) -> None:
    if res.stdout:
        sys.stdout.write(res.stdout if res.stdout.endswith("\n") else res.stdout + "\n")
    if res.stderr:
        sys.stderr.write(res.stderr if res.stderr.endswith("\n") else res.stderr + "\n")


def _shell_all(args) -> int:
    """`shell --all -- CMD`: the command on every online device, in turn."""
    command = " ".join(_words(args.command)).strip()
    if not command:
        print("shell --all needs a command, e.g.: turboadb shell --all -- getprop ro.product.model",
              file=sys.stderr)
        return 2
    devs = [
        d
        for d in list_devices(args.adb_path, server_host=args.adb_host, server_port=args.adb_port)
        if d.state == "device"
    ]
    if not devs:
        print("No online devices.", file=sys.stderr)
        return 1
    results, rc = [], 0
    for d in devs:
        res = _handler(args, serial=d.serial).shell(command, su=args.su)
        if not res.ok:
            rc = 1
        if _JSON["on"]:
            results.append(dict(res.as_dict(), serial=d.serial))
        else:
            print(f"--- {d.serial} (exit {res.exit_code}) ---")
            _print_command(res)
    if _JSON["on"]:
        print(json.dumps(results, default=str, indent=2))
    return rc


def _shell_batch(dev, args) -> int:
    """`shell --batch FILE`: each non-empty, non-# line as a command, in order."""
    with open(args.batch, encoding="utf-8") as fh:
        commands = [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    results = dev.shell_many(commands, stop_on_error=not args.keep_going, su=args.su)
    if _JSON["on"]:
        print(json.dumps([r.as_dict() for r in results], default=str, indent=2))
    else:
        for cmd, res in zip(commands, results):
            print(f"$ {cmd}")
            _print_command(res)
    return 0 if len(results) == len(commands) and all(r.ok for r in results) else 1


def _forwarding(dev, args) -> int:
    """`forward` / `reverse`: add a rule (kept until Ctrl+C, or --no-wait), list or remove."""
    forward = args.cmd == "forward"
    name = args.cmd
    if args.list:
        rules = dev.list_forwards() if forward else dev.list_reverses()
        if _JSON["on"]:
            print(json.dumps(rules, indent=2))
        elif rules:
            for rule in rules:
                print(rule)
        else:
            print(f"No {name} rules.", file=sys.stderr)
        return 0
    if args.remove_all:
        ok = dev.remove_all_forwards() if forward else dev.remove_all_reverses()
        return _report(ok, f"removed every {name} rule", f"could not remove every {name} rule")
    if args.remove:
        ok = dev.remove_forward(args.remove) if forward else dev.remove_reverse(args.remove)
        return _report(ok, f"removed {name} {args.remove}", f"could not remove {name} {args.remove}")
    first, second = (args.local, args.remote) if forward else (args.remote, args.local)
    if not (first and second):
        print(f"{name} needs two specs, e.g. tcp:8080 tcp:8080 (or --list, --remove, --remove-all)",
              file=sys.stderr)
        return 2
    handle = dev.forward(first, second) if forward else dev.reverse(first, second)
    if args.no_wait:
        return _report(True, f"{handle} — stays active; remove it with: turboadb {name} --remove {first}", "")
    print(f"{handle}\n{name.capitalize()} active. Ctrl+C to remove it.")
    try:
        _block()
    except KeyboardInterrupt:
        return _report(handle.close(), f"\n{name} removed.", f"\nERROR: could not remove the {name}")
    return 0


def _edit(dev, args) -> int:
    """`edit PATH`: pull a device text file, open it in a local editor, and push
    it back (keeping its permissions) only when it was changed."""
    import shlex
    import tempfile

    info = dev.stat_path(args.path)
    if not str(info["type"]).startswith("regular"):
        print(f"ERROR: {info['path']} is not a regular file ({info['type']})", file=sys.stderr)
        return 1
    real = info["real_path"]
    editor = (args.editor or os.environ.get("VISUAL") or os.environ.get("EDITOR")
              or ("notepad" if os.name == "nt" else "vi"))
    command = [editor] if os.path.exists(editor) else shlex.split(editor, posix=os.name != "nt")
    suffix = os.path.splitext(real)[1]
    fd, tmp = tempfile.mkstemp(prefix="turboadb-edit-", suffix=suffix)
    os.close(fd)
    try:
        dev.pull(real, tmp)
        with open(tmp, "rb") as fh:
            before = fh.read()
        code = subprocess.call(command + [tmp])
        if code != 0:
            print(f"The editor exited with {code}; {real} was not changed.", file=sys.stderr)
            return 1
        with open(tmp, "rb") as fh:
            after = fh.read()
        if after == before:
            return _report(True, f"No changes — {real} was not changed.", "")
        dev.push(tmp, real)
        if info.get("mode"):
            dev.chmod(real, info["mode"])  # adb push resets the file mode
        return _report(True, f"Saved {real}", "")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _targets(args) -> int:
    """`targets`: the saved device targets the GUI's sidebar shows."""
    from .gui.sessions import SessionStore

    store = SessionStore()
    action = args.targets_cmd
    if action == "list":
        if _JSON["on"]:
            print(json.dumps(store.sessions, indent=2))
        elif not store.sessions:
            print("No saved targets. Add one with: turboadb targets add NAME usb SERIAL")
        else:
            for s in store.sessions:
                if s["type"] == "network":
                    where = f"{s['host']}:{s['port']}"
                elif s["type"] == "remote":
                    where = f"{s.get('serial') or '(the only device)'} on {s['adb_host']}:{s['adb_port']}"
                else:
                    where = s.get("serial") or "(the only device)"
                print(f"{s['name']:24}  {s['type']:8}  {where}")
        return 0
    if action == "add":
        target = {"name": args.name, "type": args.kind}
        if args.kind == "usb":
            target["serial"] = args.address or ""
        elif args.kind == "network":
            host, port = parse_host_port(args.address or "", 5555)
            if not host:
                raise ValueError("a network target needs HOST[:PORT]")
            target.update(host=host, port=port)
        else:
            host, port = parse_host_port(args.address or "", 5037)
            if not host:
                raise ValueError("a remote target needs the adb server HOST[:PORT]")
            target.update(adb_host=host, adb_port=port, serial=args.remote_serial or "")
        store.save(target)
        return _report(True, f"Saved target {args.name!r} — use it with: turboadb -s @{args.name} …", "")
    if action == "remove":
        if store.get(args.name) is None:
            raise ValueError(f"no saved target named {args.name!r}")
        store.delete(args.name)
        return _report(True, f"Removed target {args.name!r}", "")
    if action == "export":
        count = store.export_to(args.file)
        return _report(True, f"Exported {count} target(s) to {args.file}", "")
    count = store.import_from(args.file)
    return _report(True, f"Imported {count} target(s) from {args.file}", "")


def _record(dev, args) -> int:
    """`record`: Ctrl+C stops the recording EARLY and still pulls a playable
    MP4. --continuous records back-to-back parts past Android's 3-minute cap
    until Ctrl+C."""
    import threading

    stop = threading.Event()
    result = {"paths": []}

    def work():
        base, ext = os.path.splitext(args.path)
        ext = ext or ".mp4"
        part = 0
        try:
            while True:
                path = args.path if part == 0 else f"{base}-part{part + 1:02d}{ext}"
                result["paths"].append(
                    dev.screen_record(
                        path,
                        time_limit=180 if args.continuous else args.time_limit,
                        size=args.size,
                        bit_rate=args.bit_rate,
                        display_id=args.display,
                        stop_event=stop,
                    )
                )
                part += 1
                if not args.continuous or stop.is_set():
                    break
        except BaseException as exc:  # re-raised on the main thread
            result["error"] = exc

    worker = threading.Thread(target=work, name="turboadb-record", daemon=True)
    worker.start()
    if args.continuous:
        print("Recording in 3-minute parts… press Ctrl+C to stop.", file=sys.stderr)
    else:
        print("Recording… press Ctrl+C to stop early.", file=sys.stderr)
    try:
        while worker.is_alive():
            worker.join(0.2)
    except KeyboardInterrupt:
        print("\nStopping the recording and saving it…", file=sys.stderr)
        stop.set()
        worker.join()  # a second Ctrl+C aborts outright
    if "error" in result and not result["paths"]:
        raise result["error"]
    for path in result["paths"]:
        _saved(path)
    return 0


def main(argv=None) -> int:
    _make_output_crashproof()
    parser = build_parser()
    try:  # optional shell tab-completion (pip install
        import argcomplete  # argcomplete + activate-global-python-argcomplete)

        argcomplete.autocomplete(parser)
    except ImportError:
        pass
    args = parser.parse_args(argv)
    cmd = args.cmd
    _configure_logging()
    _JSON["on"] = bool(getattr(args, "json", False))

    # Auto-fetch ONLY when a tool this command needs is missing. Upgrading an
    # existing adb stops the running server (dropping every device session), so
    # upgrades happen only via upgrade-tools / self-update — never as a side
    # effect of an ordinary command.
    if cmd not in (
        "doctor",
        "fetch-tools",
        "upgrade-tools",
        "self-update",
        "shortcut",
        "deploy-serve",
        "targets",
    ):
        try:
            need_scrcpy = cmd == "scrcpy" and not scrcpy_available()
            if need_scrcpy or not adb_available(getattr(args, "adb_path", None)):
                from . import toolsdl

                toolsdl.ensure_tools(
                    notify=lambda m: print(m, file=sys.stderr), on_progress=_progress_printer()
                )
        except Exception:
            pass

    try:
        if cmd == "doctor":
            from . import tools

            d = tools.diagnose()
            print(f"adb    : {d['adb'] or 'NOT FOUND'}")
            print(f"         {d['adb_path'] or tools.ADB_DOWNLOAD}")
            print(f"scrcpy : {'found at ' + d['scrcpy_path'] if d['scrcpy'] else 'NOT FOUND'}")
            if not d["scrcpy"]:
                print(f"         {tools.SCRCPY_DOWNLOAD}")
            if not d["adb"] or not d["scrcpy"]:
                print(
                    "\nTip: run  turboadb fetch-tools  to download what's missing "
                    "into ~/.turboadb/tools"
                )
            return 0 if d["adb"] else 1

        if cmd == "fetch-tools":
            from . import toolsdl

            want_adb = not args.scrcpy_only
            want_scrcpy = not args.adb_only
            print(f"Downloading into {toolsdl.tools_dir()} …", file=sys.stderr)
            res = toolsdl.fetch_tools(
                adb=want_adb, scrcpy=want_scrcpy, force=args.force, on_progress=_progress_printer()
            )
            if res.get("adb"):
                print(f"adb    -> {res['adb']}")
            if res.get("scrcpy"):
                print(f"scrcpy -> {res['scrcpy']}")
            for tool, err in res.get("errors", {}).items():
                print(f"{tool}: {err}", file=sys.stderr)
            return 0 if (res.get("adb") or res.get("scrcpy")) else 1

        if cmd == "upgrade-tools":
            from . import toolsdl

            checks = toolsdl.check_updates()
            supported = [t for t in ("adb", "scrcpy") if checks[t].get("supported", True)]
            for tool in ("adb", "scrcpy"):
                c = checks[tool]
                if tool not in supported:
                    state = "not downloadable on this OS (use your package manager)"
                elif c["upgrade"] is False:
                    state = "up to date"
                elif c["upgrade"]:
                    state = "UPDATE AVAILABLE"
                else:
                    state = "unknown (couldn't check)"
                print(f"{tool:7}: installed={c['installed']}  latest={c['latest']}  -> {state}")
            if args.check:
                return 0
            if not any(checks[t]["upgrade"] is True for t in supported):
                unknown = [t for t in supported if checks[t]["upgrade"] is None]
                if unknown:
                    # a failed check is NOT "everything is current"
                    print(
                        f"\nCouldn't check {', '.join(unknown)} for updates "
                        f"(network / rate limit) — try again in a while.",
                        file=sys.stderr,
                    )
                    return 1
                print("\nEverything is current — nothing to download.")
                return 0
            res = toolsdl.upgrade_tools(
                notify=lambda m: print(m, file=sys.stderr),
                on_progress=_progress_printer(),
                checks=checks,  # don't query the version sources a second time
            )
            for tool, path in res.get("updated", {}).items():
                print(f"updated {tool} -> {path}")
            for tool, err in res.get("errors", {}).items():
                print(f"{tool}: {err}", file=sys.stderr)
            return 1 if res.get("errors") else 0

        if cmd == "gui":
            return launch_gui([])

        if cmd == "shortcut":
            return create_shortcut([])

        if cmd == "targets":
            return _targets(args)

        if cmd == "deploy-serve":
            from . import remote_deploy

            pw = args.password
            if pw is None:
                import getpass

                pw = getpass.getpass(f"Password for {args.user}: ")
            return remote_deploy.deploy_serve(
                args.hosts,
                args.user,
                pw,
                update=not args.no_update,
                port=args.port,
                winrm_port=args.winrm_port or (5986 if args.ssl else 5985),
                use_ssl=args.ssl,
                test_only=args.test,
                on_status=lambda m: print(m),
            )

        if cmd == "self-update":
            from . import update as _upd

            if args.check:
                current, latest = _upd.current_version(), _upd.pypi_latest()
                newer = bool(latest) and _upd.is_newer(latest)
                if _JSON["on"]:
                    print(json.dumps({"current": current, "latest": latest, "update_available": newer}))
                elif not latest:
                    print(f"TurboADB {current} — couldn't reach PyPI to check for a newer version.")
                elif newer:
                    print(f"TurboADB {latest} is available (installed: {current}). Run: turboadb self-update")
                else:
                    print(f"TurboADB {current} is the latest version.")
                return 0 if latest else 1
            if not _upd.can_self_update():
                print(
                    "Running the standalone executable — upgrade with: pip install --upgrade turboadb",
                    file=sys.stderr,
                )
                return 1
            latest = _upd.pypi_latest()
            if latest and not _upd.is_newer(latest):
                print(
                    f"TurboADB {_upd.current_version()} is already the latest; "
                    f"refreshing adb/scrcpy…",
                    file=sys.stderr,
                )
            res = _upd.run_upgrade(notify=lambda m: print(m, file=sys.stderr))
            if not res.get("ok"):
                print(f"Update failed: {res.get('error')}", file=sys.stderr)
                return 1
            bits = [
                b
                for b in (
                    f"adb {res['adb']}" if res.get("adb") else None,
                    f"scrcpy {res['scrcpy']}" if res.get("scrcpy") else None,
                )
                if b
            ]
            print(
                f"Updated to TurboADB {res.get('new')}" + (f"  ({', '.join(bits)})" if bits else "")
            )
            return 0

        if cmd == "devices":
            try:
                devs = list_devices(
                    args.adb_path, server_host=args.adb_host, server_port=args.adb_port
                )
            except ConnectionError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps([d.__dict__ for d in devs], default=str, indent=2))
            elif not devs:
                where = (
                    f"the adb server at {args.adb_host}:{args.adb_port}" if args.adb_host else "USB"
                )
                print(
                    f"No devices found on {where}. Plug in (enable USB "
                    f"debugging), or:  turboadb connect HOST:PORT"
                )
            else:
                for d in devs:
                    print(d)
            return 0

        if cmd == "discover":
            from .devices import mdns_devices

            found = mdns_devices(args.adb_path)
            if args.json:
                print(json.dumps(found, indent=2))
            elif not found:
                print(
                    "No Wireless-debugging devices found on the LAN.\n"
                    "On the device: Settings > Developer options > Wireless "
                    "debugging (Android 11+). Pairing entries need "
                    "'turboadb pair' first."
                )
            else:
                for d in found:
                    print(f"{d['address']:22}  {d['service']:8}  {d['name']}")
                if not args.connect:
                    print(
                        f"\n[{len(found)} service(s)]  ·  connect ones are ready "
                        f"for:  turboadb connect HOST:PORT  (or discover --connect)",
                        file=sys.stderr,
                    )
            if args.connect:
                rc = 0
                handler = _handler(args, serial=None)
                for d in found:
                    if d.get("service") != "connect":
                        continue
                    host, port = parse_host_port(d["address"], 5555)
                    try:
                        print(handler.connect_tcp(host, port), file=sys.stderr)
                    except ADBError as exc:
                        print(f"{d['address']}: {exc}", file=sys.stderr)
                        rc = 1
                return rc
            return 0

        if cmd == "restart-server":
            r = _handler(args, serial=None).restart_server()
            print(r.text or r.stderr.strip() or "ADB server restarted.")
            return 0

        if cmd == "serve":
            from .devices import (
                start_shared_server,
                stop_shared_server,
                server_is_shared,
                install_startup,
                uninstall_startup,
                open_firewall,
                install_serve_task,
                uninstall_serve_task,
            )
            from .scrcpy import TUNNEL_PORT_FIREWALL_RANGE

            def _say(msg):
                # at Windows login we run under pythonw (no console / stdout=None)
                try:
                    print(msg)
                except Exception:
                    pass

            if args.status:
                shared = server_is_shared(port=args.port)
                if _JSON["on"]:
                    print(json.dumps({"port": args.port, "shared": shared}))
                else:
                    _say(f"adb server on port {args.port}: "
                         + ("shared on the network" if shared else "not shared"))
                return 0
            if args.stop:
                _say(stop_shared_server(port=args.port, adb_path=args.adb_path))
                return 0
            if args.uninstall_startup:
                a = uninstall_startup()
                b = uninstall_serve_task()
                _say("Removed auto-start." if (a or b) else "No auto-start was installed.")
                return 0
            _say(start_shared_server(port=args.port, adb_path=args.adb_path))
            # open BOTH the adb port and scrcpy's video-tunnel port so a remote
            # laptop can mirror this PC's device
            _say(open_firewall((args.port, TUNNEL_PORT_FIREWALL_RANGE)))
            rc = 0
            if args.startup_task:
                try:
                    name = install_serve_task(port=args.port)
                    _say(
                        f"Installed startup Scheduled Task '{name}' (runs at "
                        f"system startup, headless — survives logoff)."
                    )
                except Exception as exc:
                    _say(f"Could not install startup task (need admin): {exc}")
                    rc = 1
            if args.install_startup:
                try:
                    path = install_startup(port=args.port)
                    _say(f"Installed login auto-start: {path}")
                except Exception as exc:
                    _say(f"Could not install login auto-start: {exc}")
                    rc = 1
            if not (args.startup_task or args.install_startup):
                _say(
                    "Other machines can now connect with TurboADB -> Remote, "
                    "using this PC's IP/hostname.\n"
                    "Tip: add  --startup-task  so it runs headless at every "
                    "startup (best for remote/RDP hosts)."
                )
            return rc

        if cmd == "connect":
            host, port = parse_host_port(args.hostport, 5555)
            if not host:
                print(f"Error: invalid host:port '{args.hostport}'", file=sys.stderr)
                return 1
            _result(_handler(args, serial=None).connect_tcp(host, port))
            return 0

        if cmd == "disconnect":
            res = _handler(args, serial=None).adb(
                "disconnect", *([args.hostport] if args.hostport else [])
            )
            detail = "\n".join(p for p in (res.text, res.stderr.strip()) if p)
            if not res.ok or "error" in detail.lower():
                print(detail or f"adb disconnect failed (exit {res.exit_code})", file=sys.stderr)
                return 1
            print(res.text or "disconnected")
            return 0

        if cmd == "pair":
            hp = args.hostport
            host, port = parse_host_port(hp)
            if not host or port is None:
                print(
                    f"Error: invalid host:port for pair: '{hp}' (format: host:port, e.g. 192.168.1.50:41235)",
                    file=sys.stderr,
                )
                return 1
            print(_handler(args, serial=None).pair(host, port, args.code))
            return 0

        if cmd == "shell" and args.all:
            return _shell_all(args)

        # everything below operates on a device handler
        dev = _handler(args)
        if cmd == "scrcpy":
            # scrcpy doesn't need our connect() handshake, but a network target
            # must be known to the adb server before scrcpy can find it
            cfg = dev.config
            if cfg.host and not cfg.is_remote_server:
                joined = dev.connect_tcp(cfg.host, cfg.port, safe=True)
                if not joined:
                    print(f"Warning: {joined.error}", file=sys.stderr)
            opts = ScrcpyOptions(
                max_size=args.max_size,
                bit_rate=args.bit_rate,
                max_fps=args.max_fps,
                display_id=args.display_id,
                video_codec=args.video_codec,
                render_driver=args.render_driver,
                crop=args.crop,
                keyboard_mode=args.keyboard,
                force_adb_forward=args.force_adb_forward,
                no_audio=args.no_audio,
                audio_source=args.audio_source,
                audio_codec=args.audio_codec,
                audio_bit_rate=args.audio_bit_rate,
                audio_buffer=args.audio_buffer,
                audio_output_buffer=args.audio_output_buffer,
                audio_dup=args.audio_dup,
                record=args.record,
                record_format=args.record_format,
                no_playback=args.no_playback,
                stay_awake=not args.no_stay_awake,
                turn_screen_off=args.turn_screen_off,
                show_touches=args.show_touches,
                no_control=args.no_control,
                fullscreen=args.fullscreen,
                always_on_top=args.always_on_top,
                window_borderless=args.window_borderless,
                window_title=args.window_title,
                video_source=args.video_source,
                camera_facing=args.camera_facing,
                camera_size=args.camera_size,
            )
            extra = {}
            if args.compat:
                extra["compat"] = True
            if args.log:
                extra["log_path"] = args.log
            sess = dev.mirror(opts, **extra)
            print(f"scrcpy launched (pid {sess.pid}). Close its window to end.")
            if args.wait:
                return sess.wait() or 0
            return 0

        # these work while the device is offline, unauthorized or rebooting
        if cmd == "state":
            _result(dev.get_state())
            return 0
        if cmd == "serialno":
            _result(dev.get_serialno())
            return 0
        if cmd == "wait":
            dev.wait_for_device(args.seconds)
            return _report(True, "device online", "")
        if cmd == "adb":
            words = _words(args.adb_args)
            if not words:
                print("adb needs arguments, e.g.: turboadb -s SERIAL adb -- get-state", file=sys.stderr)
                return 2
            res = dev.adb(*words)
            if _JSON["on"]:
                print(json.dumps(res.as_dict(), default=str, indent=2))
            else:
                _print_command(res)
            return res.exit_code

        dev.connect()
        rc = 0

        if cmd == "info":
            _output(args, dev.device_info())
        elif cmd == "shell":
            if args.batch:
                return _shell_batch(dev, args)
            command = " ".join(_words(args.command)).strip()
            if not command:
                if _JSON["on"] or not sys.stdin.isatty():
                    print("No command given (an interactive shell needs a terminal).", file=sys.stderr)
                    return 2
                return dev.interactive_shell()
            res = dev.shell(command, su=args.su)
            if not args.json:
                _print_command(res)
                return res.exit_code
            _output(args, res)
            return res.exit_code
        elif cmd == "logcat":
            import re as _re

            try:
                pattern = _re.compile(args.grep, _re.I) if args.grep else None
            except _re.error as exc:
                print(f"ERROR: invalid --grep pattern: {exc}", file=sys.stderr)
                return 2
            buffers, priority = args.buffer, args.priority
            if args.crashes:
                buffers = buffers or ["crash", "main", "system"]
                priority = priority or "E"

            def show(line):
                if pattern is None or pattern.search(line):
                    print(line)

            print("Dumping logcat…" if args.dump else "Streaming logcat (Ctrl+C to stop)…", file=sys.stderr)
            try:
                res = dev.logcat(
                    tag=args.tag,
                    priority=priority,
                    filterspecs=args.filter,
                    buffers=buffers,
                    fmt=args.format,
                    match=args.match,
                    save_to=args.save,
                    stop_on_match=args.stop_on_match,
                    clear_first=args.clear,
                    dump=args.dump,
                    tail=args.tail,
                    on_line=show,
                )
                if args.match:
                    print(f"\n[{len(res.matches)} matched lines]", file=sys.stderr)
            except KeyboardInterrupt:
                pass
        elif cmd == "logcat-clear":
            rc = _report(dev.logcat_clear(), "logcat buffers cleared.", "could not clear logcat")
        elif cmd == "push":
            _output(args, dev.push(args.local, args.remote, on_progress=_progress(args)))
        elif cmd == "pull":
            _output(args, dev.pull(args.remote, args.local, on_progress=_progress(args)))
        elif cmd == "ls":
            entries = dev.list_dir(args.path)
            if _JSON["on"]:
                print(json.dumps(entries, indent=2))
            elif not entries:
                print(f"{args.path} is empty.", file=sys.stderr)
            else:
                for e in entries:
                    name = e["name"] + ("/" if e["is_dir"] else "")
                    print(f"{e['permissions']:10}  {e['owner']:20}  {e['size_text']:>9}  "
                          f"{e['modified']:16}  {name}")
        elif cmd == "mkdir":
            made = [dev.make_dir(p) for p in args.paths]
            rc = _report(True, "created " + ", ".join(made), "")
        elif cmd == "touch":
            made = [dev.touch(p) for p in args.paths]
            rc = _report(True, "created " + ", ".join(made), "")
        elif cmd == "rm":
            gone = dev.remove(args.paths, recursive=args.recursive)
            rc = _report(True, "deleted " + ", ".join(gone), "")
        elif cmd == "mv":
            rc = _report(True, f"moved to {dev.move(args.src, args.dst, overwrite=args.force)}", "")
        elif cmd == "cp":
            rc = _report(True, f"copied to {dev.copy(args.src, args.dst)}", "")
        elif cmd == "stat":
            info = dev.stat_path(args.path)
            if _JSON["on"]:
                print(json.dumps(info, indent=2))
            else:
                for key in ("path", "real_path", "type", "size", "mode"):
                    print(f"{key:10} {info[key]}")
        elif cmd == "edit":
            rc = _edit(dev, args)
        elif cmd == "install":
            if len(args.apks) > 1:
                _result(
                    dev.install_multiple(
                        args.apks,
                        replace=not args.no_replace,
                        downgrade=args.downgrade,
                        grant_perms=args.grant,
                        allow_test=args.test,
                    )
                )
            else:
                _result(
                    dev.install(
                        args.apks[0],
                        replace=not args.no_replace,
                        downgrade=args.downgrade,
                        grant_perms=args.grant,
                        allow_test=args.test,
                    )
                )
        elif cmd == "uninstall":
            _result(dev.uninstall(args.package, keep_data=args.keep_data))
        elif cmd == "packages":
            pkgs = dev.list_packages(
                filter_text=args.filter,
                third_party=args.third_party,
                system=args.system,
                disabled=args.disabled,
                enabled=args.enabled,
                include_path=args.path,
            )
            if args.json:
                print(json.dumps(pkgs, indent=2))
            else:
                for p in pkgs:
                    print(p)
                print(f"\n[{len(pkgs)} packages]", file=sys.stderr)
        elif cmd == "clear":
            _result(dev.clear_app(args.package))
        elif cmd == "start":
            rc = _report(dev.start_app(args.package), "launched", f"could not launch {args.package}")
        elif cmd == "stop":
            rc = _report(dev.stop_app(args.package), "stopped", f"could not stop {args.package}")
        elif cmd == "start-activity":
            extras = []
            for flag in ("es", "ei", "ez"):
                for key, value in getattr(args, flag) or []:
                    extras += [f"--{flag}", key, value]
            out = dev.start_activity(args.component, action=args.action, data=args.data, extras=extras)
            if "error" in (out or "").lower():
                print(out, file=sys.stderr)
                rc = 1
            else:
                _result(out)
        elif cmd == "activity":
            _result(dev.current_activity())
        elif cmd == "grant":
            rc = _report(dev.grant(args.package, args.permission),
                         f"granted {args.permission} to {args.package}", "grant failed")
        elif cmd == "revoke":
            rc = _report(dev.revoke(args.package, args.permission),
                         f"revoked {args.permission} from {args.package}", "revoke failed")
        elif cmd == "screenshot":
            _saved(dev.screenshot(args.path, display_id=args.display))
        elif cmd == "record":
            rc = _record(dev, args)
        elif cmd == "displays":
            found = dev.list_displays(method=args.method)
            if _JSON["on"]:
                print(json.dumps(found, indent=2))
            elif not found:
                print("The device reported no displays.", file=sys.stderr)
            else:
                for d in found:
                    print(f"{d['id']:>3}  {d.get('size') or '?':>10}  {d.get('name') or ''}".rstrip())
        elif cmd in ("forward", "reverse"):
            rc = _forwarding(dev, args)
        elif cmd == "tcpip":
            _result(dev.tcpip(args.port))
        elif cmd == "wireless":
            serial = dev.go_wireless(args.port)
            rc = _report(True, f"connected wirelessly as {serial} - the USB cable can be unplugged now", "")
        elif cmd == "ip":
            ip = dev.device_ip()
            if ip:
                _result(ip)
            else:
                rc = _report(False, "", "the device has no Wi-Fi / Ethernet IP address")
        elif cmd == "health":
            if args.full:
                rc = _write_report(dev.health_report(), args.output)
            elif _JSON["on"]:
                print(json.dumps(dev.health(), indent=2))
            else:
                print(dev.health_text())
        elif cmd == "bugreport":
            import time as _t

            out = args.path or _t.strftime("bugreport-%Y%m%d-%H%M%S.zip")
            print("Capturing bugreport (takes a few minutes)…", file=sys.stderr)
            if args.timeout:
                _saved(dev.bugreport(out, timeout=args.timeout))
            else:
                _saved(dev.bugreport(out))
        elif cmd == "reboot":
            label = f"reboot {args.mode or ''}".strip()
            ok = dev.reboot(args.mode)
            if ok and args.wait:
                if args.mode is None:
                    print("Waiting for the device to boot…", file=sys.stderr)
                    dev.wait_for_boot(args.wait_timeout)
                    label += " — the device is back"
                else:
                    print(f"--wait only waits for a normal boot, not {args.mode}.", file=sys.stderr)
            rc = _report(ok, label, f"{label} failed")
        elif cmd == "root":
            _result(dev.root())
        elif cmd == "key":
            keys = args.keys
            label = " ".join(args.keys)
            if args.longpress and len(keys) > 1:
                print("--longpress takes a single key", file=sys.stderr)
                return 2
            if len(keys) == 1:
                ok = dev.keyevent(keys[0], longpress=args.longpress, display_id=args.display)
            else:
                ok = dev.keyevents(keys, display_id=args.display)
            rc = _report(ok, f"sent {label}", f"key {label} failed")
        elif cmd == "text":
            rc = _report(dev.input_text(" ".join(_words(args.words)), display_id=args.display),
                         "typed", "typing failed")
        elif cmd == "scroll":
            rc = _report(
                dev.scroll(args.direction, display_id=args.display),
                f"scrolled {args.direction}", "scroll failed",
            )
        elif cmd == "tap":
            if len(args.coords) == 2:
                x, y = args.coords
                rc = _report(dev.tap(x, y, display_id=args.display), f"tapped {x},{y}", "tap failed")
            elif not args.coords:
                rc = _report(dev.tap_center(display_id=args.display), "tapped", "tap failed")
            else:
                print("tap takes X Y, or nothing for the centre of the screen", file=sys.stderr)
                return 2
        elif cmd == "swipe":
            rc = _report(
                dev.swipe(args.x1, args.y1, args.x2, args.y2, args.ms, display_id=args.display),
                f"swiped {args.x1},{args.y1} -> {args.x2},{args.y2}", "swipe failed",
            )
        elif cmd == "media":
            rc = _report(dev.media(args.action), f"media {args.action}", f"media {args.action} failed")
        elif cmd == "brightness":
            if args.get:
                _result(dev.get_brightness())
            elif args.level is not None:
                _result(dev.set_brightness(args.level))
            elif args.step is not None:
                _result(dev.adjust_brightness(args.step))
            elif args.fraction is not None:
                rc = _report(
                    dev.display_brightness(args.fraction),
                    f"brightness {args.fraction}",
                    "could not set brightness",
                )
            else:
                print("brightness takes a fraction 0.0-1.0, --level 0-255, --step N or --get",
                      file=sys.stderr)
                return 2
        elif cmd == "notifications":
            if args.state == "expand":
                rc = _report(dev.expand_notifications(), "notifications expanded", "could not expand")
            else:
                rc = _report(dev.collapse_notifications(), "notifications collapsed", "could not collapse")
        elif cmd == "wifi":
            _result(dev.set_wifi(args.state == "on"))
        elif cmd == "bluetooth":
            _result(dev.set_bluetooth(args.state == "on"))
        elif cmd == "airplane":
            _result(dev.set_airplane(args.state == "on"))
        elif cmd == "hotspot":
            _result(dev.set_hotspot(args.state == "on"))
        elif cmd == "mobile-data":
            _result(dev.set_mobile_data(args.state == "on"))
        elif cmd == "screen":
            ok = dev.screen_on() if args.state == "on" else dev.screen_off()
            rc = _report(ok, f"screen {args.state}", f"screen {args.state} failed")
        elif cmd == "settings":
            rc = _report(dev.open_settings(), "opened settings", "could not open settings")
        elif cmd == "unroot":
            _result(dev.unroot())
        elif cmd == "mount-rw":
            _result(dev.mount_rw(args.path))
        elif cmd == "open":
            rc = _report(dev.open_url(args.url), f"opened {args.url}", f"could not open {args.url}")
        elif cmd == "search":
            q = " ".join(_words(args.query))
            rc = _report(dev.web_search(q), f"searched {q!r}", f"could not search {q!r}")
        elif cmd == "camera":
            rc = _report(dev.open_camera(), "opened camera", "no camera app found")
        elif cmd == "gallery":
            rc = _report(dev.open_gallery(), "opened gallery", "no gallery app found")
        elif cmd == "calculator":
            rc = _report(dev.open_calculator(), "opened calculator", "no calculator app found")
        elif cmd == "close-apps":
            _result(dev.close_apps())
        elif cmd == "battery":
            if _JSON["on"]:
                print(json.dumps(dev.battery_status(), indent=2))
            else:
                print(dev.battery())
        elif cmd == "build-info":
            if args.full:
                rc = _write_report(dev.build_report(), args.output)
            elif _JSON["on"]:
                print(json.dumps(dev.build_properties(), indent=2))
            else:
                print(dev.build_info())
        elif cmd == "getprop":
            value = dev.getprop(args.name)
            if _JSON["on"]:
                print(json.dumps({args.name: value} if args.name else value, indent=2))
            elif args.name:
                print(value)
            else:
                for key in sorted(value):
                    print(f"[{key}]: [{value[key]}]")
        elif cmd == "remount":
            _result(dev.remount())
        elif cmd in ("disable-verity", "enable-verity"):
            _result(dev.disable_verity() if cmd == "disable-verity" else dev.enable_verity())
            if args.reboot:
                dev.shell("sync")
                rc = _report(dev.reboot(), "rebooting to apply it", "could not reboot")
        elif cmd == "dial":
            rc = _report(dev.dial(args.number), f"dialler opened: {args.number}", "could not open the dialler")
        elif cmd == "call":
            rc = _report(dev.call(args.number), f"calling {args.number}", "could not place the call")
        elif cmd == "end-call":
            rc = _report(dev.end_call(), "ended call", "could not end the call")
        elif cmd == "answer":
            rc = _report(dev.answer_call(), "answered", "could not answer")
        elif cmd == "call-state":
            _result(dev.call_state())
        elif cmd == "call-log":
            rows = dev.call_log(args.limit)
            if _JSON["on"]:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    print(f"{r.get('type', '?'):2} {r.get('number', ''):16} {r.get('date', '')}")
        elif cmd == "sms":
            rows = dev.sms_list(args.limit)
            if _JSON["on"]:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    print(
                        f"{r.get('type', '?'):2} {r.get('address', ''):14} "
                        f"{(r.get('body') or '')[:60]!r}"
                    )
        elif cmd == "send-sms":
            rc = _report(
                dev.send_sms(args.number, " ".join(_words(args.body))),
                f"compose opened: {args.number}",
                "could not open the SMS composer",
            )
        elif cmd == "phone-support":
            info = dev.phone_support()
            if _JSON["on"]:
                print(json.dumps(info, indent=2))
            else:
                shown = {None: "unknown", "": "none"}
                telephony = info.get("telephony")
                print(f"telephony   {'unknown' if telephony is None else ('yes' if telephony else 'no')}")
                print(f"dialler     {shown.get(info.get('dialer'), info.get('dialer'))}")
                print(f"calls       {shown.get(info.get('caller'), info.get('caller'))}")
                print(f"messages    {shown.get(info.get('messages'), info.get('messages'))}")
                print(f"phone apps  {', '.join(info.get('phone_apps') or []) or 'none'}")
        return rc

    except ADBNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except ADBError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        # bad input (ValueError from port/URL validation), serve failures
        # (RuntimeError), unreachable servers (ConnectionError is an OSError):
        # a clear one-line error instead of a traceback
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _block():
    import time

    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
