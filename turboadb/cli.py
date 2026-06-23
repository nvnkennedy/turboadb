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
``-s`` for the only device, or pass its serial.
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import subprocess

from .config import ADBConfig, ScrcpyOptions
from .core import ADBHandler
from .devices import list_devices
from .results import CommandResult, TransferResult, StreamResult
from .exceptions import ADBError, ADBNotFoundError
from .tools import NO_WINDOW

DOCS_URL = "https://pypi.org/project/turboadb/"


# --------------------------------------------------------------------------- #
# console-script helpers (entry points)
# --------------------------------------------------------------------------- #
def _here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _gui_exe_path() -> str:
    return os.path.join(_here(), "bin", "turboadb-gui.exe")


def _icon_path() -> str:
    return os.path.join(_here(), "assets", "icon.ico")


def open_docs(argv=None) -> int:
    """`turboadb-docs`: open the rendered docs, falling back to the bundled README."""
    import webbrowser
    try:
        if webbrowser.open(DOCS_URL):
            print(f"Opened docs: {DOCS_URL}")
            return 0
    except Exception:
        pass
    readme = os.path.join(_here(), "README.md")
    if os.path.exists(readme):
        webbrowser.open("file://" + readme)
        print(f"Opened bundled README: {readme}")
    else:
        print(f"Docs: {DOCS_URL}")
    return 0


def _resolve_gui_target():
    """The best way to launch the GUI from a shortcut: prefer the windowless
    pip ``turboadb-gui`` launcher (no console window, and it always runs the
    LATEST installed code so upgrades take effect), then ``pythonw -m turboadb
    gui``, and finally the bundled one-file exe."""
    import shutil
    if getattr(sys, "frozen", False):
        # running the bundled one-file exe: the exe the user launched is the
        # stable launcher (NOT the ephemeral _MEIPASS copy)
        return sys.executable, "", os.path.dirname(sys.executable)
    launcher = shutil.which("turboadb-gui")
    if launcher and launcher.lower().endswith(".exe"):
        return launcher, "", os.path.dirname(launcher)
    pydir = os.path.dirname(sys.executable)
    for base in (os.path.join(pydir, "Scripts"), pydir):
        cand = os.path.join(base, "turboadb-gui.exe")
        if os.path.exists(cand):
            return cand, "", base
    pyw = os.path.join(pydir, "pythonw.exe")
    if os.path.exists(pyw):
        return pyw, "-m turboadb gui", pydir
    exe = _gui_exe_path()
    if os.path.exists(exe):
        return exe, "", os.path.dirname(exe)
    return sys.executable, "-m turboadb gui", pydir


def _write_shortcut(folder_expr: str, name: str) -> bool:
    """Create ``<folder>/<name>.lnk`` pointing at the GUI, with our icon. The
    *folder_expr* is a PowerShell expression yielding the target directory (e.g.
    ``[Environment]::GetFolderPath('Desktop')``)."""
    if os.name != "nt":
        return False
    target, args, workdir = _resolve_gui_target()
    icon = _icon_path()
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$dir = {folder_expr}; "
        "if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }; "
        f"$lnk = $ws.CreateShortcut([IO.Path]::Combine($dir, '{name}.lnk')); "
        f"$lnk.TargetPath = '{target}'; "
        + (f"$lnk.Arguments = '{args}'; " if args else "")
        + f"$lnk.WorkingDirectory = '{workdir}'; "
        + (f"$lnk.IconLocation = '{icon}'; " if os.path.exists(icon) else "")
        + "$lnk.Description = 'TurboADB - Android ADB + scrcpy toolkit'; "
        "$lnk.Save()"
    )
    try:
        # CREATE_NO_WINDOW: never flash a console window (this runs at every
        # launch, so without it two PowerShell consoles blink on screen)
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-Command", ps],
                       check=True, capture_output=True, timeout=30,
                       creationflags=NO_WINDOW)
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
        desktop = _windows_folder(0x10)        # CSIDL_DESKTOPDIRECTORY
    except Exception:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    try:
        programs = _windows_folder(0x02)       # CSIDL_PROGRAMS
    except Exception:
        appdata = os.environ.get("APPDATA", "")
        programs = os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                                "Programs")
    return [("desktop", os.path.join(desktop, f"{name}.lnk"),
             create_desktop_shortcut),
            ("start menu", os.path.join(programs, f"{name}.lnk"),
             create_start_menu_shortcut)]


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
                out[loc] = landed            # newly created (True) or failed (False)
            elif not landed:
                out[loc] = False             # was present but the refresh lost it
    return out


