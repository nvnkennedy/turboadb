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
├── tools.py           locate adb/scrcpy by absolute path (~/.turboadb/tools, PATH's folders, an SDK);
│                      Windows' own programs by full path; the adb server's one starter and stopper; diagnose()
├── toolsdl.py         download/upgrade adb + scrcpy into ~/.turboadb/tools, where a build exists and runs
├── update.py          self-update: is a newer release out, can pip install it here, pip-upgrade, relaunch
├── proctree.py        end a process tree without taking down an adb server; the one way a child is stopped
├── touch.py           the pure parts of rapid tap bursts (see below)
├── remote_deploy.py   deploy 'serve' to remote Windows hosts over WinRM (pywinrm/NTLM)
├── remotefs.py        device-file shell helpers (ls parsing, mv/cp/rm/probe) shared by the engine and the Files tab
├── cli.py             argparse front-end; console-script entry points
├── launcher.py        Windows: the GUI as TurboADB.exe, a branded copy of this Python (see below)
├── winshell.py        Windows: taskbar mode, window app ID + pin command, .lnk shortcuts (ctypes COM)
├── assets/            icon.ico / icon.png (dark), icon-light.ico / icon-light.png
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
`send`/`read`; `read()` returns what has arrived and never waits, on any
platform, so a script may poll an idle shell), `iter_lines`/`stream` (live
logcat), `capture_png` and `screen_record` (multi-strategy capture that survives
RDP), and `mirror` (scrcpy).

A few rules hold across the methods:

- **Starting adb.** A one-shot command runs through `_exec`; a child that is
  read while it runs (a push or pull, `iter_lines` and what is built on it,
  `open_shell`, `popen`, the screencap stream, the recorder) starts through
  `_spawn`. Both turn an adb that cannot be started (locked, quarantined, being
  replaced) into an `ADBError`, so no caller ever sees a bare `OSError`. A
  `cancel` event (`_exec`, `shell(cancel=…)`) lets another thread call a
  command off: adb is killed at once and the call fails with
  `ADBConnectionError`.
- **Time limits.** `command_timeout` (60 s) is meant for quick commands. A
  command that runs as long as its data takes keeps a limit of its own, which
  `_work_timeout` only ever raises: an install (five minutes, ten for split
  APKs, plus the time the files take at 128 KB/s), a device copy (ten minutes)
  and a delete (five) get the longer of that and `command_timeout`, and no
  limit at all when `command_timeout` is None (the CLI's `--timeout 0`). For
  a push or pull see `transfer_timeout` below.
- **The device's own answer decides.** An install or uninstall worked when adb
  ended well *and* the package manager said `Success` on a line of its own
  (`_pm_answer`; a staged install says `Success.` and more on that line): the
  word anywhere else is not its answer (adb names the APK in its error, and
  an APK can be called `SuccessFactors.apk`). `uninstall(keep_data=True)` asks
  the device's package manager itself (`cmd package uninstall -k`, and
  `pm uninstall -k` where that gives no verdict), because `adb uninstall`
  refuses `-k`. A command that changes device files (`make_dir`, `touch`,
  `copy`, `move`, `remove`, `chmod`, all through `_file_command`) goes out as
  `{ command; } && echo TURBOADB_DONE` (`remotefs._marked`) and worked only
  when adb ended well and that mark is the last line of its output
  (`_marked_done`): a device before Android 7 gives adb no exit status, so
  there a refused change ends 0 like one that was made.
  `remotefs._list_remote_dir` asks once more, with a mark, about a folder that
  listed nothing while the device said something: a folder it would not list
  is an error, never an empty folder. `call_state()` reads every SIM's
  `mCallState` (a call on any of them counts, then a ring); `"unknown"` means
  the device answered without a call state, and a device that could not be
  asked is an `ADBCommandError`, since nothing was learnt about it.
- **Whose app.** `pm` and `content` mean user 0 unless told otherwise, and on
  Android Automotive that is the headless system user while the driver is a
  secondary one. `_content_user()` asks for the foreground user
  (`am get-current-user`, cached), and `_package_user()` names it for
  `pm clear`, `pm grant` and `pm revoke` when that user has the package; a
  phone, whose foreground user is 0, keeps the plain command.
