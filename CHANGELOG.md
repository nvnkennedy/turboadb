# Changelog

All notable changes are recorded here. Versions follow
[semantic versioning](https://semver.org/).

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
