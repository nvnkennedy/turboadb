# Changelog

All notable changes are recorded here. Versions follow
[semantic versioning](https://semver.org/).

## 1.0.14

- **Remote webcam now actually connects.** The remote ffmpeg was launched as a child
  of the WinRM session, so it was killed the instant the call returned — nothing was
  listening, hence "connection actively refused". It's now spawned **detached** (via
  WMI `Win32_Process.Create`) so it survives, with `listen_timeout` and a kill of any
  stale ffmpeg holding the port/camera first. The connect window is longer (camera
  init takes a moment), and if it still can't connect the error now includes **what
  ffmpeg actually reported** (camera in use, privacy, etc.) instead of just "refused".

## 1.0.13

- **Remote webcam: ffmpeg is now *pushed* to the remote (works with no internet
  there).** Instead of only having the remote download it, TurboADB copies your local
  `ffmpeg.exe` to the remote over its admin share (`\\host\C$`) — fast on a LAN and
  the reliable equivalent of TurboSSH's SFTP push — then falls back to a remote
  download, then a clear manual-drop message. This fixes "ffmpeg wasn't found on the
  remote" on locked-down lab machines.
- **Compact / standard ribbon, like TurboSSH.** A new **🗜** ribbon button (and
  **View → Toggle compact / standard ribbon**) switches between icons-only (compact,
  the default — fits without maximizing) and **icons + text** (standard, readable).
  The choice is remembered.

## 1.0.12

- **The Remote-webcam password is now remembered too — in the OS credential vault.**
  Like TurboSSH, the host / user / domain go in settings and the **password is stored
  securely via `keyring`** (Windows Credential Manager), never in `settings.json`.
  It's pre-filled next time so the whole remote connection is one click.

## 1.0.11

- **Remote webcam now provisions ffmpeg by itself.** If the remote machine has no
  ffmpeg, it **downloads it there** (one-time, into `%USERPROFILE%\.turboadb\ffmpeg`)
  over WinRM — the WinRM-friendly equivalent of TurboSSH's SFTP push — instead of
  just failing with “ffmpeg wasn't found”. Falls back to a clear message (drop
  `ffmpeg.exe` over RDP) if the remote can't reach the internet.
- **Remote connection is remembered.** The RDP host / user / domain are saved after
  a successful scan and pre-filled next time (the password is never stored).

## 1.0.10

- **The Webcam opens right away — no device needed.** It's now a standalone tab you
  open from the ribbon **📹 Webcam** button or **View → Open webcam (host camera)**,
  instead of only being reachable as a sub-tab after a device is connected. (The
  per-device Webcam sub-tab, handy beside the mirror, stays as well.)

## 1.0.9

- **Ribbon no longer overflows before you maximize.** The toolbar is now tight
  (icon-only device shortcuts with tooltips; only Connect / ADB Server keep
  labels), and the global actions — **theme · settings · help · exit** — are grouped
  at the far right and stay visible at normal window sizes instead of hiding in the
  “»” overflow until maximized.
- Webcam: the horizontal-flip toggle is renamed **Flip** (was “Mirror”, which
  clashed with scrcpy screen mirroring), the remote “diagnosing…” status no longer
  flashes “Stopped”, and remote camera names with quotes are escaped safely.

## 1.0.8

- **Webcam now has a Remote source — view a camera on another Windows / RDP
  machine, no SSH.** Pick **Source → Remote**, enter the host + admin login, and
  TurboADB uses the same WinRM/NTLM path as `deploy-serve` to launch ffmpeg there
  and stream its camera back over a direct TCP socket. (Local — which already
  covers running TurboADB inside an RDP session — stays the default.) Clear
  diagnostics when a remote camera has no video.
- **Settings is now a proper preferences window** — a sidebar with focused pages
  (Appearance · Tools · scrcpy · Logcat · Startup) instead of one long form, with
  an ffmpeg-path field and a launch-time auto-update toggle.
- **Dropdown arrows fixed.** Combo-boxes were drawing a tiny “dot” (Qt can't render
  a CSS border-triangle for a subcontrol) — they now show a real chevron, app-wide.

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
