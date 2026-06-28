"""Remote webcam over WinRM — view a camera on another Windows / RDP machine
without SSH.

TurboADB has no SSH transport, so (unlike TurboSSH, which pushes ffmpeg over SFTP)
the remote camera is reached with the **same WinRM/NTLM stack** used by
``deploy-serve``, and ffmpeg is provisioned on the remote like this:

  1. Locate ffmpeg on the remote (PATH / our caches). If found, use it.
  2. Otherwise **push the local ffmpeg.exe to the remote over the admin share**
     (``\\host\\C$``) — fast on a LAN and works even when the remote has no
     internet. This is the WinRM-friendly equivalent of TurboSSH's SFTP push.
  3. If the admin share isn't reachable, fall back to having the **remote download
     ffmpeg itself** from the public BtbN build.
  4. WinRM then runs ffmpeg's ``-list_devices`` to enumerate cameras, and launches
     ffmpeg serving MJPEG on a listening TCP socket (``tcp://0.0.0.0:PORT?listen=1``)
     with the firewall port opened; the GUI reads that socket directly.

Prereqs on the remote: WinRM on (``Enable-PSRemoting -Force``) and the account a
local admin. A physical USB camera works headlessly; a camera redirected into
someone's RDP session is only visible inside that session.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from ..remote_deploy import _ensure_winrm, _session

DEFAULT_STREAM_PORT = 28100
_FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
               "ffmpeg-master-latest-win64-gpl.zip")
# where the pushed copy lives on the remote (admin-accessible, no profile guessing)
_PUSH_DIR = r"C:\Windows\Temp\turboadb-ffmpeg"
_PUSH_EXE = _PUSH_DIR + r"\ffmpeg.exe"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Find ffmpeg on the remote: PATH, the pushed location, then the turboadb/turbossh caches.
_LOCATE = r"""
$ff = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ff) {
  foreach ($p in @("C:\Windows\Temp\turboadb-ffmpeg\ffmpeg.exe",
                   "$env:USERPROFILE\.turboadb\ffmpeg\ffmpeg.exe",
                   "$env:USERPROFILE\.turbossh\ffmpeg\ffmpeg.exe",
                   "C:\ffmpeg\bin\ffmpeg.exe")) {
    if (Test-Path $p) { $ff = $p; break }
  }
}
if ($ff) { 'FFMPEG:' + $ff } else { 'NOFFMPEG:' }
"""

# Remote self-download (fallback when the admin share isn't reachable).
_REMOTE_DOWNLOAD = r"""
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
$dir = 'C:\Windows\Temp\turboadb-ffmpeg'; $exe = Join-Path $dir 'ffmpeg.exe'
if (Test-Path $exe) { 'FFMPEG:' + $exe } else {
  try {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $zip = Join-Path $dir 'ffmpeg.zip'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    (New-Object Net.WebClient).DownloadFile('__URL__', $zip)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($zip)
    $e = $z.Entries | Where-Object { $_.FullName -like '*/bin/ffmpeg.exe' } | Select-Object -First 1
    if ($e) { [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $exe, $true) }
    $z.Dispose(); Remove-Item $zip -Force -ErrorAction SilentlyContinue
    if (Test-Path $exe) { 'FFMPEG:' + $exe } else { 'NOFFMPEG:extract failed' }
  } catch { 'NOFFMPEG:' + $_.Exception.Message }
}
"""


def _run_ps(host, login, password, script, winrm_port=5985):
    r = _session(host, login, password, winrm_port=winrm_port).run_ps(script)
    out = (r.std_out or b"").decode("utf-8", "replace")
    err = (r.std_err or b"").decode("utf-8", "replace")
    return r.status_code, out, err


def _parse_ffmpeg(out):
    for line in out.splitlines():
        if line.startswith("FFMPEG:"):
            return line[len("FFMPEG:"):].strip()
    return ""


def _smb_push(host, login, password, local_ffmpeg, log=None):
    """Copy the local ffmpeg.exe to the remote over its admin share (``\\host\\C$``)
    using the WinRM credentials. Fast on a LAN; needs SMB (445) + the admin share."""
    if os.name != "nt":
        raise RuntimeError("admin-share copy is Windows-only")
    share = rf"\\{host}\C$"
    remote_dir = rf"{share}\Windows\Temp\turboadb-ffmpeg"
    remote_exe = rf"{remote_dir}\ffmpeg.exe"
    # authenticate to the share with the remote creds (clear any stale mapping first)
    subprocess.run(["net", "use", share, "/delete", "/y"],
                   capture_output=True, creationflags=_NO_WINDOW)
    r = subprocess.run(["net", "use", share, f"/user:{login}", password],
                       capture_output=True, text=True, timeout=40,
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "net use failed").strip()[:200])
    try:
        os.makedirs(remote_dir, exist_ok=True)
        if log:
            log("Copying ffmpeg to the remote over the admin share (fast)…")
        shutil.copyfile(local_ffmpeg, remote_exe)
    finally:
        subprocess.run(["net", "use", share, "/delete", "/y"],
                       capture_output=True, creationflags=_NO_WINDOW)
    return _PUSH_EXE


def _remote_download(host, login, password, winrm_port, log=None):
    if log:
        log("Downloading ffmpeg on the remote (one-time, ~160 MB — needs internet "
            "there)…")
    code, out, err = _run_ps(host, login, password,
                             _REMOTE_DOWNLOAD.replace("__URL__", _FFMPEG_URL),
                             winrm_port)
    ff = _parse_ffmpeg(out)
    if ff:
        return ff
    reason = ""
    for line in out.splitlines():
        if line.startswith("NOFFMPEG:"):
            reason = line[len("NOFFMPEG:"):].strip()
    raise RuntimeError(reason or (err or out)[:200] or "no output")


def ensure_remote_ffmpeg(host, login, password, *, winrm_port=5985, log=None):
    """Make sure ffmpeg is on the remote, provisioning it if needed. Returns the
    remote ffmpeg path; raises RuntimeError with the real reason if it can't."""
    if not _ensure_winrm():
        raise RuntimeError("pywinrm isn't available (pip install pywinrm).")
    if log:
        log("Checking ffmpeg on the remote machine…")
    code, out, err = _run_ps(host, login, password, _LOCATE, winrm_port)
    found = _parse_ffmpeg(out)
    if found:
        return found

    # not there — push our LOCAL copy (like TurboSSH), fetching it locally first
    from .ffmpeg_tools import cached_ffmpeg, ensure_local_ffmpeg
    local = cached_ffmpeg()
    if not local:
        if log:
            log("Fetching ffmpeg locally first (one-time)…")
        local = ensure_local_ffmpeg(log or (lambda m: None))

    smb_err = ""
    try:
        return _smb_push(host, login, password, local, log=log)
    except Exception as exc:
        smb_err = str(exc)
        if log:
            log(f"Admin-share copy didn't work ({smb_err}); trying a download on the "
                f"remote instead…")
    try:
        return _remote_download(host, login, password, winrm_port, log=log)
    except Exception as dl_exc:
        raise RuntimeError(
            "couldn't get ffmpeg onto the remote machine.\n\n"
            f"• Admin-share copy (\\\\{host}\\C$) failed: {smb_err or 'n/a'}\n"
            f"• Remote download failed: {dl_exc}\n\n"
            "Easiest fix: paste ffmpeg.exe into C:\\Windows\\Temp\\turboadb-ffmpeg\\ "
            "on the remote over RDP, then Scan again.")


