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
    """ADBConfig from the shared flags (every subcommand honours --adb-host)."""
    kwargs = dict(
        serial=getattr(args, "serial", None),
        adb_path=getattr(args, "adb_path", None),
        adb_server_host=getattr(args, "adb_host", None),
        adb_server_port=getattr(args, "adb_port", 5037),
    )
    timeout = getattr(args, "timeout", None)
    if timeout is not None:
        # only when given: passing None would DISABLE the 60 s default timeout
        kwargs["command_timeout"] = timeout
    kwargs.update(overrides)
    return ADBConfig(**kwargs)


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

    sub.add_parser("doctor", help="report whether adb/scrcpy were found")

    p_fetch = sub.add_parser("fetch-tools", help="download adb (+scrcpy) into ~/.turboadb/tools")
    p_fetch.add_argument("--adb-only", action="store_true")
    p_fetch.add_argument("--scrcpy-only", action="store_true")
    p_fetch.add_argument("--force", action="store_true", help="re-download even if already cached")

    p_upg = sub.add_parser(
        "upgrade-tools", help="check for newer adb/scrcpy and update only if newer"
    )
    p_upg.add_argument("--check", action="store_true", help="only report versions; don't download")

    p_dev = sub.add_parser("devices", help="list devices on the local (or a remote) adb server")
    _add_target(p_dev)

    p_info = sub.add_parser("info", help="connect and print device identity/build")
    _add_target(p_info)

    p_shell = sub.add_parser("shell", help="run a one-shot adb shell command")
    _add_target(p_shell)
    p_shell.add_argument("--su", action="store_true", help="wrap in su -c (rooted)")
    p_shell.add_argument("command", nargs=argparse.REMAINDER)

    p_log = sub.add_parser("logcat", help="stream logcat live, with match + save")
    _add_target(p_log)
    p_log.add_argument("--tag", default=None)
    p_log.add_argument("--priority", default=None, help="V/D/I/W/E/F")
    p_log.add_argument(
        "--buffer",
        action="append",
        default=None,
        help="logcat buffer (repeatable): main/system/crash/...",
    )
    p_log.add_argument("--format", default="threadtime", help="logcat -v format")
    p_log.add_argument("--match", default=None, help="regex to flag matching lines")
    p_log.add_argument("--save", default=None, help="tee output to this file")
    p_log.add_argument("--stop-on-match", action="store_true")
    p_log.add_argument("--clear", action="store_true", help="logcat -c first")
    p_log.add_argument("--dump", action="store_true", help="-d: dump then exit")
    p_log.add_argument(
        "--tail",
        type=int,
        default=None,
        metavar="N",
        help="start from only the last N buffered lines, then "
        "stream live (-T N) — skips the device's cached "
        "backlog, which can be 100k+ old lines",
    )

    p_clear = sub.add_parser("logcat-clear", help="clear logcat buffers (-c)")
    _add_target(p_clear)

    p_push = sub.add_parser("push", help="upload a file/dir to the device")
    _add_target(p_push)
    p_push.add_argument("local")
    p_push.add_argument("remote")

    p_pull = sub.add_parser("pull", help="download a file/dir from the device")
    _add_target(p_pull)
    p_pull.add_argument("remote")
    p_pull.add_argument("local")

    p_inst = sub.add_parser("install", help="install one or more APKs (splits ok)")
    _add_target(p_inst)
    p_inst.add_argument("apks", nargs="+")
    p_inst.add_argument("--no-replace", action="store_true")
    p_inst.add_argument("--downgrade", action="store_true")
    p_inst.add_argument("--grant", action="store_true", help="grant all perms")

    p_uni = sub.add_parser("uninstall", help="uninstall a package")
    _add_target(p_uni)
    p_uni.add_argument("package")
    p_uni.add_argument("--keep-data", action="store_true")

    p_pkg = sub.add_parser("packages", help="list installed packages")
    _add_target(p_pkg)
    p_pkg.add_argument("--third-party", action="store_true")
    p_pkg.add_argument("--system", action="store_true")
    p_pkg.add_argument("filter", nargs="?", default=None)

    p_clr = sub.add_parser("clear", help="clear an app's data (pm clear)")
    _add_target(p_clr)
    p_clr.add_argument("package")

    p_start = sub.add_parser("start", help="launch an app by package")
    _add_target(p_start)
    p_start.add_argument("package")

    p_stop = sub.add_parser("stop", help="force-stop an app")
    _add_target(p_stop)
    p_stop.add_argument("package")

    p_shot = sub.add_parser("screenshot", help="capture a PNG screenshot")
    _add_target(p_shot)
    p_shot.add_argument("path")

    p_rec = sub.add_parser("record", help="record the screen, then pull it (Ctrl+C stops early)")
    _add_target(p_rec)
    p_rec.add_argument("path")
    p_rec.add_argument("--time-limit", type=int, default=30)
    p_rec.add_argument("--size", default=None, help="e.g. 1280x720")
    p_rec.add_argument("--bit-rate", default=None, help="e.g. 8M")

    p_fwd = sub.add_parser("forward", help="adb forward LOCAL REMOTE (stays until you stop)")
    _add_target(p_fwd)
    p_fwd.add_argument("local")
    p_fwd.add_argument("remote")

    p_rev = sub.add_parser("reverse", help="adb reverse REMOTE LOCAL")
    _add_target(p_rev)
    p_rev.add_argument("remote")
    p_rev.add_argument("local")

    p_scr = sub.add_parser("scrcpy", help="launch scrcpy screen mirroring")
    _add_target(p_scr)
    p_scr.add_argument("--max-size", type=int, default=None)
    p_scr.add_argument("--bit-rate", default=None)
    p_scr.add_argument("--max-fps", type=int, default=None)
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
    p_scr.add_argument("--turn-screen-off", action="store_true")
    p_scr.add_argument("--no-control", action="store_true")
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

    p_conn = sub.add_parser("connect", help="adb connect host:port")
    _add_target(p_conn)
    p_conn.add_argument("hostport")

    p_rs = sub.add_parser(
        "restart-server",
        help="kill + start the adb server (fixes 'device not visible' from adb version mismatches)",
    )
    _add_target(p_rs)

    p_serve = sub.add_parser(
        "serve",
        help="expose THIS PC's adb server to the network so other machines can "
        "drive its devices (auto 'adb -a nodaemon server start')",
    )
    _add_target(p_serve)
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

    p_disc = sub.add_parser("disconnect", help="adb disconnect host:port")
    _add_target(p_disc)
    p_disc.add_argument("hostport", nargs="?", default=None)

    p_tcp = sub.add_parser("tcpip", help="restart adbd in TCP mode on a USB device")
    _add_target(p_tcp)
    p_tcp.add_argument("port", type=int, nargs="?", default=5555)

    p_wless = sub.add_parser(
        "wireless",
        help="one-shot USB -> Wi-Fi: read the device's IP, adb tcpip, then "
        "connect (afterwards the cable can be unplugged)",
    )
    _add_target(p_wless)
    p_wless.add_argument("port", type=int, nargs="?", default=5555)

    p_discover = sub.add_parser(
        "discover",
        help="find Android 11+ Wireless-debugging devices on the LAN (adb mdns services)",
    )
    _add_target(p_discover)

    p_pair = sub.add_parser("pair", help="pair with an Android 11+ device")
    _add_target(p_pair)
    p_pair.add_argument("hostport", help="host:pairing_port")
    p_pair.add_argument("code", help="6-digit pairing code")

    p_reb = sub.add_parser("reboot", help="reboot the device")
    _add_target(p_reb)
    p_reb.add_argument(
        "mode", nargs="?", default=None, choices=[None, "recovery", "bootloader", "sideload"]
    )

    p_root = sub.add_parser("root", help="restart adbd as root")
    _add_target(p_root)

    # --- device controls (parity with the GUI Controls tab) ---
    p_key = sub.add_parser(
        "key", help="send a key by name or code (home/back/recents/vol_up/play_pause/…)"
    )
    _add_target(p_key)
    p_key.add_argument("key")
    p_text = sub.add_parser("text", help="type text into the focused field")
    _add_target(p_text)
    p_text.add_argument("words", nargs=argparse.REMAINDER)
    p_scroll = sub.add_parser("scroll", help="scroll the screen by swipe")
    _add_target(p_scroll)
    p_scroll.add_argument("direction", choices=["up", "down", "left", "right"])
    p_tap = sub.add_parser("tap", help="tap the centre of the screen")
    _add_target(p_tap)
    p_media = sub.add_parser("media", help="media control (play-pause/next/…)")
    _add_target(p_media)
    p_media.add_argument("action")
    p_bri = sub.add_parser("brightness", help="set brightness 0.0-1.0 (live)")
    _add_target(p_bri)
    p_bri.add_argument("fraction", type=float)
    p_wifi = sub.add_parser("wifi", help="wifi on|off")
    _add_target(p_wifi)
    p_wifi.add_argument("state", choices=["on", "off"])
    p_bt = sub.add_parser("bluetooth", help="bluetooth on|off")
    _add_target(p_bt)
    p_bt.add_argument("state", choices=["on", "off"])
    p_air = sub.add_parser("airplane", help="airplane mode on|off")
    _add_target(p_air)
    p_air.add_argument("state", choices=["on", "off"])
    p_hot = sub.add_parser("hotspot", help="mobile hotspot on|off (best-effort)")
    _add_target(p_hot)
    p_hot.add_argument("state", choices=["on", "off"])
    p_scr2 = sub.add_parser("screen", help="turn the device screen on|off")
    _add_target(p_scr2)
    p_scr2.add_argument("state", choices=["on", "off"])
    p_set = sub.add_parser("settings", help="open the Settings app")
    _add_target(p_set)
    p_unroot = sub.add_parser("unroot", help="restart adbd WITHOUT root")
    _add_target(p_unroot)
    p_mrw = sub.add_parser("mount-rw", help="mount / read-write (remount,rw /)")
    _add_target(p_mrw)
    p_open = sub.add_parser("open", help="open a URL (VIEW intent)")
    _add_target(p_open)
    p_open.add_argument("url")
    p_search = sub.add_parser("search", help="web-search in the browser")
    _add_target(p_search)
    p_search.add_argument("query", nargs=argparse.REMAINDER)
    p_cam = sub.add_parser("camera", help="open the camera app")
    _add_target(p_cam)
    p_gal = sub.add_parser("gallery", help="open the gallery / photos app")
    _add_target(p_gal)
    p_calc = sub.add_parser("calculator", help="open the calculator app")
    _add_target(p_calc)
    p_close = sub.add_parser("close-apps", help="close background apps")
    _add_target(p_close)
    p_bat = sub.add_parser("battery", help="battery stats (dumpsys battery)")
    _add_target(p_bat)
    p_health = sub.add_parser(
        "health", help="one-shot health snapshot (battery/temp/memory/cpu/uptime)"
    )
    _add_target(p_health)
    p_bug = sub.add_parser("bugreport", help="capture a full adb bugreport (slow — a few minutes)")
    _add_target(p_bug)
    p_bug.add_argument(
        "path", nargs="?", default=None, help="output file (default: bugreport-<time>.zip)"
    )
    p_bld = sub.add_parser("build-info", help="build / version properties")
    _add_target(p_bld)
    p_rm = sub.add_parser("remount", help="adb remount read-write")
    _add_target(p_rm)
    p_dv = sub.add_parser("disable-verity", help="adb disable-verity")
    _add_target(p_dv)
    p_ev = sub.add_parser("enable-verity", help="adb enable-verity")
    _add_target(p_ev)

    # --- telephony / messaging ---
    p_dial = sub.add_parser("dial", help="open the dialler with a number")
    _add_target(p_dial)
    p_dial.add_argument("number")
    p_call = sub.add_parser("call", help="place a call")
    _add_target(p_call)
    p_call.add_argument("number")
    p_endc = sub.add_parser("end-call", help="end the current call")
    _add_target(p_endc)
    p_ans = sub.add_parser("answer", help="answer an incoming call")
    _add_target(p_ans)
    p_clog = sub.add_parser("call-log", help="recent calls")
    _add_target(p_clog)
    p_clog.add_argument("--limit", type=int, default=20)
    p_sms = sub.add_parser("sms", help="recent SMS messages")
    _add_target(p_sms)
    p_sms.add_argument("--limit", type=int, default=20)
    p_ssms = sub.add_parser("send-sms", help="compose an SMS (opens the app)")
    _add_target(p_ssms)
    p_ssms.add_argument("number")
    p_ssms.add_argument("body", nargs=argparse.REMAINDER)

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
    p_deploy.add_argument("--winrm-port", type=int, default=5985)
    p_deploy.add_argument(
        "--no-update", action="store_true", help="don't pip-upgrade turboadb on the host first"
    )
    p_deploy.add_argument(
        "--test", action="store_true", help="only test WinRM + credentials; don't deploy"
    )

    sub.add_parser("gui", help="launch the desktop GUI")
    sub.add_parser("self-update", help="upgrade TurboADB itself (pip) + adb/scrcpy to the latest")
    sub.add_parser("shortcut", help="create Desktop + Start-menu shortcuts to the GUI")
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


