# Changelog

All notable changes are recorded here. Versions follow
[semantic versioning](https://semver.org/).

## 1.1.15

Hardening pass before a proper release.

- **No more stranded mirror window.** 1.1.14 launches scrcpy off-screen so it can
  embed without flashing. If embedding then failed (window couldn't be adopted,
  or timed out), the window was left off-screen and invisible while the tool
  claimed it was "running in its own window". It's now **rescued on-screen** with
  a real title bar / resize frame so it's usable as a plain scrcpy window, and
  the embed state is cleared so a later close doesn't trigger a relaunch loop.

## 1.1.14

- **Mirror opens embedded — no separate-window flash.** The mirror used to pop
  up as its own top-level window for a moment and then jump into the tool. Now
  scrcpy is launched **off-screen**, renders its first frame there, and is
  adopted into the panel only once it has settled — so it appears *inside* the
  tool from the start, with nothing flashing on screen first. (This reuses the
  same off-screen trick already used for clean screen recording.)
- **A proper welcome screen.** Instead of a blank rectangle when no device is
  open, the central area now shows a MobaXterm-style start page: the TurboADB
  banner, quick-start tiles (New target · Open selected · Host webcam · Get /
  update tools), a few getting-started hints, and a live footer that reflects
  how many devices are connected. It appears whenever the last tab is closed and
  steps aside the moment you open a device.

## 1.1.13

Native keyboard now types on the embedded screen (thank you for confirming) —
this release makes the mirror open on the **first** click, no failed attempt.

- **First-click start, done properly.** Your log showed scrcpy render
  (`Renderer: direct3d11`) and then die ~1 s later — right after we adopted its
  window. The cause: we reparented the window *the instant it appeared*, while
  scrcpy's SDL was still bringing up its Direct3D swapchain and hadn't drawn its
  first frame; reparenting mid-init made it lose the graphics device and exit,
  so it only worked on the (slightly slower) second attempt. Now we let the
  window **settle its first frame (~0.6 s) before adopting it**, so the first
  click embeds a stable window and stays up. The auto-retry from 1.1.12 remains
  as a safety net, but it should rarely fire now.

## 1.1.12

Two fixes from your logs.

- **First-click mirror start (for real this time).** The log showed the mirror
  *embedded and then ended ~2 s later*, and because it had briefly embedded we
  wrongly treated that as a normal close and did **not** retry — so you had to
  open it a second time. A mirror that dies within a few seconds of starting is
  now correctly seen as a **failed start and auto-retried** (an embedded window
  can only be closed via Stop, which is handled separately, so a quick
  unexpected exit is always a crash to retry). The second attempt — which
  already worked when you did it by hand — now happens automatically.
- **Type directly on the embedded screen.** Getting keyboard *focus* into the
  reparented foreign scrcpy window is unreliable across GPU/RDP sessions (that's
  why you kept needing the field). So the embedded mirror's container now
  **captures keyboard itself and sends it to the device over adb** — the same
  path the keyboard bar proved works on your device — while scrcpy keeps the
  video and the mouse. Move the mouse over the embedded screen and type: it goes
  to the device. The ⌨ bar and 🎯 button remain as fallbacks.

## 1.1.11

- **Type directly on the Live View image.** Live View is a screencap stream (a
  plain image), so it never had a keyboard path of its own — typing only worked
  in the separate field. Now the Live View **takes keyboard focus when you click
  it** and forwards every key (letters, digits, Enter, Backspace, Tab, arrows,
  and pasted text) straight to the device over adb. So on **any** device — normal
  phone or IVI — you click the screen and type, and it lands on the device.
  (The keyboard field stays as an alternative.)

  Between this and the earlier fixes there are now three ways to type into a
  mirror, all working: a **separate-window** scrcpy (fully native), the
  **embedded** window (focus is kept on it while you hover), and **Live View**
  (click + type, via adb — the most universal).

## 1.1.10

- **Type directly into the EMBEDDED mirror now.** In the embedded (Control +
  Mirror) view, mouse worked but keys didn't — because Windows routes clicks by
  position but keyboard needs *focus*, and a reparented foreign window doesn't
  hold it. TurboADB now **keeps keyboard focus on the embedded scrcpy window
  while your pointer is over the screen** (and the app is active), so you can
  type straight into the mirror — no more going through the tool's keyboard bar.
  It's polite: it doesn't grab focus while you're deliberately using that bar or
  clicking the controls, and the bar / 🎯 button remain as fallbacks. A
  separate-window mirror was already fully native.

## 1.1.9

- **Type directly in the mirror again.** Native scrcpy keyboard/mouse was never
  disabled — a mirror in its *own window* takes input directly, exactly like
  plain scrcpy. The confusion was that the tool's keyboard bar was shown even
  then, as if you had to use it. Now that bar only appears where scrcpy's own
  keyboard genuinely isn't available — the **embedded** window (foreign-window
  focus is unreliable) and **Live View** (a screencap stream has no input path).
  A separate-window mirror shows **no** keyboard bar: just click it and type.
  (Separate window remains the default for the ▶ Mirror button.)
