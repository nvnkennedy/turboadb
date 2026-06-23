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
├── cli.py             argparse front-end; console-script entry points
├── assets/            icon.ico / icon.png
├── bin/               bundled turboadb-gui.exe (shipped in the wheel)
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
`ANDROID_ADB_SERVER_ADDRESS` wants a **bare** host (it wraps it itself) is handled
here.

## Sharing devices: serve & deploy

- **`devices.start_shared_server()`** runs `adb -a … server start` detached, so a
  machine can share the devices attached to it. `open_firewall()` opens 5037 +
  27184. `install_serve_task()` registers a SYSTEM startup Scheduled Task so it
  survives logoff/reboot (more robust than a login-folder launcher).
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
device_tab.py     per-device tab; connects in the background; hosts the sub-panels
  console.py        AnsiConsole — the interactive shell terminal
  terminal.py       reader thread that pumps shell bytes into the console
  logcat_view.py    live logcat with filtering + complete-save
  file_browser.py   device filesystem tree + push/pull
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

Two patterns recur and are worth preserving:

- **Decoupled ingestion.** The shell and logcat never process incoming bytes on
  the receiving path. They enqueue + archive (both O(1)) and a timer renders a
  *bounded* slice per tick. That's what keeps the UI responsive under a million
  lines of output, and why a Save is always complete even if the on-screen buffer
  was trimmed (see `scrollback.py`).
- **Reliable stop.** With no PTY, `Ctrl+C` can't signal the device. The Stop path
  tears down the shell (killing the device-side process group), discards stale
  in-flight data via a current-reader guard, and reopens — preserving the cwd.

## Packaging

- `pyproject.toml` — package metadata, the console-script/gui-script entry
  points, optional extras (`gui`, `winrm`, `all`), and `package-data` that ships
  the bundled exe + assets inside the wheel.
- `turboadb-gui.spec` + `scripts/build_exe.py` — PyInstaller build of the
  one-file GUI exe. `collect_all` pulls the entire pywinrm/NTLM stack so WinRM
  works in the frozen exe. `scripts/gui_entry.py` is the frozen entry point (with
  a startup self-test hook).
- `scripts/release.py` — build wheel+sdist and upload to PyPI.

## Where things live at runtime

```
~/.turboadb/
├── tools/            downloaded adb + scrcpy
├── settings.json     theme, fonts, defaults
├── sessions.json     saved targets
├── logs/             temp scrollback archives (cleaned on close)
└── crash.log         uncaught GUI errors
```
