"""Deploy + start ``turboadb serve`` on remote Windows hosts over WinRM.

Uses **pywinrm with NTLM transport** (the same proven approach as TurboSSH): an
explicit ``DOMAIN\\user`` + password connects to ``http://host:5985/wsman`` over
plain WinRM **without** needing Kerberos, SPNs, or TrustedHosts configured on the
client. That's why this works where PowerShell's ``Invoke-Command`` / ``Test-WSMan``
(which insist on Negotiate/Kerberos) were failing.

On each host it runs (as the admin credential): optionally ``pip install -U
turboadb``, then ``turboadb serve --startup-task`` — starting the shared adb
server now and registering a SYSTEM startup task so the host keeps sharing its
devices headlessly. Prereqs per host: WinRM on (``Enable-PSRemoting -Force``),
the account a local admin, and Python + turboadb installed."""

from __future__ import annotations

import sys
import subprocess

from .config import validate_port


def _ensure_winrm(say=None) -> bool:
    """Make sure pywinrm is importable. When running from a normal pip install we
    transparently `pip install pywinrm` on first use; the standalone exe ships it."""
    try:
        import winrm  # noqa: F401

        return True
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        if say:
            say(
                "[ERROR] pywinrm isn't bundled in this build. Use the pip install, "
                "or run:  pip install pywinrm"
            )
        return False
    if say:
        say("[INFO] Installing pywinrm (one-time, for WinRM remoting)…")
    try:
        from .tools import NO_WINDOW

        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--disable-pip-version-check",
                "pywinrm",
            ],
            timeout=300,
            creationflags=NO_WINDOW,
        )
        import importlib

        importlib.invalidate_caches()
        import winrm  # noqa: F401

        return True
    except Exception as exc:
        if say:
            say(f"[ERROR] could not install pywinrm: {exc}  (try: pip install pywinrm)")
        return False


# Runs ON each remote host (via pywinrm run_ps). Starts serve + the SYSTEM
# startup task, printing 'STATUS:…' on success, 'WARNING:…' for a non-fatal
# problem, or 'ERROR:…' on failure.
#
# Native commands run with $ErrorActionPreference='Continue': under 'Stop',
# Windows PowerShell 5.1 turns ANY stderr line of a redirected (2>&1) native
# exe — e.g. a pip deprecation warning — into a terminating NativeCommandError,
# failing a deploy that actually worked. Success is judged by $LASTEXITCODE.
_DEPLOY_PS = r"""
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
function Join-Output($lines) {{ (($lines | ForEach-Object {{ "$_" }}) -join ' ') -replace '\s+',' ' }}
try {{
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) {{ $py = (Get-Command py -ErrorAction SilentlyContinue).Source }}
    if (-not $py) {{
        'ERROR:Python not found on the host (install Python + ''pip install turboadb'' there)'
    }} else {{
        $ErrorActionPreference='Continue'
        if ('{upd}' -eq '1') {{
            $pipOut = & $py -m pip install -U --quiet --disable-pip-version-check turboadb 2>&1
            if ($LASTEXITCODE -ne 0) {{
                'WARNING:pip upgrade of turboadb failed (exit ' + $LASTEXITCODE + '): ' + (Join-Output $pipOut)
            }}
        }}
        $out = & $py -m turboadb serve --port {port} --startup-task 2>&1
        $code = $LASTEXITCODE
        $ErrorActionPreference='Stop'
        if ($code -ne 0) {{
            'ERROR:turboadb serve exited with code ' + $code + ': ' + (Join-Output $out)
        }} else {{
            'STATUS:' + (Join-Output $out).Trim()
        }}
    }}
}} catch {{ 'ERROR:' + $_.Exception.Message }}
"""


