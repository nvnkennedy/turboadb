<div align="center">
  <img src="https://raw.githubusercontent.com/NVNKENNEDY/turboadb/main/turboadb/assets/icon.png" alt="TurboADB" width="96" height="96">

  # TurboADB

  **One pip package for driving Android over ADB + scrcpy — a Python API, a full
  CLI, and a desktop GUI. Built for Android Automotive / IVI head units and
  regular phones alike.**

  [![PyPI](https://img.shields.io/pypi/v/turboadb.svg)](https://pypi.org/project/turboadb/)
  [![Python](https://img.shields.io/pypi/pyversions/turboadb.svg)](https://pypi.org/project/turboadb/)
  [![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

  **[🌐 Website](https://nvnkennedy.github.io/turboadb/) · [⤓ Download for Windows](https://github.com/NVNKENNEDY/turboadb/releases/latest) · [📦 PyPI](https://pypi.org/project/turboadb/)**
</div>

---

TurboADB wraps `adb` and `scrcpy` so you don't have to remember their flags. The
same engine powers all three front-ends, so **every feature below works three
ways** — click it in the GUI, run it as a one-line CLI command, or call it from
Python. It bundles its own `adb`/`scrcpy` (downloaded on first run), drives
devices on *another* machine's adb server (handy over RDP / in a lab), and knows
the quirks of Android Automotive head units.

The rest of this README is a hands-on guide: for each feature you get the **GUI**
steps, the **CLI** command, and the **Python** call.

## Contents

- [Install](#install)
- [First, target a device](#first-target-a-device) — the `-s` / config every command needs
- **Feature guide**
  - [Interactive shell](#interactive-shell) · [Logcat](#logcat) · [Files](#files-pushpull) · [Apps](#apps)
  - [Device controls](#device-controls): [keys](#keys--input) · [media & connectivity](#media--connectivity) · [screen & launchers](#screen--app-launchers) · [keyboard](#on-screen-keyboard)
  - [Mirroring (scrcpy)](#mirroring-scrcpy) · [Screenshots & recording](#screenshots--recording) · [Webcam (host camera)](#webcam-host-camera)
  - [Telephony](#telephony) · [Root & mount](#root--mount) · [Reboot](#reboot) · [Device info](#device-info)
  - [Remote devices](#remote-devices) · [Share devices (serve)](#share-devices-serve) · [Deploy serve over WinRM](#deploy-serve-over-winrm)
  - [Keep things up to date](#keep-things-up-to-date)
- [CLI cheatsheet](#cli-cheatsheet)
- [Python API notes](#python-api-notes)
- [Android Automotive / IVI tips](#android-automotive--ivi-tips)
- [Build from source](#build-from-source)
- [License](#license)

## Install

Pick whichever fits — both give you the full GUI.

### A · Windows app — no Python needed

1. Download **[`turboadb-gui.exe`](https://github.com/NVNKENNEDY/turboadb/releases/latest)**
   (also linked from the [website](https://nvnkennedy.github.io/turboadb/)).
2. Double-click it. On first launch it downloads `adb` + `scrcpy` automatically
   (about 20 seconds) and adds a desktop shortcut.

### B · With pip — CLI + Python API + GUI

```bash
pip install turboadb
```

Then use any of: `turboadb-gui` (the app), `turboadb <command>` (the CLI), or
`import turboadb` (the API). Optional extras, only if you run the GUI **from
source** (e.g. on ARM where there's no exe):

```bash
pip install "turboadb[gui]"    # GUI from source (PyQt5)
pip install "turboadb[winrm]"  # remote 'serve' deploy over WinRM
pip install "turboadb[all]"    # both
```

`adb` and `scrcpy` download themselves into `~/.turboadb/tools` on first use, so
they never need to be on your PATH.

## First, target a device

Everything operates on one device. How you point at it is the only thing that
changes between local, network, and remote.

**In the GUI** — click **Connect** in the ribbon and pick the device (USB,
network, or a remote PC's adb server). Connected devices also show live in the
left sidebar; double-click one to open it in a tab.

**CLI** — list first, then pass `-s`:

```bash
turboadb devices                                  # what's attached here
turboadb -s 10BE330KG9000AF info                  # a USB serial
turboadb -s 192.168.1.50:5555 info                # a network device
turboadb connect 192.168.1.50:5555                # adb connect first if needed
turboadb --adb-host lab-pc-01 devices             # devices on ANOTHER pc's server
turboadb --adb-host lab-pc-01 -s DEVICE info      # …and drive one of them
```

**Python** — build an `ADBConfig`, then `connect()`:

```python
from turboadb import ADBHandler, ADBConfig

# USB (omit serial if it's the only device)
dev = ADBHandler(ADBConfig(serial="10BE330KG9000AF"))

# network device
dev = ADBHandler(ADBConfig(host="192.168.1.50", port=5555))

# a device on a remote machine's adb server
dev = ADBHandler(ADBConfig(adb_server_host="lab-pc-01", adb_server_port=5037,
                           serial="DEVICE"))
dev.connect()
```

Pass `safe=True` to the handler (the GUI does) to get an `OperationResult` back
instead of an exception on failure — handy when you don't want one bad call to
abort a run.

---

# Feature guide

## Interactive shell

A real terminal, not a one-shot: history, `Tab` completion, copy/paste, and a
**Stop** button that actually kills a runaway command like `logcat` (there's no
PTY, so a plain `Ctrl+C` can't — Stop tears the shell down, kills the device-side
process, and reopens, keeping your working directory). A bare `ls` is shown in
columns.

**In the GUI** — open a device → **Shell** tab → start typing. Right-click for
Copy/Paste/Save/Send-key; the red **Stop** halts whatever is running.

**CLI** — one-shot commands (everything after `--` goes to the device):

```bash
turboadb -s SERIAL shell -- getprop ro.build.version.release
turboadb -s SERIAL shell --su -- "cat /data/misc/file"   # wrap in su -c
```

**Python** — `shell()` for one-shots, `open_shell()` for an interactive session:

```python
r = dev.shell("getprop ro.product.model")
print(r.stdout, r.exit_code, r.ok)

sess = dev.open_shell()          # persistent ShellSession
sess.send("ls /sdcard\n")
print(sess.read())
sess.close()
```

## Logcat

Filter by level, tag, or live regex; pause/clear; save the **complete** log (even
under a flood the on-screen view trims to stay responsive, but every line is kept
on disk).

**In the GUI** — **Logcat** tab → set Level / tag / regex → **Start**. **Save**
writes a timestamped `.log` with everything captured.

**CLI**:

```bash
turboadb -s SERIAL logcat --tag ActivityManager --priority W
turboadb -s SERIAL logcat --match "ANR|FATAL" --save crash.log
turboadb -s SERIAL logcat --dump                  # dump current buffer and exit
turboadb -s SERIAL logcat-clear                   # clear the buffers
```

**Python** — `logcat()` streams via an `on_line` callback (and tees to a file);
`iter_lines()` is a plain generator if you'd rather loop:

```python
dev.logcat(tag="ActivityManager", match="ANR", on_line=print, save_to="crash.log")

for line in dev.iter_lines(["logcat", "-v", "threadtime"]):
    if "FATAL" in line:
        break
```

## Files (push/pull)

**In the GUI** — **Files** tab → browse the device tree → use the push/pull
buttons.

**CLI**:

```bash
turboadb -s SERIAL push app.apk /data/local/tmp/
turboadb -s SERIAL pull /sdcard/Download/log.txt .
```

**Python**:

```python
dev.push("app.apk", "/data/local/tmp/")
dev.pull("/sdcard/Download/log.txt", "log.txt")
```

## Apps

List, install (single or split APKs), uninstall, clear data, start/stop.

**In the GUI** — **Apps** tab: filter the package list, then Install / Uninstall /
Clear / Start / Stop.

**CLI**:

```bash
turboadb -s SERIAL packages --third-party
turboadb -s SERIAL install app.apk --grant            # grant all permissions
turboadb -s SERIAL install base.apk split_config.apk  # split install
turboadb -s SERIAL uninstall com.example.app
turboadb -s SERIAL clear com.example.app              # wipe app data
turboadb -s SERIAL start com.example.app
turboadb -s SERIAL stop com.example.app
```

**Python**:

```python
for pkg in dev.list_packages(third_party=True):
    print(pkg)
dev.install("app.apk", grant_perms=True)
dev.install_multiple(["base.apk", "split_config.apk"])
dev.uninstall("com.example.app")
dev.clear_app("com.example.app")
dev.start_app("com.example.app")
dev.stop_app("com.example.app")
```

## Device controls

The GUI's **Controls** tab is a grid of one-click actions. Each is also a CLI
command and an API call.

### Keys & input

**GUI** — Controls → System keys / the scroll-tap pad.

```bash
turboadb -s SERIAL key home          # back, home, recents, power, notifications…
turboadb -s SERIAL scroll down       # up | down | left | right
turboadb -s SERIAL tap                # tap the centre of the screen
```

```python
dev.keyevent("home")
dev.scroll("down")
dev.tap_center()
```

### Media & connectivity

**GUI** — Controls → Media controls / Connectivity.

```bash
turboadb -s SERIAL media play-pause   # previous | next | play-pause
turboadb -s SERIAL wifi on            # on | off
turboadb -s SERIAL bluetooth off
turboadb -s SERIAL airplane on
turboadb -s SERIAL hotspot on         # best-effort (see IVI tips)
```

```python
dev.media("play-pause")
dev.set_wifi(True)
dev.set_bluetooth(False)
dev.set_airplane(True)
dev.set_hotspot(True)
```

### Screen & app launchers

**GUI** — Controls → Screen & Power / Apps & Web.

```bash
turboadb -s SERIAL screen off         # on | off
turboadb -s SERIAL settings           # open the Settings app
turboadb -s SERIAL open https://maps.google.com
turboadb -s SERIAL search "nearest charger"
turboadb -s SERIAL camera             # also: gallery, calculator
```

```python
dev.screen_off()
dev.open_settings()
dev.open_url("https://maps.google.com")
dev.web_search("nearest charger")
dev.open_camera()        # open_gallery(), open_calculator()
```

### On-screen keyboard

Type into the focused field (useful when the unit has no soft keyboard).

**GUI** — Controls → Keyboard: type, **Send**.

```bash
turboadb -s SERIAL text "hello world"
turboadb -s SERIAL key enter
```

```python
dev.input_text("hello world")
dev.keyevent("enter")
```

## Mirroring (scrcpy)

Mirror in its own window or embedded in the tab. A **compatibility mode**
(software decode, forced tunnel host/port, UHID keyboard) handles IVI units that
choke on the defaults, and it works through a remote adb server.

**GUI** — the **Mirror** tab / **Scrcpy** ribbon button → Mirror (window), Embed,
or the IVI/compatibility option. Pick a specific display on multi-display units,
or **▦ Mirror all** to show every display at once. The **🎮 Control + Mirror**
tab puts the screen and the device controls side by side, so you can watch and
tap/press without switching tabs. **📷 Camera** mirrors the device *camera*
instead of the screen — a live webcam-style view, front or back (chosen under
**⚙ Options**); needs scrcpy 2.2+ and Android 12+. The display list loads lazily
the first time you open the tab, so connecting never starts scrcpy on its own.

**CLI**:

```bash
turboadb -s SERIAL scrcpy --max-size 1280 --bit-rate 8M
turboadb -s SERIAL scrcpy --no-control --turn-screen-off
turboadb -s SERIAL scrcpy --video-source camera --camera-facing front
```

**Python**:

```python
from turboadb import ScrcpyOptions
sess = dev.mirror(ScrcpyOptions(max_size=1280, bit_rate="8M"))
sess.wait()          # blocks until the scrcpy window closes
```

## Screenshots & recording

**GUI** — the **Screenshot** ribbon button; record from the Mirror panel.

**CLI**:

```bash
turboadb -s SERIAL screenshot shot.png
turboadb -s SERIAL record clip.mp4 --time-limit 20 --size 1280x720
```

**Python**:

```python
dev.screenshot("shot.png")
dev.screen_record("clip.mp4", time_limit=20, size="1280x720")
```

> Recording uses device-side `screenrecord` and pulls the file back, so it works
> over RDP without a video tunnel.

## Webcam (host camera)

Different from the *device* camera (`scrcpy --video-source camera`): the **📹 Webcam**
tab views a **host** webcam — a USB or laptop camera on the machine running TurboADB.
Point one at the physical head unit / bench and watch it **beside** the scrcpy mirror.

**GUI** — click the **📹 Webcam** ribbon button (or **View → Open webcam**) to open
it as a standalone tab — no device needed. (It's also a per-device sub-tab, handy
beside the mirror.) Pick a **Source**:

- **Local (this PC / RDP session)** — **🔍 Scan cameras** → pick one → **▶ Start**.
- **Remote (RDP / Windows machine)** — enter the host + admin login → **Scan** →
  **Start**. No SSH: TurboADB uses the same WinRM/NTLM path as `deploy-serve` to run
  ffmpeg on the remote and stream its camera back over a direct TCP socket.

Then **Snapshot**, **Record** (clean H.264 MP4), **Pause**, **Rotate**/**Mirror**,
and Fill/Fit/Stretch.

> **Local works over RDP too.** Capture is local DirectShow, so when TurboADB runs
> inside an RDP session it sees whatever camera that session exposes. If none shows
> up: enable camera redirection in the RDP client (Local Resources → More… →
> Cameras), turn on Windows camera privacy ("Let desktop apps access your camera"),
> and make sure nothing else is using it.
>
> **Remote** needs WinRM on the target (`Enable-PSRemoting -Force`) and the account
> a local admin. ffmpeg is **provisioned automatically**: TurboADB first **copies your
> local `ffmpeg.exe` to the remote over its admin share** (`\\host\C$`, fast on a LAN
> and no internet needed there — like TurboSSH's push); if the share isn't reachable
> it falls back to the remote downloading ffmpeg itself, and failing that you can drop
> `ffmpeg.exe` in `C:\Windows\Temp\turboadb-ffmpeg\` over RDP. The host / user / domain
> are remembered and the **password is saved in the Windows Credential vault**
> (keyring), never in a file. A physical USB camera works headlessly; a camera
> redirected into someone's RDP session is only visible inside that session.
>
> ffmpeg powers the capture; locally it's downloaded once (~160 MB, cached under
> `~/.turboadb/ffmpeg`) or set **Settings → ffmpeg path** to your own.

## Telephony

**GUI** — the **Phone** tab.

**CLI**:

```bash
turboadb -s SERIAL dial 1800123456        # open dialer pre-filled
turboadb -s SERIAL call 1800123456        # place the call
turboadb -s SERIAL answer
turboadb -s SERIAL end-call
turboadb -s SERIAL call-log --limit 20
turboadb -s SERIAL sms --limit 20
turboadb -s SERIAL send-sms 1800123456 "on my way"
```

**Python**:

```python
dev.dial("1800123456"); dev.call("1800123456")
dev.answer_call(); dev.end_call()
for c in dev.call_log(20): print(c)
for m in dev.sms_list(20): print(m)
dev.send_sms("1800123456", "on my way")
```

## Root & mount

For rooted / engineering builds.

**GUI** — the **Root / Mount** ribbon dropdown.

**CLI**:

```bash
turboadb -s SERIAL root            # restart adbd as root  (unroot to undo)
turboadb -s SERIAL remount         # adb remount
turboadb -s SERIAL mount-rw        # mount -o remount,rw /
turboadb -s SERIAL disable-verity  # syncs and offers the required reboot
```

**Python**:

```python
dev.root(); dev.unroot()
dev.remount(); dev.mount_rw()
dev.disable_verity(); dev.enable_verity()
```

## Reboot

**GUI** — the **Reboot** dropdown (system / recovery / bootloader / sideload —
the risky ones warn first, doubly so on automotive).

```bash
turboadb -s SERIAL reboot
turboadb -s SERIAL reboot recovery       # recovery | bootloader | sideload
```

```python
dev.reboot()
dev.reboot("recovery")
```

## Device info

**GUI** — shown in the tab header and the log on connect; **ℹ Build info** /
**🔋 Battery** in Controls.

```bash
turboadb -s SERIAL info --json
turboadb -s SERIAL build-info
turboadb -s SERIAL battery
```

```python
d = dev.device_info()    # dict: manufacturer, model, android_version, sdk, abi, automotive…
print(d["model"], d["android_version"], d["automotive"])
print(dev.battery())
```

## Remote devices

Drive a device that's plugged into a **different** machine, over its adb server —
exactly what you want for a lab unit reached by RDP. Add `--adb-host` to any CLI
command, or set `adb_server_host` in the config. (The host machine has to be
sharing its adb server — see the next two sections.)

```bash
turboadb --adb-host lab-pc-01 devices
turboadb --adb-host lab-pc-01 -s DEVICE shell -- pm list packages
turboadb --adb-host lab-pc-01 -s DEVICE scrcpy --max-size 1280
```

```python
dev = ADBHandler(ADBConfig(adb_server_host="lab-pc-01", serial="DEVICE"))
dev.connect()
```

## Share devices (serve)

Turn the machine a device is plugged into a host others can reach.

**GUI** — **ADB Server ▸ Share this PC's devices** (offers "start once" or "start
+ run at login").

**CLI**:

```bash
turboadb serve                    # start the shared server + open the firewall
turboadb serve --startup-task     # …and keep it running headless across reboots
turboadb serve --uninstall-startup
```

**Python**:

```python
from turboadb.devices import start_shared_server, open_firewall, install_serve_task
print(start_shared_server())
print(open_firewall((5037, 27184)))
install_serve_task()              # SYSTEM startup task, headless
```

## Deploy serve over WinRM

Push `serve` onto remote Windows hosts **from your machine** — one host or a whole
fleet — without logging into each. Uses pywinrm/NTLM, so domain credentials work
over plain WinRM.

**GUI** — the **ADB Server** button: enter the host(s) + admin credentials,
**Test connection**, then **Deploy**.

**CLI**:

```bash
turboadb deploy-serve lab-pc-01 lab-pc-02 -u "DOMAIN\user"
turboadb deploy-serve lab-pc-01 -u "DOMAIN\user" --test   # just check WinRM first
```

**Python**:

```python
from turboadb.remote_deploy import deploy_serve
deploy_serve(["lab-pc-01", "lab-pc-02"], "DOMAIN\\user", "password",
             on_status=print)
```

> Each target needs WinRM enabled (`Enable-PSRemoting -Force` once), your account a
> local admin, and Python + turboadb installed there.

## Keep things up to date

**GUI** — the **Upgrade** ribbon button checks PyPI for a newer TurboADB, updates
it, refreshes `adb`/`scrcpy`, and restarts.

```bash
turboadb self-update      # upgrade TurboADB itself, then adb/scrcpy
turboadb upgrade-tools    # only refresh adb/scrcpy
turboadb doctor           # report what's installed / missing
```

---

## CLI cheatsheet

`turboadb -h` lists everything; `turboadb <command> -h` details one. Most read
commands take `--json`. Target with `-s SERIAL`; add `--adb-host HOST` for a
remote server.

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

## Python API notes

`import turboadb` exposes the engine and its types. Calls return small
dataclasses — `CommandResult` (`.stdout`, `.stderr`, `.exit_code`, `.ok`),
`TransferResult`, `StreamResult`. Construct a handler with `safe=True` for
non-raising `OperationResult`s, or leave it default to get exceptions you can
catch (`ADBError` and friends). See [`examples/examples.py`](examples/examples.py)
and [ARCHITECTURE.md](ARCHITECTURE.md).

```python
from turboadb import ADBHandler, ADBConfig, ScrcpyOptions

dev = ADBHandler(ADBConfig(serial="SERIAL"))
dev.connect()
assert dev.shell("getprop ro.build.version.release").ok
dev.install("app.apk", grant_perms=True)
dev.screenshot("after_install.png")
```

## Android Automotive / IVI tips

- `device_info()` flags `automotive`; the GUI adapts (e.g. the mirror label).
- If the default mirror fails, use **compatibility mode** (software decode).
- `bootloader` / `sideload` reboots warn hard — many head units have no on-screen
  recovery UI and can get stuck.
- Calculator / Camera / Play Store are often absent; launchers fall back and the
  log says when nothing happened.
- Hotspot can't always be toggled purely over adb (uid permissions) — TurboADB
  tries `cmd wifi`, then falls back to opening the tethering settings.

## Build from source

```bash
git clone https://github.com/NVNKENNEDY/turboadb && cd turboadb
pip install -r requirements.txt
python -m turboadb.gui            # run the GUI from source
python scripts/build_exe.py       # rebuild the bundled Windows exe
python tests/test_offline.py      # offline checks
```

See [ARCHITECTURE.md](ARCHITECTURE.md) and [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE).
