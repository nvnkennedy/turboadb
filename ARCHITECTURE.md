# TurboADB — Architecture

This is a tour of how the codebase fits together: the layers, who calls whom, and
the few design decisions worth knowing before you change anything.

## The one rule

There is **one engine** (`turboadb.core.ADBHandler`) and **three thin
front-ends** on top of it — the Python API *is* the engine, the CLI parses
arguments and calls it, and the GUI calls it from background threads. No device
logic lives in the CLI or the GUI. If you add a capability, you add it to the
engine; the CLI and GUI just expose it.

```
            ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
            │   Python API  │   │      CLI      │   │      GUI      │
            │  (import it)  │   │ (argparse)   │   │   (PyQt5)     │
            └───────┬───────┘   └───────┬───────┘   └───────┬───────┘
                    └───────────────────┼───────────────────┘
                                        ▼
                          ┌─────────────────────────────┐
                          │   turboadb.core.ADBHandler   │   the engine
                          └─────────────┬───────────────┘
                                        ▼
                    adb / scrcpy  (bundled, auto-downloaded)
                                        ▼
                        device  (USB · TCP/IP · remote adb server)
```

## Package layout

```
turboadb/
├── __init__.py        public API surface (__all__) + __version__
├── __main__.py        enables `python -m turboadb`
├── core.py            ADBHandler — the engine. Every device operation.
├── config.py          ADBConfig, ScrcpyOptions (dataclasses)
├── results.py         CommandResult/TransferResult/StreamResult/OperationResult, strip_ansi
├── exceptions.py      ADBError hierarchy
├── devices.py         enumerate devices; share/serve helpers (shared server, firewall, startup task)
├── scrcpy.py          launch scrcpy; remote-tunnel + host resolution; software-render env
├── tools.py           locate adb/scrcpy on PATH or in ~/.turboadb/tools; diagnose()
├── toolsdl.py         download/upgrade adb + scrcpy into ~/.turboadb/tools
├── update.py          self-update: check PyPI, pip-upgrade, relaunch
├── remote_deploy.py   deploy 'serve' to remote Windows hosts over WinRM (pywinrm/NTLM)
├── remotefs.py        device-file shell helpers (ls parsing, mv/cp/rm/probe) shared by the engine and the Files tab
├── cli.py             argparse front-end; console-script entry points
├── assets/            icon.ico / icon.png
└── gui/               the PyQt5 application (see below)
```

## The engine — `core.ADBHandler`

A handler is bound to one target via `ADBConfig` (a USB serial, a `host:port`
network device, or a device on a **remote adb server**). Construction is cheap;
`connect()` does the handshake.

Two things shape every method:

- **`_run` / `_run_global`** build the right `adb` command line. `_base()` injects
  `-s <serial>` and, for a remote server, `-H <host> -P <port>` — so the *same*
  code drives a local or remote device with no special-casing upstream.
- **safe mode.** Methods take `safe=None|True|False`. In raw mode they return a
  result dataclass and raise on failure (good for scripts). In **safe mode**
  (`ADBHandler(cfg, safe=True)`, what the GUI uses) they wrap everything in
  `_guard()` and return an `OperationResult` instead of throwing, so a misbehaving
  device can't crash the UI. A `log_callback` receives the real `adb` command line,
  its duration/exit code, and full error text.

Logging levels matter: the raw `$ adb …` command trace is emitted at **DEBUG**,
and meaningful events at INFO/WARNING/ERROR, so the GUI log can hide the noise by
default and reveal it on demand.

Helpers worth knowing: `ShellSession` (a persistent interactive shell with
`send`/`read`), `iter_lines`/`stream` (live logcat), `capture_png` and
`screen_record` (multi-strategy capture that survives RDP), and `mirror` (scrcpy).

## adb / scrcpy management

TurboADB never assumes the tools are installed. `tools.py` looks on `PATH` and in
`~/.turboadb/tools`; `toolsdl.py` downloads matching `adb`/`scrcpy` there on first
run and can upgrade them. `update.py` handles upgrading TurboADB *itself* from
PyPI and relaunching. This is why a fresh `pip install turboadb` just works.

## scrcpy & remote tunnels

`scrcpy.py` is the fiddly bit. Over a **remote** adb server, scrcpy's video tunnel
has to be pinned: it sets `--tunnel-host`/`--tunnel-port` to a fixed port,
forces adb-forward, can switch to **software rendering** for stubborn IVI GPUs,
and pins scrcpy to TurboADB's own `adb` via the `ADB` env var. `resolve_host()`
strips stray `:port` suffixes; the adb v37 quirk where
`ANDROID_ADB_SERVER_ADDRESS` wants a **bare** host (it wraps it itself; an IPv6
literal keeps its brackets, as with `-H`) is handled here. `launch_scrcpy`
decides once whether a "remote" server is really this PC (the same rule the
device list uses) and the session says so (`ScrcpySession.tunnel_host`).