- **File-list checkboxes are themed.** The multi-select checkboxes used the raw
  OS control, which looked out of place; they now match the app's styling in
  both light and dark themes.

## 1.1.8

- **Mirror starts on the first click now.** The real defect: scrcpy's log is
  block-buffered to a file, so its "started" markers don't appear while it's
  running — which meant a working *separate-window* mirror was never confirmed,
  and a *hung* first launch (the common server-push race, even on ordinary
  phones) was never detected, so you had to open the mirror a second time.
  Readiness is now detected by **scrcpy's window actually existing** (works for
  embedded and separate-window alike), and a **startup watchdog** kills a launch
  that shows no window within 6 s and auto-retries it — so a single click now
  brings the mirror up by itself. If it genuinely can't start, you still get
  scrcpy's real log and reason.
- **"Empty this folder" keeps the folder.** It now deletes only the *contents*
  (files and sub-folders, dotfiles included) using absolute-path globs that can
  never match `.`/`..`, so the folder itself stays and you can add files to it
  again immediately.
- **Create files.** A new **New file** action (button + right-click) makes an
  empty file in the current directory.
- **Multi-select with checkboxes.** Every entry now has a checkbox — tick as many
  as you like with single clicks (no Ctrl needed); all actions (download, delete,
  …) act on the ticked items. Ctrl/Shift-click still works too.

## 1.1.7

- **"scrcpy started but showed nothing" is fixed.** The success check was too
  loose — scrcpy prints ``Device: …`` the instant it *connects*, before it opens
  a decoder, so a connect-then-fail was treated as a working start and left
  alone. Now only a real *renderer/texture/recording* line counts as up, so a
  failed start is properly detected — and **auto-retried**: first the same way
  (most first-launch failures, even on ordinary phones, are a transient
  server-push race that a second attempt just fixes), then compatibility mode in
  a separate window. If all three attempts fail you get scrcpy's **actual log
  and the real reason**, never a silent "started".
- **Saved files go to your Downloads folder** by default — screenshots,
  recordings, pulled files, logcat/terminal logs, bugreports and webcam captures
  — instead of an arbitrary location.
- **Files: full right-click menu.** Open, Copy to my PC, Rename, Delete
  (files *and* folders), **Delete EVERYTHING in this folder**, Select all / Clear
  selection, Upload here, New folder, Refresh — alongside the existing
  multi-select (Ctrl/Shift-click, Ctrl+A) and Delete-key support.
- **Shell welcome banner redone** as a clean bordered box (device · system · ABI
  · access), **without** the tips line, and the app **version now appears only in
  the status bar** — not in the banner or the ready message.

## 1.1.6

- **A MobaXterm-style welcome header on every shell.** Opening a device's Shell
  tab now greets you with a tidy coloured banner — the device model,
  Android version + SDK, ABI, serial and how you're connected (USB / network /
  remote adb server), plus a one-line reminder of the terminal shortcuts (↑/↓
  history, Tab completion, Ctrl+wheel zoom, Stop halts logcat). Automotive/IVI
  targets get a 🚗 badge and an "Android Automotive" note. Shown once per tab
  (not on Stop→reopen), and included in the saved terminal log.

## 1.1.5

A round of fixes from real usage — mirror reliability, the terminal, logcat,
files, and tabs.

- **scrcpy failures now tell you why.** A mirror that doesn't come up is
  auto-retried (compatibility mode, then a separate window) and, if it still
  won't start, a dialog shows scrcpy's **actual output and the exact reason**
  instead of a silent "mirror ended".
- **Flaky automotive start fixed.** A start that never renders is retried
  automatically up to twice with progressively safer options — the "had to try
  2–3 times" behaviour on head units is now done for you on the first click.
- **Ctrl + mouse-wheel (and Ctrl +/−) zoom** the text size in the terminal and
  the logcat view; the chosen size is remembered.
- **Right-click a tab** for Close, Close others, Close to the left, Close to the
  right, and Close all.
- **Terminal ↑/↓ recall previous/next commands.** The console now takes keyboard
  focus when you open the Shell tab, so the history keys (which were going
  nowhere because focus sat on the tab bar) work.
