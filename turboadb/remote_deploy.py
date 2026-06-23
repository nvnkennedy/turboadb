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


def _ensure_winrm(say=None) -> bool:
    """Make sure pywinrm is importable. When running from a normal pip install we
    transparently `pip install pywinrm` on first use; the bundled exe ships it."""
    try:
        import winrm  # noqa: F401
        return True
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        if say:
            say("[ERROR] pywinrm isn't bundled in this build. Use the pip install, "
                "or run:  pip install pywinrm")
        return False
    if say:
        say("[INFO] Installing pywinrm (one-time, for WinRM remoting)…")
    try:
        from .tools import NO_WINDOW
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        "--disable-pip-version-check", "pywinrm"],
                       timeout=300, creationflags=NO_WINDOW)
        import importlib
        importlib.invalidate_caches()
        import winrm  # noqa: F401
        return True
    except Exception as exc:
        if say:
            say(f"[ERROR] could not install pywinrm: {exc}  (try: pip install pywinrm)")
        return False


# Runs ON each remote host (via pywinrm run_ps). Starts serve + the SYSTEM
# startup task, printing 'STATUS:…' on success or 'ERROR:…' on failure.
_DEPLOY_PS = r"""
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
try {{
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) {{ $py = (Get-Command py -ErrorAction SilentlyContinue).Source }}
    if (-not $py) {{
        'ERROR:Python not found on the host (install Python + ''pip install turboadb'' there)'
    }} else {{
        if ('{upd}' -eq '1') {{
            & $py -m pip install -U --quiet --disable-pip-version-check turboadb 2>&1 | Out-Null
        }}
        $out = & $py -m turboadb serve --port {port} --startup-task 2>&1
        'STATUS:' + (($out | Out-String).Trim() -replace '\s+',' ')
    }}
}} catch {{ 'ERROR:' + $_.Exception.Message }}
"""


def _session(host, login, password, *, winrm_port=5985, use_ssl=False,
             transport="ntlm"):
    import winrm
    scheme = "https" if use_ssl else "http"
    endpoint = f"{scheme}://{host}:{winrm_port}/wsman"
    return winrm.Session(
        endpoint, auth=(login, password), transport=transport,
        server_cert_validation="ignore" if use_ssl else "validate")


def _run_ps(host, login, password, script, *, winrm_port):
    r = _session(host, login, password, winrm_port=winrm_port).run_ps(script)
    out = (r.std_out or b"").decode("utf-8", "replace").strip()
    err = (r.std_err or b"").decode("utf-8", "replace").strip()
    return r.status_code, out, err


def deploy_serve(hosts, username, password, *, update=True, port=5037,
                 test_only=False, winrm_port=5985, on_status=None) -> int:
    """Deploy (or, with *test_only*, just verify WinRM/credentials on) *hosts* —
    one or many. *username* should be ``DOMAIN\\user``. Streams leveled status
    lines to *on_status*. Returns 0 if every host succeeded, else 1."""
    def say(m):
        if on_status:
            on_status(m)

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
                code, out, err = _run_ps(h, username, password,
                                         "'OK:'+$env:COMPUTERNAME",
                                         winrm_port=winrm_port)
                if code == 0 and "OK:" in out:
                    name = out.split("OK:", 1)[1].strip().splitlines()[0]
                    say(f"[OK] {h}: WinRM reachable, credentials accepted "
                        f"(remote = {name})")
                else:
                    say(f"[ERROR] {h}: connected but check failed: "
                        f"{(err or out)[:200]}")
                    rc = 1
                continue

            script = _DEPLOY_PS.format(upd="1" if update else "0", port=port)
            code, out, err = _run_ps(h, username, password, script,
                                     winrm_port=winrm_port)
            ok = code == 0 and "STATUS:" in out and "ERROR:" not in out
            if ok:
                detail = next((l[len("STATUS:"):].strip()
                               for l in out.splitlines()
                               if l.startswith("STATUS:")), "ok")
                say(f"[OK] {h}: serve started — {detail[:220]}")
            else:
                emsg = next((l[len("ERROR:"):].strip()
                             for l in out.splitlines()
                             if l.startswith("ERROR:")), (err or out))
                say(f"[ERROR] {h}: {emsg[:240]}")
                rc = 1
        except Exception as exc:
            say(f"[ERROR] {h}: WinRM failed: {exc}")
            say(f"[INFO] {h}: make sure WinRM is on there (run on it as admin: "
                f"Enable-PSRemoting -Force), your account is a local admin, and "
                f"the user is in DOMAIN\\user form.")
            rc = 1
    return rc