def list_remote_cameras(host, login, password, *, winrm_port=5985, log=None):
    """Return ``(cameras, ffmpeg_path, diag)`` for the remote host, provisioning
    ffmpeg there first if it's missing. *diag* holds ffmpeg's raw device listing."""
    from .ffmpeg_tools import parse_dshow_devices
    ffmpeg = ensure_remote_ffmpeg(host, login, password, winrm_port=winrm_port, log=log)
    if log:
        log("Listing cameras on the remote machine…")
    script = (f"$ff = '{ffmpeg}'\n"
              f"$o = (& $ff -hide_banner -list_devices true -f dshow -i dummy "
              f"2>&1 | Out-String)\n$o\n")
    code, out, err = _run_ps(host, login, password, script, winrm_port)
    cams = parse_dshow_devices(out)
    return cams, ffmpeg, out.strip()


def _kill_by_port_ps(stream_port):
    """PS that kills any ffmpeg whose command line mentions our stream port — a
    stale one (from a killed WinRM call) holds the camera and the port."""
    return (f"Get-CimInstance Win32_Process | Where-Object {{ $_.Name -eq "
            f"'ffmpeg.exe' -and $_.CommandLine -like '*{int(stream_port)}*' }} | "
            f"ForEach-Object {{ try {{ Stop-Process -Id $_.ProcessId -Force }} "
            f"catch {{}} }}\n")