## Sharing devices: serve & deploy

- **`devices.start_shared_server()`** runs `adb -a … server start` detached, so a
  machine can share the devices attached to it. `open_firewall()` opens 5037 +
  27184 for Domain and Private networks only (the server has no password), and
  `stop_shared_server()` closes them again. `install_serve_task()` registers a
  SYSTEM startup Scheduled Task so it survives logoff/reboot (more robust than a
  login-folder launcher); it stops whatever answers on the port first, so only
  the task's own server can pass its readiness check. `~/.turboadb/sharing.json`
  records the ports this account shared (a tools update stops and restarts
  those servers once) and, in SYSTEM's profile, this user's adb keys, which the
  task's server passes to adb as `ADB_VENDOR_KEYS`.
- **`remote_deploy.deploy_serve()`** does the above on *remote* Windows hosts
  **from your machine**, over WinRM. It uses **pywinrm with NTLM transport** —
  explicit `DOMAIN\user` credentials over plain WinRM, no Kerberos/TrustedHosts
  setup needed — then runs `turboadb serve --startup-task` on each host. The GUI
  wraps this in a dialog with a pre-flight Test; the CLI exposes it as
  `turboadb deploy-serve`.

## The GUI (`turboadb/gui/`)

PyQt5, one engine call per user action, all blocking work on `QThread`s so the UI
never freezes.

```
app.py            QApplication bootstrap, theme, excepthook, app-user-model-id
main_window.py    ribbon + menu bar, device tabs, sidebar, log dock, split view,
                  upgrade/self-update, shortcuts, share/deploy actions
  status_bar.py     the coloured status bar: state, messages by kind, ADB/devices/version chips
device_tab.py     per-device tab; connects in the background; hosts the sub-panels
  console.py        AnsiConsole — the interactive shell terminal
    vtscreen.py       the VT100/xterm screen for full-screen programs (no Qt)
    screen_view.py    paints that screen over the console's scrollback
    shell_colors.py   device prompts and logcat lines the console colours (no Qt)
  terminal.py       reader thread that pumps shell bytes into the console
  logcat_view.py    live logcat with filtering + complete-save
  file_browser.py   device filesystem tree + push/pull; one per Files tab
    file_open.py      opening files in their apps; device copies whose saves go back
    transfer_log.py   transfer history model + size measuring (no Qt)
    transfer_panel.py the Transfers panel: summary, table, retry, report
    file_icons.py     native file-type icons, with the glyphs as fallback
  apps_panel.py     package list / install / uninstall / start / stop
  controls_panel.py the responsive grid of device controls
  phone_panel.py    dialer / calls / SMS
  mirror_panel.py   scrcpy launch (window / embedded / compat) + recording
connect_dialog.py / session_dialog.py / settings_dialog.py / deploy_dialog.py
scrollback.py     disk-archived scrollback so saves are complete under a flood
log_panel.py      leveled, filterable log dock
theme.py          dark/light stylesheets, emoji→QIcon, themed accents
sessions.py / settings.py   persisted saved targets + app settings (~/.turboadb)
```

These patterns recur and are worth preserving:

- **Decoupled ingestion.** The shell and logcat never process incoming bytes on
  the receiving path. They enqueue + archive (both O(1)) and a timer renders a
  *bounded* slice per tick. That's what keeps the UI responsive under a million
  lines of output, and why a Save is always complete even if the on-screen buffer
  was trimmed (see `scrollback.py`). The one bound on that: each history file
  keeps to `Scrollback.HISTORY_LIMIT_MB` (1 GB; `history_limit_mb` in
  settings.json, 0 for none) and to less while the disk is nearly full, in two
  parts, the older one dropped once the newer is full, and a save says what
  went. Logcat archives on its reader thread, before
  any cap, and applies its Filter there too, so the caps only ever drop matching
  lines; a new Filter searches the whole archive. The terminal's own text (Stop
  and reconnect notices, a submitted line, a screen clear) goes through the same
  queue when output is waiting, so it lands where it happened in the stream.
  A line typed while a command runs is drawn at once (it may be that command's
  answer); if the shell reads it as a command after all, that copy goes when
  the shell echoes it after its prompt. A line typed before a shell's first
  prompt is left to that echo.
