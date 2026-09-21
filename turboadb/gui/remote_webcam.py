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
     with the firewall port opened **only for this PC's address**; the GUI reads
     that socket directly. ffmpeg's tcp listener has no client filter or auth, so
     the scoped firewall rule is the access control: ``listen=1`` accepts a single
     client, and the rule is deleted again when the stream stops.

Prereqs on the remote: WinRM on (``Enable-PSRemoting -Force``) and the account a
local admin. A physical USB camera works headlessly; a camera redirected into
someone's RDP session is only visible inside that session.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import threading
from contextlib import contextmanager

from ..remote_deploy import _ensure_winrm, _session
from ..tools import NO_WINDOW as _NO_WINDOW
from .ffmpeg_tools import ffmpeg_url

_log = logging.getLogger(__name__)

DEFAULT_STREAM_PORT = 28100
_NET_TIMEOUT_S = 20  # `net use` listing
_SMB_CONNECT_TIMEOUT_S = 45  # an unreachable host takes ~40 s to fail
# Pushing ~160 MB over SMB is quick on a LAN; a hung share used to block the
# scan forever, which left "Scan cameras" greyed out for the whole session.
_SMB_COPY_TIMEOUT_S = 300

# where the pushed copy lives on the remote (admin-accessible, no profile guessing)
_PUSH_DIR = r"C:\Windows\Temp\turboadb-ffmpeg"
_PUSH_EXE = _PUSH_DIR + r"\ffmpeg.exe"

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


def _ps_squote(s: str) -> str:
    """Escape a value for use inside a single-quoted PowerShell string."""
    return (s or "").replace("'", "''")


def _clean_cam(camera: str) -> str:
    """Strip characters from a camera name that PowerShell would interpret
    inside the generated script (quotes, backticks, $-expansion, newlines)."""
    out = camera or ""
    for ch in ('"', "`", "$", "\r", "\n"):
        out = out.replace(ch, "")
    return out


def _clean_exe(path: str) -> str:
    """An executable path safe to embed in the generated PowerShell.

    The command goes inside a single-quoted here-string (``@'`` ... ``'@``),
    which is literal — except that a line consisting of ``'@`` ends it, after
    which the rest would be executed as script. This path is read back from the
    remote host's own output, so strip the characters that could break out.
    Elsewhere the same value is passed through _ps_squote(); this is the one
    place it is interpolated raw."""
    out = path or ""
    for ch in (chr(10), chr(13), chr(34), chr(96)):
        out = out.replace(ch, "")
    return out


def _run_ps(host, login, password, script, winrm_port=5985):
    r = _session(host, login, password, winrm_port=winrm_port).run_ps(script)
    out = (r.std_out or b"").decode("utf-8", "replace")
    err = (r.std_err or b"").decode("utf-8", "replace")
    return r.status_code, out, err


def _parse_ffmpeg(out):
    for line in out.splitlines():
        if line.startswith("FFMPEG:"):
            return line[len("FFMPEG:") :].strip()
    return ""