def start_remote_stream(host, login, password, camera, ffmpeg, *, width=1280,
                        height=720, fps=25, stream_port=DEFAULT_STREAM_PORT,
                        winrm_port=5985):
    """Launch ffmpeg on the remote serving MJPEG on ``stream_port`` and return its
    PID. ffmpeg is spawned **detached via WMI** (``Win32_Process.Create``) so it
    survives this WinRM call returning — a ``Start-Process`` child is killed the
    instant the WinRM shell closes, so it never starts listening (connection
    refused). ``listen_timeout`` lets it wait for our connection, and any stale
    ffmpeg on the port is killed first (it would hold the camera)."""
    cam = (camera or "").replace('"', "")
    rule = f"TurboADB Webcam {stream_port}"
    cmd_line = (f'"{ffmpeg}" -hide_banner -loglevel error -f dshow -rtbufsize 64M '
                f'-i video="{cam}" -an -vf scale={int(width)}:{int(height)} '
                f'-r {int(fps)} -f mjpeg -q:v 6 '
                f'tcp://0.0.0.0:{int(stream_port)}?listen=1&listen_timeout=30000')
    script = (
        _kill_by_port_ps(stream_port) +
        "Start-Sleep -Milliseconds 500\n"
        f'netsh advfirewall firewall delete rule name="{rule}" | Out-Null\n'
        f'netsh advfirewall firewall add rule name="{rule}" dir=in '
        f'action=allow protocol=TCP localport={int(stream_port)} | Out-Null\n'
        # detached spawn — a single-quoted here-string keeps the command literal
        f"$cmd = @'\n{cmd_line}\n'@\n"
        f"$r = ([wmiclass]'Win32_Process').Create($cmd)\n"
        f"'PID:' + $r.ProcessId\n'RC:' + $r.ReturnValue\n"
    )
    code, out, err = _run_ps(host, login, password, script, winrm_port)
    pid = rc = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("PID:") and line[4:].strip().isdigit():
            pid = int(line[4:].strip())
        elif line.startswith("RC:") and line[3:].strip().isdigit():
            rc = int(line[3:].strip())
    if rc not in (0, None):
        raise RuntimeError(
            f"the remote refused to launch ffmpeg (WMI Create returned {rc} — "
            f"2 = access denied, 9 = path not found, 21 = bad arguments).")
    if not pid:
        raise RuntimeError(f"couldn't start ffmpeg on the remote: "
                           f"{(err or out)[:300] or 'no PID returned'}")
    return pid


def stop_remote_stream(host, login, password, pid, *, winrm_port=5985,
                       stream_port=DEFAULT_STREAM_PORT):
    """Stop the remote ffmpeg started by :func:`start_remote_stream` — by PID and,
    for robustness, by the stream-port marker (it was detached from this session)."""
    script = ""
    if pid:
        script += (f"try {{ Stop-Process -Id {int(pid)} -Force "
                   f"-ErrorAction SilentlyContinue }} catch {{}}\n")
    script += _kill_by_port_ps(stream_port) + "'OK'\n"
    try:
        _run_ps(host, login, password, script, winrm_port)
    except Exception:
        pass


def probe_remote_camera(host, login, password, camera, ffmpeg, *, winrm_port=5985):
    """Run a short verbose ffmpeg capture on the remote and return its log, so a
    'no video' case shows the real reason (in use / privacy / Session-0)."""
    cam = (camera or "").replace('"', "")
    script = (f"$ff = '{ffmpeg}'\n"
              f'$o = (& $ff -hide_banner -loglevel verbose -f dshow -rtbufsize 64M '
              f'-i video="{cam}" -frames:v 1 -f null - 2>&1 | Out-String)\n$o\n')
    try:
        code, out, err = _run_ps(host, login, password, script, winrm_port)
        return (out or err or "").strip()[:3000]
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