- **The edit region.** In `AnsiConsole` the synthetic prompt (Android shell over a
  pipe) and the line being typed are always the last characters of the document,
  and output is inserted above them. Typing while a command streams therefore never
  mixes with its output, and editing the line never touches it.
- **Reliable stop.** The Android shell runs on a device terminal, so Stop sends a
  real Ctrl+C (`\x03`): the command stops (`ping` prints its summary) and the
  shell stays. If within 3 s neither the device prompt nor anything but the
  terminal's own `^C` comes back (the command ignores Ctrl+C), or Stop is
  pressed again before the prompt, the hard path runs: it tears down the shell
  (which ends its own device-side process group, never anything device-wide),
  discards stale in-flight data via a current-reader guard, and reopens in the
  same folder. A program that answers the Ctrl+C with a prompt of its own
  (`sqlite3`, a shell with another prompt) is never reopened under the user.
  Restart shell takes the hard path at once, and over plain pipes (no PTY,
  where `Ctrl+C` can't signal the device) Stop always does.
  The PowerShell / CMD terminals start a typed interactive `adb shell` with
  `-t -t` (a device PTY), so there Stop sends a real Ctrl+C to the device and
  keeps the adb shell; a second Stop before the device prompt returns (or no
  answer within 3 s) reopens the local shell, re-enters the same adb shell and
  `cd`s back. For a local command, Stop raises a real Ctrl+C in the shell's
  hidden console (`LocalShellSession.send_ctrl_c`: TurboADB attaches to that
  console for the call, ignoring the event itself, or a helper process does
  when TurboADB has a console of its own; shells are started with Ctrl+C
  handling on). As in a console window `ping` prints its summary, a REPL its
  KeyboardInterrupt, and the shell abandons the rest of the line, script or
  batch file (cmd's "Terminate batch job" question is answered yes); an adb
  server a typed `adb` forked has no console and is not touched. A command
  that ignores it without a word for 1.5 s, or a second Stop, has its
  processes ended (`LocalShellSession.kill_command`: never the shell, its
  `conhost.exe` or an adb server, see `proctree`); a shell whose prompt is
  still not back 1.5 s later (a loop inside PowerShell itself), or a third
  Stop, is replaced. Stop never types an answer for the user: a shell that
  waits for a line itself (`pause`, `set /p`, `Read-Host`, a `-Confirm`
  question, the rest of a `>>` block) notices Ctrl+C only after reading one,
  and Enter would confirm, run the block or let the script go on, so it is
  replaced instead. Only a shell already at its prompt (a background job was
  stopped) is sent an empty line, for a new prompt.
- **Local shells over pipes.** cmd and PowerShell run without a console, so
  `local_terminal` stands in for one: `PYTHONUNBUFFERED`, LF-only input written
  by a thread of its own, a `Read-Host` that shows its prompt, and an invisible
  OSC mark (`PROMPT_MARK_RE`) at the end of every prompt (a `prompt` /
  `set prompt=` typed in cmd, or a one-line `function prompt` in PowerShell,
  gets it again). The widget uses that mark (or a default prompt) to tell a
  command from input for a running program: conveniences (`ls`,
  `adb shell -t -t`, streaming `find`/Select-String for a plain `| findstr`,
  PowerShell's buffer width once the view was resized) apply only to commands.
  PowerShell keeps the PC's execution policy (its startup script goes in with
  `-Command`). ConPTY, which would give
  findstr, `timeout` and progress output a real console, is not used.
- **The Android shell's terminal.** `_AndroidShellWidget` opens
  `open_shell(tty=True)` (`adb shell -t -t`) unless the `android_shell_pty`
  setting is off: the device prints its own prompt and echo, and its programs
  write line by line. A hidden first line turns mksh's line editor off (adb
  gives the terminal no size, so it scrolled long lines sideways), sets
  `stty cols/rows` from the view and prints an OSC 7718 mark; output is held
  back until that mark (3 s at most), so neither the line nor the device's first
  prompt shows. The device prompt on the output tail (`[N|]host:/path $`, also
  from a nested `su`/`sh`) is what "ready" means: the console's at-prompt
  state (never "no" while a command runs, since the terminal echoes typed lines
  then too), paste pacing, a settled Ctrl+C and the folder for Tab. Input is
  one LF per line, never a trailing bare CR (adb.exe holds it) and never 0x1A
  (end of input to adb.exe), written by a thread of its own (`_ShellInput`), and
  a line typed before the first prompt waits for it (whatever follows the
  hidden line's mark, also a PS1 that looks like no prompt here). Once the
  view is wider or narrower, the next command typed at the prompt carries
  `stty cols/rows` in front (its echo hidden); nothing is typed into a
  running program. The size is
  `AnsiConsole.cell_size`: whole character cells, margins and the scroll bar
  left out whether it shows yet or not. The hidden first line also aliases
  `ls`/`grep` to `--color=auto` where the device's tools take it (toybox
  0.8.12, which colours only output that is not a terminal, gets an `ls`
  function that lists for the terminal through `cat`), and its
  mark carries the terminal's name (`tty`). The reader
  folds adb.exe's CR CR LF into CR LF,
  carrying CRs across reads. A terminal shell that ends or reports an adb error
  before its first prompt (an adb without `-t`, a build without terminals)
  falls back to pipes, where the console draws the prompt; that is undone when
  a plain shell fails at once too (the device was the problem). A device
  without shell_v2 gives a terminal even over pipes: its prompt before any
  command gives that away, and it is set up like one.
- **Colour, Ctrl+C and full-screen programs.** The console draws output as
  lines (horizontal cursor moves included). An owner that recognises its
  shell's prompt calls `mark_prompt()` after feeding the output, and the
  console colours that prompt (`shell_colors.prompt_spans`) unless it has
  colours of its own; a line that was plain text from start to end and is a
  logcat line (`shell_colors.LogcatLines`) takes its priority's colour, even
  when it came in several reads. The undrawn backlog is capped at about a
  second of drawing (`_backlog_cap`, from the measured rate). Ctrl+C on a
  device terminal calls `interrupt_output()`: the backlog goes, and so does
  what arrives before the terminal's own `^C` echo (held, released after a
  second or with the prompt if no echo comes); Stop in PowerShell/CMD drops
  the backlog too. A device terminal's owner calls `set_screen_input(fn)`:
  then output that needs a screen (the alternate screen, a scroll region,
  a cursor position on another line, moves up or down, lines inserted or
  deleted; home + ED 2 stays a plain clear) hands the rest of the stream to a
  `vtscreen.Screen` seeded with the view's last lines, painted by
  `ScreenView`, and every key goes to the program through *fn* (LF for Enter
  and Ctrl+M, never 0x1A), also the keys the window has shortcuts for; its
  cursor and status questions are answered there too. The screen's backlog
  is capped the same way, keeping the newest output (the program redraws),
  never past a switch of screens; Ctrl+C there drops it and still goes to
  the program. The screen goes when the program leaves the alternate screen
  (the view comes back as it was), at the next `mark_prompt()` on the main
  screen (its rows down to the cursor, and a few thousand that scrolled off,
  replace the lines it took over; untouched rows keep their formats), or at
  `end_screen()` when its terminal is gone (the adb shell in PowerShell/CMD
  ended). While it shows, our own notices wait until it goes. The Android
  shell resizes the device terminal of a running program with
  `ADBHandler.resize_terminal` (`stty -F` in a one-shot shell); an adb shell
  in PowerShell/CMD has no terminal name, so its programs keep the size they
  started with (`top` asks the screen for it).

