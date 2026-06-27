"""Remote webcam over WinRM — view a camera on another Windows / RDP machine
without SSH.

TurboADB has no SSH transport, so (unlike TurboSSH) the remote camera is reached
with the **same WinRM/NTLM stack** used by ``deploy-serve``:

  1. WinRM runs ffmpeg's ``-list_devices`` on the remote to enumerate cameras.
  2. WinRM launches ffmpeg on the remote, capturing the camera and serving MJPEG on
     a **listening TCP socket** (``tcp://0.0.0.0:PORT?listen=1``), and opens that
     port in the remote firewall.
  3. The GUI opens a plain TCP socket to ``host:PORT`` and reads the MJPEG stream —
     a binary-clean tunnel, decoded by the same frame reader the local path uses.
  4. WinRM stops the remote ffmpeg (by PID) when you stop.

Prereqs on the remote: WinRM on (``Enable-PSRemoting -Force``), the account a local
admin, and ffmpeg present (on PATH or in ``~\\.turboadb\\ffmpeg``). A physical USB
camera on that machine works headlessly; a camera *redirected* into someone's RDP
session is only visible inside that session.
"""

from __future__ import annotations

from ..remote_deploy import _ensure_winrm, _session

DEFAULT_STREAM_PORT = 28100

# Locate ffmpeg on the remote: PATH, then the turboadb/turbossh caches.
_FIND_FFMPEG = r"""
$ff = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ff) {
  foreach ($p in @("$env:USERPROFILE\.turboadb\ffmpeg\ffmpeg.exe",
                   "$env:USERPROFILE\.turbossh\ffmpeg\ffmpeg.exe",
                   "C:\ffmpeg\bin\ffmpeg.exe")) {
    if (Test-Path $p) { $ff = $p; break }
  }
}
"""


def _run_ps(host, login, password, script, winrm_port=5985):
    r = _session(host, login, password, winrm_port=winrm_port).run_ps(script)
    out = (r.std_out or b"").decode("utf-8", "replace")
    err = (r.std_err or b"").decode("utf-8", "replace")
    return r.status_code, out, err


def list_remote_cameras(host, login, password, *, winrm_port=5985):
    """Return ``(cameras, ffmpeg_path, diag)`` for the remote host. *diag* holds
    ffmpeg's raw device listing so a "no cameras" case is explainable."""
    if not _ensure_winrm():
        raise RuntimeError("pywinrm isn't available (pip install pywinrm).")
    from .ffmpeg_tools import parse_dshow_devices
    script = _FIND_FFMPEG + r"""
if (-not $ff) { 'NOFFMPEG'; exit }
'FFMPEG:' + $ff
$o = (& $ff -hide_banner -list_devices true -f dshow -i dummy 2>&1 | Out-String)
$o
"""
    code, out, err = _run_ps(host, login, password, script, winrm_port)
    if "NOFFMPEG" in out:
        raise RuntimeError(
            "ffmpeg wasn't found on the remote machine. Install it there (on PATH) "
            "or drop ffmpeg.exe in %USERPROFILE%\\.turboadb\\ffmpeg\\.")
    ffmpeg = ""
    for line in out.splitlines():
        if line.startswith("FFMPEG:"):
            ffmpeg = line[len("FFMPEG:"):].strip()
            break
    cams = parse_dshow_devices(out)
    return cams, ffmpeg, out.strip()


def start_remote_stream(host, login, password, camera, ffmpeg, *, width=1280,
                        height=720, fps=25, stream_port=DEFAULT_STREAM_PORT,
                        winrm_port=5985):
    """Open the firewall and launch ffmpeg on the remote, serving MJPEG on
    ``stream_port``. Returns the remote ffmpeg PID (int) to stop later."""
    # strip any double-quote, and double single-quotes so the name survives being
    # embedded in the PowerShell single-quoted $al literal below
    cam = (camera or "").replace('"', "").replace("'", "''")
    rule = f"TurboADB Webcam {stream_port}"
    # one ffmpeg arg string; the camera name is wrapped in quotes for spaces
    ff_args = (f'-hide_banner -loglevel error -f dshow -rtbufsize 64M '
               f'-i video="{cam}" -an -vf scale={int(width)}:{int(height)} '
               f'-r {int(fps)} -f mjpeg -q:v 6 '
               f'tcp://0.0.0.0:{int(stream_port)}?listen=1')
    script = (
        f"$ff = '{ffmpeg}'\n"
        f'netsh advfirewall firewall delete rule name="{rule}" | Out-Null\n'
        f'netsh advfirewall firewall add rule name="{rule}" dir=in '
        f'action=allow protocol=TCP localport={int(stream_port)} | Out-Null\n'
        f"$al = '{ff_args}'\n"
        f"$p = Start-Process -FilePath $ff -ArgumentList $al "
        f"-WindowStyle Hidden -PassThru\n"
        f"'PID:' + $p.Id\n"
    )
    code, out, err = _run_ps(host, login, password, script, winrm_port)
    pid = None
    for line in out.splitlines():
        if line.startswith("PID:"):
            try:
                pid = int(line[len("PID:"):].strip())
            except ValueError:
                pid = None
            break
    if pid is None:
        raise RuntimeError(f"couldn't start ffmpeg on the remote: "
                           f"{(err or out)[:300] or 'no PID returned'}")
    return pid


def stop_remote_stream(host, login, password, pid, *, winrm_port=5985):
    """Kill the remote ffmpeg started by :func:`start_remote_stream`."""
    if not pid:
        return
    script = (f"try {{ Stop-Process -Id {int(pid)} -Force "
              f"-ErrorAction SilentlyContinue }} catch {{}}\n'OK'\n")
    try:
        _run_ps(host, login, password, script, winrm_port)
    except Exception:
        pass


def probe_remote_camera(host, login, password, camera, ffmpeg, *, winrm_port=5985):
    """Run a short verbose ffmpeg capture on the remote and return its log, so a
    'no video' case shows the real reason (in use / privacy / Session-0)."""
    cam = (camera or "").replace('"', "")
    script = _FIND_FFMPEG + (
        f"if (-not $ff) {{ $ff = '{ffmpeg}' }}\n"
        f'$o = (& $ff -hide_banner -loglevel verbose -f dshow -rtbufsize 64M '
        f'-i video="{cam}" -frames:v 1 -f null - 2>&1 | Out-String)\n'
        f"$o\n"
    )
    try:
        code, out, err = _run_ps(host, login, password, script, winrm_port)
        return (out or err or "").strip()[:3000]
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
