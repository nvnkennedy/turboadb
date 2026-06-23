# TurboADB

**An Android ADB + scrcpy device toolkit for automotive/embedded Android
(Android Automotive OS / IVI head units) and general Android work.**

TurboADB wraps Google's `adb` and `scrcpy` behind one robust, structured layer
you can drive three ways from a single `pip install turboadb`:

1. **Python API** — `import turboadb` for scripts and test frameworks.
2. **CLI** — `turboadb …`, fully argument-driven (argparse).
3. **Desktop GUI** — `turboadb-gui`, a full PyQt5 app shipped as a prebuilt
   Windows `.exe` (PyQt5 baked in) so it runs even where PyQt5 has no wheel
   (e.g. Windows ARM64) — **no PyQt5 install required**.

Every action returns a structured result (`CommandResult` / `TransferResult` /
`StreamResult`), with a **safe mode** that returns an `OperationResult` instead
of raising, and a typed exception hierarchy (`ADBError` base). All blocking work
can run on background threads, so it never blocks or crashes the caller.

---

## Table of contents

- [Install](#install)
- [The GUI](#the-gui)
- [Quick start (API)](#quick-start-api)
- [Connecting — USB vs network (Wi-Fi / Ethernet)](#connecting--usb-vs-network-wifi--ethernet)
- [Running shell commands](#running-shell-commands)
- [Live logcat (continuous logs)](#live-logcat-continuous-logs)
- [File transfer (push / pull)](#file-transfer-push--pull)
- [App management](#app-management)
- [Media — screenshot & screen record](#media--screenshot--screen-record)
- [Port forwarding (forward / reverse)](#port-forwarding-forward--reverse)
- [scrcpy mirroring (the visual session)](#scrcpy-mirroring-the-visual-session)
- [Automotive (Android Automotive OS / IVI)](#automotive-android-automotive-os--ivi)
- [Result objects & error handling](#result-objects--error-handling)
- [CLI reference](#cli-reference)
- [API map](#api-map)
- [Building the GUI exe & releasing](#building-the-gui-exe--releasing)
- [License](#license)

---

## Install

```bash
pip install turboadb
```

That gives you the Python API, the `turboadb` CLI, and `turboadb-gui` (which
launches the bundled Windows exe; on other platforms install the GUI extra:
`pip install "turboadb[gui]"`).

TurboADB needs Google's **platform-tools (`adb`)** and, for mirroring,
**`scrcpy`**. It auto-detects them on your `PATH`, in the Android SDK, or in
common install locations. If they're missing, **let TurboADB fetch them for you**:

```bash
turboadb fetch-tools     # downloads adb (+scrcpy) into ~/.turboadb/tools
turboadb doctor          # shows where adb / scrcpy were found
```

**It's automatic, too.** The first time you run TurboADB after an install **or
upgrade**, it downloads the **latest** platform-tools + scrcpy into a per-user
cache (`~/.turboadb/tools/`), which is first in the detection order — so a fresh
`pip install -U turboadb` always gives you current tools with zero setup. Each
new version re-fetches the latest. Disable with **`TURBOADB_AUTO_FETCH=0`** (for
offline/CI/locked-down machines), then fetch manually with `turboadb fetch-tools`.

- **GUI:** downloads automatically on launch (progress bar), or use the ribbon's
  **Get tools** button to refresh.
- **Python:** `turboadb.fetch_tools()` (explicit) or it happens on first device use.

> Why not at `pip install` time? Wheels don't run code on install, and
> downloading during install breaks offline/CI/proxy setups and can hang pip.
> First-run/on-upgrade fetch (the Playwright model) gives the same "just works"
> result while keeping the wheel small and license-clean — *you* fetch Google's
> adb (under its SDK terms); TurboADB never redistributes it. scrcpy is Apache-2.0.

- `adb`: <https://developer.android.com/tools/releases/platform-tools>
- `scrcpy`: <https://github.com/Genymobile/scrcpy>
  (Windows: `winget install scrcpy` · macOS: `brew install scrcpy` · Linux: `apt install scrcpy`)

Override detection any time with `ADBConfig(adb_path=..., scrcpy_path=...)`,
the `TURBOADB_ADB` / `TURBOADB_SCRCPY` environment variables, or the GUI's
Settings dialog.

---

## The GUI

```bash
turboadb-gui            # or: python -m turboadb.gui   (from source)
turboadb-shortcut       # make a Desktop shortcut (Windows)
```

A tabbed, multi-device workspace:

- **Tabbed multi-device sessions** — each device opens its own tab; tabs are
  closable, movable, and there's a **`+`** new-tab button. A **Split/tile** view
  shows several devices at once.
- **Sidebar device manager** — saved targets **plus a LIVE `adb devices`** list
  that auto-refreshes; a **Quick connect** filter box; right-click context menu
  (New / Open / Edit / Duplicate / Delete). Double-click a live device to open it.
- **Ribbon toolbar** with colorful buttons: Device, Shell, Logcat, Files, Apps,
  Scrcpy (mirror), Screenshot, Split, Settings, Help, Exit.
- **Per-device panels (tabs):**
  - **Shell** — type straight into the terminal; **one Enter runs the command**
    (reliable cooked line-editing, with Up/Down history and Ctrl+C). Full
    **native text selection + copy/paste**, **save output to file**, a right-click
    menu (Copy / Paste / Save / Send-key: Tab/Esc/Ctrl-C/D/Z…), scrollback, and
    **auto-reconnect** — after a reboot or unplug it detects the dropped shell and
    reconnects when the device comes back.
  - **Logcat** — live viewer with regex + level filter, pause, clear, save, and
    **instant stop**.
  - **Files** — adb push/pull browser with file *and* folder pickers for both
    directions and a progress bar.
  - **Apps** — install (incl. split APKs), list, uninstall, clear, start, stop.
  - **Controls** — navigation (Home/Back/Recents/DPAD), volume, **media** (via the
    active media session), **brightness** (via settings), Wi-Fi / Bluetooth /
    Airplane / Hotspot, screen on/off; **app & web shortcuts** (open any URL,
    web-search, launch Browser / YouTube / Spotify / Maps / Play Store / Settings);
    and a **text-send box that types into the device's focused field** — a keyboard
    for head units / devices with no on-screen keyboard.
  - **Phone** — a phone simulation: a **dialler** (dial / call / answer / end),
    the **call log**, and **SMS messages** (read + compose), all over adb.
  - **Root / Mount** header menu — `adb root`, `remount`, `mount -o remount,rw /`,
    `disable-verity` (auto sync + reboot prompt).
  - **Mirror** — scrcpy with a display picker and compatibility mode. By default
    it opens a reliable separate window; tick **embed (experimental)** to host the
    scrcpy screen *inside* the tab (Windows). Embedding is opt-in because
    reparenting a foreign window can be flaky on some setups / over Remote Desktop.
- A ribbon **Upgrade** button checks for newer adb/scrcpy and downloads **only if
  a newer version exists**.
- A dark, color-coded **log dock** at the bottom.
- **Settings** (persisted to `~/.turboadb/settings.json`, applied live):
  dark/light theme, terminal font + size, default scrcpy options, adb/scrcpy
  paths, logcat format. Defaults to a clean **black** dark theme; the light
  theme isn't glaring.
- **Crash-proof:** a startup-failure native popup + crash log, a global
  exception hook that logs and shows a non-fatal popup, and clean thread
  shutdown when a tab closes.

The window/app icon is an automotive **speedometer** fused with the **Android
robot** and a terminal prompt.

---

## Quick start (API)

```python
from turboadb import ADBHandler, ADBConfig

# USB: the only attached device (or pass serial="..." to pick one)
with ADBHandler() as dev:
    print(dev.device_info().value if False else dev.shell("getprop ro.build.version.release").text)
    dev.push("app.apk", "/data/local/tmp/app.apk")
    dev.install("app.apk", grant_perms=True)

    # live logs, with a regex match + tee to a file
    dev.logcat(tag="ActivityManager", match=r"ANR|FATAL",
               on_line=print, save_to="boot.log", stop_on_match=True)

    dev.screenshot("shot.png")
    dev.mirror(max_size=1280)            # launch scrcpy
```

The raw adb path is always available at `dev.adb_path`, and any adb command is a
call away: `dev.adb("shell", "wm", "size")`.

---

## Connecting — USB vs network (Wi-Fi / Ethernet)

**USB (local):**

```python
from turboadb import ADBHandler, ADBConfig, list_devices

for d in list_devices():
    print(d)                              # serial, state, model…

with ADBHandler(ADBConfig(serial="emulator-5554")) as dev:
    ...
```

**Network (remote head unit / IVI on the bench LAN):**

```python
# Enable TCP mode once while on USB, then connect wirelessly:
with ADBHandler() as usb:
    usb.tcpip(5555)

with ADBHandler(ADBConfig(host="192.168.1.50", port=5555)) as hu:
    print(hu.shell("getprop ro.build.characteristics").text)
```

**Remote machine (device plugged into another PC — e.g. an RDP/bench host):**

Drive a device that's physically attached to a *different* machine by talking to
that machine's adb server — you see and control the devices attached over there,
with your local files used for push/pull.

```bash
# 1) ON the machine with the device, expose its adb server once:
adb -a nodaemon server start          # listens on all interfaces (port 5037)

# 2) From your machine, point TurboADB at it:
turboadb devices --adb-host 192.168.1.20            # 'adb devices' THERE
turboadb -s SERIAL --adb-host 192.168.1.20 shell -- getprop ro.product.model
turboadb -s SERIAL --adb-host 192.168.1.20 logcat --match "ANR|FATAL"
```

```python
from turboadb import ADBHandler, ADBConfig, remote_devices

print(remote_devices("192.168.1.20"))          # devices on that machine
with ADBHandler(ADBConfig(adb_server_host="192.168.1.20", serial="SERIAL")) as dev:
    print(dev.shell("getprop ro.product.model").text)
    dev.logcat(match="ANR|FATAL", on_line=print)
    dev.mirror()                               # scrcpy via the remote server
```

In the **GUI**: **Device ▾ → Connect to a remote PC's ADB (devices there)…** —
enter the machine's IP, pick from the devices attached over there, and it opens
a full tab (Shell / Logcat / Files / Apps / Mirror) driven through that server.
Save it as a target with **type "Remote ADB server"**. (Keep it on a trusted
network/VPN — the adb server is unauthenticated; or tunnel 5037 over SSH.)

**Android 11+ wireless pairing:**

```python
dev = ADBHandler(ADBConfig(host="192.168.1.50", port=5555))
dev.pair("192.168.1.50", 37123, "482913")    # host:pairing_port + code
dev.connect()
```

`connect()` auto-runs `adb connect host:port` for network targets, waits for the
device, and verifies it's `device` (not `unauthorized`/`offline`) with a clear,
actionable error if not.

---

## Running shell commands

```python
res = dev.shell("pm list packages -3")
print(res.ok, res.exit_code, res.duration)
for line in res.lines:
    print(line)

dev.shell("settings put global development_settings_enabled 1", check=True)
dev.shell("svc power stayon true", su=True)      # wrap in su -c for rooted devices

# an interactive shell session (used by the GUI terminal, usable in scripts)
sh = dev.open_shell()
sh.send_line("top -n 1")
import time; time.sleep(1)
print(sh.read().decode(errors="replace"))
sh.close()
```

---

## Live logcat (continuous logs)

Stream logcat **live, line by line**, cleanly formatted, with regex matching,
match callbacks, stop-on-match, and tee-to-file — plus tag/priority filters and
buffer selection.

```python
# tag + minimum priority, collect ANR/FATAL, save everything, stop on first hit
res = dev.logcat(tag="ActivityManager", priority="I",
                 match=r"ANR|FATAL", on_match=lambda l: print("HIT:", l),
                 save_to="session.log", stop_on_match=True,
                 buffers=["main", "system", "crash"], clear_first=True)
print(res.lines, "lines,", len(res.matches), "matches")

# arbitrary streaming command + a stop event from another thread
import threading
stop = threading.Event()
dev.logcat(on_line=print, stop_event=stop)       # stop.set() to end

dev.logcat_clear()                                # adb logcat -c
```

`fmt=` sets the `-v` format (default `threadtime`); `dump=True` does `-d`
(dump current buffer and exit); `filterspecs=[...]` passes explicit `TAG:LEVEL`
specs. Lines are ANSI/control-char cleaned by default (`clean=True`).

---

## File transfer (push / pull)

Files **and** folders, with a live percent callback (the GUI shows a progress
bar):

```python
dev.push("local_dir/", "/sdcard/local_dir", on_progress=lambda p: print(p, "%"))
dev.pull("/sdcard/Download", "out/", on_progress=print)

tr = dev.push("big.bin", "/data/local/tmp/big.bin")
print(tr.human_size, tr.human_speed, tr.duration)   # 12.0MB 48.0MB/s 0.25
```

---

## App management

```python
dev.install("app.apk", replace=True, grant_perms=True)        # adb install -r -g
dev.install_multiple(["base.apk", "split_config.en.apk"])     # split APKs
dev.uninstall("com.example.app", keep_data=False)

dev.list_packages(third_party=True)              # -> ["com.foo", ...]
dev.clear_app("com.example.app")                 # pm clear
dev.start_app("com.example.app")                 # launcher intent
dev.start_activity("com.example.app/.MainActivity")
dev.stop_app("com.example.app")                  # am force-stop
dev.grant("com.example.app", "android.permission.CAMERA")
dev.revoke("com.example.app", "android.permission.CAMERA")
print(dev.current_activity())                    # foreground component
```

---

## Media — screenshot & screen record

```python
dev.screenshot("shot.png")                       # exec-out screencap -p
png_bytes = dev.screenshot()                     # raw PNG bytes if no path

# record on-device, then pull it (stop early with a stop_event)
dev.screen_record("clip.mp4", time_limit=20, size="1280x720", bit_rate="8M")
```

---

## Port forwarding (forward / reverse)

Stoppable handles (`adb forward` / `adb reverse`):

```python
fwd = dev.forward("tcp:9222", "localabstract:chrome_devtools_remote")
# ... use 127.0.0.1:9222 on the host ...
fwd.close()                                       # adb forward --remove

with dev.reverse("tcp:8000", "tcp:8000"):         # device reaches your PC:8000
    ...
print(dev.list_forwards())
```

---

## scrcpy mirroring (the visual session)

Launch real-time screen mirroring + control — the visual analog of opening a
remote desktop for a host:

```python
from turboadb import ScrcpyOptions

sess = dev.mirror(max_size=1280, bit_rate="8M", stay_awake=True)
# ... a scrcpy window is now mirroring & controlling the device ...
sess.stop()

# full control via ScrcpyOptions (crop is great for IVI displays):
opts = ScrcpyOptions(crop="1920x1080:0:0", display_id=0,
                     record="drive.mp4", turn_screen_off=True)
dev.mirror(opts)
```

---

## Automotive (Android Automotive OS / IVI)

```python
with ADBHandler(ADBConfig(host="192.168.1.50")) as hu:
    info = hu.device_info()
    print(info["manufacturer"], info["model"], "Android", info["android_version"])
    if hu.is_automotive():
        print("This is an Android Automotive OS head unit.")

    # head units expose several displays (center stack, cluster, passenger):
    for d in hu.list_displays():
        print("display", d["id"], d["size"])
    hu.mirror(display_id=2)                  # mirror a specific IVI display

    # "scrcpy won't work" on this unit? use the automotive compatibility profile
    # (forces H.264, caps size/fps, disables audio — fixes most IVI encoders):
    hu.mirror(compat=True)
```

`device_info()` includes an `automotive` flag (from the automotive hardware
feature / build characteristics), so you can branch IVI-specific flows.

**In the GUI**, automotive devices show a **Mirror (IVI)** split-button with:
*Mirror (default)*, *Mirror a specific display…* (lists the head unit's displays
to choose from), and *Mirror (compatibility mode)*. If a mirror fails, the error
suggests trying compatibility mode or a different display. The default scrcpy
video codec is configurable in **Settings** (auto / h264 / h265 / av1).

**Why scrcpy sometimes fails on IVI — and what TurboADB does about it**

| Symptom on a head unit                    | TurboADB's fix                                   |
|-------------------------------------------|--------------------------------------------------|
| Black screen / wrong screen mirrored      | `list_displays()` + `display_id=` to pick the right one |
| "Could not open video stream" / encoder error | `compat=True` → forces `--video-codec h264`, caps size/fps |
| Audio init fails / no video on Android <11 | audio is **off by default**; compat mode keeps it off |
| Bandwidth/lag over Wi-Fi                   | `max_size=`, `bit_rate=`, `max_fps=`             |

---

## Result objects & error handling

Every operation returns a structured result:

| Result          | Key fields / props                                              |
|-----------------|----------------------------------------------------------------|
| `CommandResult` | `.ok`, `.exit_code`, `.stdout`, `.stderr`, `.duration`, `.text`, `.lines` |
| `TransferResult`| `.size_bytes`, `.duration`, `.files`, `.human_size`, `.human_speed` |
| `StreamResult`  | `.lines`, `.matches`, `.matched`, `.saved_to`                  |
| `OperationResult`| `.success`/`bool`, `.value`, `.error`, `.unwrap()` (safe mode) |

**Raise mode (default)** — great for test automation:

```python
try:
    dev.shell("false", check=True)
except ADBError as exc:
    print("failed:", exc)
```

**Safe mode** — great for GUIs (never throws):

```python
dev = ADBHandler(cfg, safe=True)
res = dev.install("app.apk")
if res:                      # OperationResult is falsy on failure
    print("ok:", res.value)
else:
    print("error:", res.error)
```

Exception hierarchy (catch `ADBError` for everything):
`ADBNotFoundError`, `ADBConnectionError`, `ADBTimeoutError`,
`ADBNotConnectedError`, `ADBCommandError`, `ADBTransferError`,
`ADBInstallError`, `ScrcpyError`.

---

## CLI reference

```
turboadb doctor                      # is adb / scrcpy installed?
turboadb fetch-tools [--adb-only|--scrcpy-only --force]   # download into the cache
turboadb upgrade-tools [--check]     # update adb/scrcpy only if a newer version exists
turboadb devices                     # list attached/known devices
turboadb info        [-s S]          # device identity / build / automotive flag
turboadb shell       [-s S] -- CMD…  # one-shot adb shell (use --su to wrap in su -c)
turboadb logcat      [-s S] [--tag T --priority I --match RE --save F --stop-on-match --clear --dump]
turboadb logcat-clear[-s S]
turboadb push        [-s S] LOCAL REMOTE
turboadb pull        [-s S] REMOTE LOCAL
turboadb install     [-s S] APK [APK…] [--grant --downgrade --no-replace]
turboadb uninstall   [-s S] PKG [--keep-data]
turboadb packages    [-s S] [--third-party --system] [FILTER]
turboadb clear       [-s S] PKG
turboadb start|stop  [-s S] PKG
turboadb screenshot  [-s S] PATH
turboadb record      [-s S] PATH [--time-limit 30 --size 1280x720 --bit-rate 8M]
turboadb forward     [-s S] LOCAL REMOTE      # stays until Ctrl+C
turboadb reverse     [-s S] REMOTE LOCAL
turboadb scrcpy      [-s S] [--max-size 1280 --bit-rate 8M --record F --turn-screen-off --no-control --wait]
turboadb connect     HOST:PORT
turboadb restart-server               # kill+start adb server (device-not-visible fixes)
turboadb disconnect  [HOST:PORT]
turboadb tcpip       [-s S] [PORT]
turboadb pair        HOST:PAIRPORT CODE
turboadb reboot      [-s S] [recovery|bootloader|sideload]
turboadb root        [-s S]
# device controls (same as the GUI Controls tab):
turboadb key         [-s S] home|back|recents|vol_up|play_pause|…
turboadb text        [-s S] hello world          # type into the focused field
turboadb scroll      [-s S] up|down|left|right    # swipe-scroll (works on touch)
turboadb tap         [-s S]
turboadb media       [-s S] play-pause|next|previous|stop
turboadb brightness  [-s S] 0.6                   # live, 0.0-1.0
turboadb wifi|bluetooth|airplane  [-s S] on|off
turboadb open        [-s S] youtube.com           # VIEW intent (app or browser)
turboadb search      [-s S] weather today
turboadb camera | close-apps | battery | build-info   [-s S]
turboadb remount | disable-verity | enable-verity     [-s S]
# phone:
turboadb dial NUMBER | call NUMBER | answer | end-call   [-s S]
turboadb call-log [--limit N] | sms [--limit N] | send-sms NUMBER message…  [-s S]
turboadb gui                          # launch the desktop GUI
```

`-s` accepts a USB serial **or** a `host:port` network target. Add `--json` to
most commands for machine-readable output. Examples:

```bash
turboadb -s 192.168.1.50:5555 shell -- dumpsys power | findstr mWakefulness
turboadb logcat --tag ActivityManager --priority I --match "ANR|FATAL" --save boot.log
turboadb install base.apk split_config.en.apk --grant
turboadb screenshot dash.png
turboadb scrcpy --max-size 1280 --bit-rate 8M
```

Other console scripts: `turboadb-gui`, `turboadb-docs`, `turboadb-shortcut`.

---

## API map

```
turboadb
├── ADBHandler(config=ADBConfig|None, *, serial=, safe=, quiet=, log_callback=)
│   ├── connect() / disconnect() / is_connected / get_state() / wait_for_device()
│   ├── tcpip(port) / connect_tcp(host, port) / pair(host, port, code)
│   ├── reboot(mode) / root() / unroot() / remount()
│   ├── getprop(name?) / device_info() / is_automotive()
│   ├── shell(cmd, su=, check=) / shell_many() / open_shell() -> ShellSession
│   ├── iter_lines(args) / stream(args, …) / logcat(…) / logcat_clear()
│   ├── push(local, remote, on_progress=) / pull(remote, local, on_progress=)
│   ├── install() / install_multiple() / uninstall() / list_packages()
│   ├── clear_app() / start_app() / start_activity() / stop_app()
│   ├── grant() / revoke() / current_activity()
│   ├── keyevent(name|code) / input_text(text) / tap() / swipe()
│   ├── set_wifi() / set_bluetooth() / set_airplane() / set_hotspot() / screen_on/off()
│   ├── remount() / mount_rw() / disable_verity() / enable_verity()
│   ├── screenshot(path?) / screen_record(path, …)
│   ├── forward(local, remote) / reverse(remote, local) -> ForwardHandle
│   ├── list_forwards() / remove_all_forwards()
│   ├── list_displays()  ·  mirror(options?|**opts, compat=) -> ScrcpySession
│   └── adb(*args)  ·  adb_path  ·  serial
├── ADBConfig / ScrcpyOptions
├── Device / list_devices() / first_online()
├── launch_scrcpy() / ScrcpySession
├── find_adb() / find_scrcpy() / adb_available() / scrcpy_available() / diagnose()
├── fetch_tools() / download_platform_tools() / download_scrcpy() / tools_dir()
├── CommandResult / TransferResult / StreamResult / OperationResult / strip_ansi
└── ADBError + ADBNotFoundError / ADBConnectionError / ADBTimeoutError /
    ADBNotConnectedError / ADBCommandError / ADBTransferError /
    ADBInstallError / ScrcpyError
```

---

## Building the GUI exe & releasing

```bash
python scripts/make_icon.py        # (re)generate the icon
python scripts/build_exe.py        # -> dist/turboadb-gui.exe (PyQt5 baked in)
# copy the exe into turboadb/bin/ so the wheel ships it, then:
python scripts/release.py patch    # bump -> test -> build -> twine check -> upload
```

`turboadb-gui` runs the bundled exe when present and falls back to running from
source (`turboadb[gui]`) otherwise. The release helper reads the PyPI token from
`TWINE_PASSWORD` (never hard-coded) and supports `--wheel-only` if a flaky
network makes the sdist+wheel upload hang.

---

## Troubleshooting (incl. Remote Desktop / RDP)

**Running TurboADB on a machine you reach over RDP**

- **adb works over RDP** — it's just a local process on the RDP host. But a device
  plugged into *your* laptop is **not** visible on the remote host unless USB is
  redirected. Easiest path — in the GUI use **Device ▾ → Connect wirelessly
  (IP / host:port)** (or `turboadb connect HOST:5555`):
  - **Network adb** (recommended for IVI/head units): on the device do
    `adb tcpip 5555` once over USB, then connect by IP from the RDP host — no USB
    redirection needed. Android 11+: **Device ▾ → Pair device**.
  - If a device is plugged into the remote host but shows offline/missing, use
    **Device ▾ → Restart ADB server** (or `turboadb restart-server`) — that clears
    the common adb-version-mismatch that hides devices.
  - Or enable **USB redirection** in your RDP client (mstsc → Local Resources →
    More → your device).
- **scrcpy over RDP** often can't open its video stream (no local GPU/decoder in
  the RDP session). The embedded **Mirror** tab detects scrcpy exiting early and
  tells you so; try **compatibility mode** (forces H.264), and if it still fails,
  mirroring isn't available in that RDP session — use it from a local session.

**"scrcpy won't work" on a head unit** → use **Mirror → compatibility mode**, or
pick the right display from **Mirror → display picker** (see the automotive table
above).

**adb not found / outdated** → `turboadb doctor` shows what's resolved;
`turboadb upgrade-tools` updates to the latest only if newer; set
`TURBOADB_AUTO_FETCH=0` to stop automatic downloads.

## License

MIT — see [LICENSE](LICENSE).
