# Changelog

All notable changes are recorded here. Versions follow
[semantic versioning](https://semver.org/).

## 2.0.0

A redesigned app, the Windows executable back inside the pip package, and a
full code-review pass.

**Highlights**

- **One install, exe included:** the wheel bundles the Windows GUI executable,
  so `pip install turboadb` gives you `turboadb-gui` even without PyQt5. The same
  `TurboADB-2.0.0-win64.exe` is attached to the GitHub Release.
- **Phone tab:** dialler with keypad, live call state, recent calls and messages.
- **Redesigned window:** colourful theme-following icons, a Device Control
  control centre, no device header (actions sit beside the section tabs) and a
  boxed two-line welcome banner in every terminal.
- **Device type detection:** Android Automotive, infotainment head unit, TV,
  watch, tablet or phone, which picks the IVI-compatible screen profile.
- **Calmer themes:** Graphite / Porcelain by default plus Mocha / Latte,
  Forest / Sage, Plum / Rose and Deep teal / Mint; light themes are dimmer.
- **Better screen mirroring:** starts on the first click, sharper video, typing
  straight on the embedded screen, and recording without restarting the view.
- **Real Windows shells:** PowerShell and Command Prompt tabs run Python, Git,
  `where`, current-folder programs and non-English text like a normal console.
- **Notifications:** every action shows a small toast; errors show a red popup
  with a sound and a Copy button.

### Details

- New **Phone** tab for each device: a dialler with a keypad and Call / End /
  Answer / Open in dialler, a live call-state pill (Idle, Ringing, In call or
  No telephony), colour-coded recent calls with Call back, and messages with a
  Compose in Messages box. Enter only opens the dialler and Call back only
  fills the number in, so nothing rings by accident. If the device refuses the
  call log or messages, the list shows the reason instead of an error.
- The screen mirror embedded in a tab works again. The scrcpy window is now
  made a child window before it is re-parented and the attachment is checked
  with the correct Windows call; previously every embed was wrongly treated as
  rejected and fell back to a separate window. The status line no longer sticks
  on "embedding…" when a real fallback happens.
- The first Start click in Device Control now works right after TurboADB
  launches. The window used to be adopted while scrcpy was still preparing its
  first frame on a cold ADB server, which made scrcpy exit and showed a
  "didn't start — retrying" toast before the automatic retry worked. It is now
  embedded only once scrcpy has shown its window.
- Webcam: starting no longer flashes "Camera stopped" while the feed comes up.
  A camera that rejects the requested frame rate switches to its native mode
  within about 0.3 s and is remembered for later starts; the status says
  "Starting…" until the first frame arrives, and a camera that stays silent
  reports "No video" after 12 s instead of waiting forever.
- File browser: deleting, renaming or pulling a file whose name starts or ends
  with spaces no longer acts on a different file; pasting a folder into its own
  subfolder is refused instead of copying without end; Delete, F2, F4, Ctrl+C
  and Ctrl+V work again; push, pull and paste onto an existing folder merge
  (asking before overwriting) instead of nesting a copy; rename asks before
  replacing; saving an edited device file keeps its permissions; symlinked
  files and folders can be pulled and edited; unreadable entries are listed as
  "Unknown"; a mistyped path no longer becomes the current folder; local
  delete handles read-only files and junctions; the editor keeps non-breaking
  spaces and refuses device nodes and oversize targets; transfers can be
  cancelled; and pulled names Windows can't store are renamed and logged.
- CLI: global options such as `--adb-host`, `--json` and `-s` work before or
  after the subcommand, and `connect` / `pair` / `disconnect` honour
  `--adb-host`. Commands exit 1 when the device refuses an action, unexpected
  errors print one line instead of a traceback, `--timeout` no longer disables
  the 60 s default, `scrcpy --no-control` starts, `scrcpy -s ip:port` connects
  first, split-APK `install` passes `--downgrade`, and Ctrl+C during `record`
  stops and still saves the MP4.
- Tools: automatic fetching only installs a missing `adb` / `scrcpy` and never
  upgrades (so a routine command can't restart the shared ADB server); Linux and
  macOS commands no longer re-check for updates on every run; `find_adb` no
  longer changes `PATH` / `ADB`, and an explicit path beats `TURBOADB_ADB`,
  which beats the GUI setting.
- Engine: text passed to device commands (URLs, package names, SMS bodies,
  paths, typed text) is quoted for the device shell, and `%` is typed
  literally. Port-forward removal is scoped to the device and reports failure.
  Root, unroot, remount, verity and remount-rw raise on refusal instead of
  reporting "ok"; install errors show adb's real reason; `get_state` reports
  `unauthorized` / `offline`. `tel:`, `geo:`, `mailto:` and `market:` links keep
  their scheme and `#` is encoded in dial codes. Screenshots never silently
  return a different display. Call-log and SMS queries return complete
  multi-line rows and surface permission errors. Safe mode never raises for
  invalid arguments, and `with ADBHandler(...)` only disconnects connections it
  opened. Raw `$ adb …` traces log at DEBUG and the library no longer installs
  its own log handler.
- New API: `ADBHandler.restart_server()`, `config.parse_host_port()`,
  `scrcpy.TUNNEL_PORT_FIREWALL_RANGE`, `install_multiple(downgrade=)`,
  `shell_many(safe=, timeout=, su=, check=)` and `ShellSession.at_eof`;
  `ForwardHandle.close()` returns whether removal succeeded.
- Sharing: the shared ADB server no longer reports success when it failed to
  bind, remote deploy no longer fails on harmless stderr output, and enabling
  auto-start from the standalone executable gives a clear error instead of
  opening the GUI at every login.

- Redesigned window. The top bar now holds only global actions (Connect, ADB
  server, Tools, plus theme, log, settings and help icons) instead of repeating
  the device sections. The Devices sidebar is a fixed panel with a device
  count, a + button, a list sized to its devices and an empty state. The log
  panel no longer repeats its title, and the Settings,
  Connect, Session, Deploy and Report dialogs share aligned forms with a footer
  holding the primary button.
- Colourful icons: a new set of theme-following vector icons replaces the emoji
  across the top bar, menus, sidebar, section tabs and every page and dialog.
  Each section keeps one colour (Terminal teal, Logcat amber, Files blue,
  Device Control purple, Apps orange, Phone green, Webcam red), and actions
  are colour-coded (start green, delete red, save teal). File lists show
  folder and file-type icons instead of emoji prefixes, with folders still
  sorted first. Logcat, Apps and Webcam show a hint while empty.
- Terminals: the Android / PowerShell / CMD switcher shares one row with the
  terminal actions, so terminals get more height. The font stays 10 pt unless you choose another size in
  Settings or with zoom. Zooming one terminal (A+ / A− or Ctrl + wheel)
  resizes all of them together.
- The window opens maximized, and page toolbars wrap onto a second row instead
  of widening the window: opening a device used to demand 2,002 px, which
  pushed the right side (including the device controls) off the screen.
- Sharper embedded screen: scrcpy now draws with OpenGL and trilinear
  filtering, so small text stays crisp when the phone is scaled down (it falls
  back to scrcpy's default renderer if OpenGL fails), and the default video
  bitrate is 16 Mbit/s (settings still carrying the old 8M default move once).
  The "Click here, then type" bar under the screen is gone; type on the screen
  itself or use Device Control's Keyboard section.
- Device list: the ‹ button in its header (or Ctrl+B) hides it, and a slim bar
  with a › arrow on the window's left edge brings it back. The list hides
  itself while a device screen is showing and returns when the screen stops.
  Connected and Saved targets are separate boxes with underlined headers and
  clearer icons, and Device Control has its own screen-and-pointer icon.
- Themes switch in about 0.12 s with a device open (was about 0.8 s): pages
  no longer carry their own stylesheets, and the Themes menu and Settings list
  the dark and light palettes as separate groups. The logcat right-click menu
  is readable in light themes again.
- Notifications: every action updates the status bar and shows a small toast
  (one toast, updated in place); errors show a large red toast at the bottom
  with an alert sound. The log panel's "Silent" option now only mutes popups
  while the log is open; it used to hide every warning toast.
- Typing on the embedded screen works without the old typing bar and is fast:
  click anywhere on the screen, including the video, and the PC keyboard goes
  straight to scrcpy, so keys arrive immediately and a held key stops repeating
  when released. If Windows refuses that focus hand-off, keys go over ADB
  instead, where held-key repeats are dropped while a key is still being sent
  and identical keys are sent together, so Backspace no longer keeps deleting
  after you let go. (With the direct route, symbols from non-US keyboard
  layouts and IME input are not typed; UHID keyboard mode is unaffected.)
- Recording while the screen is live no longer restarts or blanks it. A second,
  windowless scrcpy records alongside the live view (falling back to on-device
  recording if it can't start), and stopping it finalises the MP4 properly
  instead of killing scrcpy and leaving an unplayable file.
- The "Screen is off" placeholder follows the display's real shape: head units,
  clusters and other wide displays show a display frame (with a car icon for
  automotive devices) and phones a phone frame.
- Device type detection: `ADBHandler.device_kind()` (also merged into
  `device_info()` and `turboadb info`) classifies a device as Android
  Automotive (the automotive feature or build characteristic), an infotainment
  head unit running ordinary Android (no telephony and a landscape display),
  TV, watch, tablet or phone, with the evidence it used. Both car kinds get the
  IVI-compatible screen profile, the display-shaped placeholder and the IVI
  displays tab.
- The device page no longer has a header row: Screen, Screenshot, Reboot and
  More sit at the right end of the section tabs, and the terminals open with a
  two-line welcome banner (the Android one shows the connection, device type,
  Android version, CPU and serial). The "TurboADB" title next to Connect is
  gone.
- Light themes are dimmer (page brightness about a third lower) so they are
  no longer glaring; every text colour still meets the contrast checks.
- Toasts no longer print "QWindowsWindow::setGeometry: Unable to set
  geometry" warnings.
- Connecting no longer shows a red "shell failed: adb command timed out after
  0.45s" error when the phone is a little slow to answer the first identity
  check: the check (`ADBHandler.quick_identity()`) now allows 1.5 s, also reads
  the CPU type, and a slow reply is logged quietly. The background device
  details request no longer raises error popups either, so the terminal banner
  keeps the device's details. The red error popup has a Copy button, because
  Ctrl+C there went to the terminal and stopped its command.
- PowerShell and Command Prompt tabs behave like normal Windows shells:
  - **Frozen exe leaks:** the exe no longer passes its internal variables
    (`_PYI_*`, `QT_PLUGIN_PATH`, `_MEI…` PATH entries) or its private DLL search
    folder to the shells, which broke Python, Git and other tools there. The
    source run no longer adds PyQt5's Qt folder to PATH.
  - **Current-folder programs:** in CMD, `tool.exe` runs the copy in the current
    folder even when TurboADB was started from a shell that disabled that.
  - **Registry variables:** new ones are expanded the way Windows expands them.
  - **Non-English text:** it types, prints and works in paths (UTF-8 in both shells).
  - **Tab completion:** it offers `.\tool.exe` for programs in the current
    folder and follows `cd` / `pushd`.
  - **Interactive programs:** a bare `python`, `py` or `node` shows its prompt
    instead of seeming to hang (its input is a pipe, so it is started with
    `-i`), and in PowerShell `where python` runs `where.exe` (`where` is
    PowerShell's alias for `Where-Object`).
- Device Control is a control centre: while the screen is off, a device-shaped
  frame offers Start screen and Open in separate window; controls are large
  Back / Home / Recents buttons, media and volume icon buttons, one Wi-Fi /
  Bluetooth / Mobile data / Airplane / Hotspot tile each with On and Off, and
  colourful app launcher tiles and keyboard keys.
- The status bar no longer says "no device connected" while devices are
  attached but not yet opened.
- New themes: the default pair is Graphite (dark) and Porcelain (light),
  layered neutral surfaces with soft text instead of pure black or glaring
  white. Mocha/Latte, Forest/Sage, Plum/Rose and Deep teal/Mint are extra pairs
  in Settings and the Themes menu, and the top-bar toggle switches to the other
  half of the current pair. Terminal,
  logcat and log panels use toned-down ANSI and level colours, every colour
  comes from one palette, live theme switches also update placeholder text,
  text colours are tested for readable contrast, and tab labels are no longer
  clipped.
- Closing a device tab or its screen panel no longer stops half-way (leaving
  adb, logcat or scrcpy running) when a background task had already finished.
- Pairing, the shell-lost check and "refresh shortcuts" no longer freeze the
  window; a shell that keeps dropping stops reconnecting after three attempts
  in 30 s and waits for the device instead.
- A closed tab's reconnect loop stops immediately; quitting during an ADB
  server restart no longer crashes; the Webcam tab can be reopened after
  closing it; a second screenshot can no longer destroy one in progress; a
  cancelled screen start no longer leaves Record stuck on "Starting recording…".
- Screen mirror: closing a tab during a recording finishes the MP4 instead of
  truncating it; a failed embed falls back to a separate window; the window no
  longer flashes at the desktop corner while a recording closes; long sessions
  stop re-reading the whole scrcpy log twice a second; closing the IVI display
  wall no longer freezes; typed text and taps keep their order; scrcpy's
  temporary logs are cleaned up.
- Device tabs: Stop / Ctrl+C in the Android shell no longer kills every logcat
  on the device (including the Logcat tab); a Reconnect button appears after a
  failed connection, a reconnect timeout or a reboot to recovery/bootloader;
  Device → Mirror honours the selected display and the IVI compatibility
  default; a USB "only device" target no longer opens a second tab.
- Main window: changing the ADB path in Settings offers to restart the ADB
  server; warning toasts are rate-limited; Help opens the documentation site;
  the "up to date" dialog shows the real tool versions; device polling pauses
  while the ADB server restarts.
- File browser: the built-in editor keeps line endings and refuses files it
  can't decode instead of corrupting them; folder symlinks such as `/sdcard`
  open as folders; transfers run one at a time; local copy, delete and folder
  listing run off the UI thread; device paths are quoted.
- Logcat keeps working after a one-off dump or stop, an early Stop no longer
  leaks `adb logcat`, and changing the filter redraws a bounded slice.
- Webcam: the remote stream's firewall rule is limited to this PC and removed
  when the stream stops; closing during a remote start stops the remote
  ffmpeg; long recordings are no longer cut off while finalising; the ffmpeg
  download is written atomically.
- "Save full output" reports failures instead of silently truncating, and
  writes off the UI thread. Closing a local terminal also ends programs it
  started.
- Terminal: 256-colour and true-colour output, `ESC ( B`, erase-to-line-start,
  PageUp/PageDown and `cd -` / quoted directories now behave correctly; split
  escape sequences no longer leak into saved logs; font zoom is debounced.
- Settings are type-checked on load, cached, and written atomically under a
  lock; the Settings dialog saves only what you changed. The unused "open docs
  on first run" option was removed.
- Dialogs clean up after themselves; editing a saved target renames it instead
  of creating a copy, and incomplete targets are rejected before saving.
  Repeated error popups are rate-limited.

### Release baseline

- Removed the unused split/tile workspace mode and its obsolete menu and
  documentation entries.
- Standardized the package and UI on the 2.0.0 release version.
- The wheel bundles the Windows GUI executable (`turboadb/bin/turboadb-gui.exe`),
  and GitHub Releases carry the same versioned `TurboADB-<version>-win64.exe`.
  `turboadb-gui` starts the bundled executable when PyQt5 isn't installed.
  `scripts/release.py` builds or reuses the executable, bundles it and checks
  the wheel contains it (`--no-exe` builds a lean package).
- Refined device-session startup, terminal focus recovery, shell naming, and
  live-device handling introduced during the 1.1.x stabilization work.
- Reworked the layered tab UI with native Qt fade transitions and clear primary,
  device, and terminal tab hierarchy; removed the harsh nested-tab divider.
- Added persistent ADB/device/version status indicators and a reordered
  **Device Control** toolbar.
- Added an automotive-only IVI display wall with simultaneous lightweight
  previews and per-display control, maximise, screenshot, and recording actions.
- Serialized scrcpy start/stop/record transitions so cancelled launches and
  retries cannot create duplicate recording sessions or overlapping windows.
- Made device input FIFO and bounded, hardened embedded keyboard focus and tab
  shutdown, and isolated each device-side recording by remote file and PID.
- Allowed concurrent remote mirrors to select from TCP 27184-27199 instead of
  forcing every session onto the same tunnel port.

## 1.1.17

Release packaging and stability follow-up.

- Fixed duplicate device-tab creation and reduced the fallback device poll rate;
  event-driven ADB tracking remains the primary update path.
- Hardened ADB socket reads, reconnect handling, transfer cancellation, IPv6
  target formatting, persistence writes, and remote-deploy input validation.
- Fixed local-terminal completion so a nested `adb shell` never receives Windows
  folder suggestions or alters the local shell path.
- Restored the framed terminal welcome banner while removing the redundant
  date/time prompt strip; PowerShell/CMD and ADB prompts retain the active path.
- Added Windows single-instance protection and targeted regression coverage.

## 1.1.16

Comprehensive architectural hardening, concurrency safety, and code quality pass.

- **Instant Event-Driven Device Detection:** Added `_DeviceTracker` socket stream on `host:track-devices-l`. Devices are detected and reported in ~1.3ms the moment they are plugged in or unplugged, eliminating the 2-second polling latency.
- **ADB Server Daemon Stability Fix:** Detached Windows daemon process launch (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`) preventing fatal pipe ACK aborts (`cannot write ACK to handle The pipe is being closed (232)`).
- **TCP RST Abort Elimination:** Removed `SO_LINGER(1, 0)` from socket queries to prevent Windows Winsock `WSAECONNRESET` disconnects on port 5037.
- **Smart UI Debouncing & Shell Reconnect:** Added signature caching in `MainWindow._on_devices()` to eliminate list flicker and required 2 consecutive empty polls before declaring devices disconnected. Guarded `_on_shell_lost()` with device state check to restart shell cleanly without disabling UI actions.
- **Pixel-Perfect Monospace Welcome Banner:** Exact unicode character width calculation and strict monospace CSS rules ensuring pristine right-border alignment on terminal banners.
- **Lazy Remote File Browser:** Remote directory listing deferred until the Files subtab is viewed, keeping USB bandwidth free during shell startup.
- **Fully Enclosed Terminal Welcome Boxes:** Replaced unclosed right ASCII borders on terminal welcome boxes with ANSI-aware enclosed boxes (`┌─ ┐`, `│ │`, `└─ ┘`) across Android Shell, PowerShell, and Command Prompt sessions.
- **Autonomous Tool-Side ADB Server Lifecycle:** TurboADB now self-starts and verifies the local ADB daemon on application launch in a background worker (`_AdbInitThread`), eliminating reliance on external commands or pre-running daemons. Added immediate `Detecting devices…` visual feedback in the sidebar and Welcome screen.
- **Zero-Freeze GUI:** Synchronous ADB execution (`device_info`, scrcpy mirror start, and logcat buffer clearing) offloaded to background threads. Tab-completion in the terminal capped with a 1.5s timeout and safe fallback.
- **Process & Handle Leak Elimination:** Fixed child process reaping across `ShellSession`, `iter_lines`, and `ScrcpySession` by ensuring OS pipe handles are explicitly closed and child processes waited on.
- **Qt Memory Leak Protection:** Tabs closed in the GUI now destroy underlying C++ Qt widgets (`deleteLater()`). Circular `QThread` lambda closures across all panels replaced with structured tracking to avoid retaining worker threads.
- **Zip-Slip & Path Traversal Hardening:** Archive extraction in `toolsdl` fully sanitized across all extraction paths.
- **Type Safety & Data Handling:** `CommandResult.stdout` supports both `str` and raw `bytes`, with safe decoding in `text` and `lines` properties. IPv6 bracketed hosts and port extraction normalized without corrupting hexadecimal segments.

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
