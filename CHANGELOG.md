# Changelog

All notable changes are recorded here. Versions follow
[semantic versioning](https://semver.org/).

## 1.0.17

- **Remote-deploy credentials are remembered.** The ADB Server deploy dialog no
  longer asks for the admin login every single time: the user is kept in
  settings and the **password in the OS credential vault** (Windows Credential
  Manager, like the webcam password) and both are pre-filled. A **Remember**
  checkbox controls it — untick to forget the stored credentials.
- **Deploying ‘serve’ now shows a blocking progress popup** with live per-host
  status, and finishes with a summary (which hosts succeeded / failed) instead
  of silently logging in the background while nothing visible happened.
- **Live View is much smoother and sharper.** The frame rate is now adaptive —
  it streams as fast as the link allows (up to 10 fps) instead of a hard 2 fps —
  and the expensive PNG decode + smooth scaling moved **off the UI thread**, so
  the app no longer stutters under the stream. Frames are smooth-downscaled once
  from the native screenshot, keeping the picture crisp. Tap/swipe mapping stays
  exact.
- **Better scrcpy quality over Remote Desktop.** The software-render caps went
  from 1024 px / 4 Mbps to **1280 px / 8 Mbps**, SDL now uses **linear
  filtering** when scaling the frame to the window (the old nearest-neighbour is
  what looked jagged/blocky), and the embedded mirror no longer forces a window
  resize twice a second (a steady stutter source). A tip in the log suggests
  disabling audio forwarding over RDP for extra smoothness.
- **Saved targets no longer show their name twice** — auto-named targets (name =
  address/serial) rendered as "192.168.1.7:5555 · 192.168.1.7:5555"; the label
  now appears once.
- **The Apps tab is finally useful on IVI / automotive builds:** on an
  automotive device it now lists **all** apps by default (nearly everything
  there is a preinstalled system app, so the old third-party-only default showed
  a near-empty list). Starting an app gained two extra fallbacks that reach
  in-house/system apps with **no launcher activity** (resolve without the
  LAUNCHER category, then the package's first MAIN activity via
  `cmd package query-activities`).
- **Connectivity toggles work on modern / automotive Android:** Wi-Fi falls back
  to `cmd wifi set-wifi-enabled` (the classic `svc wifi` was removed in
  Android 12+), Bluetooth to `cmd bluetooth_manager`, and airplane mode to the
  settings key + broadcast. When *every* method is refused (locked-down IVI),
  the log now says so honestly instead of reporting a fake "ok".

## 1.0.16

New capabilities and a proper test/CI foundation — all additive, with the
existing behavior unchanged.

**New features**

- **Find Wi-Fi devices automatically.** Device menu (and the Connect ▾ menu) →
  **Discover Wi-Fi devices** uses `adb mdns services` to list Android 11+
  Wireless-debugging devices on the LAN and offers to connect + save them. Also
  `turboadb discover` on the CLI and `turboadb.mdns_devices()` in the library.
- **One-click go wireless.** On a USB device: Root/Mount ▾ → **Go wireless
  (USB → Wi-Fi)** reads the device's IP, runs `adb tcpip`, and connects — then
  the cable can be unplugged. CLI: `turboadb wireless`; library:
  `handler.go_wireless()`.
- **Run a command on ALL devices.** Device menu → **Run a command on all
  devices** streams one `adb shell` command across every connected device — handy
  for a bench of head units.
- **Device health snapshot.** A new **❤ Health** button per device (and
  `turboadb health` / `handler.health()`): battery %, temperature, memory, CPU
  load and uptime in one view.
- **Bugreport capture.** Root/Mount ▾ → **Capture bugreport** and
  `turboadb bugreport` save a full `adb bugreport` zip.
- **Logcat crash preset.** A **Crashes** button sets the crash buffer + Error
  level + FATAL/ANR highlighting in one click. The live regex filter now also
  re-filters what's **already on screen**, not just new lines.
- **Files: drag-and-drop upload.** Drag files/folders from your file manager onto
  the Files list to upload them to the current directory.
- **Export / Import saved targets** (File menu) — share bench configs as JSON.
- **WinRM over HTTPS (5986)** checkbox in the remote-deploy dialog.
- Optional **shell tab-completion** for the CLI (`pip install "turboadb[completion]"`).

**Reliability / correctness**

- scrcpy downloads are **SHA-256 verified** against the release's published sums,
  and every downloaded zip is CRC-checked before install — a corrupt or truncated
  download fails cleanly instead of installing garbage.
- The scrcpy GitHub version check is now **ETag-cached**, so repeated checks stop
  tripping GitHub's unauthenticated rate limit (which had surfaced as spurious
  "couldn't check"). The launch update check against PyPI is cached to once/day.