def _report(ok, ok_msg: str, fail_msg: str) -> int:
    """Print the outcome of a bool-returning action and give its exit code."""
    if ok:
        print(ok_msg)
        return 0
    print(fail_msg, file=sys.stderr)
    return 1


def _record(dev, args) -> int:
    """`record`: Ctrl+C stops the recording EARLY and still pulls a playable
    MP4, instead of aborting and leaving a partial file behind."""
    import threading

    stop = threading.Event()
    result = {}

    def work():
        try:
            result["path"] = dev.screen_record(
                args.path,
                time_limit=args.time_limit,
                size=args.size,
                bit_rate=args.bit_rate,
                stop_event=stop,
            )
        except BaseException as exc:  # re-raised on the main thread
            result["error"] = exc

    worker = threading.Thread(target=work, name="turboadb-record", daemon=True)
    worker.start()
    print("Recording… press Ctrl+C to stop early.", file=sys.stderr)
    try:
        while worker.is_alive():
            worker.join(0.2)
    except KeyboardInterrupt:
        print("\nStopping the recording and saving it…", file=sys.stderr)
        stop.set()
        worker.join()  # a second Ctrl+C aborts outright
    if "error" in result:
        raise result["error"]
    print(f"Saved {result['path']}")
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
                winrm_port=args.winrm_port,
                test_only=args.test,
                on_status=lambda m: print(m),
            )

        if cmd == "self-update":
            from . import update as _upd

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
                print(
                    f"\n[{len(found)} service(s)]  ·  connect ones are ready "
                    f"for:  turboadb connect HOST:PORT",
                    file=sys.stderr,
                )
            return 0

        if cmd == "restart-server":
            r = _handler(args, serial=None).restart_server()
            print(r.text or r.stderr.strip() or "ADB server restarted.")
            return 0

        if cmd == "serve":
            from .devices import (
                start_shared_server,
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
            print(_handler(args, serial=None).connect_tcp(host, port))
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

        # everything below operates on a connected handler
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
                no_audio=args.no_audio,
                audio_source=args.audio_source,
                audio_codec=args.audio_codec,
                audio_bit_rate=args.audio_bit_rate,
                audio_buffer=args.audio_buffer,
                audio_output_buffer=args.audio_output_buffer,
                audio_dup=args.audio_dup,
                record=args.record,
                turn_screen_off=args.turn_screen_off,
                no_control=args.no_control,
                video_source=args.video_source,
                camera_facing=args.camera_facing,
                camera_size=args.camera_size,
            )
            sess = dev.mirror(opts)
            print(f"scrcpy launched (pid {sess.pid}). Close its window to end.")
            if args.wait:
                return sess.wait() or 0
            return 0

        dev.connect()
        rc = 0

        if cmd == "info":
            _output(args, dev.device_info())
        elif cmd == "shell":
            command = " ".join(_words(args.command)).strip()
            if not command:
                print("No command given.", file=sys.stderr)
                return 2
            res = dev.shell(command, su=args.su)
            if not args.json:
                if res.stdout:
                    sys.stdout.write(res.stdout if res.stdout.endswith("\n") else res.stdout + "\n")
                if res.stderr:
                    sys.stderr.write(res.stderr)
                return res.exit_code
            _output(args, res)
            return res.exit_code
        elif cmd == "logcat":
            print("Streaming logcat (Ctrl+C to stop)…", file=sys.stderr)
            try:
                res = dev.logcat(
                    tag=args.tag,
                    priority=args.priority,
                    buffers=args.buffer,
                    fmt=args.format,
                    match=args.match,
                    save_to=args.save,
                    stop_on_match=args.stop_on_match,
                    clear_first=args.clear,
                    dump=args.dump,
                    tail=args.tail,
                    on_line=print,
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
        elif cmd == "install":
            if len(args.apks) > 1:
                print(
                    dev.install_multiple(
                        args.apks,
                        replace=not args.no_replace,
                        downgrade=args.downgrade,
                        grant_perms=args.grant,
                    )
                )
            else:
                print(
                    dev.install(
                        args.apks[0],
                        replace=not args.no_replace,
                        downgrade=args.downgrade,
                        grant_perms=args.grant,
                    )
                )
        elif cmd == "uninstall":
            print(dev.uninstall(args.package, keep_data=args.keep_data))
        elif cmd == "packages":
            pkgs = dev.list_packages(
                filter_text=args.filter, third_party=args.third_party, system=args.system
            )
            if args.json:
                print(json.dumps(pkgs, indent=2))
            else:
                for p in pkgs:
                    print(p)
                print(f"\n[{len(pkgs)} packages]", file=sys.stderr)
        elif cmd == "clear":
            print(dev.clear_app(args.package))
        elif cmd == "start":
            rc = _report(dev.start_app(args.package), "launched", f"could not launch {args.package}")
        elif cmd == "stop":
            rc = _report(dev.stop_app(args.package), "stopped", f"could not stop {args.package}")
        elif cmd == "screenshot":
            print(f"Saved {dev.screenshot(args.path)}")
        elif cmd == "record":
            rc = _record(dev, args)
        elif cmd == "forward":
            fwd = dev.forward(args.local, args.remote)
            print(f"{fwd}\nForward active. Ctrl+C to remove it.")
            try:
                _block()
            except KeyboardInterrupt:
                rc = _report(fwd.close(), "\nforward removed.", "\nERROR: could not remove the forward")
        elif cmd == "reverse":
            rev = dev.reverse(args.remote, args.local)
            print(f"{rev}\nReverse active. Ctrl+C to remove it.")
            try:
                _block()
            except KeyboardInterrupt:
                rc = _report(rev.close(), "\nreverse removed.", "\nERROR: could not remove the reverse")
        elif cmd == "tcpip":
            print(dev.tcpip(args.port))
        elif cmd == "wireless":
            serial = dev.go_wireless(args.port)
            print(f"connected wirelessly as {serial} - the USB cable can be unplugged now")
        elif cmd == "health":
            print(dev.health_text())
        elif cmd == "bugreport":
            import time as _t

            out = args.path or _t.strftime("bugreport-%Y%m%d-%H%M%S.zip")
            print("Capturing bugreport (takes a few minutes)…", file=sys.stderr)
            print(f"Saved {dev.bugreport(out)}")
        elif cmd == "reboot":
            label = f"reboot {args.mode or ''}".strip()
            rc = _report(dev.reboot(args.mode), label, f"{label} failed")
        elif cmd == "root":
            print(dev.root())
        elif cmd == "key":
            rc = _report(dev.keyevent(args.key), "ok", f"key {args.key} failed")
        elif cmd == "text":
            rc = _report(dev.input_text(" ".join(_words(args.words))), "typed", "typing failed")
        elif cmd == "scroll":
            rc = _report(
                dev.scroll(args.direction), f"scrolled {args.direction}", "scroll failed"
            )
        elif cmd == "tap":
            rc = _report(dev.tap_center(), "tapped", "tap failed")
        elif cmd == "media":
            rc = _report(dev.media(args.action), f"media {args.action}", f"media {args.action} failed")
        elif cmd == "brightness":
            rc = _report(
                dev.display_brightness(args.fraction),
                f"brightness {args.fraction}",
                "could not set brightness",
            )
        elif cmd == "wifi":
            print(dev.set_wifi(args.state == "on"))
        elif cmd == "bluetooth":
            print(dev.set_bluetooth(args.state == "on"))
        elif cmd == "airplane":
            print(dev.set_airplane(args.state == "on"))
        elif cmd == "hotspot":
            print(dev.set_hotspot(args.state == "on"))
        elif cmd == "screen":
            ok = dev.screen_on() if args.state == "on" else dev.screen_off()
            rc = _report(ok, f"screen {args.state}", f"screen {args.state} failed")
        elif cmd == "settings":
            rc = _report(dev.open_settings(), "opened settings", "could not open settings")
        elif cmd == "unroot":
            print(dev.unroot())
        elif cmd == "mount-rw":
            print(dev.mount_rw())
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
            print(dev.close_apps())
        elif cmd == "battery":
            print(dev.battery())
        elif cmd == "build-info":
            print(dev.build_info())
        elif cmd == "remount":
            print(dev.remount())
        elif cmd == "disable-verity":
            print(dev.disable_verity())
        elif cmd == "enable-verity":
            print(dev.enable_verity())
        elif cmd == "dial":
            rc = _report(dev.dial(args.number), f"dialler opened: {args.number}", "could not open the dialler")
        elif cmd == "call":
            rc = _report(dev.call(args.number), f"calling {args.number}", "could not place the call")
        elif cmd == "end-call":
            rc = _report(dev.end_call(), "ended call", "could not end the call")
        elif cmd == "answer":
            rc = _report(dev.answer_call(), "answered", "could not answer")
        elif cmd == "call-log":
            for r in dev.call_log(args.limit):
                print(f"{r.get('type', '?'):2} {r.get('number', ''):16} {r.get('date', '')}")
        elif cmd == "sms":
            for r in dev.sms_list(args.limit):
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