### Rapid tap bursts

`touch.py` holds the pure parts: parsing ``getevent -pl``, choosing the
touchscreen, scaling a display point into the panel's own units and building the
device-side loop. `ADBHandler.tap_burst` runs that loop in ONE ``adb shell`` so
no tap waits for the PC, prefers ``sendevent`` (the device's own tool, so the
events are packed for its kernel) when the shell may write to the input node,
and falls back to ``input tap`` otherwise — as it always does for a burst aimed
at another display, since an input node belongs to one screen, and on a display
turned against its panel (the panel reports in its own orientation; an explicit
``events`` burst turns the point back by the rotation ``dumpsys input`` reports).
The loop stops at the first refused tap, and prints its progress so the caller
can follow it; a burst is ended only when that progress stops coming.

### Push, pull and screen recording

adb prints its ``[ NN%]`` progress only to a terminal, never into the pipe
TurboADB reads. `ADBHandler._transfer` therefore measures the copy itself
(`_TransferWatch`, from the first second on): what a pull has written on this PC,
what the device holds for a push. `transfer_timeout` is a stall limit, not a
deadline, and a pull that is ended early removes the one file it created.
`screen_record_continuous` records the next part while the last one is pulled,
so the pulls leave no gap. The recorder and transfer children run in a session
of their own on Linux and macOS, so a Ctrl+C in the terminal reaches Python
alone and the CLI can still stop the recording cleanly; children still running
when the interpreter exits are ended then, with their clean-up. Every child the
engine, scrcpy and the GUI's streams start is stopped through
`proctree.stop_process`.