- **Files: multi-select and folder delete.** Ctrl/Shift-click to select several
  entries (Ctrl+A for all), Delete removes them — files and whole folders,
  recursively — in one confirmed step; F2 renames; a combined Upload picker for
  files or a folder. Downloading several items pulls them into a chosen folder.
- **`logcat` in the shell actually stops now.** Killing the local adb didn't
  reliably kill an orphaned device-side `logcat`, so it kept streaming after
  Stop; TurboADB now reaps it over a separate connection.
- **No more UI hang under a flood.** The shell reader coalesces a burst of output
  into a few batched updates instead of thousands of cross-thread signals
  (interactive output stays instant), and stopping a runaway `logcat` (above)
  removes the flood at its source.

## 1.1.4

Makes typing actually work on every device, and de-clutters the mirror toolbar.

- **A device-keyboard bar that always works.** Whenever you're mirroring or in
  Live View, a keyboard field appears under the screen: click it and every key
  you press — letters, digits, Enter, Backspace, Tab, arrows, and pasted text —
  is sent straight to the device over adb (`input text` / `keyevent`). Because
  it's a normal field inside TurboADB, it does **not** depend on any Win32
  window-focus behaviour, so it types reliably on a **normal phone and on an IVI
  head unit alike**, whether the mirror is embedded or in its own window, local
  or over RDP. This is now the primary, guaranteed way to type.
- **Embedded-window keyboard reworked.** The embed now converts the scrcpy
  window to a proper `WS_CHILD` child (the standard approach real embedders use)
  and, on the 🎯 Mirror keys button, brings TurboADB to the foreground and hands
  keyboard focus to the child (Alt-nudge + `AttachThreadInput` + `SetFocus`).
  When it works you type directly into scrcpy; when a given GPU/RDP session
  won't cooperate, the keyboard bar above still types. (The embed + child focus
  routing were validated with a cross-process test harness; end-to-end
  synthesised keystrokes depend on window foreground, which only a real click
  provides — hence the always-available bar.)
- **De-cluttered mirror toolbar.** Down from ~13 wrapping buttons to a clean
  row: **▶ Mirror · 🖥 Live View · ■ Stop · 🔴 Record · 📸 Screenshot · Display ·
  ⚙ Options · ⋯ More**. Camera, Mirror-all-displays, Re-scan, Type-a-block and
  Max-view moved into the **⋯ More** menu.

## 1.1.3

UI revamp for the mirror options and the Control + Mirror view.

- **⚙ Options is now a real popover panel, not a tick-mark menu.** Grouped,
  properly themed controls — Output checkboxes (audio / compatibility /
  software rendering / embed) each with a one-line hint, a **Keyboard** radio
  group (Standard SDK · UHID) and a **Camera source** radio pair (Back · Front)
  — replacing the cramped checkable menu whose tiny ticks and stay-open hack
  read as broken. Toggle as many options as you like; the popover stays open
  until you click outside or hit Done.
- **Control + Mirror got a proper layout:** the screen and the controls now sit
  in two titled cards (📱 Screen · 🎛 Controls) around a **visible, grabbable
  splitter handle** that highlights on hover — the old invisible 1-px seam made
  the divider undiscoverable. The controls column has a compact variant so the
  mirror keeps most of the width (cards reflow to a single column).
- Theme: radio buttons are now properly styled (accent dot, hover ring) and all
  splitters app-wide use the new pill handles.

## 1.1.2

Field fixes from real infotainment testing: typing in the mirror now behaves
exactly like plain scrcpy, and the embedded mirror is finally first-class.

- **Typing works like native scrcpy again.** TurboADB auto-selected the UHID
  keyboard over RDP — but most IVI/head-unit kernels have no uhid support, so
  keys silently went nowhere while plain scrcpy (which defaults to SDK
  injection) typed fine. The default is now **Standard (SDK) — identical to
  running scrcpy by hand** — with UHID as an explicit opt-in radio under
  ⚙ Options → **Keyboard mode**, including an honest warning when chosen.
- **The embedded mirror can now hold the keyboard.** The window is adopted as a
  real Win32 child (`WS_CHILD` style conversion, frame-change applied) instead
  of a reparented top-level popup that Windows could never give keyboard focus
  to — the reason typing worked in a separate scrcpy window but not embedded.
  Focus is handed to the mirror the moment it embeds (via `AttachThreadInput` +
  `SetFocus`, the standard cross-process pattern), when you return to the tab,
  when you click the container — and a slim **“⌨ Type in mirror”** bar appears
  above the embedded view for one-click keyboard focus whenever another panel
  has taken it.
- **Embedding is honoured over Remote Desktop.** It used to be silently disabled
  whenever software rendering was on, so “Embed in this tab” appeared to do
  nothing at all on RDP setups.