- **Whose connection.** `owns_connection` is true when this handler's own
  `adb connect` made the network connection (adb answered "connected to", not
  "already connected to"), and stays true when the same target is connected
  again (a tab's reconnect loop after a reboot), until `disconnect()`. Only
  such a connection is this app's to drop (see The device tab).
- `parse_display_info` leaves out the virtual display scrcpy mirrors through
  (named `scrcpy`, not physical), which Android lists while a screen runs.

## adb / scrcpy management

TurboADB never assumes the tools are installed. `tools.py` finds them, first hit
wins: an explicit path, `TURBOADB_ADB` / `TURBOADB_SCRCPY`, the GUI's path
setting, `~/.turboadb/tools`, a copy bundled in the package, `PATH`, and the
common SDK and install folders. An explicit path or an environment variable
that names no executable is an error, never a reason to fall through (a
different adb would restart the server the chosen one started); a stale path in
Settings is a preference, and is skipped with a warning.

Auto-detection looks at absolute locations only, never in the current folder,
and `find_adb` / `find_scrcpy` always answer with an absolute path.
`shutil.which` starts with the working folder on Windows (and takes an empty or
relative `PATH` entry for it anywhere), so with it an `adb.bat` lying in a
downloaded project would be started as adb. `tools._on_path` asks each
absolute folder of `PATH` for the file by its full path instead; a location
built on an unset variable is skipped (`_first_file`), and the Unix folders
are left out on Windows, where `/usr/bin` names a folder of whichever drive
is current.
`_on_path` is the lookup for everything TurboADB itself starts by name: adb,
scrcpy, ffmpeg for the webcam and the editor of `turboadb edit`. Windows' own
programs (netsh, schtasks, PowerShell, rundll32) are started with
`executable=tools.windows_program(name)`, their full path in System32, for the
same reason: named bare, a program of that name in the current folder runs in
their place.

`toolsdl.py` downloads `adb`/`scrcpy` into `~/.turboadb/tools` on first run and
can upgrade them, which is why a fresh `pip install turboadb` just works. Each
is downloaded only where a build exists for the machine
(`adb_download_supported()`: Windows, macOS and Linux on x86-64, Google's one
Linux build; `scrcpy_download_supported()`: Windows). `ensure_tools` only ever
fetches a tool that is missing: replacing adb means stopping the running
server, so upgrades go through `upgrade_tools`. A downloaded adb must answer
`adb version` from its staging folder (`_require_runnable`) before the server
is stopped or anything is replaced, so a build that cannot run here (musl,
NixOS) never takes the place of a working system adb. After a swap the tools
cache is cleared, so the copy just installed is the one found and reported.

`update.py` handles upgrading TurboADB *itself* from PyPI and relaunching. It
keeps two questions apart: whether a newer release exists (`check`,
`check_cached`, asked from any copy) and whether pip can install it here
(`can_self_update`; `self_update_blocker` says why not: the standalone exe, a
source checkout). The GUI tells a copy pip cannot update about a newer release
too, with the blocker's text as the way to get it.

## scrcpy & remote tunnels

`scrcpy.py` is the fiddly bit. Over a **remote** adb server, scrcpy's video tunnel
has to be pinned: it sets `--tunnel-host` and keeps the port inside a bounded
range (`--port=27184:27199`, the range the firewall rules open, so that
concurrent tabs do not race for one port),
forces adb-forward, can switch to **software rendering** for stubborn IVI GPUs,
and pins scrcpy to TurboADB's own `adb` via the `ADB` env var. `resolve_host()`
strips stray `:port` suffixes; the adb v37 quirk where
`ANDROID_ADB_SERVER_ADDRESS` wants a **bare** host (it wraps it itself; an IPv6
literal keeps its brackets, as with `-H`) is handled here. `launch_scrcpy`
decides once whether a "remote" server is really this PC (the same rule the
device list uses) and the session says so (`ScrcpySession.tunnel_host`).
`list_displays` (scrcpy's own display list) captures scrcpy's output in a temp
file, not in pipes: scrcpy's adb clients inherit its output, and on Windows a
pipe is read until the last of them lets go of it, which can hold the call
long past its timeout. At the timeout scrcpy's whole process tree is ended
(`proctree.stop_process(grace=0, tree=True)`).

## Sharing devices: serve & deploy

- **`devices.start_shared_server()`** runs `adb -a … server start` detached, so a
  machine can share the devices attached to it. A server that has not answered
  within its 10 s is stopped before the failure is reported: left running, it
  would bind every interface a moment later, with the share reported as failed
  and its port not recorded. `open_firewall()` opens 5037 and scrcpy's tunnel
  ports (27184–27199) for Domain and Private networks only (the server has no
  password), and `stop_shared_server()` closes them again.
  `~/.turboadb/sharing.json` records the ports this account shared (a tools
  update stops and restarts those servers once) and, in SYSTEM's profile, this
  user's adb keys, which the startup task's server (below) passes to adb as
  `ADB_VENDOR_KEYS`.
- **`install_serve_task()`** registers a SYSTEM startup Scheduled Task so the
  share survives logoff/reboot (more robust than a login-folder launcher); it
  stops whatever answers on the port first, so only the task's own server can
  pass its readiness check. What the task runs, it runs as SYSTEM at every
  start, so none of it may be a file that an account without administrator
  rights can change (`_others_can_change` reads the owner and the access list
  of the file and of every folder above it). An adb that is (the one in
  `~/.turboadb/tools` always) is pinned as a copy under
  `%ProgramFiles%\TurboADB\adb` (`_protected_adb`; installing the task again
  brings the copy up to date), and the install is refused when that folder
  would not be protected either. The interpreter and the package cannot be
  copied: the task is installed with a warning that names them.
  `uninstall_serve_task()` returns False when there is no task and raises when
  the task is there and could not be removed. Deleting it takes administrator
  rights, and schtasks answers for a task this user may not look at as for one
  that does not exist, so `_serve_task_registered` also looks for the
  scheduler's own file of that name. Stop sharing and
  `serve --uninstall-startup` then say that the task shares the devices again
  at the next start, instead of that nothing was installed.
- **`remote_deploy.deploy_serve()`** does the above on *remote* Windows hosts
  **from your machine**, over WinRM. It uses **pywinrm with NTLM transport** —
  explicit `DOMAIN\user` credentials over plain WinRM, no Kerberos/TrustedHosts
  setup needed — then runs `turboadb serve --startup-task` on each host. `serve`
  itself never downloads adb (its startup task runs as SYSTEM, where a download
  would be a second adb), so the script first asks the host's TurboADB whether
  it finds one and runs `turboadb fetch-tools --adb-only` when it finds none,
  except where downloads are turned off or an adb path set on purpose is wrong,
  which `serve` then explains. What `serve` warns about on the host comes back
  as `WARNING:` / `INFO:` lines and reaches the deploy log whole (`_NOTE_MAX`).
  The GUI wraps this in a dialog with a pre-flight Test; the CLI exposes it as
  `turboadb deploy-serve`.
- **In the GUI** a share ends on a warning, not on "can now connect", when the
  firewall's answer says other machines may be kept out
  (`connect_dialog.firewall_warning`: no rule added, a port left out, a Public
  network). A login auto-start that could not be set up is a warning too,
  never a failed share: the server is shared by then. The Connect dialog's
  share runs through `MainWindow.run_share_task(…, log=True)`, which logs what
  the share reports even after that dialog has closed.

## The GUI (`turboadb/gui/`)

PyQt5, one engine call per user action, all blocking work on `QThread`s so the UI
never freezes.

```
app.py            QApplication bootstrap, theme, excepthook, app-user-model-id
  taskbar.py        the taskbar icon for Windows' light/dark mode, live; what a pin starts
main_window.py    ribbon + menu bar, device tabs, sidebar, log dock, split view,
                  upgrade/self-update, shortcuts, share/deploy actions
  status_bar.py     the coloured status bar: state, messages by kind, ADB/devices/version chips
device_tab.py     per-device tab; connects in the background; hosts the sub-panels
  console.py        AnsiConsole — the interactive shell terminal
    vtscreen.py       the VT100/xterm screen for full-screen programs (no Qt)
    screen_view.py    paints that screen over the console's scrollback
    shell_colors.py   what the console colours: words that say what happened, prompts, logcat lines (no Qt)
  terminal.py       reader thread that pumps shell bytes into the console
  local_terminal.py PowerShell / CMD sessions over pipes: environment, input, Ctrl+C, output transcoding
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
    screencap_view.py the ADB screencap renderer, for builds where scrcpy does not run
    display_wall.py   every display side by side (IVI Displays); display_controls.py is a tile's key row
  device_commands.py ordered device commands off the UI thread; which display a key names
  camera_widget.py  the host webcam: local, or a remote PC's over WinRM (remote_webcam.py, ffmpeg_tools.py)
  device_access.py  the write-access dialog (adb root / disable-verity / remount)
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
  lines; a new Filter searches the whole archive. What the reader hands to the
  page stays bounded and cheap while nothing drains it (a hidden page, a
  minimised window): its backlog holds `_LINES_MAX` lines, an emptied batch
  leaves the list, and its count of dropped lines moves to the batch after it
  unless a filter change or Clear has made it stale (`_Capture.live_from`, the
  one mark of that moment, which the reader and the panel both read). The
  terminal's own text (Stop
  and reconnect notices, a submitted line, a screen clear) goes through the same
  queue when output is waiting, so it lands where it happened in the stream.
  A line typed while a command runs is drawn at once (it may be that command's
  answer); if the shell reads it as a command after all, that copy goes when
  the shell echoes it after its prompt. A line typed before a shell's first
  prompt is left to that echo.
- **The edit region.** In `AnsiConsole` the synthetic prompt (Android shell over a
  pipe) and the line being typed are always the last characters of the document,
  and output is inserted above them. Typing while a command streams therefore never
  mixes with its output, and editing the line never touches it. The region's
  lengths (`_prompt_len`, `_line_len`) are document positions, which Qt counts
  in UTF-16 units (an emoji takes two); `_line` and `_cpos` are characters, and
  `_doc_len`, `_char_index` and `_doc_spans` convert (word and prompt colours
  are found in characters and painted in positions). What an input method is
  composing is drawn by the console itself in the edit line at the caret,
  underlined (`_preedit`): Qt's own drawing would put it wherever the caret
  was left in the scrollback. It counts in `_line_len` but is no part of
  `_line`, so Enter never runs it. A plain left press on a selection collapses it,
  so Qt never arms a drag: the view is editable, and a drag would move the
  text out of the document behind the edit region's back.
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
  `cd`s back. That adb-shell mode ends only when PowerShell's or CMD's own
  prompt shows, never on a typed `exit`, which may leave only a shell inside
  it (`su`, `sh`, `run-as`): until then Stop, Tab and typed text keep going to
  the device. For a local command, Stop raises a real Ctrl+C in the shell's
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
  Stop, is replaced. What counts as the command's answer is any output since
  the moment the Ctrl+C was raised. The report of `send_ctrl_c` names that
  moment (`CtrlC.at`, taken just before the event) because the report follows
  the event a moment later, and a REPL has printed its KeyboardInterrupt and
  prompt by then; a command that answered runs on, and only another Stop ends
  it. Stop never types an answer for the user: a shell that runs no program
  and waits for a line itself (`pause`, `set /p`, `Read-Host`, a `-Confirm`
  question, the rest of a `>>` block) notices Ctrl+C only after reading one,
  and Enter would confirm, run the block or let the script go on, so it is
  replaced instead, when its prompt is still not back 3 s after the Ctrl+C. It
  gets as long as a command that ignores Ctrl+C because the prompt of a shell
  that did stop can take a while (a prompt function that asks git, a .NET call
  that has to return first). Only a shell already at its prompt (a background
  job was stopped) is sent an empty line, for a new prompt. Stop, the button
  and Ctrl+C alike, drops the lines of a paste that have not run yet, in all
  three terminals.
- **Local shells over pipes.** cmd and PowerShell run without a console, so
  `local_terminal` stands in for one: `PYTHONUNBUFFERED`, LF-only input written
  by a thread of its own, a `Read-Host` that shows its prompt, and an invisible
  OSC mark (`PROMPT_MARK_RE`) at the end of every prompt (a `prompt` /
  `set prompt=` typed in cmd, or a one-line `function prompt` in PowerShell,
  gets it again). The widget uses that mark (or a default prompt) to tell a
  command from input for a running program: conveniences (`ls`,
  `adb shell -t -t`, streaming `find`/Select-String for a plain `| findstr`,
  PowerShell's buffer width once the view was resized) apply only to commands.
  cmd's `ls` becomes `dir` only when the session's PATH has no `ls` of its own
  (Git for Windows, MSYS2, busybox). `local_terminal.find_command` looks that
  up, and the grep that `gnu_grep` asks for its version, once per session and
  off the UI thread, in the absolute folders of the session's PATH with its
  PATHEXT, never in the current folder (that is TurboADB's own working folder,
  not the shell's, and a `grep.bat` lying there would be run).
  PowerShell keeps the PC's execution policy (its startup script goes in with
  `-Command`). ConPTY, which would give
  findstr, `timeout` and progress output a real console, is not used.
  Over pipes cmd and most console tools write the console code page, while
  PowerShell, git and Python write UTF-8, so `OutputTranscoder` keeps each
  line that is valid UTF-8 and decodes any other with the code page. A read
  may end anywhere in a line: a character it cuts off is held for its rest
  (the start of a UTF-8 sequence when the line is UTF-8 up to there, else
  what the code page's incremental decoder cannot decode yet: the lead byte
  of a double-byte character), for `HOLD_GRACE_S` at most, and the rest of a
  code-page line that was cut inside a character or by a full read is decoded
  with the code page too, whatever it looks like by itself.
  cmd's `cls` writes nothing but a form feed to a pipe, so cmd's
  terminal alone turns on `AnsiConsole.set_form_feed_clears()`; everywhere
  else, and while a device terminal has the output, a form feed is a line
  feed, as on a terminal.
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
  hidden line's mark, also a PS1 that looks like no prompt here). Such lines
  queue (`_early_lines`) and go out one per prompt; a line typed while some
  still wait goes behind them (sent at once it would run ahead of them), the
  console stays in typing-ahead mode until the queue is empty, and Stop clears
  it. Once the
  view is wider or narrower, the next command typed at the prompt carries
  `stty cols/rows` in front (its echo hidden); nothing is typed into a
  running program. The size is
  `AnsiConsole.cell_size`: whole character cells, margins and the scroll bar
  left out whether it shows yet or not. The hidden first line's mark carries
  the terminal's name (`tty`); it leaves `ls` and `grep` as they are, so they
  do not colour their output (see the colour rules below). The reader
  folds adb.exe's CR CR LF into CR LF,
  carrying CRs across reads. A terminal shell that ends or reports an adb error
  before its first prompt (an adb without `-t`, a build without terminals)
  falls back to pipes, where the console draws the prompt; that is undone when
  a plain shell fails at once too (the device was the problem). A device
  without shell_v2 gives a terminal even over pipes: its prompt before any
  command gives that away, and it is set up like one.
- **Colour, Ctrl+C and full-screen programs.** The console draws output as
  lines (horizontal cursor moves included). A line of the view is at most
  `_LINE_MAX` (4,096) characters: Qt lays a line out whole for every insert
  into it and paints it whole, so output that never ends its line (minified
  JSON, base64) continues on the next line of the view (`_out_cut`), as a
  terminal wraps at its width. A cut line's block is marked (`_CUT`), Copy
  joins such lines again (`_selected_text`), and the saved history is
  untouched. Whole lines that need no format of their own are gathered and go
  in with what follows them as ONE insert (an insert per line makes a flood of
  short lines many times as slow), and a slice of the queue is cut at
  `_SLICE_LINES` lines as well as at `_SUB` characters. Moves and overwrites
  along a line work in document positions and never split a character
  (`_char_edge`). A tab moves to the next multiple of 8 columns, counting only
  the text since the tab before it. Colour it adds is kept for what
  matters; output with colours of its own keeps them. An owner that
  recognises its shell's prompt calls `mark_prompt()` after feeding the
  output, and the console colours that prompt (`shell_colors.prompt_spans`)
  unless it has colours of its own, in one quiet colour
  (`theme.TERM_PROMPT_COLORS`, which the prompts the console styles itself
  use too), red only for a failed command's status and root's `#`. A local
  shell's own prompt (PowerShell's, cmd's) is styled as it arrives in the
  stream (`_style_local_prompts_stream`), but only where the read ends with
  it or the terminal's OSC mark (or the first byte of that mark) follows it,
  and never on a device terminal: an output line that merely starts like a
  prompt has text after it. A line
  that was plain text from start to end and is a logcat line
  (`shell_colors.LogcatLines`) takes its priority's colour when it is a
  warning, error or fatal one (`COLOURED_LEVELS`), even when it came in
  several reads; the rest stay plain. In any other plain line, bar a prompt
  and the command after it, only the words `shell_colors.meaning_spans` finds
  are coloured (`theme.TERM_MEANING_COLORS`): errors, denials and blocks red,
  warnings amber, successes green. They are words of their own (not part of
  a path, a file name, an identifier or a `key=`), matched through a letter
  tree (`_trie`); `may_mean` rules most lines out before that, and a line's
  text and its words' formats go in as one edit, laid out once, so floods
  stay fast. The undrawn backlog is capped at about a
  second of drawing (`_backlog_cap`, from the measured rate). That rate
  (`_render_rate`) is taken from every tick that ran out of time with output
  still waiting, however little it drew (short lines are slow to draw, and
  slow drawing must be measured too), or that drew 32 KB; once everything is
  drawn, a rate below the first guess goes back to it. Ctrl+C on a
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
  started with (`top` asks the screen for it). An escape sequence that a read
  cut off waits for the next read, in the console's saved history as on a
  program's screen. It is looked for only in the last 4,096 characters and
  only after the last string terminator there, and what follows the last ST
  of a read is tokenized without the DCS/SOS/PM/APC string form
  (`vtscreen._TOKEN_AFTER_ST`), since no string can end there: a read full of
  unfinished string starts costs no more than its length.

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
what the device holds for a push. A Windows folder lists each file with the
size it had when it was last closed (0 bytes for one adb still writes), so
there the watch asks every file that is listed differently from its snapshot
for its own size. Measuring never takes more than a fifth of the time, and
with a stall limit set the longest pause between two looks is a quarter of
it. `transfer_timeout` is that stall limit, not a deadline, and a pull that is
ended early removes the one file it created. `pulled()`, the files that are
new or changed against the snapshot taken before adb started, is also what a
pull's result counts: what it copied, not what its folder held before. A copy
that failed reports adb's own `error:` lines (the first five), which come
before its "1 file pushed" summary.

Android takes file names that Windows does not: with a `:` in the name NTFS
writes into a hidden stream of another, empty file and reports no error, a
trailing dot or space is dropped, and `nul` is a device. On Windows the engine
therefore picks the name a pulled file is stored under itself (`_pull_target`).
A name taken from the device is made one Windows can store
(`_windows_file_name`). A destination that was typed is read as Windows reads
it (`_typed_target`: its trailing dots and spaces go, so the path as typed
goes on naming the copy), and only the characters Windows forbids are
replaced. adb is given that path, a WARNING says so where a name was
replaced, and whatever looks at the copy afterwards (the transfer's result, a
recording's `_save_clip`, `edit_file`) uses the name it was stored under.

`replace_file` (the save path of `edit_file` and of the Files tab's editor and
Open) pushes the new content under a temporary name and writes it over the
file on the device (`cat` into it), so the file keeps its owner, mode and
SELinux label; the write counts only when the device's last line is its mark
(`remotefs.WRITTEN_MARK`: a device before Android 7 gives no exit status).
Writing over a file empties it first, so the device is asked before whether
its file system has room, and without it the save is refused with the file
as it was. If the write is cut short all the same (the file's
size-and-time stamp changed, or the device no longer answers), the pushed
copy stays beside the file and the error names it; the next save of that file
that works removes it.

A recording aimed at a folder (one that exists, or a path ending in a
separator) is given a file of its own there, named after the time
(`_recording_file`): into a folder adb would pull the clip under its device
name, which is not the path that is checked and returned afterwards. The
name is taken at once, as an empty file that holds it until the clip is
recorded (or the program exits), so two recordings started in the same second
each get a file.
`bugreport` likewise returns the file that was saved: its path with `.zip`
added as adb adds it, or in a folder the file adb names.
`screen_record_continuous` records the next part while the last one is pulled,
so the pulls leave no gap. The recorder and transfer children run in a session
of their own on Linux and macOS, so a Ctrl+C in the terminal reaches Python
alone and the CLI can still stop the recording cleanly; children still running
when the interpreter exits are ended then, with their clean-up. Every child the
engine, scrcpy and the GUI's streams start is stopped through
`proctree.stop_process`.

### Shutdown

The window goes first. `MainWindow.closeEvent` detaches and parks its workers
(`_release_workers`), closes every tab's session and then calls
`mirror_panel.finish_recordings(window)`: while the window, the device
connections and the adb server are still there, a modal dialog names the
recordings that are still being stopped and saved and waits for their workers,
each within its own limits (scrcpy's close timeout, the engine's stall limit
for a pull). Stopping the server under the pull of a device recording would
lose the recording, and a scrcpy still writing its MP4 index would get the
exit's few seconds instead of its own. Only the dialog's button, **Close
without saving**, gives
up the wait (Esc and a close request do not); a recording that turned out not
to be saved is reported before the app goes. A "Run a command on ALL devices"
that is still running is called off as the workers are released
(`_BroadcastThread.stop()` sets the cancel event that each per-device
`shell(cancel=…)` carries, and the engine kills that adb at once): each
command may run for a minute, and the process does not exit before the last
of them has ended.

A closed tab, like a display tile taken off its wall, is destroyed through
`mirror_panel.delete_when_released`: it is only hidden until no screen panel
in it reports `still_closing()` (see Screens, recordings and the webcam).
Destroying a panel destroys the scrcpy window embedded in it, which waits for
as long as a hung scrcpy does not answer, and a panel whose recording is still
being saved is the one that reports the result.

`gui/app.py::_stop_started_tools` runs when the app exits. It first waits
(3 s at most, `local_terminal.join_closers`) for the PowerShell / CMD terminals
the closing tabs ended: their process-tree kills run on daemon threads, which
died with the app before the kill landed and left the shell and its commands
running. A shell that had ended by itself (`exit`) may have left commands
running on its hidden console (`start /b ping -t`); closing its terminal ends
those too (`proctree.kill_tree(pid, include_root=False)`, on Windows, where
the open process handle keeps the PID the shell's own). Then `scrcpy.stop_all()`
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
until that launcher ends without adb's "daemon started successfully": it found
a server there after all. A launcher keeps what its credit took the place of
(`_Launch.replaced`), and the record then goes back to that: a server this
process had started before, which was only slow to answer when the second
launcher ran, stays TurboADB's to stop (as far as the launcher before it said
so: that one is asked next), and any other is another program's, started in
the same moment, and is left alone.
`tools.kill_adb_server` is the only stopper (restart, "Stop sharing", tools
upgrades, exit): it waits until the port is released and forgets the record.
The default port follows `ANDROID_ADB_SERVER_PORT` (`tools.local_adb_port`), as
adb's own commands do, so the socket probes, the tracker and the start agree.
After startup the GUI compares the server's `host:version` protocol (and
`host:server-status` executable) with its own adb and warns about a server that
another adb would keep restarting.

Whatever stops the server holds the device tabs on it first
(`DeviceTab.prepare_for_adb_restart`, see The device tab): a restart, a share
or its undoing, a tools download or upgrade, a self-update. A plain adb
command that finds no server starts one itself, which TurboADB would not have
recorded (and, during a tools update, from the folder being replaced). **Check
for updates** therefore checks adb and scrcpy first, on a worker
(`_tools_check_worker`), and leaves the tabs alone: an answer of "up to date"
must not cost every tab its shell. The tabs are held only when the upgrade
that follows replaces a tool, and that upgrade reuses the check. The check
counts as a tool task until its answer is in, so a download asked for
meanwhile is turned away like any second tool task.

On a first launch without adb, where adb is downloaded by itself (auto-fetch
on, and a build exists for the machine), the window skips the startup check
that could only fail and waits in the state "waiting for the download of adb"
(`_adb_awaits_download`). The server starts once the download is in; a
download that fails, or never starts, still ends in the startup check, which
says what is wrong.

The device list (`devices.list_devices`) asks a running server over its
socket, which takes no adb. When it has to start the server and there is no
adb to start it with, `ADBNotFoundError` goes out: a missing adb is never an
empty device list. With `strict=True` (the GUI and the CLI) a server that
cannot start is a `ConnectionError` with the launcher's own words
(`last_adb_server_error()`).

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
(`_release_terminals` brings them back, resumes a Logcat capture, lists a built
Files page's folder again and loads a built Phone page again), and a
Terminal opened for the first time meanwhile opens after the release.

### The device tab

`DeviceTab` connects on a worker and asks the device everything a new tab
needs in ONE `adb shell` (`_DeviceProbe`: prompt, build properties, feature
list, `wm size`, displays). The tab opens on the identity sections; the rest
follows from the same process. A probe that brought no profile, or one without
the feature list, runs once more a moment later (`PROBE_RETRY_MS`): a device
that is still starting answers adbd but lists no features, and classifies as a
phone whatever it is. A profile without features is kept meanwhile and applied
when the repeat brings none, since a head unit says "automotive" in its build
characteristics either way.

A network device that a tab's own `adb connect` connected is disconnected
again when the last tab on it closes (`_Connections`, the engine's
`owns_connection`). A connect that failed lets go of its handler through
`_release_connection` too: `adb connect` may have made the connection before
the connect failed (a device that never authorised this PC), a retry is then
answered "already connected" and owns nothing, so the first handler remains
the one to drop it with.

**The ADB-restart hold.** `prepare_for_adb_restart()` suspends everything in
the tab that runs adb by itself: the shells, a Logcat capture, a running wait
for the device (`_ReconnectThread.cancel()`: no adb command starts after it,
and nothing is reported) and a built Phone page. A wait asked for during the
hold is deferred. `finish_adb_restart(True)` starts the one recovery, for at
least the time an ended wait still had; `finish_adb_restart(False)` offers
Reconnect and releases the Phone page without loading it.

**The Phone page** has `suspend_for_device_loss()` /
`resume_after_reconnect(reload=True)`, named as on the Logcat page. While
held, its timers, a show, Refresh and the continuations of running jobs ask
nothing; a Refresh or a first show during the hold runs when it ends. A
reconnect and the end of an adbd restart load a built page again (a hidden
one when it is next shown). The call-state poll calls the engine in raw mode,
so a poll that fails is not logged as an error each time, and only a real
answer stops it: a device without a call state, a missing telephony service
or a permission denial show "No telephony", while a query that adb or the
device did not answer shows "Checking…" and is asked again. A support check
that failed, or that the device answered with nothing known, is not kept as
the answer either: Refresh, the next show and a reconnect ask again. End asks
the call state once more while the device is known to be idle, and sends the
End-call key only for a ringing or active call: with no call Android takes
that key for its power button.

**Which display a key names** is decided in one place,
`device_commands.input_display(display_id, displays, sdk)`. Android sends a
key without a display to whichever display has the focus, so on a device with
several displays, display 0 is named like any other where `input` takes a
display (API level 29 or later, and known); a device with one display, an
older Android or an unknown version keeps the plain command. Every key route
asks it: a tile's key row (`DisplayControls`), a screen's own keyboard route
and Device Control's provider (`MirrorPanel.key_display_id()`; `_KeyBatcher`
sends what its owner names) and the screencap view
(`ScreencapView.key_display_id()`). `DeviceTab._note_sdk` forwards the API
level to the screen panel and the display wall, and
`DisplayWall._sync_key_displays` tells each tile how many displays there are.
Taps keep the plain command for display 0: a tap without a display goes
there.

### Files

**Names that are never acted on.** A table's `unsafe_names` are left alone: on
the device side, names the listing could not identify exactly; on the PC side
(Windows), names that end in a dot or a space (`_inexact_local_name`), which
WSL, Git Bash and NTFS-3G make. Windows drops those characters from a path,
so a path to `photos.` reaches `photos`. Every row action refuses such a row
with a message (Delete, Rename, Edit, Open, entering the folder, Push, Copy,
a drag, a drop onto such a folder), and a path of that kind dropped from
outside is neither pushed nor copied.

**Device changes are judged by a mark.** A device before Android 7 gives adb
no exit status: `adb shell` exits 0 whatever the command did. The commands
the tab sends itself (New folder, New file, Paste, Rename) therefore go out
with the same mark as the engine's file commands (see The engine; the tab
keeps its own `_marked` / `_marked_done`) and count as done only when that
mark is the last line of the output. Delete is the engine's
(`ADBHandler.remove`, which refuses `/` and checks every path first); the tab
looks afterwards whether the items are gone (`_check_removed`), and its retry
after a refusal leaves out what the first try did remove, because the engine
deletes nothing while one of its paths is missing.

Storage that ignores case (/sdcard) answers for one item under both spellings
of its name. A rename to the same name in other letters therefore goes
through a temporary name in two steps (`remotefs._rename_cmd`), and the
engine's `move` and `copy` ask the device whether the folder found at the
destination is the source itself (`_in_other_letters`): `move` then renames
it instead of moving it into itself, and `copy` refuses.

**Rows and dialogs.** Events keep running while a dialog is open, and a
write-access retry may open another folder meanwhile. Device Delete and
Rename therefore take the folder together with the names before their
dialog, build their paths from it, and are dropped with a line in the log
when the pane shows another folder afterwards (`_remote_pane_left`).

**Saving.** The built-in editor notes a device file's `(size, modified)`
before the pull and after each save, as files opened in their apps do
(`_device_stamp`), compares it before saving and asks before it replaces a
file that something else changed. A PC file is saved through a temporary
file beside it, which then takes its place: on Windows with `ReplaceFileW`
(`_put_in_place`), which hands the old file's other data streams (the mark of
a downloaded file), its own permissions and its creation time to the new
one. A drive that cannot swap gets the plain rename, and a file that is in
use is written in place.