def _session(
    host,
    login,
    password,
    *,
    winrm_port=5985,
    use_ssl=False,
    transport="ntlm",
    read_timeout=None,
    operation_timeout=None,
):
    import winrm

    scheme = "https" if use_ssl else "http"
    endpoint = f"{scheme}://{host}:{winrm_port}/wsman"
    # Bound the timeouts so an unreachable / non-responding host fails in a known
    # time instead of hanging the (now modal) deploy popup. 120 s is plenty for a
    # dependency-free `pip install -U turboadb` + starting the server; read_timeout
    # must exceed operation_timeout (a pywinrm requirement).
    op = operation_timeout if operation_timeout is not None else 120
    rd = read_timeout if read_timeout is not None else op + 30
    return winrm.Session(
        endpoint,
        auth=(login, password),
        transport=transport,
        # HTTPS without certificate verification provides encryption but not host
        # authentication, leaving administrator credentials vulnerable to MITM.
        # Keep certificate validation on for both HTTP/HTTPS; users of private
        # CAs should trust the CA in Windows rather than silently bypass it.
        server_cert_validation="validate",
        read_timeout_sec=rd,
        operation_timeout_sec=op,
    )


def _run_ps(host, login, password, script, *, winrm_port, use_ssl=False):
    r = _session(host, login, password, winrm_port=winrm_port, use_ssl=use_ssl).run_ps(script)
    out = (r.std_out or b"").decode("utf-8", "replace").strip()
    err = (r.std_err or b"").decode("utf-8", "replace").strip()
    return r.status_code, out, err


def deploy_serve(
    hosts,
    username,
    password,
    *,
    update=True,
    port=5037,
    test_only=False,
    winrm_port=5985,
    use_ssl=False,
    on_status=None,
) -> int:
    """Deploy (or, with *test_only*, just verify WinRM/credentials on) *hosts* —
    one or many. *username* should be ``DOMAIN\\user``. Streams leveled status
    lines to *on_status*. Returns 0 if every host succeeded, else 1."""

    def say(m):
        if on_status:
            on_status(m)

    port = validate_port(port)
    winrm_port = validate_port(winrm_port, "winrm_port")
    hosts = [h.strip() for h in hosts if h and h.strip()]
    if not hosts:
        say("[WARNING] No hosts given.")
        return 1
    if not _ensure_winrm(say):
        return 1

    rc = 0
    for h in hosts:
        say(f"[INFO] {h}: connecting over WinRM (NTLM) as {username}…")
        try:
            if test_only:
                code, out, err = _run_ps(
                    h,
                    username,
                    password,
                    "'OK:'+$env:COMPUTERNAME",
                    winrm_port=winrm_port,
                    use_ssl=use_ssl,
                )
                if code == 0 and "OK:" in out:
                    name = out.split("OK:", 1)[1].strip().splitlines()[0]
                    say(f"[OK] {h}: WinRM reachable, credentials accepted (remote = {name})")
                else:
                    say(f"[ERROR] {h}: connected but check failed: {(err or out)[:200]}")
                    rc = 1
                continue

            script = _DEPLOY_PS.format(upd="1" if update else "0", port=port)
            code, out, err = _run_ps(
                h, username, password, script, winrm_port=winrm_port, use_ssl=use_ssl
            )
            for line in out.splitlines():
                if line.startswith("WARNING:"):
                    say(f"[WARNING] {h}: {line[len('WARNING:'):].strip()[:240]}")
            ok = code == 0 and "STATUS:" in out and "ERROR:" not in out
            if ok:
                detail = next(
                    (
                        line[len("STATUS:") :].strip()
                        for line in out.splitlines()
                        if line.startswith("STATUS:")
                    ),
                    "ok",
                )
                say(f"[OK] {h}: serve started — {detail[:220]}")
            else:
                emsg = next(
                    (
                        line[len("ERROR:") :].strip()
                        for line in out.splitlines()
                        if line.startswith("ERROR:")
                    ),
                    (err or out),
                )
                say(f"[ERROR] {h}: {emsg[:240]}")
                rc = 1
        except Exception as exc:
            say(f"[ERROR] {h}: WinRM failed: {exc}")
            say(
                f"[INFO] {h}: make sure WinRM is on there (run on it as admin: "
                f"Enable-PSRemoting -Force), your account is a local admin, and "
                f"the user is in DOMAIN\\user form."
            )
            rc = 1
    return rc
