<div align="center">
  <img src="https://raw.githubusercontent.com/NVNKENNEDY/turboadb/main/turboadb/assets/icon.png" alt="TurboADB" width="96" height="96">

  # TurboADB

  **One pip package for driving Android over ADB + scrcpy — a Python API, a full
  CLI, and a desktop GUI. Built for Android Automotive / IVI head units and
  regular phones alike.**

  [![PyPI](https://img.shields.io/pypi/v/turboadb.svg)](https://pypi.org/project/turboadb/)
  [![Python](https://img.shields.io/pypi/pyversions/turboadb.svg)](https://pypi.org/project/turboadb/)
  [![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/NVNKENNEDY/turboadb/blob/main/LICENSE)

  **[🌐 Website](https://nvnkennedy.github.io/turboadb/) · [⤓ Download for Windows](https://github.com/NVNKENNEDY/turboadb/releases/latest) · [📦 PyPI](https://pypi.org/project/turboadb/)**
</div>

---

> **New in 2.0.0:** a redesigned window with colourful icons, a **Phone** tab,
> device type detection (car, head unit, TV, watch, tablet, phone), calmer
> themes, real PowerShell / Command Prompt tabs, and the Windows executable
> inside the pip package. Full notes in the
> [changelog](https://github.com/NVNKENNEDY/turboadb/blob/main/CHANGELOG.md).

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
- [The app at a glance](#the-app-at-a-glance) — top bar, sidebar, device tabs, themes
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

1. Download **`TurboADB-2.0.0-win64.exe`** from the
   **[latest GitHub Release](https://github.com/NVNKENNEDY/turboadb/releases/latest)**
   (also linked from the [website](https://nvnkennedy.github.io/turboadb/)).
2. Double-click it. On first launch it downloads `adb` + `scrcpy` automatically
   (about 20 seconds) and adds a desktop shortcut.

### B · With pip — CLI, Python API, and GUI

```bash
pip install turboadb
```

On Windows this already includes the app: the package carries the same
executable (which is why the download is about 59 MB), so `turboadb-gui` starts
it even without PyQt5. To run the GUI from the Python code instead — on Linux or
macOS, for example — add the GUI extra:

```bash
pip install "turboadb[gui]"
```

Then use any of: `turboadb-gui` (the app), `turboadb <command>` (the CLI), or
`import turboadb` (the API). Other optional extras are:

```bash
pip install "turboadb[winrm]"  # remote 'serve' deploy over WinRM
pip install "turboadb[all]"    # GUI + WinRM support
```

Upgrade any time with `pip install --upgrade turboadb`.

`adb` and `scrcpy` download themselves into `~/.turboadb/tools` on first use, so
they never need to be on your PATH.

## First, target a device

Everything operates on one device. How you point at it is the only thing that
changes between local, network, and remote.

**In the GUI** — click **Connect ▾ → Connect to a device…** and choose how it is
connected: **USB**, **Network** (IP and port) or **Remote** (a device on another
PC's adb server). Attached devices also show live under **Connected** in the
left sidebar; double-click one to open it in a tab. **Connect ▾** also has
**Discover Wi-Fi devices** and **Pair device** (Android 11+), and a USB device
can switch itself to Wi-Fi with **More ▾ → Go wireless (USB → Wi-Fi)**.

**CLI** — list first, then pass `-s`:

```bash
turboadb devices                                  # what's attached here
turboadb -s 10BE330KG9000AF info                  # a USB serial
turboadb -s 192.168.1.50:5555 info                # a network device
turboadb connect 192.168.1.50:5555                # adb connect first if needed
turboadb -s SERIAL wireless                       # USB device -> Wi-Fi adb in one step
turboadb discover                                 # find wireless-debugging devices
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

## The app at a glance

- **Top bar** — **Connect ▾** (connect, save a target, discover Wi-Fi devices,
  pair, restart ADB), **ADB server ▾** (deploy to remote machines, share this
  PC's devices, stop sharing), **Tools ▾** (check for updates, reinstall ADB and
  scrcpy, open the host webcam, create shortcuts), then icons for the theme, the
  log panel, settings and help.
- **Devices sidebar** — **Connected** devices and **Saved targets**. Type in the
  search box to filter or quick-connect to a host; hide the sidebar with **‹** or
  Ctrl+B.
- **Device tabs** — every device gets **Terminal**, **Logcat**, **Files**,
  **Device Control**, **Apps**, **Phone** and **Webcam** (plus **IVI Displays**
  on cars). **Screen ▾**, **Screenshot**, **Reboot ▾** and **More ▾** sit at the
  right end of that row. **More ▾** holds **Split view** (Terminal, Device
  Control, Files and Logcat side by side, stacked or in a grid), **Device
  health…**, **Build details…**, **Root and mount**, **Go wireless (USB → Wi-Fi)**
  and **Capture bugreport…**.
- **Device type** — TurboADB detects Android Automotive, an infotainment head
  unit, TV, watch, tablet or phone and shows it in the terminal's welcome banner.
  Cars get the IVI-compatible screen profile and the **IVI Displays** tab.
- **Themes** — Graphite (dark) and Porcelain (light) by default, plus Mocha /
  Latte, Forest / Sage, Plum / Rose and Deep teal / Mint. The top-bar icon
  switches to the other half of the pair; pick any theme from the **Themes** menu
  or **Settings → Themes**.
- **Notifications** — every action shows in the status bar and a small toast;
  errors show a red popup with a sound and a **Copy** button. The log panel (its
  top-bar icon) filters Normal, Verbose, Warnings + Errors or Errors only.
- **Settings** — Appearance (terminal font), Tools (adb / scrcpy / ffmpeg paths),
  scrcpy (video and audio), Logcat, Themes and Startup.
- **Menu bar** — File (new target, save output, export / import saved targets),
  View, Themes, Device (including **Run a command on ALL devices…**), Tools and
  Help.

---

# Feature guide

## Interactive shell

A real terminal, not a one-shot: history, `Tab` completion, copy/paste, and a
**Stop** button that actually kills a runaway command like `logcat` (there's no
PTY, so a plain `Ctrl+C` can't — Stop tears the shell down, kills the device-side
process, and reopens, keeping your working directory). A bare `ls` is shown in
columns.

**In the GUI** — open a device → **Terminal** tab → start typing. A boxed
two-line banner at the top shows the connection, device type, Android version,
CPU and serial. Right-click for Copy / Paste / **Send key** / **Save full output
to file…**; **Stop** (or Ctrl+C) halts whatever is running. **A−** / **A+** (or
Ctrl + mouse wheel) resize every terminal together.

The **Android / PowerShell / CMD** switcher at the left of the toolbar opens
PowerShell or Command Prompt on this PC, with TurboADB's `adb` first on PATH and
`ANDROID_SERIAL` set to the device. They behave like a normal console: Python,
Node, Git, `where` and programs in the current folder all work.

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

**In the GUI** — **Logcat** tab → pick the level and how much history to include
(**Live from now**, **Last 1,000 + live**, …), optionally a **Tag**, a live
**Filter** regex or a **Highlight** pattern such as `error|anr` → **Start**.
**Crashes** is a one-click preset, **Pause** / **Clear** do what they say, and
**Save…** writes everything captured.

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

**In the GUI** — **Files** tab: **This PC** on one side, **Device** on the other.
Select files and press **Push** or **Pull**, or drag and drop between the panes
(also from Explorer). Both panes have **New folder**, **New file**, **Edit** (F4,
a built-in editor), **Copy** / **Paste**, **Rename** (F2) and **Delete**, plus
quick folders such as Downloads and `/sdcard`.

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

**In the GUI** — **Apps** tab: **Install APK(s)…**, or filter the package list
(**Third-party only**) and select one for **Start**, **Stop**, **Clear data** or
**Uninstall**.

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

The **Device Control** tab has a **Device controls** panel beside the screen with
one-click actions. Each is also a CLI command and an API call.

### Keys & input

**GUI** — Device controls → **Navigation** (Back, Home, Recents, Power,
Notifications, Settings).

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

**GUI** — Device controls → **Media & volume** and **Quick settings** (Wi-Fi,
Bluetooth, Mobile data, Airplane mode, Hotspot, each with On and Off).

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

**GUI** — Device controls → **Screen & power** and **Apps & web** (a URL or
search box plus Browser, YouTube, Spotify, Maps, Play Store, Gallery, Calculator
and Camera tiles).

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

**GUI** — Device controls → **Keyboard**: type, then **Send** (Enter, Backspace,
Space, Tab, Esc and Search keys are next to it). While the screen is showing you
can also click it and type on your PC keyboard, or use **Options → Type or paste
text…**.

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

**GUI** — open **Device Control**, pick a display, then press **Start screen**
to show it in the tab or **Separate window** (**Screen ▾** beside the section
tabs does the same from any tab). Click the screen and type on your PC keyboard.
The toolbar also has **Stop**, a screenshot icon, **Record…**, **Audio on / off**,
**Options**, **Maximize view** and **Hide controls** (hides the side panel
without stopping the screen).

**Options** holds audio forwarding and its source, **Compatibility mode (IVI /
automotive)**, software rendering, the keyboard mode (Standard SDK or UHID
hardware keyboard), **Device camera** with Back / Front — the device's own camera
instead of its screen, needing scrcpy 2.2+ and Android 12+ — and **Manage
displays…** to start, record or screenshot each display. On cars the **IVI
Displays** tab opens the **IVI display wall**: live previews of every display,
each with Control, Maximize, Screenshot and Record. Audio needs Android 11+; tune
codec, bitrate and latency in **Settings → scrcpy**. The display list loads the
first time you open the tab, so connecting never starts scrcpy on its own.

**CLI**:

```bash
turboadb -s SERIAL scrcpy --max-size 1280 --bit-rate 8M
turboadb -s SERIAL scrcpy --bit-rate 16M --audio-source playback --audio-dup
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

**GUI** — **Screenshot** at the right end of the section tabs (or the screenshot
icon in Device Control); **Record…** in the Device Control screen toolbar. If
the screen is already live, a second, windowless recorder runs beside it, so the
view never restarts, and stopping saves a playable MP4.

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

> The CLI `record` command and `screen_record()` use device-side `screenrecord`
> and pull the file back, so they work over RDP without a video tunnel.

## Webcam (host camera)

Different from the *device* camera (`scrcpy --video-source camera`): the **📹 Webcam**
tab views a **host** webcam — a USB or laptop camera on the machine running TurboADB.
Point one at the physical head unit / bench and watch it **beside** the scrcpy mirror.

**GUI** — choose **Tools ▾ → Open host webcam** (or **View → Open webcam (host
camera)**, or the **Host webcam** tile on the start page) to open it as a
standalone tab — no device needed. Every device also has its own **Webcam** tab,
handy beside the screen. Pick a **Source**:

- **Local — this PC** — **Scan cameras** → pick a camera, resolution and frame
  rate → **Start camera**.
- **Remote — Windows PC** — enter the **RDP host**, **User**, **Domain** and
  **Password** → **Scan cameras** → **Start camera**. No SSH: TurboADB uses the
  same WinRM/NTLM path as `deploy-serve` to run ffmpeg on the remote and stream
  its camera back over a direct TCP socket.

Then **Snapshot**, **Record** (clean H.264 MP4), **Pause**, **Rotate**, **Flip**,
and **View**: Fit, Fill or Stretch. Right-click the video to copy the image.

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

**GUI** — open a device and pick its **Phone** tab. Calls and messages load the
first time you open the tab, never while connecting.

- **Dialler** — type or paste a number, or use the keypad (hold **0** for **+**).
  **Call** places the call, **End** hangs up or rejects, **Answer** picks up a
  ringing call, and **Open in dialler** (or **Enter** in the number field) only
  opens the device dialler pre-filled. The toolbar pill shows **Idle**,
  **Ringing** or **In call**, or **No telephony** on devices without it (e.g. IVI
  head units). **Refresh** reloads the call state, calls and messages.
- **Recent calls** — incoming, outgoing, missed and rejected calls with their
  time and duration. Double-click a call or press **Call back** to put its number
  in the dialler (it never calls by itself); **Message** starts a message to it.
- **Messages** — received and sent SMS with a one-line preview. Select one to
  address a reply, type under **New message** and press **Compose in Messages**:
  the device's Messages app opens with the draft, and you press Send there.

If the device refuses access to its call log or messages, the list says so in
place of the rows and the log gets one warning.

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

**GUI** — **More ▾ → Root and mount** (at the right end of the section tabs).

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

**GUI** — **Reboot ▾** (System / Recovery / Bootloader / Sideload — the risky
ones warn first, doubly so on automotive).

```bash
turboadb -s SERIAL reboot
turboadb -s SERIAL reboot recovery       # recovery | bootloader | sideload
```

```python
dev.reboot()
dev.reboot("recovery")
```

## Device info

**GUI** — the Android terminal's welcome banner shows the connection, device
type, Android version, CPU and serial on connect. **More ▾ → Device health…**
and **Build details…** show the rest, and **Battery** is in Device controls →
Screen & power.

```bash
turboadb -s SERIAL info --json      # includes kind, kind_label, display_size, telephony
turboadb -s SERIAL build-info
turboadb -s SERIAL battery
turboadb -s SERIAL health           # battery, temperature, memory, CPU, uptime
turboadb -s SERIAL bugreport        # save a full bug report zip
```

```python
d = dev.device_info()    # dict: manufacturer, model, android_version, sdk, abi, automotive, kind…
print(d["model"], d["android_version"], d["kind_label"])
kind = dev.device_kind() # {"kind": "phone" | "tablet" | "tv" | "watch" | "automotive" | "headunit", …}
print(kind["label"], "because", kind["reason"])
print(dev.battery())     # raw dumpsys battery text
print(dev.health())      # dict: battery, temperature, memory, CPU, uptime
```

The device type comes from the device itself: Android Automotive OS reports the
`android.hardware.type.automotive` feature (or `automotive` in
`ro.build.characteristics`); an infotainment head unit running ordinary Android
has no telephony feature and a natively landscape display; TVs, watches and
tablets report their own feature or characteristic. Both car kinds set
`automotive`, which selects the IVI-compatible screen profile in the GUI.

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

**GUI** — **ADB server ▾ → Share THIS PC's devices…** (**Start once** or **Start
+ run at login**; opening the firewall needs Administrator). **ADB server ▾ →
Stop sharing & remove auto-start** undoes it.

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
print(open_firewall((5037, "27184-27199")))
install_serve_task()              # SYSTEM startup task, headless
```

## Deploy serve over WinRM

Push `serve` onto remote Windows hosts **from your machine** — one host or a whole
fleet — without logging into each. Uses pywinrm/NTLM, so domain credentials work
over plain WinRM.

**GUI** — **ADB server ▾ → Deploy to remote machine(s) (RDP / WinRM)…**: enter
the host(s) + admin credentials, **Test connection**, then **Deploy**.

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

**GUI** — **Tools ▾ → Check for updates…** checks PyPI for a newer TurboADB,
updates it, refreshes `adb`/`scrcpy`, and restarts (with no newer TurboADB it
still checks the tools). **Tools ▾ → Download and reinstall ADB and scrcpy…**
refreshes only the tools.

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
connect  disconnect  pair  tcpip  wireless  discover  restart-server
reboot  root  unroot  remount  mount-rw  disable-verity  enable-verity
key  text  scroll  tap  media  brightness  wifi  bluetooth  airplane
hotspot  screen  settings  open  search  camera  gallery  calculator
close-apps  battery  health  bugreport  build-info  dial  call  end-call
answer  call-log  sms  send-sms  serve  deploy-serve  doctor  fetch-tools
upgrade-tools  self-update  shortcut  gui
```

## Python API notes

`import turboadb` exposes the engine and its types. Calls return small
dataclasses — `CommandResult` (`.stdout`, `.stderr`, `.exit_code`, `.ok`),
`TransferResult`, `StreamResult`. Construct a handler with `safe=True` for
non-raising `OperationResult`s, or leave it default to get exceptions you can
catch (`ADBError` and friends). See [`examples/examples.py`](https://github.com/NVNKENNEDY/turboadb/blob/main/examples/examples.py)
and [ARCHITECTURE.md](https://github.com/NVNKENNEDY/turboadb/blob/main/ARCHITECTURE.md).

```python
from turboadb import ADBHandler, ADBConfig, ScrcpyOptions

dev = ADBHandler(ADBConfig(serial="SERIAL"))
dev.connect()
assert dev.shell("getprop ro.build.version.release").ok
dev.install("app.apk", grant_perms=True)
dev.screenshot("after_install.png")
```

## Android Automotive / IVI tips

- `device_info()` flags `automotive` for Android Automotive OS and for head units
  running ordinary Android (see `device_kind()` in Device info); the GUI then
  uses the IVI-compatible screen profile and exposes the IVI display wall in
  Device Control → Options, with a preview and action set for every display.
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
python scripts/build_exe.py       # build the versioned Windows exe in dist/
python tests/test_offline.py      # offline checks
```

See [ARCHITECTURE.md](https://github.com/NVNKENNEDY/turboadb/blob/main/ARCHITECTURE.md) and [CHANGELOG.md](https://github.com/NVNKENNEDY/turboadb/blob/main/CHANGELOG.md).

## License

[MIT](https://github.com/NVNKENNEDY/turboadb/blob/main/LICENSE).