- The previous platform-tools/scrcpy is kept as a `.old` folder after an update —
  a one-step offline rollback if a new release misbehaves.
- The tool-download progress dialog now names the current stage ("adb (1/2)…",
  "scrcpy (2/2)…") instead of the bar appearing to jump backwards.
- Hotspot enable uses a **random** password instead of a hardcoded one.
- Saved targets keep a **stable position** when edited (no more jumping to the
  bottom of the sidebar).
- `server_is_shared()` now genuinely tests the all-interfaces binding.
- A rotating debug log at `~/.turboadb/turboadb.log`, and background-thread
  crashes are captured to `crash.log` too.

**Project infrastructure**

- A real **pytest suite** (`tests/`) with a fake-adb harness that exercises the
  actual arg-building/parsing/safe-mode code paths — plus tests for the tool
  update/swap/zip-slip machinery. **GitHub Actions CI** runs it on Windows +
  Linux across Python 3.8–3.13, with ruff lint and a build check. A tagged
  **release workflow** builds and attaches the Windows GUI exe.
- `py.typed` marker (the library now ships its type hints), Python 3.8–3.13
  classifiers, and a `[test]` / `[completion]` extra.

## 1.0.15

- **Logcat no longer floods 100,000s of cached lines the moment you press Start.**
  Plain `adb logcat` dumps the device's ENTIRE in-memory log buffer before
  following live — that backlog (easily 500k old lines in seconds) is what looked
  like a runaway stream. The Logcat tab has a new **History** selector, defaulting
  to **Live only** (`-T 1`): you get *actual new logs from now*, with "Last
  1,000 / 10,000 / Full buffer" available when you do want the cached history.
  Also: the library gained `logcat(tail=N)` and the CLI `turboadb logcat --tail N`,
  and **Pause no longer throws away** the lines that arrived while paused.
- **"Upgrade adb/scrcpy" now actually updates — every time.** Three bugs stacked up
  to make the update silently do nothing sometimes:
  1. The new platform-tools were extracted **over a running adb server**, whose
     locked `adb.exe` made the (error-suppressed) delete fail silently. The server
     is now stopped first and the new copy is staged and **swapped in atomically**
     (with a loud, actionable error if a file really is still locked).
  2. A failed download was **stamped as done** anyway, so it was never retried.
     The stamp is now written only when nothing failed.
  3. A failed *version check* (no network, GitHub rate limit) was reported as
     **"already up to date"**. It now says clearly that the check couldn't run.
  Plus: versions are compared numerically (no pointless re-downloads on formatting
  differences, never a downgrade), the **managed** copy is what gets checked,
  the GUI pauses its 3-second device poll during the replace (it was restarting —
  and re-locking — the server mid-swap), and a warning is shown when a custom
  `adb path` / `TURBOADB_ADB` overrides the freshly downloaded adb.
- **Settings no longer wipe each other.** Pressing OK in Settings used to reset
  everything the dialog doesn't show — recent hosts, ribbon density, the remembered
  webcam login. Unrelated keys are now preserved.
- The two dead Startup settings now work: the **launch update check** (notify-only
  log line; installing still only happens via the 🔄 Upgrade button) and the
  **shortcut self-healing** opt-out.
- **Files tab no longer freezes the window** — listing, mkdir, rename and delete
  run off the UI thread (over a remote adb server they could block for 15+ s).
  Same for the device scans in the target-edit dialog.
- **Stability:** closing a tab (or the app) while a connect/scan/action thread is
  still running no longer risks a hard crash ("QThread destroyed while running");
  shell teardown no longer burns a 700 ms timeout per tab; "Share this PC's
  devices" no longer races the device poll for port 5037.
- **Honest results:** a recording that had to be force-stopped now warns it may be
  truncated; a webcam recording that saved 0 bytes is reported as failed instead
  of "saved"; `turboadb upgrade-tools` exits non-zero when the check couldn't run;
  the self-update dialog now surfaces adb/scrcpy refresh failures instead of
  showing the old versions as if they were fresh.
- Fixed `turboadb shell -- <command>` sending a literal `--` to the device shell
  (same for `text` / `search` / `send-sms`).
- Hardening: zip extraction sanitizes archive paths (zip-slip); the admin-share
  password for the remote webcam is passed via stdin instead of the command line;
  PowerShell snippets escape quotes (shortcut creation broke for names/paths with
  an apostrophe); stale scrollback temp files from crashed sessions are cleaned up
  at launch; `_toggle_max` ("⛶ Max view") restores only the docks it hid.
- Exports: `stop_shared_server`, `install_serve_task`, `uninstall_serve_task`,
  `open_firewall`, `is_local_host` are importable from `turboadb` now.

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
