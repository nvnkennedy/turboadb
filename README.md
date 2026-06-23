<div align="center">
  <img src="turboadb/assets/icon.png" alt="TurboADB" width="96" height="96">

  # TurboADB

  **One pip package for driving Android over ADB + scrcpy — a Python API, a full
  CLI, and a desktop GUI. Built for Android Automotive / IVI head units and
  regular phones alike.**

  [![PyPI](https://img.shields.io/pypi/v/turboadb.svg)](https://pypi.org/project/turboadb/)
  [![Python](https://img.shields.io/pypi/pyversions/turboadb.svg)](https://pypi.org/project/turboadb/)
  [![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
</div>

---

TurboADB wraps `adb` and `scrcpy` so you don't have to remember their flags. The
same engine powers all three front-ends, so anything you can click in the GUI you
can also script in Python or run as a one-line CLI command. It bundles its own
`adb`/`scrcpy` (downloaded on first run), works against devices on **another
machine's** adb server (handy over RDP / in a lab), and knows the quirks of
Android Automotive head units.

## Contents

- [What you get](#what-you-get)
- [Install](#install)
- [Three ways to use it](#three-ways-to-use-it)
- Features
  - [Interactive shell](#interactive-shell) · [Live logcat](#live-logcat) ·
    [File browser](#file-browser) · [App manager](#app-manager)
  - [Device controls](#device-controls) · [Screen mirroring (scrcpy)](#screen-mirroring-scrcpy) ·
    [Screenshots &amp; recording](#screenshots--recording)
  - [Phone / telephony](#phone--telephony) · [Root &amp; mount](#root--mount)
  - [Remote devices over the network](#remote-devices-over-the-network) ·
    [Share this PC's devices (serve)](#share-this-pcs-devices-serve) ·
    [Deploy serve to remote hosts](#deploy-serve-to-remote-hosts)
  - [Auto-update](#auto-update)
- [CLI reference](#cli-reference)
- [Python API](#python-api)
- [Notes for Android Automotive / IVI](#notes-for-android-automotive--ivi)
- [Building from source](#building-from-source)
- [License](#license)

## What you get

| | |
|---|---|
| **[Interactive shell](#interactive-shell)** | A real `adb shell` terminal with history, tab-completion, copy/paste, and a Stop button that actually kills a runaway command. |
| **[Live logcat](#live-logcat)** | Level/tag/regex filtering, pause, and a complete save — never drops lines under a flood, even over RDP. |
| **[File browser](#file-browser)** | Browse the device filesystem, push/pull files. |
| **[App manager](#app-manager)** | List, install (incl. split APKs), uninstall, clear data, start/stop. |
| **[Device controls](#device-controls)** | Keys, media, Wi-Fi/BT/airplane/hotspot, screen on/off, brightness, app launchers, an on-screen keyboard. |
| **[Mirroring](#screen-mirroring-scrcpy)** | scrcpy in a separate window or embedded, with a compatibility mode for IVI units. |
| **[Capture](#screenshots--recording)** | Screenshots and screen recording. |
| **[Telephony](#phone--telephony)** | Dial, call, answer/end, call log, SMS. |
| **[Root / mount](#root--mount)** | root/unroot, remount, mount rw, disable/enable verity. |
| **[Remote](#remote-devices-over-the-network)** | Drive devices plugged into another PC over its adb server. |
| **[Serve + deploy](#share-this-pcs-devices-serve)** | Share this PC's devices, or push `serve` onto remote Windows hosts over WinRM. |

## Install

```bash
pip install turboadb
```

That's everything — the CLI, the Python API, and the bundled GUI executable. On
first run TurboADB downloads `adb` and `scrcpy` into `~/.turboadb/tools` for you.

If you want to run the GUI from source instead of the bundled exe (e.g. on an ARM
host where there's no exe), add PyQt5:

```bash
pip install "turboadb[gui]"      # GUI from source
pip install "turboadb[winrm]"    # remote 'serve' deploy over WinRM
pip install "turboadb[all]"      # both
```

No Python? Grab the standalone Windows GUI from the GitHub Releases page and
double-click it.

## Three ways to use it

**GUI** — `turboadb-gui` (a desktop shortcut is created on first launch):

```bash
turboadb-gui
```

**CLI** — fully argument-driven, one device action per command:

```bash
turboadb devices
turboadb -s SERIAL shell -- getprop ro.build.version.release
turboadb -s SERIAL install app.apk --grant
turboadb -s SERIAL screenshot shot.png
```

**Python** — the same engine, for test frameworks and scripts:

```python
from turboadb import ADBHandler, ADBConfig

dev = ADBHandler(ADBConfig(serial="SERIAL"))
dev.connect()
print(dev.shell("getprop ro.product.model").stdout)
dev.install("app.apk", grant_perms=True)
dev.screenshot("shot.png")
```

---

## Interactive shell

A proper terminal, not a one-shot. You type into it, history works, `Tab`
completes commands and paths, and selection/copy/paste behave like any console.
Because it runs without a pseudo-terminal (which keeps typing reliable on
Windows), a flooding command like `logcat` can't be stopped with `Ctrl+C` alone —
so there's a **Stop** button (and a smart `Ctrl+C`) that tears the shell down,
kills the device-side process, and reopens instantly, keeping your working
directory. A bare `ls` is shown in columns like a real terminal would.

CLI equivalent: `turboadb -s SERIAL shell -- <command>`.

## Live logcat

Filter by level, tag, or live regex; pause and resume; clear. Under a flood
(especially over a slow/RDP link) the on-screen view drops lines to stay
responsive, but **every line is archived to disk** so a Save writes the complete
log, not just what's on screen. Saved files are timestamped `.log` files.

CLI: `turboadb -s SERIAL logcat --tag ActivityManager --match "ANR|FATAL" --save boot.log`.

## File browser

Browse the device filesystem in a tree, and push/pull files.
CLI: `turboadb -s SERIAL push local.apk /data/local/tmp/` and
`turboadb -s SERIAL pull /sdcard/log.txt .`.

## App manager

List installed packages (filter third-party/system), install single or split
APKs (with `--grant` to grant all permissions), uninstall, clear app data, and
force-start/stop.
CLI: `packages`, `install`, `uninstall`, `clear`, `start`, `stop`.

## Device controls

A panel of one-click actions, every one of which is also a CLI command:

- **System keys** — back, home, recents, power, notifications
- **Media** — volume up/down/mute, prev/play-pause/next
- **Connectivity** — Wi-Fi, Bluetooth, airplane, hotspot (on/off)
- **Screen & power** — screen on/off, open Settings, reboot
- **Apps & web** — open a URL, web search, browser/YouTube/Maps/Store/Gallery/Camera/Calculator
- **Keyboard** — type text into the focused field, plus Enter/Backspace/Tab/Esc/Search

## Screen mirroring (scrcpy)

Mirror in a separate window or embedded in the tab. A **compatibility mode**
(software decode, forced tunnel host/port, UHID keyboard) handles IVI units that
choke on the defaults, and it works through a remote adb server too. Pick a
specific display on multi-display head units.
CLI: `turboadb -s SERIAL scrcpy --max-size 1280 --bit-rate 8M`.

## Screenshots & recording

One-click PNG screenshot and screen recording (device-side `screenrecord`, pulled
back when you stop — works over RDP without a video tunnel).
CLI: `turboadb -s SERIAL screenshot shot.png` and
`turboadb -s SERIAL record clip.mp4 --time-limit 20`.

## Phone / telephony

Open the dialer, place/answer/end calls, read the call log, list SMS, and compose
a message. Useful on IVI units with telephony.
CLI: `dial`, `call`, `end-call`, `answer`, `call-log`, `sms`, `send-sms`.

## Root & mount

For rooted/engineering builds: `root` / `unroot`, `remount`, `mount-rw`
(`mount -o remount,rw /`), and `disable-verity` / `enable-verity` (which sync and
offer the required reboot).

## Remote devices over the network

You don't need the device plugged into your own machine. Point TurboADB at
**another PC's adb server** and drive whatever is connected there — exactly what
you want when the unit lives in a lab and you reach it over RDP.

```bash
# list and drive devices on another machine's adb server
turboadb --adb-host lab-pc-01 --adb-port 5037 devices
turboadb --adb-host lab-pc-01 -s DEVICE shell -- pm list packages
```

```python
dev = ADBHandler(ADBConfig(adb_server_host="lab-pc-01", adb_server_port=5037,
                           serial="DEVICE"))
```

For that to work, the machine with the device has to share its adb server — see
next.

## Share this PC's devices (serve)

Turn the machine a device is plugged into a host that others can reach:

```bash
turboadb serve --startup-task
```

This starts an adb server on all interfaces, opens the firewall (5037 + scrcpy's
27184), and registers a SYSTEM startup task so it keeps sharing headlessly across
reboots. In the GUI it's the **ADB Server ▸ Share this PC's devices** option.

## Deploy serve to remote hosts

You can also push `serve` onto remote Windows hosts **from your machine**, without
logging into each one, over WinRM:

```bash
turboadb deploy-serve lab-pc-01 lab-pc-02 -u "DOMAIN\user"
turboadb deploy-serve lab-pc-01 -u "DOMAIN\user" --test   # just check WinRM first
```

In the GUI this is the **ADB Server** button: enter the host(s) + admin
credentials, Test connection, Deploy. It uses pywinrm with NTLM, so it works with
domain credentials over plain WinRM. Each target needs WinRM enabled
(`Enable-PSRemoting -Force` once), Python + turboadb installed, and your account a
local admin.

## Auto-update

The GUI's **Upgrade** button checks PyPI for a newer TurboADB; if there is one it
updates via pip, refreshes `adb`/`scrcpy`, and restarts into the new version. From
the CLI: `turboadb self-update`. Tools-only refresh: `turboadb upgrade-tools`.

---

## CLI reference

`turboadb -h` lists everything; `turboadb <command> -h` details one. Most read
commands take `--json` for machine-readable output. Target a device with
`-s SERIAL` (or `-s host:port`); add `--adb-host HOST` to use a remote adb server.

```
devices  info  shell  logcat  logcat-clear  push  pull  install  uninstall
packages  clear  start  stop  screenshot  record  forward  reverse  scrcpy
connect  disconnect  pair  tcpip  reboot  root  unroot  remount  mount-rw
disable-verity  enable-verity  key  text  scroll  tap  media  brightness
wifi  bluetooth  airplane  hotspot  screen  settings  open  search  camera
gallery  calculator  close-apps  battery  build-info  dial  call  end-call
answer  call-log  sms  send-sms  serve  deploy-serve  doctor  fetch-tools
upgrade-tools  self-update  shortcut  gui
```

## Python API

```python
from turboadb import ADBHandler, ADBConfig

dev = ADBHandler(ADBConfig(serial="SERIAL"))
dev.connect()

dev.set_wifi(True)
dev.keyevent("home")
dev.media("play-pause")
dev.set_hotspot(True)
dev.screen_off()
info = dev.device_info()          # manufacturer, model, android_version, automotive…
for line in dev.logcat(tag="ActivityManager", match="ANR"):
    print(line)
dev.reboot()
```

Results are small dataclasses (`CommandResult`, `TransferResult`, …) with `.ok`,
`.stdout`, `.exit_code`, etc. In "safe" mode (used by the GUI) calls return an
`OperationResult` instead of raising. See [`examples/examples.py`](examples/examples.py)
and [ARCHITECTURE.md](ARCHITECTURE.md).

## Notes for Android Automotive / IVI

- `device_info()` flags `automotive` so the GUI adapts (e.g. the mirror label).
- Mirroring: use **compatibility mode** if the default scrcpy fails.
- `bootloader` / `sideload` reboots are gated behind an extra warning — many head
  units have no on-screen recovery UI and can get stuck.
- Apps like Calculator/Camera/Play Store are often absent; launchers fall back
  gracefully and the log says when nothing happened.
- Hotspot can't always be toggled purely via adb (uid permissions) — TurboADB
  tries `cmd wifi`, then falls back to opening the tethering settings.

## Building from source

```bash
git clone <your-repo-url> turboadb && cd turboadb
pip install -r requirements.txt
python -m turboadb.gui            # run the GUI from source
python scripts/build_exe.py       # rebuild the bundled Windows exe
python tests/test_offline.py      # offline checks
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the pieces fit together and
[CHANGELOG.md](CHANGELOG.md) for release notes.

## License

[MIT](LICENSE).