**Local copies** compare source and destination by where they really are
(`os.path.realpath`: junctions, links, `subst` and mapped drives resolved),
so a folder is never copied into itself through another name for it, and the
`copytree` hook (`_loop_guard`) leaves out, and reports, a link that leads
back into the folder being copied or to the folder the copy goes into.
`transfer_log.measure_remote` writes each path once (in a shell variable)
and cuts its commands at 100 paths or 24,000 characters, counted as a
Windows command line holds them: Windows starts no command line longer than
32,767 characters, and past that no size is measured at all.

### Screens, recordings and the webcam

`MirrorPanel` shows one display: scrcpy embedded or in its own window, or ADB
screencap frames (`ScreencapView`). Starting is a replace operation: one
requested start waits while a launch is cancelled or the previous session
closes. `_screen_wanted()` says whether a screen shows or is on its way (not
after Stop) and `_screen_display()` for which display. A display picked
meanwhile is therefore the one that starts, the picker stays with the display
that is or will be on screen, and a change of renderer leaves a start that
waits alone (it picks its renderer when it begins) and brings no screen back
after Stop.

A recording runs in one of three modes: `integrated` (the one scrcpy that
shows the screen records it; Record pressed while a screen is starting or
closing), `parallel` (a second, windowless scrcpy beside a live screen, so
the view never restarts) and `device` (`screenrecord` on the device, pulled
afterwards: where no scrcpy screen runs, and the fallback of a parallel
recorder that writes nothing). While scrcpy itself records or is about to,
`start()` refuses every other start: the start that carries the recording may
still be waiting, and another would take its place. `_begin_record` notes the
size and time of a file already at the chosen path (`_rec_overwrites`), and
`_recording_written()` takes a file for the recording only once it holds data
and is not that old file unchanged; it confirms a parallel recorder and
decides what is reported as saved.

