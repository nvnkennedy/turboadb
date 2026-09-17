# TurboADB command line reference

Every `turboadb` command, with its options and examples. The same reference is
on the website: <https://nvnkennedy.github.io/turboadb/cli.html>.

`turboadb -h` lists the commands and `turboadb <command> -h` shows one command's
options, straight from the installed version.

## Contents

- [Getting started](#getting-started)
- [Global options](#global-options) · [Output and exit codes](#output-and-exit-codes) · [Environment variables](#environment-variables)
- Commands:
  [Setup and tools](#setup-and-tools) ·
  [Devices and connection](#devices-and-connection) ·
  [Shell, logs and reports](#shell-logs-and-reports) ·
  [Files](#files) ·
  [Apps](#apps) ·
  [Screen, capture and mirroring](#screen-capture-and-mirroring) ·
  [Keys and input](#keys-and-input) ·
  [Connectivity](#connectivity) ·
  [Open apps and the web](#open-apps-and-the-web) ·
  [Device status](#device-status) ·
  [Phone and messages](#phone-and-messages) ·
  [Root and system](#root-and-system) ·
  [Port forwarding](#port-forwarding) ·
  [Sharing devices with other PCs](#sharing-devices-with-other-pcs)
- [Other console scripts](#other-console-scripts) · [Shell tab completion](#shell-tab-completion)

---

## Getting started

```bash
pip install turboadb
turboadb doctor                                   # are adb and scrcpy available?
turboadb devices                                  # what's attached
turboadb -s 10BE330KG9000AF info                  # talk to one device by serial
turboadb -s 192.168.1.50:5555 screenshot shot.png # …or a network device
turboadb --adb-host lab-pc-01 -s DEVICE info      # …or a device on another PC
turboadb targets add bench network 192.168.1.50   # save a target once…
turboadb -s @bench info                           # …and use it by name
```

`adb` and `scrcpy` download themselves into `~/.turboadb/tools` on first use, so
they don't need to be on your PATH.

> **Git Bash on Windows** rewrites arguments that look like Unix paths, so
> `turboadb ls /sdcard` reaches the device as `/C:/Program Files/Git/sdcard`. Run
> `export MSYS_NO_PATHCONV=1` first (or write `//sdcard`). PowerShell and Command
> Prompt are not affected.

## Global options

These work on every device command, **before or after** the command name
(`turboadb -s SERIAL info` and `turboadb info -s SERIAL` are the same).

| Option | Meaning |
|---|---|
| `-s`, `--serial SERIAL` | The device: a USB serial, or `host:port` for a network device. Omit it when only one device is attached. `-s @NAME` uses a saved target (see [`targets`](#turboadb-targets)). |
| `--adb-host HOST` | Use the adb server on another machine, to drive a device plugged into that PC (it must share its server — see [`serve`](#turboadb-serve)). |
| `--adb-port PORT` | adb server port (default `5037`). |
| `--adb-path PATH` | Use this adb executable. |
| `--scrcpy-path PATH` | Use this scrcpy executable. |
| `--timeout SECONDS` | Timeout for each command, and for push, pull and `bugreport` too (defaults: 60 s for commands, 600 s for transfers). |
| `--json` | Machine-readable output (see below). |
| `-V`, `--version` | Print the TurboADB version. |

`doctor`, `fetch-tools`, `upgrade-tools`, `self-update`, `shortcut`, `gui` and
`deploy-serve` don't talk to a device, so of these options they take only
`--json` (before or after the command name, like everything else).

## Output and exit codes

With `--json`, every command prints **one** JSON document on stdout and nothing
else; progress and warnings go to stderr, so `turboadb … --json | jq` always
works.

Commands that return data print it as JSON (`devices`, `info`, `shell`, `adb`,
`push`, `pull`, `ls`, `stat`, `packages`, `displays`, `getprop`, `battery`,
`health`, `build-info`, `call-log`, `sms`, `phone-support`, `targets list`,
`forward --list`, `doctor`, `fetch-tools`, `upgrade-tools` …). Actions print
`{"ok": true, "message": "…"}`; commands that return a value (`pair`,
`disconnect`, `restart-server`, `connect`, `scrcpy` → `{"pid": N}` …) print
`{"ok": true, "result": …}`; commands that save a file print
`{"ok": true, "path": "…"}`. `health --full` and `build-info --full` print
`{"ok": true, "report": "…"}` unless `-o FILE` is given, `record` prints
`{"ok": true, "paths": [...]}` (every part of a `--continuous` recording), and
`logcat` streams plain lines.

| Exit code | Meaning |
|---|---|
| `0` | Success. |
| `1` | The action failed or the device refused it, an adb error, or invalid input (the reason is printed as one `ERROR:` line). |
| `2` | Wrong command-line usage (for example a missing argument). |
| `3` | adb could not be found — run `turboadb fetch-tools` or set `TURBOADB_ADB`. |
| `130` | Stopped with Ctrl+C. |

`shell` and `adb` exit with the device command's own exit code.

## Environment variables

| Variable | Effect |
|---|---|
| `TURBOADB_ADB` | Path to the adb executable to use. |
| `TURBOADB_SCRCPY` | Path to the scrcpy executable to use. |
| `TURBOADB_AUTO_FETCH=0` | Never download adb / scrcpy automatically (offline, CI or locked-down machines). |
| `VISUAL`, `EDITOR` | The editor `turboadb edit` opens. |

---

## Setup and tools

### `turboadb doctor`
Report whether adb and scrcpy were found, and where. With `--json` it prints the
whole diagnosis as one object.
```bash
turboadb doctor
turboadb doctor --json
```

### `turboadb fetch-tools`
Download adb (and scrcpy) into `~/.turboadb/tools`. `--adb-only` and
`--scrcpy-only` are mutually exclusive — asking for both leaves nothing to
download.

| Option | Meaning |
|---|---|
| `--adb-only` | Only platform-tools (adb). |
| `--scrcpy-only` | Only scrcpy. |
| `--force` | Download again even if present. |
| `--json` | Print what was downloaded (and any errors) as one object. |

```bash
turboadb fetch-tools
turboadb fetch-tools --scrcpy-only --force
```

### `turboadb upgrade-tools`
Update adb and scrcpy, downloading only when a newer version exists.

| Option | Meaning |
|---|---|
| `--check` | Only report what would be updated. |
| `--json` | Print the version check (with `--check`) or what was updated. |

```bash
turboadb upgrade-tools --check
turboadb upgrade-tools --check --json
```

### `turboadb self-update`
Upgrade TurboADB itself from PyPI, then adb and scrcpy.

| Option | Meaning |
|---|---|
| `--check` | Only report whether a newer TurboADB exists. |

```bash
turboadb self-update --check
turboadb self-update
```

### `turboadb shortcut`
Create Desktop and Start-menu shortcuts to the GUI (Windows).

| Option | Meaning |
|---|---|
| `--json` | Print the outcome as `{"ok": …, "message": "…"}`. |

```bash
turboadb shortcut
```

### `turboadb gui`
Launch the desktop app (the same as `turboadb-gui`).
```bash
turboadb gui
```

---

## Devices and connection

### `turboadb devices`
List the devices on the local adb server, or on another PC's with `--adb-host`
(or `-s @NAME` for a saved remote target — the list comes from *that* server).
```bash
turboadb devices
turboadb --adb-host lab-pc-01 devices --json
turboadb -s @lab-pc devices
```

### `turboadb info`
Connect and print the device's identity and build: manufacturer, model, Android
version, SDK, CPU, device type (`kind`: phone, tablet, tv, watch, automotive,
headunit), telephony and display size.
```bash
turboadb -s SERIAL info
turboadb -s SERIAL info --json
```

### `turboadb connect`
`adb connect` to a network device.

| Argument | Meaning |
|---|---|
| `HOST:PORT` | The device's address, for example `192.168.1.50:5555`. |

```bash
turboadb connect 192.168.1.50:5555
```

### `turboadb disconnect`
`adb disconnect` one network device, or all of them when no address is given.
```bash
turboadb disconnect 192.168.1.50:5555
turboadb disconnect
```

### `turboadb pair`
Pair with an Android 11+ device using Wireless debugging.

| Argument | Meaning |
|---|---|
| `HOST:PORT` | The pairing address shown on the device. |
| `CODE` | The six-digit pairing code shown on the device. |

```bash
turboadb pair 192.168.1.50:37123 482913
```

### `turboadb tcpip`
Restart adbd on a USB device in TCP mode.

| Argument | Meaning |
|---|---|
| `PORT` | Optional, default `5555`. |

```bash
turboadb -s SERIAL tcpip 5555
```

### `turboadb wireless`
Switch a USB device to Wi-Fi adb in one step: read its IP address, run
`adb tcpip`, then connect. Afterwards the cable can be unplugged.

| Argument | Meaning |
|---|---|
| `PORT` | Optional, default `5555`. |

```bash
turboadb -s SERIAL wireless
```

### `turboadb discover`
Find Android 11+ devices with Wireless debugging on the network (adb mDNS).

| Option | Meaning |
|---|---|
| `--connect` | Also connect every device that is ready to connect. |

```bash
turboadb discover
turboadb discover --connect
```

### `turboadb restart-server`
Kill and start the adb server. Fixes devices that don't show up after an adb
version mismatch.
```bash
turboadb restart-server
```

### `turboadb wait`
Wait until the device is online (`adb wait-for-device`).

| Argument | Meaning |
|---|---|
| `SECONDS` | Optional: give up after this long (exit code `1`). |

```bash
turboadb -s SERIAL wait 60
```

### `turboadb state`
The device's adb state: `device`, `offline`, `unauthorized`, `recovery`,
`bootloader`, `sideload` or `unknown`. Works while the device is offline or
rebooting.
```bash
turboadb -s SERIAL state
```

### `turboadb serialno`
The device's serial number (`adb get-serialno`).
```bash
turboadb -s 192.168.1.50:5555 serialno
```

### `turboadb ip`
The device's Wi-Fi or Ethernet IP address.
```bash
turboadb -s SERIAL ip
```

### `turboadb targets`
Saved device targets — the same list as the GUI sidebar. Use one anywhere with
`-s @NAME`.

| Subcommand | Meaning |
|---|---|
| `list` | Show the saved targets (`--json` for JSON). |
| `add NAME usb [SERIAL]` | A USB device (no serial: the only attached device). |
| `add NAME network HOST[:PORT]` | A network device (port `5555` by default). |
| `add NAME remote ADBHOST[:PORT] [SERIAL]` | A device on another PC's shared adb server. |
| `remove NAME` | Delete a saved target. |
| `export FILE` | Write every target to a JSON file. |
| `import FILE` | Merge targets from a JSON file. |

```bash
turboadb targets add bench network 192.168.1.50
turboadb targets add lab remote lab-pc-01 10BE330KG9000AF
turboadb -s @lab screenshot lab.png
turboadb targets export targets.json
```

---

## Shell, logs and reports

### `turboadb shell`
Run a command on the device. Put `--` before the command so its own options
aren't read as TurboADB options. With no command, it opens an interactive shell.

| Option | Meaning |
|---|---|
| `--su` | Run it as root (`su -c`). |
| `--all` | Run the command on every online device, one after another — on the adb server `--adb-host` or `-s @NAME` selects. |
| `--batch FILE` | Run each line of FILE as a command, in order (blank and `#` lines are skipped). Stops at the first failing command. |
| `--keep-going` | With `--batch`: carry on after a failing command. |
| `COMMAND…` | The command and its arguments. |

```bash
turboadb -s SERIAL shell
turboadb -s SERIAL shell -- getprop ro.build.version.release
turboadb -s SERIAL shell --su -- "cat /data/misc/file"
turboadb shell --all -- getprop ro.product.model
turboadb -s SERIAL shell --batch setup.txt --keep-going
```

### `turboadb adb`
Run any adb command for the selected device, with `-s`, `--adb-host` and the
timeout added for you. Everything after `--` goes to adb.
```bash
turboadb -s @bench adb -- get-devpath
turboadb --adb-host lab-pc-01 -s DEVICE adb -- shell ls /sdcard
```

### `turboadb logcat`
Stream logcat live, with filters, and save it to a file.

| Option | Meaning |
|---|---|
| `--tag TAG` | Only this tag. |
| `--priority P` | Minimum level: `V`, `D`, `I`, `W`, `E` or `F`. |
| `--filter TAG:LEVEL` | A logcat filter spec, for example `ActivityManager:I`; repeat for several. Replaces `--tag` and `--priority`. |
| `--buffer NAME` | Buffer to read (`main`, `system`, `crash`, `radio`, `events`); repeat for several. |
| `--crashes` | Crashes and errors only: the `crash`, `main` and `system` buffers at level `E`. |
| `--format FORMAT` | Output format (default `threadtime`). |
| `--grep REGEX` | Print only lines matching this regular expression (case-insensitive). |
| `--match REGEX` | Count lines matching this regular expression, and report the total at the end. |
| `--stop-on-match` | Stop at the first line matching `--match`. |
| `--save FILE` | Also write the lines to a file. |
| `--clear` | Clear the buffers first. |
| `--dump` | Print the current buffer and exit instead of streaming. |
| `--tail N` | Only the last N lines: with `--dump` the last N buffered lines; otherwise the live stream starts there. |

```bash
turboadb -s SERIAL logcat --tag ActivityManager --priority W
turboadb -s SERIAL logcat --filter ActivityManager:I --filter CarService:D
turboadb -s SERIAL logcat --crashes --dump
turboadb -s SERIAL logcat --grep "bluetooth|a2dp" --save bt.log
turboadb -s SERIAL logcat --match "ANR|FATAL" --stop-on-match
turboadb -s SERIAL logcat --dump --tail 500
```

### `turboadb logcat-clear`
Clear the logcat buffers (`logcat -c`).
```bash
turboadb -s SERIAL logcat-clear
```

### `turboadb bugreport`
Capture a full `adb bugreport`. It is slow — a few minutes is normal. `--timeout`
sets how long it may take (default 15 minutes).

| Argument | Meaning |
|---|---|
| `PATH` | Optional output file (a `.zip`). |

```bash
turboadb -s SERIAL bugreport
turboadb -s SERIAL bugreport reports/head-unit.zip --timeout 1800
```

---

## Files

Device paths are checked before anything changes: a missing path, or a folder
without `-r`, is an error and nothing is touched.

### `turboadb push`
Upload a file or folder to the device, with progress.
```bash
turboadb -s SERIAL push app.apk /data/local/tmp/
turboadb -s SERIAL push ./maps /sdcard/maps --timeout 3600
```

### `turboadb pull`
Download a file or folder from the device, with progress.
```bash
turboadb -s SERIAL pull /sdcard/Download/log.txt .
turboadb -s SERIAL pull /sdcard/DCIM ./photos --json
```

### `turboadb ls`
List a device folder, folders first: permissions, owner, size, date and name.

| Argument | Meaning |
|---|---|
| `PATH` | Optional, default `/sdcard`. |

```bash
turboadb -s SERIAL ls /sdcard/Download
turboadb -s SERIAL ls /data/local/tmp --json
```

### `turboadb mkdir`
Create one or more device folders, with any missing parents.
```bash
turboadb -s SERIAL mkdir /sdcard/logs/today
```

### `turboadb touch`
Create empty device files.
```bash
turboadb -s SERIAL touch /sdcard/marker.txt
```

### `turboadb rm`
Delete device files. `/` is always refused.

| Option | Meaning |
|---|---|
| `PATH…` | One or more paths. |
| `-r`, `--recursive` | Also delete folders and everything in them. |

```bash
turboadb -s SERIAL rm /sdcard/old.log
turboadb -s SERIAL rm -r /sdcard/logs
```

### `turboadb mv`
Move or rename a device file or folder. When the destination is an existing
folder, the item moves into it.

| Option | Meaning |
|---|---|
| `-f`, `--force` | Replace an existing destination. |

```bash
turboadb -s SERIAL mv /sdcard/a.txt /sdcard/b.txt
turboadb -s SERIAL mv /sdcard/clip.mp4 /sdcard/Movies
```

### `turboadb cp`
Copy a device file or folder. Into an existing folder, it merges; a folder is
never copied into itself.
```bash
turboadb -s SERIAL cp /sdcard/config.xml /sdcard/config.bak
turboadb -s SERIAL cp /sdcard/DCIM /sdcard/Backup
```

### `turboadb stat`
The path, real path (symlinks followed), type, size and octal mode of a device
file.
```bash
turboadb -s SERIAL stat /system/build.prop
```

### `turboadb edit`
Edit a device text file in a local editor. TurboADB pulls it, opens the editor,
and pushes it back only if it was changed, keeping its permissions. Needs write
access to the file (for system files: `root` and `remount` first).

| Option | Meaning |
|---|---|
| `--editor COMMAND` | The editor (default `$VISUAL`, then `$EDITOR`, else Notepad on Windows and `vi` elsewhere). The editor must wait until the file is closed (for VS Code: `"code --wait"`). |

```bash
turboadb -s SERIAL edit /data/local/tmp/config.ini
turboadb -s SERIAL edit /vendor/etc/audio_policy.conf --editor "code --wait"
```

---

## Apps

### `turboadb packages`
List installed packages.

| Option | Meaning |
|---|---|
| `--third-party` | Only apps the user installed. |
| `--system` | Only system apps. |
| `--enabled` | Only enabled packages. |
| `--disabled` | Only disabled packages. |
| `--path` | Show each package's APK path. |
| `FILTER` | Optional text the package name must contain. |

```bash
turboadb -s SERIAL packages --third-party
turboadb -s SERIAL packages --disabled
turboadb -s SERIAL packages google --path --json
```

### `turboadb install`
Install one APK, or several APKs as one split install.

| Option | Meaning |
|---|---|
| `APK…` | One or more APK files. |
| `--grant` | Grant all runtime permissions. |
| `--downgrade` | Allow installing an older version. |
| `--no-replace` | Fail instead of replacing an installed app. |
| `--test` | Allow test-only APKs (`-t`). |

```bash
turboadb -s SERIAL install app.apk --grant
turboadb -s SERIAL install app-debug.apk --test
turboadb -s SERIAL install base.apk split_config.en.apk split_config.arm64_v8a.apk
```

### `turboadb uninstall`
Uninstall a package.

| Option | Meaning |
|---|---|
| `PACKAGE` | The package name. |
| `--keep-data` | Keep the app's data and cache. |

```bash
turboadb -s SERIAL uninstall com.example.app
```

### `turboadb clear`
Clear an app's data (`pm clear`).
```bash
turboadb -s SERIAL clear com.example.app
```

### `turboadb start`
Launch an app by package name. It resolves the launcher activity, so it also
works on head units where `monkey` is blocked.
```bash
turboadb -s SERIAL start com.android.settings
```

### `turboadb start-activity`
Start a specific activity (`am start -n`), with an optional intent action, data
and extras.

| Option | Meaning |
|---|---|
| `COMPONENT` | `package/.Activity`. |
| `--action ACTION` | Intent action. |
| `--data URI` | Intent data. |
| `--es KEY VALUE` | A string extra; repeat for several. |
| `--ei KEY VALUE` | An integer extra; repeat for several. |
| `--ez KEY VALUE` | A boolean extra (`true` / `false`); repeat for several. |

```bash
turboadb -s SERIAL start-activity com.android.settings/.Settings
turboadb -s SERIAL start-activity com.example/.MainActivity --es mode demo --ez debug true
```

### `turboadb activity`
The activity in the foreground — handy on head units to see which app is showing.
```bash
turboadb -s SERIAL activity
```

### `turboadb stop`
Force-stop an app.
```bash
turboadb -s SERIAL stop com.example.app
```

### `turboadb grant`
Grant a runtime permission (`pm grant`).
```bash
turboadb -s SERIAL grant com.example.app android.permission.ACCESS_FINE_LOCATION
```

### `turboadb revoke`
Revoke a runtime permission (`pm revoke`).
```bash
turboadb -s SERIAL revoke com.example.app android.permission.CAMERA
```

### `turboadb close-apps`
Close background apps.
```bash
turboadb -s SERIAL close-apps
```

---

## Screen, capture and mirroring

Commands with `--display N` work on one display of a multi-display device (an IVI
cluster, centre stack or passenger screen). `turboadb displays` lists the ids.

### `turboadb displays`
List the device's displays: id, size and name.

| Option | Meaning |
|---|---|
| `--method METHOD` | `adb` (fast), `scrcpy`, or `auto` (default: adb, then scrcpy). |

```bash
turboadb -s SERIAL displays
turboadb -s SERIAL displays --json
```

### `turboadb screenshot`
Save a PNG screenshot of the device screen.

| Option | Meaning |
|---|---|
| `--display N` | Capture this display. |

```bash
turboadb -s SERIAL screenshot shot.png
turboadb -s SERIAL screenshot cluster.png --display 2
```

### `turboadb record`
Record the screen on the device (`screenrecord`) and pull the video. It needs no
video tunnel, so it works over Remote Desktop. Ctrl+C stops early and still
saves the file.

| Option | Meaning |
|---|---|
| `PATH` | Output `.mp4` file. |
| `--time-limit SECONDS` | Length (default `30`; Android caps one recording at 180). |
| `--size WxH` | Video size, for example `1280x720`. |
| `--bit-rate RATE` | For example `8M`. |
| `--display N` | Record this display. |
| `--continuous` | Keep recording past the 3-minute cap, in 3-minute parts (`clip.mp4`, `clip-part02.mp4` …), until Ctrl+C. |
| `--json` | One `{"ok": …, "paths": [...]}` object listing every part. A part that fails after earlier ones were saved still exits `1`. |

```bash
turboadb -s SERIAL record clip.mp4 --time-limit 20 --size 1280x720 --bit-rate 8M
turboadb -s SERIAL record drive.mp4 --continuous --display 0
```

### `turboadb scrcpy`
Mirror and control the screen with scrcpy.

| Option | Meaning |
|---|---|
| `--display-id N` | Mirror this display. |
| `--compat` | Compatibility profile for head units whose encoders fail with scrcpy's defaults: H.264, capped size and frame rate, no audio, forward tunnel. Try it first when mirroring doesn't start on an IVI. |
| `--max-size PX` | Limit the longer side, for example `1280`. |
| `--bit-rate RATE` | Video bitrate, for example `8M`. |
| `--max-fps N` | Frame-rate cap. |
| `--video-codec CODEC` | `h264`, `h265` or `av1`. |
| `--crop W:H:X:Y` | Mirror part of the screen, for example `1920:720:0:0`. |
| `--render-driver NAME` | SDL render driver, for example `software` over Remote Desktop. |
| `--keyboard MODE` | Keyboard injection: `sdk`, `uhid` or `aoa`. |
| `--force-adb-forward` | Use a forward tunnel, for devices that block `adb reverse`. |
| `--no-audio` | Don't forward sound. |
| `--audio-source SOURCE` | `output`, `playback`, `mic`, `voice-call-downlink` or `voice-performance`. |
| `--audio-codec CODEC` | `opus`, `aac`, `flac` or `raw`. |
| `--audio-bit-rate RATE` | For example `128K`. |
| `--audio-buffer MS` | Audio buffer. |
| `--audio-output-buffer MS` | Playback buffer on the PC. |
| `--audio-dup` | Keep playing sound on the device too (with `--audio-source playback`). |
| `--record FILE` | Also record to a file. |
| `--record-format FORMAT` | `mp4` or `mkv`. |
| `--no-playback` | Don't show the video (for example to only `--record`). |
| `--turn-screen-off` | Turn the device screen off while mirroring. |
| `--no-stay-awake` | Let the device sleep while mirroring. |
| `--show-touches` | Show touches on the device. |
| `--no-control` | View only: no keyboard or mouse control. |
| `--fullscreen` | Start full screen. |
| `--always-on-top` | Keep the window above others. |
| `--window-borderless` | No window frame. |
| `--window-title TITLE` | The window title. |
| `--video-source SOURCE` | `display` (default) or `camera`. |
| `--camera-facing FACING` | `front`, `back` or `external` (with `--video-source camera`). |
| `--camera-size WxH` | Camera resolution. |
| `--log FILE` | Write scrcpy's own log to a file (for troubleshooting). |
| `--wait` | Block until the scrcpy window closes. |
| `--json` | Print `{"ok": true, "result": {"pid": N}}` instead of the launch message. |

```bash
turboadb -s SERIAL scrcpy --max-size 1280 --bit-rate 8M
turboadb -s SERIAL scrcpy --compat --display-id 2 --log scrcpy.log
turboadb -s SERIAL scrcpy --bit-rate 16M --audio-source playback --audio-dup
turboadb -s SERIAL scrcpy --no-playback --record drive.mkv --record-format mkv
turboadb -s SERIAL scrcpy --video-source camera --camera-facing front
turboadb --adb-host lab-pc-01 -s DEVICE scrcpy --render-driver software --wait
```

### `turboadb screen`
Turn the device screen on or off.
```bash
turboadb -s SERIAL screen off
turboadb -s SERIAL screen on
```

---

## Keys and input

### `turboadb key`
Send keys by name or Android keycode number. Several keys are sent in order.

Names: `home`, `back`, `recents`, `menu`, `search`, `power`, `sleep`, `wake`,
`notifications`, `call`, `enter`, `del` (Backspace), `tab`, `space`, `esc`, `up`,
`down`, `left`, `right`, `center`, `vol_up`, `vol_down`, `vol_mute`, `play_pause`,
`media_play`, `media_pause`, `stop`, `next`, `prev`, `rewind`, `fast_forward`,
`brightness_up`, `brightness_down`.

| Option | Meaning |
|---|---|
| `KEY…` | One or more key names or numbers. |
| `--longpress` | Long-press the key (one key only). |
| `--display N` | Send to this display. |

```bash
turboadb -s SERIAL key home
turboadb -s SERIAL key down down enter
turboadb -s SERIAL key power --longpress
turboadb -s SERIAL key 26          # KEYCODE_POWER by number
```

### `turboadb text`
Type text into the focused field.

| Option | Meaning |
|---|---|
| `--display N` | Type on this display. |

```bash
turboadb -s SERIAL text "hello world"
turboadb -s SERIAL key enter
```

### `turboadb tap`
Tap a point, or the centre of the screen when no point is given.

| Option | Meaning |
|---|---|
| `X Y` | Optional point, in screen pixels. |
| `--display N` | Tap on this display. |

```bash
turboadb -s SERIAL tap 540 1200
turboadb -s SERIAL tap
```

### `turboadb swipe`
Swipe from one point to another.

| Option | Meaning |
|---|---|
| `X1 Y1 X2 Y2` | Start and end points, in screen pixels. |
| `--ms MS` | Duration (default `200`). |
| `--display N` | Swipe on this display. |

```bash
turboadb -s SERIAL swipe 500 1500 500 400 --ms 300
```

### `turboadb scroll`
Scroll the screen with a swipe: `up`, `down`, `left` or `right`.

| Option | Meaning |
|---|---|
| `--display N` | Scroll on this display. |

```bash
turboadb -s SERIAL scroll down
```

### `turboadb media`
Control media playback through the active media session (falls back to media
keys): `play-pause`, `play`, `pause`, `next`, `previous`, `stop`,
`fast-forward`, `rewind` or `mute`.
```bash
turboadb -s SERIAL media play-pause
turboadb -s SERIAL media next
```

### `turboadb brightness`
Set the screen brightness live from `0.0` to `1.0`, or read and step the
brightness setting (0–255).

| Option | Meaning |
|---|---|
| `FRACTION` | Live brightness, `0.0` to `1.0`. |
| `--get` | Print the brightness setting. |
| `--level N` | Set the brightness setting, `0`–`255` (turns auto-brightness off). |
| `--step N` | Change the setting by N, for example `20` or `-20`. |

```bash
turboadb -s SERIAL brightness 0.6
turboadb -s SERIAL brightness --get
turboadb -s SERIAL brightness --step -20
```

### `turboadb notifications`
Pull the notification shade down (`expand`) or close it (`collapse`).
```bash
turboadb -s SERIAL notifications expand
```

---

## Connectivity

### `turboadb wifi`
Turn Wi-Fi `on` or `off`.
```bash
turboadb -s SERIAL wifi on
```

### `turboadb bluetooth`
Turn Bluetooth `on` or `off`.
```bash
turboadb -s SERIAL bluetooth off
```

### `turboadb airplane`
Turn airplane mode `on` or `off`.
```bash
turboadb -s SERIAL airplane on
```

### `turboadb hotspot`
Turn the mobile hotspot `on` or `off`. Best effort: when adb isn't allowed to
toggle it, TurboADB opens the tethering settings instead.
```bash
turboadb -s SERIAL hotspot on
```

### `turboadb mobile-data`
Turn mobile data `on` or `off`.
```bash
turboadb -s SERIAL mobile-data off
```

---

## Open apps and the web

### `turboadb settings`
Open the Settings app.
```bash
turboadb -s SERIAL settings
```

### `turboadb open`
Open a URL or link with the app that handles it (a VIEW intent). The shortcuts
`browser`, `youtube`, `maps`, `spotify` and `play-store` open those sites.
```bash
turboadb -s SERIAL open https://maps.google.com
turboadb -s SERIAL open youtube
```

### `turboadb search`
Search the web in the browser.
```bash
turboadb -s SERIAL search "nearest charger"
```

### `turboadb camera`
Open the camera app.
```bash
turboadb -s SERIAL camera
```

### `turboadb gallery`
Open the gallery / photos app.
```bash
turboadb -s SERIAL gallery
```

### `turboadb calculator`
Open the calculator app.
```bash
turboadb -s SERIAL calculator
```

---

## Device status

### `turboadb battery`
Battery details (`dumpsys battery`). With `--json`: level, temperature, status and
the other fields as numbers and booleans.
```bash
turboadb -s SERIAL battery
turboadb -s SERIAL battery --json
```

### `turboadb health`
A one-shot health snapshot: battery, temperature, memory, CPU and uptime.

| Option | Meaning |
|---|---|
| `--full` | The detailed report, with the raw system dumps. |
| `-o`, `--output FILE` | Save the `--full` report to a file. |
| `--json` | The snapshot as data, or with `--full` the report as `{"ok": true, "report": "…"}` (or `{"ok": true, "path": "…"}` with `-o`). |

```bash
turboadb -s SERIAL health
turboadb -s SERIAL health --full -o health.txt
turboadb -s SERIAL health --full --json
```

### `turboadb build-info`
Build and version properties.

| Option | Meaning |
|---|---|
| `--full` | The detailed report, with every property. |
| `-o`, `--output FILE` | Save the `--full` report to a file. |
| `--json` | The properties as data, or with `--full` the report as `{"ok": true, "report": "…"}` (or `{"ok": true, "path": "…"}` with `-o`). |

```bash
turboadb -s SERIAL build-info
turboadb -s SERIAL build-info --full -o build.txt
```

### `turboadb getprop`
One Android property, or all of them.
```bash
turboadb -s SERIAL getprop ro.build.fingerprint
turboadb -s SERIAL getprop --json
```

---

## Phone and messages

### `turboadb dial`
Open the dialler with a number filled in (it does not call).
```bash
turboadb -s SERIAL dial 1800123456
```

### `turboadb call`
Place a call.
```bash
turboadb -s SERIAL call 1800123456
```

### `turboadb answer`
Answer a ringing call.
```bash
turboadb -s SERIAL answer
```

### `turboadb end-call`
Hang up or reject the current call.
```bash
turboadb -s SERIAL end-call
```

### `turboadb call-state`
The call state: `idle`, `ringing` or `in call`.
```bash
turboadb -s SERIAL call-state
```

### `turboadb call-log`
Recent calls.

| Option | Meaning |
|---|---|
| `--limit N` | How many (default `20`). |

```bash
turboadb -s SERIAL call-log --limit 50
```

### `turboadb sms`
Recent SMS messages.

| Option | Meaning |
|---|---|
| `--limit N` | How many (default `20`). |

```bash
turboadb -s SERIAL sms --limit 10 --json
```

### `turboadb send-sms`
Open the Messages app with a draft to a number; you press Send on the device.
```bash
turboadb -s SERIAL send-sms 1800123456 "on my way"
```

### `turboadb phone-support`
What the device has for calls and messages, found without calling anyone:
telephony, the app handling the dialler, calls and SMS (`none` when no app
does), and any phone-like apps such as a customised head unit's own Bluetooth
phone app.
```bash
turboadb -s SERIAL phone-support
turboadb -s SERIAL phone-support --json
```

---

## Root and system

For rooted and engineering builds.

### `turboadb root`
Restart adbd as root.
```bash
turboadb -s SERIAL root
```

### `turboadb unroot`
Restart adbd without root.
```bash
turboadb -s SERIAL unroot
```

### `turboadb remount`
`adb remount` the system partitions read-write.
```bash
turboadb -s SERIAL remount
```

### `turboadb mount-rw`
Remount a partition read-write (`mount -o remount,rw`), `/` by default. Pass
`/system` or `/vendor` on devices that need it.
```bash
turboadb -s SERIAL mount-rw
turboadb -s SERIAL mount-rw /vendor
```

### `turboadb disable-verity`
`adb disable-verity`. It takes effect after a reboot.

| Option | Meaning |
|---|---|
| `--reboot` | Sync and reboot straight away to apply it. |

```bash
turboadb -s SERIAL disable-verity --reboot
```

### `turboadb enable-verity`
`adb enable-verity`. It takes effect after a reboot.

| Option | Meaning |
|---|---|
| `--reboot` | Sync and reboot straight away to apply it. |

```bash
turboadb -s SERIAL enable-verity --reboot
```

### `turboadb reboot`
Reboot the device, optionally into `recovery`, `bootloader`, `fastboot` or
`sideload`. Many head units have no on-screen recovery UI, so use those modes
with care.

| Option | Meaning |
|---|---|
| `--wait` | After a normal reboot, wait until Android has finished booting. |
| `--wait-timeout SECONDS` | How long `--wait` waits (default `180`). |

```bash
turboadb -s SERIAL reboot --wait
turboadb -s SERIAL reboot recovery
```

---

## Port forwarding

### `turboadb forward`
`adb forward LOCAL REMOTE`. It stays active until you press Ctrl+C, which removes
it; `--no-wait` leaves it in place and exits.

| Option | Meaning |
|---|---|
| `LOCAL REMOTE` | The PC-side and device-side specs, for example `tcp:8080 tcp:8080`. |
| `--no-wait` | Add the rule and exit, leaving it active. |
| `--list` | List this device's forward rules. |
| `--remove SPEC` | Remove one rule by its PC-side spec. |
| `--remove-all` | Remove this device's forward rules. |

```bash
turboadb -s SERIAL forward tcp:8080 tcp:8080
turboadb -s SERIAL forward tcp:9222 localabstract:chrome_devtools_remote --no-wait
turboadb -s SERIAL forward --list
turboadb -s SERIAL forward --remove tcp:9222
```

### `turboadb reverse`
`adb reverse REMOTE LOCAL`. It stays active until you press Ctrl+C, which removes
it; `--no-wait` leaves it in place and exits.

| Option | Meaning |
|---|---|
| `REMOTE LOCAL` | The device-side and PC-side specs, for example `tcp:3000 tcp:3000`. |
| `--no-wait` | Add the rule and exit, leaving it active. |
| `--list` | List this device's reverse rules. |
| `--remove SPEC` | Remove one rule by its device-side spec. |
| `--remove-all` | Remove this device's reverse rules. |

```bash
turboadb -s SERIAL reverse tcp:3000 tcp:3000
turboadb -s SERIAL reverse --remove-all
```

---

## Sharing devices with other PCs

### `turboadb serve`
Share this PC's adb server on the network so other machines can drive its
devices (`--adb-host` on their side). Opens the firewall ports, which needs
Administrator; what it could not do is reported on stderr and exits `1`.

`serve` never downloads adb by itself: the startup task runs it as SYSTEM, where
a download would put a second adb in another profile and bind port 5037 with it.
Run `turboadb fetch-tools` (or set `TURBOADB_ADB`) if adb is missing there.

| Option | Meaning |
|---|---|
| `--port PORT` | adb server port (default `5037`). |
| `--startup-task` | Keep sharing across reboots with a headless SYSTEM startup task. |
| `--install-startup` | Start sharing at login instead. |
| `--uninstall-startup` | Remove the auto-start again. |
| `--status` | Report whether the adb server is shared. |
| `--stop` | Stop sharing and go back to a local-only adb server. |
| `--json` | With `--status`, print `{"port": …, "shared": …}`. |

```bash
turboadb serve
turboadb serve --startup-task
turboadb serve --status
turboadb serve --stop
```

### `turboadb deploy-serve`
Set up `turboadb serve` on remote Windows PCs from your machine, over WinRM
(NTLM). Needs `pip install "turboadb[winrm]"`; each target needs WinRM enabled
(`Enable-PSRemoting -Force`) and an admin account.

| Option | Meaning |
|---|---|
| `HOST…` | One or more remote machines. |
| `-u`, `--user USER` | Admin account, for example `DOMAIN\user`. |
| `-p`, `--password PASSWORD` | The password (asked for when omitted). |
| `--port PORT` | adb server port on the targets (default `5037`). |
| `--ssl` | WinRM over HTTPS. |
| `--winrm-port PORT` | WinRM port (default `5985`, or `5986` with `--ssl`). |
| `--no-update` | Don't upgrade TurboADB on the targets. |
| `--test` | Only check the WinRM connection. |
| `--json` | Print `{"ok": …, "hosts": [...]}`; the running commentary moves to stderr. |

```bash
turboadb deploy-serve lab-pc-01 -u "DOMAIN\user" --test
turboadb deploy-serve lab-pc-01 lab-pc-02 -u "DOMAIN\user" --ssl
```

---

## Other console scripts

| Script | What it does |
|---|---|
| `turboadb-gui` | Start the desktop app (on Windows it runs the bundled executable when PyQt5 isn't installed). |
| `turboadb-docs` | Open the documentation website (or the bundled README offline). |
| `turboadb-shortcut` | Create Desktop and Start-menu shortcuts (Windows). |

## Shell tab completion

Optional: `pip install "turboadb[completion]"`, then in bash or zsh:

```bash
eval "$(register-python-argcomplete turboadb)"
```