- **Control + Mirror view embeds by default** — the side-by-side layout is the
  whole point there; a separate floating window defeated it. The mirror and the
  controls now genuinely live next to each other, with the focus bar keeping
  typing unambiguous between the two panes.

## 1.1.1

The **1.1 feature release** — the work that landed across 1.0.15–1.0.19,
promoted to a stable minor, plus the CI/packaging fixes that make it build
cleanly from source on every supported Python. Detailed per-patch notes remain
below.

**Devices & connectivity**
- **Wi-Fi device discovery** on the LAN (`adb mdns services`) — Device / Connect
  menu, `turboadb discover`, and `turboadb.mdns_devices()`.
- **One-click go-wireless** (USB → Wi-Fi): reads the device IP, `adb tcpip`,
  reconnects — then the cable can be unplugged. `turboadb wireless` /
  `handler.go_wireless()`.
- **Run a command on ALL connected devices** at once from the GUI.

**Diagnostics**
- **Device health snapshot** (battery / temperature / memory / CPU / uptime) —
  the ❤ Health button, `turboadb health`, `handler.health()`.
- **Bugreport capture** and a one-click **logcat crash preset** (crash buffer,
  Error level, FATAL/ANR highlighting); the live filter re-filters what's already
  on screen.
- **Logcat "Live only" by default** — no more flood of the device's cached
  backlog when you press Start; pick how much history you want.

**Automotive / IVI**
- Apps tab lists **all apps** on automotive builds (not just third-party), with
  extra launch fallbacks that reach in-house/system apps that have no launcher
  activity.
- Wi-Fi / Bluetooth / airplane toggles use the modern `cmd` interfaces on
  Android 12+ and report an **honest "permission-denied"** when a locked-down
  head unit refuses, instead of a fake "ok".

**Mirroring & remote**
- **Smoother, sharper Live View** (adaptive frame rate, decode + scale off the UI
  thread, cached capture method) and **better scrcpy over RDP** (1280 px / 8 Mbps,
  linear SDL scaling, no resize thrash).
- **Remote-deploy credentials remembered** (user in settings, password in the OS
  vault) with a blocking, summarised deploy popup and bounded WinRM timeouts;
  optional WinRM-over-HTTPS.

**Files, targets, webcam**
- Drag-and-drop upload in the Files tab (off the UI thread); **export / import**
  saved targets; sidebar no longer repeats an auto-named target.

**Quality & packaging**
- On-demand adb/scrcpy downloads are **SHA-256 + CRC verified**, ETag-cached
  (no more GitHub rate-limit false "up to date"), with a `.old` rollback copy and
  a reliable, atomic in-place update.
- A real **pytest suite** with a fake-adb harness, **GitHub Actions CI**
  (Windows + Linux, Python 3.8–3.13) with ruff lint and a build check, a tagged
  **release** workflow, and a **concurrency-controlled Pages** deploy.
- Ships type hints (`py.typed`); portable `license` metadata so source installs
  work on Python 3.8.

## 1.0.19

- **Packaging fix: installs from source on Python 3.8 again.** `pyproject.toml`
  used the modern SPDX `license = "MIT"` string, which only newer setuptools
  (≥77, unavailable on Python 3.8) accepts — so an editable/source build on 3.8
  (and the 3.8 CI job) failed with *"`project.license` must be valid exactly by
  one definition"*. Reverted to the universal `license = { text = "MIT" }` table
  form (+ the MIT classifier). The prebuilt wheels were unaffected; this only
  matters when building from the sdist. Added a test that validates the
  packaging metadata so it can't regress.

## 1.0.18

A deep-review pass hardening the 1.0.17 changes, focused on the remote/RDP and
IVI paths.

- **Live View no longer re-probes screencap every frame.** The screen-capture
  method that works is now remembered and reused, so on a device that needs the
  file-based fallback, each frame does one capture instead of two failed probes
  plus a file write/read/delete — a big load drop now that Live View runs at up
  to 10 fps.
- **Fixed a Live View crash risk** when stopping over a slow link: a still-running
  capture thread is now parked until it exits instead of having its reference
  dropped mid-run (which could hard-crash Qt with "QThread destroyed while
  running"). Same safe teardown when the tab/app closes.
- **Fixed a 64-bit handle bug** in the embedded-mirror resize: `GetWindowRect`
  was called without an argument prototype, which could truncate the window
  handle on 64-bit Windows; it's now declared correctly.
- **Remote deploy can't hang forever.** WinRM now uses bounded read/operation
  timeouts (120 s), so an unreachable or wedged host fails in a known time
  instead of blocking the modal deploy popup indefinitely.

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