`still_closing()` is true while a panel is still needed after
`close_panel()`: a session starting or closing, the scrcpy of an integrated
recording, the workers that stop and save a recording, a recorder still
launching. `delete_when_released(widget, released=None)` (a closed tab; a
tile the display wall removes) hides the widget and deletes it once no panel
in it says so, looking again on every `screen_stopped` and on a 250 ms poll.
`finish_recordings(window)` is the exit's wait (see Shutdown). A separate
scrcpy window that does not answer is restored with `ShowWindowAsync`, never
waited for.

The host webcam (`camera_widget.py`) reads a remote PC's camera from an
ffmpeg that listens on a TCP port there (`remote_webcam.py`). Each stream
claims a port of its own (`claim_stream_port()`: 28100–28115, tracked per
process and not per host, since one PC may be reached by several names) and
gives it back only after its remote teardown. A stream's ffmpeg is found on
the remote by the port it listens on, and its firewall rule is named after
it: on one shared port two views of the same PC would end each other's
stream.
Recording encodes on this PC, so Record fetches the local ffmpeg when there
is none (`_FfmpegFetch`); a remote camera is watched without one.

### Logcat, the log panel and the settings files

Python's matcher never releases the GIL, so a Filter or Highlight pattern
that backtracks catastrophically freezes the window whichever thread runs it.
`logcat_view.compile_filter` therefore matches as plain text whatever it
cannot trust, and says why: text that is no valid regular expression, nested
repeats, a repeat whose alternatives can match the same text (`_Overlaps`),
and a pattern that is too slow in a trial (`_too_slow`). The trial searches
three probe lines at growing lengths up to 512 characters, each within 10 ms,
and tries one start inside a run of one character at growing lengths up to
4,096, each within 4 ms: for ten common characters and for every character
the pattern names (64 at most), as the end of a line and with more after the
run. Repeats that can share such a run (`\s*\s*x`, `/+\s*/+x`) search an
ordinary line in no time, and a line with a few thousand of that character
for minutes.