def create_shortcut(argv=None) -> int:
    """`turboadb-shortcut`: (re)create the Desktop + Start-menu shortcuts."""
    if os.name != "nt":
        print("Shortcut creation is Windows-only.", file=sys.stderr)
        return 2
    res = ensure_shortcuts(force=True)
    ok = res.get("desktop") or res.get("start menu")
    if ok:
        where = " and ".join(k for k, v in res.items() if v)
        print(f"Created the 'TurboADB' shortcut ({where}).")
        return 0
    print("Could not create the shortcut.", file=sys.stderr)
    return 1


def _staged_exe(src: str) -> str:
    """Return a temp COPY of the bundled exe to actually run.

    Why: a running .exe is locked by Windows, so if ``turboadb-gui`` ran the
    installed exe in place, ``pip install --upgrade`` could not replace it and
    you'd be stuck on stale code. By running a copy from %TEMP%, the installed
    exe is never locked and upgrades always succeed. The temp name includes the
    version+size, so a new build lands in a new file (no clash with a copy that
    might still be running)."""
    import tempfile
    import shutil
    try:
        from . import __version__ as ver
    except Exception:
        ver = "x"
    try:
        sig = f"{ver}-{os.path.getsize(src)}"
        dst = os.path.join(tempfile.gettempdir(), f"turboadb-gui-{sig}.exe")
        if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
            shutil.copy2(src, dst)
        return dst
    except Exception:
        return src                      # fall back to running in place