def _existing_connection(share):
    """True when this logon session already has a connection to *share* (e.g. the
    user's own mapping) — we must reuse it and never delete it."""
    try:
        r = subprocess.run(
            ["net", "use"],
            capture_output=True,
            timeout=_NET_TIMEOUT_S,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("`net use` listing failed: %s", exc)
        return False
    target = share.lower()
    text = (r.stdout or b"").decode("utf-8", "replace")
    return any(tok.lower() == target for line in text.splitlines() for tok in line.split())


def _call_with_timeout(fn, timeout):
    """Run *fn()* on a daemon thread; raise TimeoutError if it doesn't return."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # re-raised in the caller
            box["error"] = exc

    t = threading.Thread(target=target, name="turboadb-smb", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"no answer within {int(timeout)} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _wnet_connect(share, login, password):
    """Authenticate to *share* with ``WNetAddConnection2W`` — a temporary (not
    persistent) UNC connection. The password is passed in memory: it never
    appears on a command line (visible to any process viewer), and no console is
    needed (``net use … *`` prompts on the console, not reliably from a pipe).
    Returns the Win32 error code (0 = success)."""
    import ctypes
    from ctypes import wintypes

    class NETRESOURCEW(ctypes.Structure):
        _fields_ = [
            ("dwScope", wintypes.DWORD),
            ("dwType", wintypes.DWORD),
            ("dwDisplayType", wintypes.DWORD),
            ("dwUsage", wintypes.DWORD),
            ("lpLocalName", wintypes.LPWSTR),
            ("lpRemoteName", wintypes.LPWSTR),
            ("lpComment", wintypes.LPWSTR),
            ("lpProvider", wintypes.LPWSTR),
        ]

    res = NETRESOURCEW()
    res.dwType = 1  # RESOURCETYPE_DISK
    res.lpRemoteName = share
    fn = ctypes.WinDLL("mpr").WNetAddConnection2W
    fn.argtypes = [ctypes.POINTER(NETRESOURCEW), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    fn.restype = wintypes.DWORD
    return int(fn(ctypes.byref(res), password or "", login or None, 0))


def _wnet_disconnect(share):
    import ctypes
    from ctypes import wintypes

    fn = ctypes.WinDLL("mpr").WNetCancelConnection2W
    fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
    fn.restype = wintypes.DWORD
    return int(fn(share, 0, True))


def _win_error_text(code):
    try:
        import ctypes

        return f"{ctypes.FormatError(code).strip()} (error {code})"
    except Exception:
        return f"error {code}"


@contextmanager
def _share_connection(share, login, password):
    """Be authenticated to *share* for the duration of the block. An existing
    connection (the user's own mapping) is reused and left alone; only a
    connection made here is removed afterwards."""
    if _existing_connection(share):
        yield
        return
    code = _call_with_timeout(
        lambda: _wnet_connect(share, login, password), _SMB_CONNECT_TIMEOUT_S
    )
    if code == 1219:  # ERROR_SESSION_CREDENTIAL_CONFLICT: other creds already open
        _log.info("reusing the existing session to %s (credential conflict)", share)
        yield
        return
    if code != 0:
        raise RuntimeError(f"couldn't authenticate to {share}: {_win_error_text(code)}")
    try:
        yield
    finally:
        try:
            _call_with_timeout(lambda: _wnet_disconnect(share), _NET_TIMEOUT_S)
        except Exception as exc:
            _log.warning("couldn't close the temporary connection to %s: %s", share, exc)


def _smb_push(host, login, password, local_ffmpeg, log=None):
    """Copy the local ffmpeg.exe to the remote over its admin share (``\\host\\C$``)
    using the WinRM credentials. Fast on a LAN; needs SMB (445) + the admin share."""
    if os.name != "nt":
        raise RuntimeError("admin-share copy is Windows-only")
    share = rf"\\{host}\C$"
    remote_dir = rf"{share}\Windows\Temp\turboadb-ffmpeg"
    remote_exe = rf"{remote_dir}\ffmpeg.exe"
    with _share_connection(share, login, password):
        if log:
            log("Copying ffmpeg to the remote over the admin share (fast)…")
        # copy under a temp name + rename, so an interrupted copy never leaves
        # a truncated ffmpeg.exe that _LOCATE would then "find" forever
        part = remote_exe + f".{os.getpid()}.part"

        def copy():
            os.makedirs(remote_dir, exist_ok=True)
            shutil.copyfile(local_ffmpeg, part)
            os.replace(part, remote_exe)

        try:
            # Only the connect was bounded before; a share that stops answering
            # mid-copy made this call (and the scan behind it) hang for good.
            _call_with_timeout(copy, _SMB_COPY_TIMEOUT_S)
        finally:
            try:
                os.remove(part)
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log.debug("couldn't remove %s: %s", part, exc)
    return _PUSH_EXE


def _remote_download(host, login, password, winrm_port, log=None):
    if log:
        log("Downloading ffmpeg on the remote (one-time, ~160 MB — needs internet there)…")
    code, out, err = _run_ps(
        host, login, password, _REMOTE_DOWNLOAD.replace("__URL__", ffmpeg_url()), winrm_port
    )
    ff = _parse_ffmpeg(out)
    if ff:
        return ff
    reason = ""
    for line in out.splitlines():
        if line.startswith("NOFFMPEG:"):
            reason = line[len("NOFFMPEG:") :].strip()
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

    if not cached_ffmpeg() and log:
        log("Fetching ffmpeg locally first (one-time)…")
    # ensure_local_ffmpeg returns the cached copy, but only after re-checking the
    # SHA-256 recorded beside it — never push an unverified binary to a host.
    local = ensure_local_ffmpeg(log or (lambda m: None))

    smb_err = ""
    try:
        return _smb_push(host, login, password, local, log=log)
    except Exception as exc:
        smb_err = str(exc)
        if log:
            log(
                f"Admin-share copy didn't work ({smb_err}); trying a download on the "
                f"remote instead…"
            )
    try:
        return _remote_download(host, login, password, winrm_port, log=log)
    except Exception as dl_exc:
        raise RuntimeError(
            "couldn't get ffmpeg onto the remote machine.\n\n"
            f"• Admin-share copy (\\\\{host}\\C$) failed: {smb_err or 'n/a'}\n"
            f"• Remote download failed: {dl_exc}\n\n"
            "Easiest fix: paste ffmpeg.exe into C:\\Windows\\Temp\\turboadb-ffmpeg\\ "
            "on the remote over RDP, then Scan again."
        ) from dl_exc


def list_remote_cameras(host, login, password, *, winrm_port=5985, log=None):
    """Return ``(cameras, ffmpeg_path, diag)`` for the remote host, provisioning
    ffmpeg there first if it's missing. *diag* holds ffmpeg's raw device listing."""
    from .ffmpeg_tools import parse_dshow_devices

    ffmpeg = ensure_remote_ffmpeg(host, login, password, winrm_port=winrm_port, log=log)
    if log:
        log("Listing cameras on the remote machine…")
    script = (
        f"$ff = '{_ps_squote(ffmpeg)}'\n"
        f"$o = (& $ff -hide_banner -list_devices true -f dshow -i dummy "
        f"2>&1 | Out-String)\n$o\n"
    )
    code, out, err = _run_ps(host, login, password, script, winrm_port)
    cams = parse_dshow_devices(out)
    return cams, ffmpeg, out.strip()


def _listen_url(stream_port):
    """The exact listen address our remote ffmpeg serves on (also its kill marker)."""
    return f"tcp://0.0.0.0:{int(stream_port)}?listen=1"


def _rule_name(stream_port):
    return f"TurboADB Webcam {int(stream_port)}"


def _delete_rule_ps(stream_port):
    return f'netsh advfirewall firewall delete rule name="{_rule_name(stream_port)}" | Out-Null\n'


def _kill_by_port_ps(stream_port):
    """PS that kills an ffmpeg serving OUR exact listen URL — a stale one (from a
    killed WinRM call) holds the camera and the port. An exact substring match
    (not a ``*28100*`` wildcard) so unrelated ffmpeg jobs are never touched."""
    marker = _ps_squote(_listen_url(stream_port))
    return (
        f"Get-CimInstance Win32_Process | Where-Object {{ $_.Name -eq "
        f"'ffmpeg.exe' -and $_.CommandLine -and $_.CommandLine.Contains('{marker}') }} | "
        f"ForEach-Object {{ try {{ Stop-Process -Id $_.ProcessId -Force }} "
        f"catch {{}} }}\n"
    )


def local_address_for(host, port=5985):
    """This PC's IP address on the route to *host* — the source address the remote
    sees (unless NAT sits in between). A connected UDP socket sends nothing; it
    only makes the OS pick the route. Hostnames are resolved like WinRM does."""
    last = None
    try:
        infos = socket.getaddrinfo(host, int(port), 0, socket.SOCK_DGRAM)
    except OSError as exc:
        raise RuntimeError(f"couldn't resolve {host}: {exc}") from exc
    for family, _type, _proto, _canon, addr in infos:
        s = socket.socket(family, socket.SOCK_DGRAM)
        try:
            s.connect(addr)
            ip = s.getsockname()[0]
        except OSError as exc:
            last = exc
            continue
        finally:
            s.close()
        ip = (ip or "").split("%")[0]  # drop an IPv6 scope id
        if ip and ip not in ("0.0.0.0", "::"):
            return ip
    raise RuntimeError(f"couldn't determine this PC's address towards {host} ({last})")


def _firewall_ps(stream_port, client_ip, winrm_port):
    """PS that (re)creates the inbound rule for the stream port, scoped to
    *client_ip*. If that address isn't among the remote's established WinRM peers
    (we're behind NAT), the peers' addresses are allowed instead — still limited to
    authenticated WinRM clients, never "any"."""
    rule = _rule_name(stream_port)
    return (
        f"$me = '{_ps_squote(client_ip)}'\n"
        "$peers = @()\n"
        f"try {{ $peers = @(Get-NetTCPConnection -LocalPort {int(winrm_port)} -State Established "
        "-ErrorAction Stop | ForEach-Object { ([string]$_.RemoteAddress) -replace '^::ffff:','' } "
        "| Select-Object -Unique) } catch {}\n"
        "$allow = $me\n"
        "if ($peers.Count -gt 0 -and -not ($peers -contains $me)) { $allow = ($peers -join ',') }\n"
        + _delete_rule_ps(stream_port)
        + f'netsh advfirewall firewall add rule name="{rule}" dir=in '
        f'action=allow protocol=TCP localport={int(stream_port)} "remoteip=$allow" | Out-Null\n'
        "'ALLOW:' + $allow\n"
    )


def start_remote_stream(
    host,
    login,
    password,
    camera,
    ffmpeg,
    *,
    width=1280,
    height=720,
    fps=25,
    stream_port=DEFAULT_STREAM_PORT,
    winrm_port=5985,
    client_ip=None,
):
    """Launch ffmpeg on the remote serving MJPEG on ``stream_port`` and return its
    PID. ffmpeg is spawned **detached via WMI** (``Win32_Process.Create``) so it
    survives this WinRM call returning — a ``Start-Process`` child is killed the
    instant the WinRM shell closes, so it never starts listening (connection
    refused). ``listen_timeout`` lets it wait for our connection, and any stale
    ffmpeg on the port is killed first (it would hold the camera).

    The inbound firewall rule only admits *client_ip* (default: this PC's address
    towards *host*); it is removed again by :func:`stop_remote_stream` and on
    every failure here."""
    cam = _clean_cam(camera)
    if not client_ip:
        client_ip = local_address_for(host, winrm_port)
    cmd_line = (
        f'"{_clean_exe(ffmpeg)}" -hide_banner -loglevel error -thread_queue_size 512 '
        f'-f dshow -rtbufsize 64M -framerate {int(fps)} '
        f'-i video="{cam}" -an -vf scale={int(width)}:{int(height)} '
        f"-r {int(fps)} -f mjpeg -q:v 6 "
        f"{_listen_url(stream_port)}&listen_timeout=30000"
    )
    script = (
        _kill_by_port_ps(stream_port)
        + "Start-Sleep -Milliseconds 500\n"
        + _firewall_ps(stream_port, client_ip, winrm_port)
        # detached spawn — a single-quoted here-string keeps the command literal
        + f"$cmd = @'\n{cmd_line}\n'@\n"
        f"$r = ([wmiclass]'Win32_Process').Create($cmd)\n"
        # nothing will listen: close the port again straight away
        f"if ($r.ReturnValue -ne 0) {{ {_delete_rule_ps(stream_port).strip()} }}\n"
        f"'PID:' + $r.ProcessId\n'RC:' + $r.ReturnValue\n"
    )
    try:
        code, out, err = _run_ps(host, login, password, script, winrm_port)
    except Exception:
        # the script may have run (e.g. a read timeout) — don't leave the port open
        stop_remote_stream(host, login, password, None, winrm_port=winrm_port,
                           stream_port=stream_port)
        raise
    pid = rc = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("PID:") and line[4:].strip().isdigit():
            pid = int(line[4:].strip())
        elif line.startswith("RC:") and line[3:].strip().isdigit():
            rc = int(line[3:].strip())
        elif line.startswith("ALLOW:"):
            _log.info("remote webcam port %s opened for %s only", stream_port, line[6:].strip())
    if rc not in (0, None):
        raise RuntimeError(
            f"the remote refused to launch ffmpeg (WMI Create returned {rc} — "
            f"2 = access denied, 9 = path not found, 21 = bad arguments)."
        )
    if not pid:
        stop_remote_stream(host, login, password, None, winrm_port=winrm_port,
                           stream_port=stream_port)
        raise RuntimeError(
            f"couldn't start ffmpeg on the remote: {(err or out)[:300] or 'no PID returned'}"
        )
    return pid


def stop_remote_stream(
    host, login, password, pid, *, winrm_port=5985, stream_port=DEFAULT_STREAM_PORT
):
    """Stop the remote ffmpeg started by :func:`start_remote_stream` — by PID (only
    if that PID is still an ffmpeg) and by its exact listen URL (it was detached
    from this session) — and delete the firewall rule. Never raises."""
    script = ""
    if pid:
        script += (
            f"$p = Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue\n"
            f"if ($p -and $p.ProcessName -eq 'ffmpeg') {{ try {{ Stop-Process -Id {int(pid)} "
            f"-Force -ErrorAction SilentlyContinue }} catch {{}} }}\n"
        )
    script += _kill_by_port_ps(stream_port) + _delete_rule_ps(stream_port) + "'OK'\n"
    try:
        _run_ps(host, login, password, script, winrm_port)
    except Exception as exc:
        _log.warning(
            "couldn't stop the remote webcam stream on %s (ffmpeg / firewall rule may "
            "remain until the next start): %s: %s", host, type(exc).__name__, exc
        )


def probe_remote_camera(host, login, password, camera, ffmpeg, *, winrm_port=5985):
    """Run a short verbose ffmpeg capture on the remote and return its log, so a
    'no video' case shows the real reason (in use / privacy / Session-0)."""
    cam = _clean_cam(camera)
    script = (
        f"$ff = '{_ps_squote(ffmpeg)}'\n"
        f"$o = (& $ff -hide_banner -loglevel verbose -f dshow -rtbufsize 64M "
        f'-i video="{cam}" -frames:v 1 -f null - 2>&1 | Out-String)\n$o\n'
    )
    try:
        code, out, err = _run_ps(host, login, password, script, winrm_port)
        return (out or err or "").strip()[:3000]
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