The log panel keeps its record per level (`log_panel._Record`: the newest
20,000 lines of each), and its view holds no more than 20,000 lines, so
whatever "Show" is set to, a line on screen is in the record that Save and a
change of "Show" work from. In one list for all levels the hidden adb trace,
far more frequent than anything else, would push out errors that are still on
screen.

`settings.json` and `sessions.json` (and a targets file to import) are read
as `utf-8-sig`: a file edited by hand may start with a byte order mark
(PowerShell 5.1's `-Encoding UTF8`, an editor's "UTF-8 with BOM"), and read
as plain UTF-8 such a file does not parse, which leaves the defaults in its
place.

## TurboADB on Windows: its name, its taskbar icon, its pins

Windows names a process after its program file, and the GUI ran in
`pythonw.exe`: Task Manager listed "Python" with Python's icon, and a taskbar
pin of the window started a bare `pythonw`. Three parts fix that.

- **TurboADB.exe** (`launcher.py`). `turboadb-gui`, `turboadb gui` and
  `python main.py` start the GUI through `launcher.relaunch()`, which runs it in
  `~/.turboadb/launcher/<environment>/bin/TurboADB.exe`: a copy of this Python's
  `pythonw.exe` whose icon and version resources (Task Manager's name is the
  `FileDescription`) are replaced with `UpdateResourceW`, beside copies of the
  Python DLLs and a `pyvenv.cfg` pointing at the Python home — the layout of a
  virtualenv made with copies. `TurboADB.exe -m turboadb_launch` runs a
  generated module of the copy's own site folder, which puts the originating
  `sys.path` back (a source checkout first, as `python main.py` has it), runs
  the `.pth` files of its site folders (editable installs) and sets
  `sys.executable` back to the real interpreter: pip, the update checks and
  every helper process run the real Python, exactly as before. The copy is
  rebuilt when Python, TurboADB or the icon change and checked once by starting
  it (`--probe`); a copy that fails is not retried until something changes, and
  any failure runs the GUI in-process as before. The launching process waits
  1.5 s (a GUI that ends at once runs in-process instead) and ends; inside a job
  that allows no breakaway (an IDE) it stays with the GUI. `TURBOADB_NO_LAUNCHER=1`
  turns it off; the test suite sets it. The one-file exe is TurboADB already.
  `state.json` beside the copy records what it was built of (`built`: the stamp
  of Python's files, TurboADB's version and the icon, and a hash of the
  generated files), whether its check passed (`ok`) and what it was made for
  (`prefix`, the environment, and `root`, the folder holding TurboADB).
  `_forget_orphans` removes the copies whose Python,
  environment or TurboADB folder is gone (a deleted venv, a removed checkout);
  a folder on a drive that is not connected says nothing, and a copy without
  that record is judged by what its generated script names.
  A shortcut or a pin starts TurboADB.exe itself, with nothing before it to
  look at the copy. So unless `relaunch()` started it (which says so with
  `TURBOADB_LAUNCHER_CHECKED`), `run_branded` checks the copy first
  (`_stale`): the launcher turned off, a failed check, a stamp that differs
  from this Python's and TurboADB's (a `pip install -U` or a Python update
  since), or no PyQt5. A stale copy hands the start over (`_hand_over`): it
  runs `turboadb.cli.launch_gui` in the real interpreter and ends, so the GUI
  comes up in a copy built anew, in plain Python when the launcher is turned
  off, or as the wheel's executable when PyQt5 is missing. If the hand-over
  cannot start, the GUI runs in the copy after all.
