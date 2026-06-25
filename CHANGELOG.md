# Changelog

All notable changes are recorded here. Versions follow
[semantic versioning](https://semver.org/).

## 1.0.7

- **New “📹 Webcam” tab — view a host webcam, locally and over RDP.** Point a USB /
  laptop camera at the physical head unit or bench and watch it *beside* the scrcpy
  mirror. Because it captures via local DirectShow, it also works when TurboADB runs
  **inside an RDP session** — it sees whatever camera that session exposes (e.g. a
  redirected USB cam). Scan cameras, pick quality/fps, **snapshot**, **record**
  (re-encoded to a clean H.264 MP4), **pause**, **rotate** and **mirror/flip**, with
  Fill/Fit/Stretch view modes and a live fps readout. ffmpeg is fetched once
  (~160 MB, cached under `~/.turboadb/ffmpeg`) or you can point Settings → **ffmpeg
  path** at your own. When no camera shows up, it explains the usual causes (RDP
  camera redirection, Windows camera privacy, or the device being in use).

## 1.0.6

- **Nothing auto-starts on connect anymore.** The Mirror tab now loads the
  device's display list **lazily** — only the first time you actually open it —
  instead of on connect. Opening a device no longer kicks off any scrcpy/adb
  activity in the background.
- **New “🛑 Stop sharing & remove auto-start”** (ADB Server ▾ menu). One click
  stops sharing this PC's devices and removes *both* auto-start vectors — the
  login launcher **and** the SYSTEM startup task — returning adb to local-only.
- **One-click dark/light toggle** in the ribbon (🌙/☀). The glyph always shows
  the theme you'll switch to; applies live to the whole app.
- **Logcat keyword highlighting.** A new *highlight* box marks matches in-line
  (case-insensitive regex, e.g. `error|anr|crash`) **without** hiding the rest —
  the Level colours still apply, matches just pop. The fast single-insert path is
  kept for non-matching lines so a flood stays smooth.
- **Mirror the device CAMERA** (📷 Camera) instead of the screen — a live
  webcam-style view, front or back (chosen under ⚙ Options). Needs scrcpy 2.2+ on
  the host and Android 12+ on the device.

## 1.0.5

- **Tab labels no longer truncate.** The emoji now renders as a real tab *icon*
  with plain text (inline emoji in a styled tab label throws off Qt's width
  calculation). Fixed on both the device tabs and the inner sub-tabs.
- **New “🎮 Control + Mirror” view** — a side-by-side sub-tab with the device
  screen on the left and the full controls panel on the right, so you can watch
  and tap/press at the same time without switching tabs.

## 1.0.4

- **Screen recording: no more black opening frame.** Recording now prefers
  `scrcpy --record` via an off-screen window — it muxes the H.264 stream from the
  first real frame (no encoder warm-up black frame, no 3-min cap, sharp), and is
  closed politely so the mp4 is finalized. Falls back to device-side
  `screenrecord` for a genuinely remote adb server, and auto-switches to it if the
  scrcpy recorder fails to start.

## 1.0.3

- **Recording quality + length.** Explicit high bitrate at native resolution
  (was soft/low), and a single device-side clip now auto-continues into the next
  part past Android’s ~3-minute `screenrecord` cap instead of just stopping.
- **Displays auto-load on connect** into a dropdown that no longer truncates and
  remembers your selection.
- **“▦ Mirror all”** — mirror every display at once, each in its own window (IVI
  cluster + centre + passenger).
- Tab labels set to not elide.

## 1.0.2

- **Detailed usage docs** — the README now documents every feature three ways
  (GUI · CLI · Python), and the website gained a matching **Guide** page.

## 1.0.1

- Fixed the PyPI logo (absolute image URL), set the author to **Naveen Daniel
  Kennedy**, and added Website / Source / Changelog / Bug-tracker links.

## 1.0.0

First stable release. TurboADB has been in daily use against phones and Android
Automotive head units throughout the 0.9.x series; 1.0.0 marks the API and CLI as
stable and rounds out the feature set.

### Highlights

- **One engine, three front-ends.** Everything the GUI does is available from the
  CLI (65 commands) and the Python API. Full parity — audited, not assumed.
- **Interactive shell** with history, tab-completion, columnised `ls`, and a
  **Stop** that reliably kills a runaway command (e.g. `logcat`) even with no PTY
  and over a remote adb server.
- **Logcat & shell never lose data.** Output is archived to disk as it streams, so
  a Save writes the *complete* log even when the on-screen view drops lines to
  stay responsive under a flood — verified at 1,000,000+ lines without the UI
  freezing.
- **Remote devices.** Drive devices on another machine's adb server
  (`--adb-host`), and **share** this PC's devices with `serve --startup-task`
  (shared server + firewall + headless SYSTEM startup task).
- **Deploy `serve` to remote Windows hosts over WinRM** — from the GUI **ADB
  Server** button, `turboadb deploy-serve`, or `remote_deploy.deploy_serve()`.
  Uses pywinrm/NTLM so domain credentials work over plain WinRM, with a pre-flight
  Test.
- **Screen mirroring (scrcpy)** in a window or embedded, with an IVI compatibility
  mode and remote-tunnel handling; **screenshots** and **screen recording** that
  work over RDP.
- **Device controls**: system keys, media, Wi-Fi/BT/airplane/hotspot, screen
  on/off, brightness, app launchers, on-screen keyboard.
- **Telephony**: dial, call, answer/end, call log, SMS.
- **Root/mount**: root/unroot, remount, mount-rw, disable/enable verity (with the
  required reboot handled).
- **Auto-update** from the GUI Upgrade button or `turboadb self-update` — updates
  TurboADB, refreshes adb/scrcpy, and restarts.
- **Bundled tools**: `adb` and `scrcpy` are downloaded into `~/.turboadb/tools` on
  first run; nothing to install by hand.
- **GUI niceties**: dark/light themes tuned for readability, a leveled and
  filterable log dock (the noisy `adb` trace is hidden by default), a responsive
  controls grid, desktop + Start-menu shortcuts, and a Save dialog with
  Open file / Open folder.

### Notes

- Windows is the primary GUI target (bundled `.exe`); the CLI and Python API are
  cross-platform. The GUI also runs from source via `pip install "turboadb[gui]"`.
- The remote-deploy feature needs WinRM enabled on each target
  (`Enable-PSRemoting -Force`), the account a local admin, and Python + turboadb
  installed there.