### Shutdown

`gui/app.py::_stop_started_tools` runs when the app exits. It first waits
(3 s at most, `local_terminal.join_closers`) for the PowerShell / CMD terminals
the closing tabs ended: their process-tree kills run on daemon threads, which
died with the app before the kill landed and left the shell and its commands
running. Then `scrcpy.stop_all()`
closes any mirror this process started (every `ScrcpySession` registers
itself), all at once within one time budget,
and `ADBHandler.stop_server` stops the adb server if this process started it
(`tools.owned_adb_server`) and this PC is not sharing its devices. Turning
`stop_adb_on_exit` off leaves both running: the main window then releases
separate scrcpy windows (`MirrorPanel.keep_window_open`) before its tabs close.
The server steps run inside `tools.server_lock_patience`: another thread can
hold the server lock across a blocking adb call (a restart, a share, a closing
tab's disconnect), and past a few seconds the exit leaves the server running
rather than keep a closed app alive; other threads still wait for the lock.

### The adb server: one starter, one stopper

`tools.ensure_adb_server` is the only way TurboADB starts the local server. It
keeps the `adb start-server` launcher it spawned per port, and every caller that
finds the port dead while that launcher runs waits on it instead of spawning
another, each with its own timeout, outside the lock. The GUI pre-warm
(`gui/adb_path.prewarm_adb_server`, once per process), `MainWindow`'s startup
check, device tabs (`ADBHandler._connect`), restarts and the device list all
share it. A server that answers once a launcher ran is recorded as started here,
until that launcher ends without adb's "daemon started successfully": then
another program started it in the same moment, and it is not TurboADB's to stop.
`tools.kill_adb_server` is the only stopper (restart, "Stop sharing", tools
upgrades, exit): it waits until the port is released and forgets the record.
The default port follows `ANDROID_ADB_SERVER_PORT` (`tools.local_adb_port`), as
adb's own commands do, so the socket probes, the tracker and the start agree.
After startup the GUI compares the server's `host:version` protocol (and
`host:server-status` executable) with its own adb and warns about a server that
another adb would keep restarting.

### Write access to protected files

`ADBHandler.access_status()` reports root, `ro.debuggable`, the build type, the
dm-verity mode and the bootloader state from one shell call. `make_writable()`
runs `adb root`, optionally `adb disable-verity`, then `adb remount`, and
handles the reboots those need (after a reboot adbd is not root, so root runs
again). The GUI shows `gui/device_access.WriteAccessDialog` when a change is
refused (`is_permission_problem`), runs the choice through `DeviceTab`, and
holds every terminal meanwhile: the Android shell pauses and reconnects, and
PowerShell/CMD reopen the `adb shell` they were in, in the same device folder.
While held, the tab's shell-lost and reconnect paths leave the terminals alone
(`_release_terminals` brings them back and resumes a Logcat capture), and a
Terminal opened for the first time meanwhile opens after the release.

## Packaging

- `pyproject.toml` — package metadata, the console-script/gui-script entry
  points, optional extras (`gui`, `winrm`, `all`), and package assets.
- `turboadb-gui.spec` + `scripts/build_exe.py` — PyInstaller build of the
  versioned GUI executable (`dist/TurboADB-<version>-win64.exe`). `collect_all`
  pulls the entire pywinrm/NTLM stack so WinRM works in the frozen exe.
  `scripts/gui_entry.py` is the frozen entry point (with a startup self-test hook).
- `scripts/release.py` — copy that exe into `turboadb/bin/turboadb-gui.exe`
  (package data, git-ignored), build wheel+sdist, check the wheel contains the
  exe, and upload to PyPI. `turboadb-gui` starts the bundled exe when PyQt5 is
  not installed.

## Where things live at runtime

```
~/.turboadb/
├── tools/            downloaded adb + scrcpy (verified against the publisher's checksum)
├── ffmpeg/           webcam encoder + ffmpeg.exe.sha256, re-checked on every reuse
├── settings.json     theme, fonts, defaults   (+ .lock, cross-process writes)
├── sessions.json     saved targets            (+ .lock; re-read and merged before each write)
├── sharing.json      ports shared with `serve`, the startup task's port and adb keys
├── logs/             temp scrollback archives (cleaned on close; 1 GB each at most;
│                     named after their process, which holds a lock there while
│                     it runs, so a start's clean-up spares another's)
└── crash.log         uncaught GUI errors
```

All of these resolve through `config.user_dir()` / `user_path()`, which re-read
`HOME`/`USERPROFILE` on every call.

```
```