- **The window's identity** (`winshell.set_window_identity`, from
  `gui/taskbar.py`). The main window carries the app ID and what a pin shows
  and runs: `RelaunchCommand` (what the shortcuts start, see below), the name
  and the icon. Windows keeps 255 characters of each, so the command
  is a module name rather than a script path, with an 8.3 fallback.
- **The icon follows the taskbar's mode.** `scripts/make_icon.py` draws the
  dark and a light variant; `winshell.icon_file()` keeps lasting copies in
  `~/.turboadb/icons` (shortcuts must not point into a pip-upgraded package or
  the one-file exe's temp folder). A native event filter catches
  `WM_SETTINGCHANGE` "ImmersiveColorSet"; after a short pause the window icon
  follows `SystemUsesLightTheme`, and the icons of TurboADB's pins, Start-menu
  and Desktop shortcuts follow in a worker thread (their `.lnk` files, through
  `IShellLinkW`, which keeps their app ID). Shortcuts are written through COM
  with the app ID (so a pin made of one groups with the running window), and an
  out-of-date one is rewritten once, never on every launch. The shell is only
  touched on the real `windows` platform, never from the tests' offscreen
  windows.

What a shortcut or a pin starts is decided in `cli._resolve_gui_target`: the
one-file exe itself (or the `turboadb-gui` that started it, below); with
PyQt5, this environment's TurboADB.exe; otherwise the `turboadb-gui`
launcher beside the interpreter, else `pythonw -m turboadb gui`. TurboADB.exe
runs the GUI of this Python, so without PyQt5 it would open nothing. Where
nothing starts the GUI at all (no PyQt5 and no bundled exe) no shortcut is
written (`_no_gui`). The PowerShell fallback that writes a shortcut where COM
fails holds no value in its script: name, target, arguments, folder and icon
travel in `TURBOADB_LNK_*` environment variables, because a path with an
apostrophe or a typographic quote would end a quoted string in the script.

Without PyQt5 on Windows, `turboadb-gui` runs the executable bundled in the
wheel from a copy in `~/.turboadb/gui` (`cli._staged_exe`): a running exe is
locked, so running the packaged one in place would block
`pip install --upgrade`, and the folder is the user's own, where no other
account can leave a file under the copy's name. The name carries version and
size; a copy is run again only while it equals the packaged exe byte for
byte, a new one is written under a temporary name and renamed, and the copies
of other builds are deleted. `launch_gui` tells that exe what started it
(`TURBOADB_GUI_STARTED_BY`), so its shortcuts, their upkeep and its pin's
`RelaunchCommand` name `turboadb-gui` and not the copy, which the next
upgrade replaces. A shortcut or a pin that starts a copy of the wheel's exe
all the same is pointed at what starts TurboADB, at every start and on a
mode change (`winshell.revive_shortcuts`, through `retarget_shortcut`, which
keeps its app ID and icon). `cli._revive_shortcuts` adds the Desktop and
Start-menu shortcuts to the pins whether or not shortcut upkeep is on, and
creates none. `winshell.pinned_shortcuts()` covers `User Pinned\TaskBar` and
`User Pinned\ImplicitAppShortcuts\*`, where Windows keeps the pin of a window
whose app ID no Start-menu shortcut has.

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
  not installed, from a copy in `~/.turboadb/gui` (see above).

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
├── launcher/         Windows: one TurboADB.exe per Python environment (launcher.py)
├── gui/              Windows: the copy of the wheel's exe that turboadb-gui runs without PyQt5
├── icons/            the dark and light icons that shortcuts and pins point at
└── crash.log         uncaught GUI errors
```

All of these resolve through `config.user_dir()` / `user_path()`, which re-read
`HOME`/`USERPROFILE` on every call.

Two folders are outside it on purpose, on Windows under
`%ProgramFiles%\TurboADB\`, where only administrators can write: `adb\`, the
copy of adb that the SYSTEM startup task runs (`devices._protected_adb`), and,
on a remote PC, `ffmpeg\`, the remote webcam's ffmpeg (`gui/remote_webcam.py`).
The first runs as SYSTEM and the second with an administrator's rights, so
neither is taken from a folder another account could have filled first.