def launch_gui(argv=None) -> int:
    """`turboadb-gui`: launch the PyQt5 app.

    Runs the GUI from the **installed Python package** first, so that
    ``pip install --upgrade turboadb`` ALWAYS takes effect on the next launch.
    Falls back to the bundled Windows exe (run from a temp copy, so the installed
    one is never locked and upgrades never get blocked) when PyQt5 can't be
    imported (e.g. a platform with no PyQt5 wheel)."""
    try:
        import PyQt5  # noqa: F401
        from .gui.app import main as gui_main
        return gui_main()
    except ImportError:
        pass
    exe = _gui_exe_path()
    if os.name == "nt" and os.path.exists(exe):
        args = list(argv) if argv is not None else sys.argv[1:]
        return subprocess.call([_staged_exe(exe)] + args)
    print("The GUI needs PyQt5 (or the bundled Windows exe). Install it with:  "
          "pip install \"turboadb[gui]\".", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- #
# argument plumbing
# --------------------------------------------------------------------------- #
def _add_target(p: argparse.ArgumentParser) -> None:
    p.add_argument("-s", "--serial", default=None,
                   help="device serial, or host:port for a network device")
    p.add_argument("--adb-path", default=None, help="path to the adb executable")
    p.add_argument("--adb-host", default=None,
                   help="remote adb server host — drive a device plugged into "
                        "another machine (that machine: adb -a nodaemon server start)")
    p.add_argument("--adb-port", type=int, default=5037,
                   help="remote adb server port (default 5037)")
    p.add_argument("--timeout", type=float, default=None,
                   help="per-command timeout (seconds)")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def _handler(args) -> ADBHandler:
    cfg = ADBConfig(serial=args.serial, adb_path=getattr(args, "adb_path", None),
                    command_timeout=getattr(args, "timeout", None),
                    adb_server_host=getattr(args, "adb_host", None),
                    adb_server_port=getattr(args, "adb_port", 5037))
    return ADBHandler(cfg)


def _output(args, obj) -> None:
    if getattr(args, "json", False):
        if isinstance(obj, (CommandResult, TransferResult)):
            print(json.dumps(obj.as_dict(), default=str, indent=2))
        elif isinstance(obj, StreamResult):
            print(json.dumps({"lines": obj.lines, "matches": obj.matches,
                              "saved_to": obj.saved_to}, default=str, indent=2))
        else:
            print(json.dumps(obj, default=str, indent=2))
    else:
        print(obj)


def build_parser() -> argparse.ArgumentParser:
    from . import __version__
    parser = argparse.ArgumentParser(
        prog="turboadb",
        description="Android ADB + scrcpy device toolkit (automotive & general).")
    parser.add_argument("-V", "--version", action="version",
                        version=f"turboadb {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="report whether adb/scrcpy were found")

    p_fetch = sub.add_parser("fetch-tools",
                             help="download adb (+scrcpy) into ~/.turboadb/tools")
    p_fetch.add_argument("--adb-only", action="store_true")
    p_fetch.add_argument("--scrcpy-only", action="store_true")
    p_fetch.add_argument("--force", action="store_true",
                         help="re-download even if already cached")

    p_upg = sub.add_parser("upgrade-tools",
                           help="check for newer adb/scrcpy and update only if newer")
    p_upg.add_argument("--check", action="store_true",
                       help="only report versions; don't download")

    p_dev = sub.add_parser("devices", help="list devices on the local (or a "
                                           "remote) adb server")
    p_dev.add_argument("--adb-path", default=None)
    p_dev.add_argument("--adb-host", default=None,
                       help="list devices on a remote machine's adb server")
    p_dev.add_argument("--adb-port", type=int, default=5037)
    p_dev.add_argument("--json", action="store_true")

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
    p_log.add_argument("--buffer", action="append", default=None,
                       help="logcat buffer (repeatable): main/system/crash/...")
    p_log.add_argument("--format", default="threadtime", help="logcat -v format")
    p_log.add_argument("--match", default=None, help="regex to flag matching lines")
    p_log.add_argument("--save", default=None, help="tee output to this file")
    p_log.add_argument("--stop-on-match", action="store_true")
    p_log.add_argument("--clear", action="store_true", help="logcat -c first")
    p_log.add_argument("--dump", action="store_true", help="-d: dump then exit")

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

    p_rec = sub.add_parser("record", help="record the screen, then pull it")
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
    p_scr.add_argument("--record", default=None, help="record mirror to FILE")
    p_scr.add_argument("--turn-screen-off", action="store_true")
    p_scr.add_argument("--no-control", action="store_true")
    p_scr.add_argument("--wait", action="store_true",
                       help="block until the scrcpy window is closed")

    p_conn = sub.add_parser("connect", help="adb connect host:port")
    p_conn.add_argument("hostport")
    p_conn.add_argument("--adb-path", default=None)

    p_rs = sub.add_parser("restart-server",
                          help="kill + start the adb server (fixes 'device not "
                               "visible' from adb version mismatches)")
    p_rs.add_argument("--adb-path", default=None)

    p_serve = sub.add_parser(
        "serve",
        help="expose THIS PC's adb server to the network so other machines can "
             "drive its devices (auto 'adb -a nodaemon server start')")
    p_serve.add_argument("--port", type=int, default=5037,
                         help="adb server port (default 5037)")
    p_serve.add_argument("--adb-path", default=None)
    p_serve.add_argument("--install-startup", action="store_true",
                         help="also run automatically at every Windows login, so "
                              "it never has to be started by hand again")
    p_serve.add_argument("--startup-task", action="store_true",
                         help="register a SYSTEM startup Scheduled Task (headless, "
                              "survives logoff — best for remote/RDP hosts) and "
                              "start it now")
    p_serve.add_argument("--uninstall-startup", action="store_true",
                         help="remove the login auto-start launcher + startup task")

    p_disc = sub.add_parser("disconnect", help="adb disconnect host:port")
    p_disc.add_argument("hostport", nargs="?", default=None)
    p_disc.add_argument("--adb-path", default=None)

    p_tcp = sub.add_parser("tcpip", help="restart adbd in TCP mode on a USB device")
    _add_target(p_tcp)
    p_tcp.add_argument("port", type=int, nargs="?", default=5555)

    p_pair = sub.add_parser("pair", help="pair with an Android 11+ device")
    p_pair.add_argument("hostport", help="host:pairing_port")
    p_pair.add_argument("code", help="6-digit pairing code")
    p_pair.add_argument("--adb-path", default=None)

    p_reb = sub.add_parser("reboot", help="reboot the device")
    _add_target(p_reb)
    p_reb.add_argument("mode", nargs="?", default=None,
                       choices=[None, "recovery", "bootloader", "sideload"])

    p_root = sub.add_parser("root", help="restart adbd as root")
    _add_target(p_root)

    # --- device controls (parity with the GUI Controls tab) ---
    p_key = sub.add_parser("key", help="send a key by name or code "
                                       "(home/back/recents/vol_up/play_pause/…)")
    _add_target(p_key); p_key.add_argument("key")
    p_text = sub.add_parser("text", help="type text into the focused field")
    _add_target(p_text); p_text.add_argument("words", nargs=argparse.REMAINDER)
    p_scroll = sub.add_parser("scroll", help="scroll the screen by swipe")
    _add_target(p_scroll)
    p_scroll.add_argument("direction", choices=["up", "down", "left", "right"])
    p_tap = sub.add_parser("tap", help="tap the centre of the screen")
    _add_target(p_tap)
    p_media = sub.add_parser("media", help="media control (play-pause/next/…)")
    _add_target(p_media); p_media.add_argument("action")
    p_bri = sub.add_parser("brightness", help="set brightness 0.0-1.0 (live)")
    _add_target(p_bri); p_bri.add_argument("fraction", type=float)
    p_wifi = sub.add_parser("wifi", help="wifi on|off")
    _add_target(p_wifi); p_wifi.add_argument("state", choices=["on", "off"])
    p_bt = sub.add_parser("bluetooth", help="bluetooth on|off")
    _add_target(p_bt); p_bt.add_argument("state", choices=["on", "off"])
    p_air = sub.add_parser("airplane", help="airplane mode on|off")
    _add_target(p_air); p_air.add_argument("state", choices=["on", "off"])
    p_hot = sub.add_parser("hotspot", help="mobile hotspot on|off (best-effort)")
    _add_target(p_hot); p_hot.add_argument("state", choices=["on", "off"])
    p_scr2 = sub.add_parser("screen", help="turn the device screen on|off")
    _add_target(p_scr2); p_scr2.add_argument("state", choices=["on", "off"])
    p_set = sub.add_parser("settings", help="open the Settings app")
    _add_target(p_set)
    p_unroot = sub.add_parser("unroot", help="restart adbd WITHOUT root")
    _add_target(p_unroot)
    p_mrw = sub.add_parser("mount-rw", help="mount / read-write (remount,rw /)")
    _add_target(p_mrw)
    p_open = sub.add_parser("open", help="open a URL (VIEW intent)")
    _add_target(p_open); p_open.add_argument("url")
    p_search = sub.add_parser("search", help="web-search in the browser")
    _add_target(p_search); p_search.add_argument("query", nargs=argparse.REMAINDER)
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
    _add_target(p_dial); p_dial.add_argument("number")
    p_call = sub.add_parser("call", help="place a call")
    _add_target(p_call); p_call.add_argument("number")
    p_endc = sub.add_parser("end-call", help="end the current call"); _add_target(p_endc)
    p_ans = sub.add_parser("answer", help="answer an incoming call"); _add_target(p_ans)
    p_clog = sub.add_parser("call-log", help="recent calls")
    _add_target(p_clog); p_clog.add_argument("--limit", type=int, default=20)
    p_sms = sub.add_parser("sms", help="recent SMS messages")
    _add_target(p_sms); p_sms.add_argument("--limit", type=int, default=20)
    p_ssms = sub.add_parser("send-sms", help="compose an SMS (opens the app)")
    _add_target(p_ssms); p_ssms.add_argument("number")
    p_ssms.add_argument("body", nargs=argparse.REMAINDER)

    p_deploy = sub.add_parser(
        "deploy-serve",
        help="install/start 'turboadb serve' on remote Windows host(s) over WinRM "
             "(NTLM) — the same as the GUI 'ADB Server' button")
    p_deploy.add_argument("hosts", nargs="+", help="remote hostname(s) or IP(s)")
    p_deploy.add_argument("-u", "--user", required=True,
                          help="admin login, DOMAIN\\user (local admin on targets)")
    p_deploy.add_argument("-p", "--password", default=None,
                          help="password (omit to be prompted securely)")
    p_deploy.add_argument("--port", type=int, default=5037,
                          help="adb server port on the targets (default 5037)")
    p_deploy.add_argument("--winrm-port", type=int, default=5985)
    p_deploy.add_argument("--no-update", action="store_true",
                          help="don't pip-upgrade turboadb on the host first")
    p_deploy.add_argument("--test", action="store_true",
                          help="only test WinRM + credentials; don't deploy")

    sub.add_parser("gui", help="launch the desktop GUI")
    sub.add_parser("self-update",
                   help="upgrade TurboADB itself (pip) + adb/scrcpy to the latest")
    sub.add_parser("shortcut",
                   help="create Desktop + Start-menu shortcuts to the GUI")
    return parser


# --------------------------------------------------------------------------- #
# command dispatch
# --------------------------------------------------------------------------- #
def _ensure_progress(pct):
    sys.stderr.write(f"\r  {pct:3d}%")
    sys.stderr.flush()
    if pct >= 100:
        sys.stderr.write("\n")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.cmd

    # one-time auto-fetch of latest adb/scrcpy after an install/upgrade
    # (skipped for doctor/fetch-tools/upgrade-tools, and when TURBOADB_AUTO_FETCH=0)
    if cmd not in ("doctor", "fetch-tools", "upgrade-tools", "self-update",
                   "shortcut", "deploy-serve"):
        try:
            from . import toolsdl
            toolsdl.ensure_tools(notify=lambda m: print(m, file=sys.stderr),
                                 on_progress=_ensure_progress)
        except Exception:
            pass

    try:
        if cmd == "doctor":
            from . import tools
            d = tools.diagnose()
            print(f"adb    : {d['adb'] or 'NOT FOUND'}")
            print(f"         {d['adb_path'] or tools.ADB_DOWNLOAD}")
            print(f"scrcpy : {'found at ' + d['scrcpy_path'] if d['scrcpy'] else 'NOT FOUND'}")
            if not d['scrcpy']:
                print(f"         {tools.SCRCPY_DOWNLOAD}")
            if not d["adb"] or not d["scrcpy"]:
                print("\nTip: run  turboadb fetch-tools  to download what's missing "
                      "into ~/.turboadb/tools")
            return 0 if d["adb"] else 1

        if cmd == "fetch-tools":
            from . import toolsdl
            want_adb = not args.scrcpy_only
            want_scrcpy = not args.adb_only
            print(f"Downloading into {toolsdl.tools_dir()} …", file=sys.stderr)

            def _prog(pct):
                sys.stderr.write(f"\r  {pct:3d}%")
                sys.stderr.flush()
                if pct >= 100:
                    sys.stderr.write("\n")
            res = toolsdl.fetch_tools(adb=want_adb, scrcpy=want_scrcpy,
                                      force=args.force, on_progress=_prog)
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
            for tool in ("adb", "scrcpy"):
                c = checks[tool]
                state = ("up to date" if c["upgrade"] is False else
                         "UPDATE AVAILABLE" if c["upgrade"] else "unknown")
                print(f"{tool:7}: installed={c['installed']}  "
                      f"latest={c['latest']}  -> {state}")
            if args.check:
                return 0
            if not (checks["adb"]["upgrade"] or checks["scrcpy"]["upgrade"]):
                print("\nEverything is current — nothing to download.")
                return 0
            res = toolsdl.upgrade_tools(
                notify=lambda m: print(m, file=sys.stderr),
                on_progress=_ensure_progress)
            for tool, path in res.get("updated", {}).items():
                print(f"updated {tool} -> {path}")
            for tool, err in res.get("errors", {}).items():
                print(f"{tool}: {err}", file=sys.stderr)
            return 0

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
                args.hosts, args.user, pw, update=not args.no_update,
                port=args.port, winrm_port=args.winrm_port,
                test_only=args.test, on_status=lambda m: print(m))

        if cmd == "self-update":
            from . import update as _upd
            if not _upd.can_self_update():
                print("Running the bundled exe — upgrade with: "
                      "pip install --upgrade turboadb", file=sys.stderr)
                return 1
            latest = _upd.pypi_latest()
            if latest and not _upd.is_newer(latest):
                print(f"TurboADB {_upd.current_version()} is already the latest; "
                      f"refreshing adb/scrcpy…", file=sys.stderr)
            res = _upd.run_upgrade(notify=lambda m: print(m, file=sys.stderr))
            if not res.get("ok"):
                print(f"Update failed: {res.get('error')}", file=sys.stderr)
                return 1
            bits = [b for b in (f"adb {res['adb']}" if res.get("adb") else None,
                                f"scrcpy {res['scrcpy']}" if res.get("scrcpy") else None)
                    if b]
            print(f"Updated to TurboADB {res.get('new')}"
                  + (f"  ({', '.join(bits)})" if bits else ""))
            return 0

        if cmd == "devices":
            try:
                devs = list_devices(args.adb_path, server_host=args.adb_host,
                                    server_port=args.adb_port)
            except ConnectionError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps([d.__dict__ for d in devs], default=str, indent=2))
            elif not devs:
                where = (f"the adb server at {args.adb_host}:{args.adb_port}"
                         if args.adb_host else "USB")
                print(f"No devices found on {where}. Plug in (enable USB "
                      f"debugging), or:  turboadb connect HOST:PORT")
            else:
                for d in devs:
                    print(d)
            return 0

        if cmd == "restart-server":
            h = ADBHandler(ADBConfig(adb_path=args.adb_path))
            h._run_global(["kill-server"], check=False, timeout=15)
            r = h._run_global(["start-server"], check=False, timeout=30)
            print(r.text or r.stderr.strip() or "ADB server restarted.")
            return 0

        if cmd == "serve":
            from .devices import (start_shared_server, install_startup,
                                  uninstall_startup, open_firewall,
                                  install_serve_task, uninstall_serve_task)

            def _say(msg):
                # at Windows login we run under pythonw (no console / stdout=None)
                try:
                    print(msg)
                except Exception:
                    pass

            if args.uninstall_startup:
                a = uninstall_startup(); b = uninstall_serve_task()
                _say("Removed auto-start." if (a or b)
                     else "No auto-start was installed.")
                return 0
            _say(start_shared_server(port=args.port, adb_path=args.adb_path))
            # open BOTH the adb port and scrcpy's video-tunnel port so a remote
            # laptop can mirror this PC's device
            _say(open_firewall((args.port, 27184)))
            if args.startup_task:
                try:
                    name = install_serve_task(port=args.port)
                    _say(f"Installed startup Scheduled Task '{name}' (runs at "
                         f"system startup, headless — survives logoff).")
                except Exception as exc:
                    _say(f"Could not install startup task (need admin): {exc}")
            if args.install_startup:
                path = install_startup(port=args.port)
                _say(f"Installed login auto-start: {path}")
            if not (args.startup_task or args.install_startup):
                _say("Other machines can now connect with TurboADB -> Remote, "
                     "using this PC's IP/hostname.\n"
                     "Tip: add  --startup-task  so it runs headless at every "
                     "startup (best for remote/RDP hosts).")
            return 0

        if cmd == "connect":
            h = ADBHandler(ADBConfig(adb_path=args.adb_path))
            hp = args.hostport
            host, _, port = hp.rpartition(":")
            host, port = (host, int(port)) if port.isdigit() else (hp, 5555)
            print(h.connect_tcp(host, port))
            return 0

        if cmd == "disconnect":
            h = ADBHandler(ADBConfig(adb_path=args.adb_path))
            res = h._run_global(["disconnect"] + ([args.hostport] if args.hostport
                                                  else []), check=False)
            print(res.text or "disconnected")
            return 0

        if cmd == "pair":
            h = ADBHandler(ADBConfig(adb_path=args.adb_path))
            host, _, port = args.hostport.rpartition(":")
            print(h.pair(host, int(port), args.code))
            return 0

        # everything below operates on a connected handler
        dev = _handler(args)
        if cmd == "scrcpy":
            # scrcpy doesn't require our connect() handshake
            dev._connected = True
            dev._serial = args.serial
            opts = ScrcpyOptions(
                max_size=args.max_size, bit_rate=args.bit_rate, max_fps=args.max_fps,
                record=args.record, turn_screen_off=args.turn_screen_off,
                no_control=args.no_control)
            sess = dev.mirror(opts)
            print(f"scrcpy launched (pid {sess.pid}). Close its window to end.")
            if args.wait:
                sess.wait()
            return 0

        dev.connect()

        if cmd == "info":
            _output(args, dev.device_info())
        elif cmd == "shell":
            command = " ".join(args.command).strip()
            if not command:
                print("No command given.", file=sys.stderr)
                return 2
            res = dev.shell(command, su=args.su)
            if not args.json:
                if res.stdout:
                    sys.stdout.write(res.stdout if res.stdout.endswith("\n")
                                     else res.stdout + "\n")
                if res.stderr:
                    sys.stderr.write(res.stderr)
                return res.exit_code
            _output(args, res)
            return res.exit_code
        elif cmd == "logcat":
            print("Streaming logcat (Ctrl+C to stop)…", file=sys.stderr)
            try:
                res = dev.logcat(tag=args.tag, priority=args.priority,
                                 buffers=args.buffer, fmt=args.format,
                                 match=args.match, save_to=args.save,
                                 stop_on_match=args.stop_on_match,
                                 clear_first=args.clear, dump=args.dump,
                                 on_line=print)
                if args.match:
                    print(f"\n[{len(res.matches)} matched lines]", file=sys.stderr)
            except KeyboardInterrupt:
                pass
        elif cmd == "logcat-clear":
            dev.logcat_clear()
            print("logcat buffers cleared.")
        elif cmd == "push":
            _output(args, dev.push(args.local, args.remote,
                                   on_progress=_progress(args)))
        elif cmd == "pull":
            _output(args, dev.pull(args.remote, args.local,
                                   on_progress=_progress(args)))
        elif cmd == "install":
            if len(args.apks) > 1:
                print(dev.install_multiple(args.apks, replace=not args.no_replace,
                                           grant_perms=args.grant))
            else:
                print(dev.install(args.apks[0], replace=not args.no_replace,
                                  downgrade=args.downgrade, grant_perms=args.grant))
        elif cmd == "uninstall":
            print(dev.uninstall(args.package, keep_data=args.keep_data))
        elif cmd == "packages":
            pkgs = dev.list_packages(filter_text=args.filter,
                                     third_party=args.third_party, system=args.system)
            if args.json:
                print(json.dumps(pkgs, indent=2))
            else:
                for p in pkgs:
                    print(p)
                print(f"\n[{len(pkgs)} packages]", file=sys.stderr)
        elif cmd == "clear":
            print(dev.clear_app(args.package))
        elif cmd == "start":
            print("launched" if dev.start_app(args.package) else "failed")
        elif cmd == "stop":
            print("stopped" if dev.stop_app(args.package) else "failed")
        elif cmd == "screenshot":
            print(f"Saved {dev.screenshot(args.path)}")
        elif cmd == "record":
            print(f"Saved {dev.screen_record(args.path, time_limit=args.time_limit, size=args.size, bit_rate=args.bit_rate)}")
        elif cmd == "forward":
            fwd = dev.forward(args.local, args.remote)
            print(f"{fwd}\nForward active. Ctrl+C to remove it.")
            try:
                _block()
            except KeyboardInterrupt:
                fwd.close()
                print("\nforward removed.")
        elif cmd == "reverse":
            rev = dev.reverse(args.remote, args.local)
            print(f"{rev}\nReverse active. Ctrl+C to remove it.")
            try:
                _block()
            except KeyboardInterrupt:
                rev.close()
                print("\nreverse removed.")
        elif cmd == "tcpip":
            print(dev.tcpip(args.port))
        elif cmd == "reboot":
            dev.reboot(args.mode)
            print(f"reboot {args.mode or ''}".strip())
        elif cmd == "root":
            print(dev.root())
        elif cmd == "key":
            print("ok" if dev.keyevent(args.key) else "failed")
        elif cmd == "text":
            dev.input_text(" ".join(args.words)); print("typed")
        elif cmd == "scroll":
            dev.scroll(args.direction); print(f"scrolled {args.direction}")
        elif cmd == "tap":
            dev.tap_center(); print("tapped")
        elif cmd == "media":
            dev.media(args.action); print(f"media {args.action}")
        elif cmd == "brightness":
            dev.display_brightness(args.fraction); print(f"brightness {args.fraction}")
        elif cmd == "wifi":
            print(dev.set_wifi(args.state == "on"))
        elif cmd == "bluetooth":
            print(dev.set_bluetooth(args.state == "on"))
        elif cmd == "airplane":
            print(dev.set_airplane(args.state == "on"))
        elif cmd == "hotspot":
            print(dev.set_hotspot(args.state == "on"))
        elif cmd == "screen":
            (dev.screen_on() if args.state == "on" else dev.screen_off())
            print(f"screen {args.state}")
        elif cmd == "settings":
            dev.open_settings(); print("opened settings")
        elif cmd == "unroot":
            print(dev.unroot())
        elif cmd == "mount-rw":
            print(dev.mount_rw())
        elif cmd == "open":
            dev.open_url(args.url); print(f"opened {args.url}")
        elif cmd == "search":
            q = " ".join(args.query); dev.web_search(q); print(f"searched {q!r}")
        elif cmd == "camera":
            print("opened camera" if dev.open_camera() else "no camera app found")
        elif cmd == "gallery":
            print("opened gallery" if dev.open_gallery() else "no gallery app found")
        elif cmd == "calculator":
            print("opened calculator" if dev.open_calculator() else "no calculator app found")
        elif cmd == "close-apps":
            dev.close_apps(); print("closed background apps")
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
            dev.dial(args.number); print(f"dialler opened: {args.number}")
        elif cmd == "call":
            dev.call(args.number); print(f"calling {args.number}")
        elif cmd == "end-call":
            dev.end_call(); print("ended call")
        elif cmd == "answer":
            dev.answer_call(); print("answered")
        elif cmd == "call-log":
            for r in dev.call_log(args.limit):
                print(f"{r.get('type','?'):2} {r.get('number',''):16} {r.get('date','')}")
        elif cmd == "sms":
            for r in dev.sms_list(args.limit):
                print(f"{r.get('type','?'):2} {r.get('address',''):14} "
                      f"{(r.get('body') or '')[:60]!r}")
        elif cmd == "send-sms":
            dev.send_sms(args.number, " ".join(args.body))
            print(f"compose opened: {args.number}")
        return 0

    except ADBNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except ADBError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _progress(args):
    if getattr(args, "json", False):
        return None
    state = {"last": -1}

    def cb(pct):
        if pct != state["last"]:
            state["last"] = pct
            sys.stderr.write(f"\r  {pct:3d}%")
            sys.stderr.flush()
            if pct >= 100:
                sys.stderr.write("\n")
    return cb


def _block():
    import time
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
